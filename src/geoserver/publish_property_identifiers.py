"""Publish property IDs and invalidate only the two building-layer MVT caches."""

from copy import deepcopy
import logging
import re

from src.geoserver.update_energy_certificate_class import (
    LAYERS,
    env_bool,
    geoserver_config,
    get_json,
    put_json,
    reload_geoserver,
    truncate_gwc_layer,
)


logger = logging.getLogger("geoserver.property_identifiers")


def ensure_property_identifier(feature_type: dict, geometry_column: str) -> dict:
    """Preserve the full existing contract, adding only this string when needed."""
    result = deepcopy(feature_type)
    entries = result.get("metadata", {}).get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    virtual_table_entry = next((entry for entry in entries
                                if entry.get("@key") == "JDBC_VIRTUAL_TABLE"), None)
    if virtual_table_entry is None:
        raise RuntimeError("Building layer has no JDBC virtual table")
    virtual_table = virtual_table_entry["virtualTable"]
    sql = virtual_table.get("sql", "")
    if re.search(r"\bproperty_identifier\b", sql) is None:
        needle = f", {geometry_column} AS geom"
        if needle not in sql:
            raise RuntimeError("Cannot safely locate the building-layer SQL projection")
        virtual_table["sql"] = sql.replace(needle, f", property_identifier{needle}", 1)
    attributes = result.setdefault("attributes", {}).setdefault("attribute", [])
    matching = [attr for attr in attributes if attr.get("name") == "property_identifier"]
    if len(matching) > 1:
        raise RuntimeError("Duplicate property_identifier attributes")
    if matching:
        if matching[0].get("binding") != "java.lang.String":
            raise RuntimeError("property_identifier must be published as a String")
    else:
        geom_index = next((index for index, attr in enumerate(attributes)
                           if attr.get("name") == "geom"), len(attributes))
        attributes.insert(geom_index, {
            "name": "property_identifier", "minOccurs": 0, "maxOccurs": 1,
            "nillable": True, "binding": "java.lang.String",
        })
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = geoserver_config()
    # Preflight both layers before mutating either one.
    prepared = []
    for layer, layer_config in LAYERS.items():
        path = (f"/rest/workspaces/{config['workspace']}"
                f"/datastores/{config['datastore']}/featuretypes/{layer}.json")
        original = get_json(config, path)["featureType"]
        updated = ensure_property_identifier(original, layer_config["geometry_column"])
        prepared.append((layer, path, original, updated))
    changed = False
    for layer, path, original, updated in prepared:
        if original == updated:
            logger.info("%s already publishes property_identifier as String", layer)
            continue
        status = put_json(config, f"{path}?recalculate=", {"featureType": updated})
        logger.info("Published property_identifier on %s: HTTP %s", layer, status)
        changed = True
    if changed:
        reload_geoserver(config)
    if env_bool("GEOSERVER_TRUNCATE_GWC", True):
        for layer in LAYERS:
            truncate_gwc_layer(config, f"{config['workspace']}:{layer}")


if __name__ == "__main__":
    main()
