"""Add ventilation categories/labels to the two existing building layers only."""

import argparse
from copy import deepcopy
import json
import logging
from pathlib import Path
from urllib.request import urlopen

from src.geoserver.update_energy_certificate_class import (
    LAYERS, geoserver_config, get_gwc_layer, get_json, put_json, reload_geoserver,
    truncate_gwc_layer,
)
from src.geoserver.ventilation import (
    CODE_LIST_URL, ensure_ventilation_attributes, ensure_ventilation_sql,
    verify_code_list,
)


logger = logging.getLogger("geoserver.ventilation")


def ensure_ventilation(feature_type: dict, geometry_column: str) -> dict:
    result = deepcopy(feature_type)
    entries = result.get("metadata", {}).get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    matches = [entry for entry in entries if entry.get("@key") == "JDBC_VIRTUAL_TABLE"]
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one building-layer JDBC virtual table")
    virtual_table = matches[0]["virtualTable"]
    virtual_table["sql"] = ensure_ventilation_sql(virtual_table.get("sql", ""), geometry_column)
    ensure_ventilation_attributes(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Publish; otherwise read-only preflight")
    parser.add_argument("--snapshot-dir", type=Path, help="New directory for both original feature types")
    parser.add_argument("--verify-code-list", action="store_true", help="Compare the pinned labels with the public register")
    args = parser.parse_args()
    if args.apply and not args.snapshot_dir:
        parser.error("--apply requires --snapshot-dir for recovery")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = geoserver_config()
    if args.verify_code_list:
        with urlopen(CODE_LIST_URL, timeout=30) as response:
            verify_code_list(json.load(response))
        logger.info("Authoritative ventilation code list matches the reviewed snapshot")
    prepared = []
    for layer, settings in LAYERS.items():
        path = (f"/rest/workspaces/{config['workspace']}"
                f"/datastores/{config['datastore']}/featuretypes/{layer}.json")
        original = get_json(config, path)["featureType"]
        updated = ensure_ventilation(original, settings["geometry_column"])
        prepared.append((layer, path, original, updated))
        logger.info("%s: %s", layer, "update needed" if original != updated else "already current")
    if not args.apply:
        return
    args.snapshot_dir.mkdir(parents=True, exist_ok=False)
    for layer, _, original, _ in prepared:
        (args.snapshot_dir / f"{layer}.json").write_text(
            json.dumps({"featureType": original}, ensure_ascii=False, indent=2) + "\n"
        )
        cache = get_gwc_layer(config, f"{config['workspace']}:{layer}")
        (args.snapshot_dir / f"{layer}-gwc.json").write_text(json.dumps(cache, indent=2) + "\n")
    changed = False
    for layer, path, original, updated in prepared:
        if original == updated:
            continue
        if get_json(config, path)["featureType"] != original:
            raise RuntimeError(f"{layer} changed during preflight; re-read before publishing")
        put_json(config, f"{path}?recalculate=", {"featureType": updated})
        changed = True
        logger.info("Published ventilation on %s", layer)
    if changed:
        reload_geoserver(config)
    for layer, path, _, _ in prepared:
        current = get_json(config, path)["featureType"]
        if ensure_ventilation(current, LAYERS[layer]["geometry_column"]) != current:
            raise RuntimeError(f"Ventilation publication readback failed: {layer}")
    # Also truncate on a rerun: a previous publication may have stopped before
    # invalidation. No density layer, cache configuration, or datastore changes.
    for layer in LAYERS:
        truncate_gwc_layer(config, f"{config['workspace']}:{layer}")
    logger.info("Publication finished; verify regenerated MVTs before frontend handoff")


if __name__ == "__main__":
    main()
