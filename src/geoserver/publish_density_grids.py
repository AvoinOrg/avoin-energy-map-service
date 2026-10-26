import argparse
import json
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import escape
from urllib.parse import quote

from src.geoserver.update_energy_certificate_class import (
    MVT_FORMAT,
    geoserver_config,
    get_json,
    get_xml,
    put_xml,
    reload_geoserver,
    request,
    set_child_text,
)


logger = logging.getLogger("geoserver.density_grids")


@dataclass(frozen=True)
class DensityLayerConfig:
    table_name: str
    title: str
    zoom_start: int
    zoom_stop: int
    min_cached_level: int
    max_cached_level: int
    meta_width: int = 4
    meta_height: int = 4


DENSITY_LAYERS = {
    "energymap_density_grid_z5_8": DensityLayerConfig(
        table_name="energymap_density_grid_z5_8",
        title="Energymap building density grid z5-z8",
        zoom_start=5,
        zoom_stop=8,
        min_cached_level=5,
        max_cached_level=8,
    ),
    "energymap_density_grid_z9_12": DensityLayerConfig(
        table_name="energymap_density_grid_z9_12",
        title="Energymap building density grid z9-z12",
        zoom_start=9,
        zoom_stop=12,
        min_cached_level=9,
        max_cached_level=12,
    ),
}


def post_json(config: dict[str, str], path: str, payload: dict) -> int:
    body = json.dumps(payload).encode()
    status, _ = request(
        config,
        path,
        method="POST",
        body=body,
        content_type="application/json",
        accept=None,
    )
    return status


def put_json(config: dict[str, str], path: str, payload: dict) -> int:
    body = json.dumps(payload).encode()
    status, _ = request(
        config,
        path,
        method="PUT",
        body=body,
        content_type="application/json",
        accept=None,
    )
    return status


def feature_type_exists(config: dict[str, str], layer_name: str) -> bool:
    path = (
        f"/rest/workspaces/{config['workspace']}"
        f"/datastores/{config['datastore']}"
        f"/featuretypes/{layer_name}.json"
    )
    try:
        get_json(config, path)
        return True
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return False
        raise


def feature_type_payload(layer_name: str, layer: DensityLayerConfig) -> dict:
    return {
        "featureType": {
            "name": layer_name,
            "nativeName": layer.table_name,
            "title": layer.title,
            "srs": "EPSG:3857",
            "projectionPolicy": "FORCE_DECLARED",
            "enabled": True,
            "advertised": True,
        }
    }


def publish_feature_type(config: dict[str, str], layer_name: str, layer: DensityLayerConfig) -> None:
    payload = feature_type_payload(layer_name, layer)
    base_path = (
        f"/rest/workspaces/{config['workspace']}"
        f"/datastores/{config['datastore']}"
        "/featuretypes"
    )

    if feature_type_exists(config, layer_name):
        path = f"{base_path}/{layer_name}.json?recalculate=nativebbox,latlonbbox"
        status = put_json(config, path, payload)
        logger.info("Updated density feature type %s: HTTP %s", layer_name, status)
    else:
        path = f"{base_path}?recalculate=nativebbox,latlonbbox"
        status = post_json(config, path, payload)
        logger.info("Created density feature type %s: HTTP %s", layer_name, status)


def configure_gwc_layer(config: dict[str, str], layer_name: str, layer: DensityLayerConfig) -> None:
    qualified_layer = f"{config['workspace']}:{layer_name}"
    encoded = quote(qualified_layer, safe="")
    path = f"/gwc/rest/layers/{encoded}.xml"
    root = get_xml(config, path)

    mime_formats = root.find("mimeFormats")
    if mime_formats is None:
        mime_formats = ET.SubElement(root, "mimeFormats")
    for child in list(mime_formats):
        mime_formats.remove(child)
    ET.SubElement(mime_formats, "string").text = MVT_FORMAT

    grid_subsets = root.find("gridSubsets")
    if grid_subsets is None:
        grid_subsets = ET.SubElement(root, "gridSubsets")
    grid_subset = None
    for candidate in grid_subsets.findall("gridSubset"):
        grid_set_name = candidate.find("gridSetName")
        if grid_set_name is not None and grid_set_name.text == "EPSG:900913":
            grid_subset = candidate
            break
    if grid_subset is None:
        grid_subset = ET.SubElement(grid_subsets, "gridSubset")
        ET.SubElement(grid_subset, "gridSetName").text = "EPSG:900913"

    set_child_text(grid_subset, "zoomStart", str(layer.zoom_start))
    set_child_text(grid_subset, "zoomStop", str(layer.zoom_stop))
    set_child_text(grid_subset, "minCachedLevel", str(layer.min_cached_level))
    set_child_text(grid_subset, "maxCachedLevel", str(layer.max_cached_level))

    meta_width_height = root.find("metaWidthHeight")
    if meta_width_height is None:
        meta_width_height = ET.SubElement(root, "metaWidthHeight")
    for child in list(meta_width_height):
        meta_width_height.remove(child)
    ET.SubElement(meta_width_height, "int").text = str(layer.meta_width)
    ET.SubElement(meta_width_height, "int").text = str(layer.meta_height)

    status = put_xml(config, path, root)
    logger.info(
        "Configured GWC layer %s zooms %s-%s cache %s-%s MVT-only: HTTP %s",
        qualified_layer,
        layer.zoom_start,
        layer.zoom_stop,
        layer.min_cached_level,
        layer.max_cached_level,
        status,
    )


