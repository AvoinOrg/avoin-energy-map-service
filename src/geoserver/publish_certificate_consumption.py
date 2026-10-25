"""Publish the versioned certificate-consumption payload on both building layers."""

import argparse
from copy import deepcopy
import json
import logging
from pathlib import Path
import re
import time

from src.etl.certificate_consumption import CONSUMPTION_COLUMN
from src.geoserver.update_energy_certificate_class import (
    LAYERS, geoserver_config, get_gwc_layer, get_json, put_json,
    reload_geoserver, truncate_gwc_layer,
)


def ensure_consumption(feature_type, geometry_column):
    result = deepcopy(feature_type)
    entries = result.get("metadata", {}).get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    matches = [entry for entry in entries if entry.get("@key") == "JDBC_VIRTUAL_TABLE"]
    if len(matches) != 1:
        raise RuntimeError("Expected one building JDBC virtual table")
    table = matches[0]["virtualTable"]
    sql = table.get("sql", "")
    needle = f", {geometry_column} AS geom"
    if sql.count(needle) != 1:
        raise RuntimeError("Unexpected building geometry projection")
    if not re.search(r"\bFROM\s+(?:public\.)?main\b", sql, flags=re.I):
        raise RuntimeError("Consumption publication requires the main table")
    occurrences = len(re.findall(rf"\b{CONSUMPTION_COLUMN}\b", sql))
    if occurrences == 0:
        table["sql"] = sql.replace(needle, f", {CONSUMPTION_COLUMN}{needle}", 1)
    elif occurrences != 1 or f", {CONSUMPTION_COLUMN}" not in sql:
        raise RuntimeError("Ambiguous existing consumption projection")
    attrs = result.setdefault("attributes", {}).setdefault("attribute", [])
    matches = [attr for attr in attrs if attr.get("name") == CONSUMPTION_COLUMN]
    if len(matches) > 1 or matches and matches[0].get("binding") != "java.lang.String":
        raise RuntimeError("Unexpected consumption attribute type")
    if not matches:
        index = next((i for i, attr in enumerate(attrs) if attr.get("name") == "geom"), len(attrs))
        attrs.insert(index, {"name": CONSUMPTION_COLUMN, "binding": "java.lang.String",
                             "minOccurs": 0, "maxOccurs": 1, "nillable": True})
    return result


def clear_consumption_cache(config, qualified_layer):
    # GWC's global quota bookkeeping can contend with another tile/cache write.
    # Retrying the same regenerable layer is safe; do not mask other failures.
    for attempt in range(3):
        try:
            truncate_gwc_layer(config, qualified_layer)
            return
        except RuntimeError as error:
            if "could not serialize access due to concurrent update" not in str(error) or attempt == 2:
                raise
            logging.warning("Retrying %s cache clear after quota contention", qualified_layer)
            time.sleep(2 ** attempt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--snapshot-dir", type=Path)
    args = parser.parse_args()
    if args.apply and args.snapshot_dir is None:
        parser.error("--apply requires --snapshot-dir")
    config = geoserver_config()
    prepared = []
    for layer, settings in LAYERS.items():
        path = (f"/rest/workspaces/{config['workspace']}/datastores/{config['datastore']}"
                f"/featuretypes/{layer}.json")
        original = get_json(config, path)["featureType"]
        updated = ensure_consumption(original, settings["geometry_column"])
        cache = get_gwc_layer(config, f"{config['workspace']}:{layer}")
        prepared.append((layer, path, original, updated, cache))
        print(layer, "update needed" if original != updated else "already current", flush=True)
    if not args.apply:
        return
    args.snapshot_dir.mkdir(parents=True, exist_ok=False)
    for layer, _, original, _, cache in prepared:
        (args.snapshot_dir / f"{layer}.json").write_text(json.dumps({"featureType": original}, indent=2) + "\n")
        (args.snapshot_dir / f"{layer}-gwc.json").write_text(json.dumps(cache, indent=2) + "\n")
    changed = False
    for layer, path, original, updated, _ in prepared:
        if original != updated:
            if get_json(config, path)["featureType"] != original:
                raise RuntimeError(f"{layer} changed during preflight; re-read before publishing")
            put_json(config, f"{path}?recalculate=", {"featureType": updated})
            changed = True
    if changed:
        reload_geoserver(config)
    for layer, path, _, _, cache in prepared:
        current = get_json(config, path)["featureType"]
        if ensure_consumption(current, LAYERS[layer]["geometry_column"]) != current:
            raise RuntimeError(f"Publication readback failed: {layer}")
        if get_gwc_layer(config, f"{config['workspace']}:{layer}") != cache:
            raise RuntimeError(f"Unexpected cache configuration change: {layer}")
        clear_consumption_cache(config, f"{config['workspace']}:{layer}")
    print("Published; verify direct and regenerated cached MVT before frontend handoff")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
