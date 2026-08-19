import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from base64 import b64encode
from html import escape
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


logger = logging.getLogger("geoserver.energy_classes")

ENERGY_CLASS_ATTRIBUTES = {
    "property_identifier": "java.lang.String",
    "energy_certificate_class": "java.lang.String",
    "energy_certificate_class_year": "java.lang.Integer",
    "energy_certificate_valid_until": "java.sql.Timestamp",
    "energy_certificate_previous_class": "java.lang.String",
    "energy_certificate_recommendations_fi": "java.lang.String",
    "energy_certificate_recommendations_sv": "java.lang.String",
    "energy_certificate_heated_net_area": "java.lang.Double",
    "energy_certificate_ventilation_type_id": "java.lang.String",
    "energy_certificate_ventilation_description_fi": "java.lang.String",
    "energy_certificate_ventilation_description_sv": "java.lang.String",
    "modeled_energy_class": "java.lang.String",
    "modeled_energy_e_value": "java.lang.Double",
    "energy_class": "java.lang.String",
    "is_energy_class_modeled": "java.lang.Boolean",
    "energy_class_year": "java.lang.Integer",
}
MVT_FORMAT = "application/vnd.mapbox-vector-tile"
POLYGON_LAYER = "energymap_building_polygons"
POLYGON_GWC_ZOOM_START = "12"
POLYGON_GWC_ZOOM_STOP = "17"
POLYGON_GWC_MIN_CACHED_LEVEL = "12"
POLYGON_GWC_MAX_CACHED_LEVEL = "14"
POLYGON_GWC_META_WIDTH_HEIGHT = ("1", "1")
OLD_CERTIFICATE_COLUMNS = {
    "energy_certificate_id",
    "energy_certificate_version",
    "energy_certificate_signed_at",
    "energy_certificate_e_value",
    "energy_certificate_e_class",
    "energy_certificate_e_class_limit_2018",
    "energy_certificate_use_purpose",
    "energy_certificate_sub_use_purpose_fi",
    "energy_certificate_heating_form_1_id",
    "energy_certificate_heating_form_2_id",
    "energy_certificate_heating_form_1_description_fi",
    "energy_certificate_heating_form_2_description_fi",
    "energy_certificate_heat_distribution_id",
    "energy_certificate_heat_distribution_description_fi",
}

LAYERS = {
    POLYGON_LAYER: {
        "geometry_column": "polygon",
        "geometry_binding": "org.locationtech.jts.geom.MultiPolygon",
    },
    "energymap_building_points": {
        "geometry_column": "point",
        "geometry_binding": "org.locationtech.jts.geom.Point",
    },
}


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def geoserver_config() -> dict[str, str]:
    return {
        "base_url": os.getenv("GEOSERVER_BASE_URL", "https://sandbox.gis.avoin.org/geoserver").rstrip("/"),
        "user": require_env("GEOSERVER_USER"),
        "password": require_env("GEOSERVER_PASSWORD"),
        "workspace": os.getenv("GEOSERVER_WORKSPACE", "sandbox_energiakartta"),
        "datastore": os.getenv("GEOSERVER_DATASTORE", "avoin-postgres-energymap"),
    }


def auth_header(user: str, password: str) -> str:
    token = b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def request(
    config: dict[str, str],
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
    accept: str | None = "application/json",
) -> tuple[int, bytes]:
    headers = {"Authorization": auth_header(config["user"], config["password"])}
    if accept:
        headers["Accept"] = accept
    if content_type:
        headers["Content-Type"] = content_type

    req = Request(config["base_url"] + path, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=120) as response:
            return response.status, response.read()
    except HTTPError as exc:
        details = exc.read()[:1000].decode(errors="replace")
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {details}") from exc


def get_json(config: dict[str, str], path: str) -> dict[str, Any]:
    _, body = request(config, path)
    return json.loads(body)


def put_json(config: dict[str, str], path: str, payload: dict[str, Any]) -> int:
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


def get_xml(config: dict[str, str], path: str) -> ET.Element:
    _, body = request(config, path, accept="text/xml")
    return ET.fromstring(body)


def put_xml(config: dict[str, str], path: str, root: ET.Element) -> int:
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    status, _ = request(
        config,
        path,
        method="PUT",
        body=body,
        content_type="text/xml",
        accept=None,
    )
    return status