def run_gwc_task(
    config: dict[str, str],
    layer_name: str,
    layer: DensityLayerConfig,
    task_type: str,
    threads: int,
) -> None:
    qualified_layer = f"{config['workspace']}:{layer_name}"
    gwc_layer = get_json(
        config,
        f"/gwc/rest/layers/{quote(qualified_layer, safe='')}.json",
    )["GeoServerLayer"]
    subset = gwc_layer.get("gridSubsets", [{}])[0]
    coords = subset.get("extent", {}).get("coords")
    if not coords:
        raise RuntimeError(f"{qualified_layer}: GWC grid subset extent is missing")

    coord_xml = "".join(f"<double>{float(value)}</double>" for value in coords)
    body = f"""<seedRequest>
  <name>{escape(qualified_layer)}</name>
  <bounds><coords>{coord_xml}</coords></bounds>
  <gridSetId>EPSG:900913</gridSetId>
  <zoomStart>{layer.min_cached_level}</zoomStart>
  <zoomStop>{layer.max_cached_level}</zoomStop>
  <format>{MVT_FORMAT}</format>
  <type>{task_type}</type>
  <threadCount>{threads}</threadCount>
</seedRequest>""".encode()

    status, _ = request(
        config,
        f"/gwc/rest/seed/{quote(qualified_layer, safe='')}.xml",
        method="POST",
        body=body,
        content_type="text/xml",
        accept="text/plain",
    )
    logger.info("Started GWC %s for %s: HTTP %s", task_type, qualified_layer, status)


def poll_seed_status(config: dict[str, str], layer_names: list[str], interval_seconds: int) -> None:
    while True:
        active_count = 0
        for layer_name in layer_names:
            qualified_layer = f"{config['workspace']}:{layer_name}"
            _, body = request(
                config,
                f"/gwc/rest/seed/{quote(qualified_layer, safe='')}.json",
            )
            status = json.loads(body).get("long-array-array", [])
            logger.info("%s seed status: %s", qualified_layer, status)
            active_count += sum(1 for row in status if len(row) >= 5 and row[-1] == 1)
        if active_count == 0:
            return
        time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish energymap density grid layers")
    parser.add_argument("--truncate", action="store_true", help="Truncate GWC caches after publishing")
    parser.add_argument("--seed", action="store_true", help="Start GWC seeds after publishing")
    parser.add_argument("--poll", action="store_true", help="Poll seed status until all started seeds finish")
    parser.add_argument("--seed-threads", type=int, default=2)
    parser.add_argument("--poll-interval", type=int, default=30)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = geoserver_config()

    for layer_name, layer in DENSITY_LAYERS.items():
        publish_feature_type(config, layer_name, layer)

    reload_geoserver(config)

    for layer_name, layer in DENSITY_LAYERS.items():
        configure_gwc_layer(config, layer_name, layer)

    if args.truncate:
        for layer_name, layer in DENSITY_LAYERS.items():
            run_gwc_task(config, layer_name, layer, task_type="truncate", threads=1)

    if args.seed:
        for layer_name, layer in DENSITY_LAYERS.items():
            run_gwc_task(config, layer_name, layer, task_type="seed", threads=args.seed_threads)

    if args.seed and args.poll:
        poll_seed_status(
            config,
            list(DENSITY_LAYERS),
            interval_seconds=args.poll_interval,
        )


if __name__ == "__main__":
    main()
