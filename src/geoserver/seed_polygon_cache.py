import argparse
import json
import logging
import time
from html import escape
from urllib.parse import quote

from src.geoserver.update_energy_certificate_class import (
    MVT_FORMAT,
    POLYGON_GWC_MAX_CACHED_LEVEL,
    POLYGON_GWC_MIN_CACHED_LEVEL,
    POLYGON_LAYER,
    geoserver_config,
    get_gwc_layer,
    request,
)


logger = logging.getLogger("geoserver.seed_polygon_cache")


def seed_polygon_cache(config: dict[str, str], thread_count: int) -> None:
    qualified_layer = f"{config['workspace']}:{POLYGON_LAYER}"
    layer = get_gwc_layer(config, qualified_layer)
    subset = layer.get("gridSubsets", [{}])[0]
    coords = subset.get("extent", {}).get("coords")
    if not coords:
        raise RuntimeError(f"{qualified_layer}: GWC grid subset extent is missing")

    coord_xml = "".join(f"<double>{float(value)}</double>" for value in coords)
    body = f"""<seedRequest>
  <name>{escape(qualified_layer)}</name>
  <bounds><coords>{coord_xml}</coords></bounds>
  <gridSetId>EPSG:900913</gridSetId>
  <zoomStart>{POLYGON_GWC_MIN_CACHED_LEVEL}</zoomStart>
  <zoomStop>{POLYGON_GWC_MAX_CACHED_LEVEL}</zoomStop>
  <format>{MVT_FORMAT}</format>
  <type>seed</type>
  <threadCount>{thread_count}</threadCount>
</seedRequest>""".encode()

    encoded = quote(qualified_layer, safe="")
    status, _ = request(
        config,
        f"/gwc/rest/seed/{encoded}.xml",
        method="POST",
        body=body,
        content_type="text/xml",
        accept="text/plain",
    )
    logger.info(
        "Started polygon seed for %s, zooms %s-%s, threads=%s: HTTP %s",
        qualified_layer,
        POLYGON_GWC_MIN_CACHED_LEVEL,
        POLYGON_GWC_MAX_CACHED_LEVEL,
        thread_count,
        status,
    )


def get_seed_status(config: dict[str, str]) -> list[list[int]]:
    qualified_layer = f"{config['workspace']}:{POLYGON_LAYER}"
    encoded = quote(qualified_layer, safe="")
    _, body = request(config, f"/gwc/rest/seed/{encoded}.json")
    return json.loads(body).get("long-array-array", [])


def poll_seed_status(config: dict[str, str], interval_seconds: int) -> None:
    while True:
        status = get_seed_status(config)
        logger.info("Polygon seed status: %s", status)
        active = [row for row in status if len(row) >= 5 and row[-1] == 1]
        if not active:
            return
        time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start sandbox polygon GWC MVT cache seed")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--poll-interval", type=int, default=30)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = geoserver_config()
    seed_polygon_cache(config, thread_count=args.threads)
    if args.poll:
        poll_seed_status(config, interval_seconds=args.poll_interval)


if __name__ == "__main__":
    main()