def metadata_entries(feature_type: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = feature_type.setdefault("metadata", {})
    entry = metadata.get("entry")
    if entry is None:
        metadata["entry"] = []
        return metadata["entry"]
    if isinstance(entry, list):
        return entry
    metadata["entry"] = [entry]
    return metadata["entry"]


def remove_old_certificate_columns(sql: str) -> str:
    for column in sorted(OLD_CERTIFICATE_COLUMNS):
        sql = sql.replace(f", {column}", "")
        sql = sql.replace(f"{column}, ", "")
    return sql


def ensure_certificate_class_sql(sql: str, geometry_column: str) -> str:
    sql = remove_old_certificate_columns(sql)
    needle = f", {geometry_column} AS geom"
    if needle not in sql:
        raise RuntimeError(f"Could not find SQL insertion point {needle!r}")

    missing_columns = [
        column
        for column in ENERGY_CLASS_ATTRIBUTES
        if re.search(rf"\b{re.escape(column)}\b", sql) is None
    ]
    if not missing_columns:
        return sql

    insertion = "".join(f", {column}" for column in missing_columns)
    return sql.replace(needle, f"{insertion}{needle}", 1)


def ensure_energy_class_attributes(
    feature_type: dict[str, Any],
    geometry_binding: str,
) -> None:
    attrs = feature_type.setdefault("attributes", {}).setdefault("attribute", [])
    attrs[:] = [
        attr
        for attr in attrs
        if attr.get("name") not in OLD_CERTIFICATE_COLUMNS
    ]

    geom_index = next(
        (
            index
            for index, attr in enumerate(attrs)
            if attr.get("name") == "geom"
            or attr.get("binding") == geometry_binding
        ),
        len(attrs),
    )
    for column, binding in ENERGY_CLASS_ATTRIBUTES.items():
        attribute = {
            "name": column,
            "minOccurs": 0,
            "maxOccurs": 1,
            "nillable": True,
            "binding": binding,
        }
        names = [attr.get("name") for attr in attrs]
        if column in names:
            attrs[names.index(column)].update(attribute)
            continue
        attrs.insert(geom_index, attribute)
        geom_index += 1


def update_feature_type(config: dict[str, str], layer: str, layer_config: dict[str, str]) -> None:
    path = (
        f"/rest/workspaces/{config['workspace']}"
        f"/datastores/{config['datastore']}"
        f"/featuretypes/{layer}.json"
    )
    feature_type = get_json(config, path)["featureType"]

    virtual_table_entry = next(
        (
            entry
            for entry in metadata_entries(feature_type)
            if entry.get("@key") == "JDBC_VIRTUAL_TABLE"
        ),
        None,
    )
    if virtual_table_entry is None:
        raise RuntimeError(f"{layer}: JDBC_VIRTUAL_TABLE metadata is missing")

    virtual_table = virtual_table_entry["virtualTable"]
    virtual_table["sql"] = ensure_certificate_class_sql(
        virtual_table.get("sql", ""),
        layer_config["geometry_column"],
    )
    ensure_energy_class_attributes(feature_type, layer_config["geometry_binding"])

    status = put_json(config, f"{path}?recalculate=", {"featureType": feature_type})
    logger.info("Updated %s feature type: HTTP %s", layer, status)


def reload_geoserver(config: dict[str, str]) -> None:
    status, _ = request(config, "/rest/reload", method="POST", accept=None)
    logger.info("Reloaded GeoServer catalog: HTTP %s", status)


def check_datastore_jit_off(config: dict[str, str]) -> None:
    path = (
        f"/rest/workspaces/{config['workspace']}"
        f"/datastores/{config['datastore']}.xml"
    )
    root = get_xml(config, path)
    params = root.find("connectionParameters")
    if params is None:
        raise RuntimeError("GeoServer datastore connectionParameters are missing")

    entry = next(
        (
            candidate
            for candidate in params.findall("entry")
            if candidate.attrib.get("key") == "Session startup SQL"
        ),
        None,
    )
    if entry is not None and (entry.text or "").strip() == "SET jit = off":
        logger.info("Datastore Session startup SQL already disables JIT")
        return

    logger.warning(
        "Datastore Session startup SQL does not disable JIT; leaving the datastore "
        "unchanged because REST responses contain an encrypted password that must "
        "not be written back as if it were plaintext"
    )


def get_gwc_layer(config: dict[str, str], qualified_layer: str) -> dict[str, Any]:
    encoded = quote(qualified_layer, safe="")
    return get_json(config, f"/gwc/rest/layers/{encoded}.json")["GeoServerLayer"]


def set_gwc_mvt_only(config: dict[str, str], qualified_layer: str) -> None:
    encoded = quote(qualified_layer, safe="")
    path = f"/gwc/rest/layers/{encoded}.xml"
    root = get_xml(config, path)
    mime_formats = root.find("mimeFormats")
    if mime_formats is None:
        mime_formats = ET.SubElement(root, "mimeFormats")

    for child in list(mime_formats):
        mime_formats.remove(child)
    ET.SubElement(mime_formats, "string").text = MVT_FORMAT

    status = put_xml(config, path, root)
    logger.info("Set GWC layer %s to MVT-only: HTTP %s", qualified_layer, status)


def set_child_text(parent: ET.Element, name: str, value: str) -> None:
    child = parent.find(name)
    if child is None:
        child = ET.SubElement(parent, name)
    child.text = value


def configure_polygon_gwc_layer(config: dict[str, str]) -> None:
    qualified_layer = f"{config['workspace']}:{POLYGON_LAYER}"
    encoded = quote(qualified_layer, safe="")
    path = f"/gwc/rest/layers/{encoded}.xml"
    root = get_xml(config, path)

    grid_subset = root.find("./gridSubsets/gridSubset")
    if grid_subset is None:
        raise RuntimeError(f"{qualified_layer}: gridSubset is missing")
    set_child_text(grid_subset, "zoomStart", POLYGON_GWC_ZOOM_START)
    set_child_text(grid_subset, "zoomStop", POLYGON_GWC_ZOOM_STOP)
    set_child_text(grid_subset, "minCachedLevel", POLYGON_GWC_MIN_CACHED_LEVEL)
    set_child_text(grid_subset, "maxCachedLevel", POLYGON_GWC_MAX_CACHED_LEVEL)

    meta_width_height = root.find("metaWidthHeight")
    if meta_width_height is None:
        meta_width_height = ET.SubElement(root, "metaWidthHeight")
    for child in list(meta_width_height):
        meta_width_height.remove(child)
    for value in POLYGON_GWC_META_WIDTH_HEIGHT:
        ET.SubElement(meta_width_height, "int").text = value

    status = put_xml(config, path, root)
    logger.info(
        "Configured polygon GWC layer zooms %s-%s, cache %s-%s, metatile %sx%s: HTTP %s",
        POLYGON_GWC_ZOOM_START,
        POLYGON_GWC_ZOOM_STOP,
        POLYGON_GWC_MIN_CACHED_LEVEL,
        POLYGON_GWC_MAX_CACHED_LEVEL,
        POLYGON_GWC_META_WIDTH_HEIGHT[0],
        POLYGON_GWC_META_WIDTH_HEIGHT[1],
        status,
    )


def truncate_gwc_layer(config: dict[str, str], qualified_layer: str) -> None:
    layer = get_gwc_layer(config, qualified_layer)
    subset = layer.get("gridSubsets", [{}])[0]
    coords = subset.get("extent", {}).get("coords") or [
        -20037508.342789244,
        -20037508.342789244,
        20037508.342789244,
        20037508.342789244,
    ]
    zoom_stop = max(
        int(subset.get("zoomStop", subset.get("maxCachedLevel", 24))),
        24,
    )
    coord_xml = "".join(f"<double>{float(value)}</double>" for value in coords)
    body = f"""<seedRequest>
  <name>{escape(qualified_layer)}</name>
  <bounds><coords>{coord_xml}</coords></bounds>
  <gridSetId>EPSG:900913</gridSetId>
  <zoomStart>0</zoomStart>
  <zoomStop>{zoom_stop}</zoomStop>
  <format>{MVT_FORMAT}</format>
  <type>truncate</type>
  <threadCount>1</threadCount>
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
    logger.info("Truncated GWC MVT cache for %s: HTTP %s", qualified_layer, status)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = geoserver_config()

    check_datastore_jit_off(config)

    for layer, layer_config in LAYERS.items():
        update_feature_type(config, layer, layer_config)

    reload_geoserver(config)

    for layer in LAYERS:
        set_gwc_mvt_only(config, f"{config['workspace']}:{layer}")
    configure_polygon_gwc_layer(config)

    if env_bool("GEOSERVER_TRUNCATE_GWC", True):
        for layer in LAYERS:
            truncate_gwc_layer(config, f"{config['workspace']}:{layer}")


if __name__ == "__main__":
    main()
