"""Import immutable national boundary and Ryhti geography snapshots."""

import argparse
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
from uuid import UUID
from urllib.parse import urlsplit, parse_qs

import geopandas as gpd
import requests
from shapely import force_2d, make_valid, union_all
from shapely.geometry import MultiPolygon
from sqlalchemy import text

from src.db.session import SessionLocal
from src.etl.load_property_identifiers import download_snapshot, snapshot_checksum

CLASSIFICATION = Path(__file__).resolve().parents[2] / "assets/municipality-region-2026.csv"
RYHTI_URL = "https://paikkatiedot.ymparisto.fi/geoserver/www/open_building.csv.gz"
from src.area_contract import LEVEL_WIDTH
INPUT_VERSION = "areas-input-v1"


def json_text(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def area_code(value, level):
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing or invalid area code")
    code = str(value).strip()
    if not re.fullmatch(r"[0-9]{1," + str(LEVEL_WIDTH[level]) + "}", code):
        raise ValueError(f"Invalid {level} code")
    return code.zfill(LEVEL_WIDTH[level])


def input_manifest(path, source_url, **extra):
    url = urlsplit(source_url)
    if url.username or url.password or any(k.lower() in
            {"api_key", "apikey", "key", "token", "access_token", "password", "signature"}
            for k in parse_qs(url.query)):
        raise ValueError("Use a public source URL without credentials in published manifests")
    path = Path(path)
    checksum = snapshot_checksum(path)
    upstream = None
    for candidate in (path.with_suffix(path.suffix + ".source.json"), path.with_suffix(".source.json")):
        if candidate.exists():
            metadata = json.loads(candidate.read_text())
            if metadata.get("sha256", metadata.get("csv_sha256")) == checksum:
                # Sidecars are evidence, not an arbitrary public JSON payload.
                # Keep only non-credential attribution/version fields; the caller
                # supplies the separately validated canonical source URL.
                upstream = {k:v for k,v in metadata.items() if k in {
                    "retrieved_at", "publisher", "license", "feature_type", "feature_count",
                    "crs", "vintage", "sha256", "csv_sha256", "source_sha256", "source_version"}}
                break
    return {"file": path.name, "sha256": checksum, "source_url": source_url,
            "retrieved_at": upstream.get("retrieved_at") if upstream else None,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "file_modified_at": datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),
            **({"upstream_manifest": upstream} if upstream else {}), **extra}


def copy_rows(session, table, columns, rows):
    """COPY trusted internal identifiers; CSV quotes data and preserves nulls."""
    if not all(re.fullmatch(r"[a-z_]+", s) for s in (table, *columns)):
        raise ValueError("Invalid internal COPY identifier")
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    connection = session.connection().connection.driver_connection
    count = 0

    def flush():
        buffer.seek(0)
        with connection.cursor() as cursor:
            cursor.copy_expert(f"COPY {table} ({','.join(columns)}) FROM STDIN WITH (FORMAT CSV)", buffer)
        buffer.seek(0)
        buffer.truncate(0)

    for row in rows:
        writer.writerow(row)
        count += 1
        if count % 10_000 == 0:
            flush()
    if buffer.tell():
        flush()
    return count


def download_wfs(path, endpoint, typename, expected_count, page_size=250):
    """Stable pagination with completeness checks; replace only after success."""
    if page_size < 1:
        raise ValueError("page_size must be positive")
    features, ids, reported = [], set(), None
    with requests.Session() as http:
        capabilities = http.get(endpoint, params={"service": "WFS", "version": "2.0.0",
                                                  "request": "GetCapabilities"}, timeout=60)
        capabilities.raise_for_status()
        if typename not in capabilities.text:
            raise RuntimeError("WFS feature type is not advertised")
        schema = http.get(endpoint, params={"service": "WFS", "version": "2.0.0",
                                           "request": "DescribeFeatureType", "typeNames": typename}, timeout=60)
        schema.raise_for_status()
        if "ExceptionReport" in schema.text:
            raise RuntimeError("WFS schema request failed")
        while True:
            response = http.get(endpoint, params={
                "service": "WFS", "version": "2.0.0", "request": "GetFeature",
                "typeNames": typename, "outputFormat": "application/json",
                "srsName": "EPSG:3067", "count": page_size,
                "startIndex": len(features), "sortBy": "id A",
            }, timeout=(20, 180))
            response.raise_for_status()
            data = response.json()
            total = data.get("numberMatched", data.get("totalFeatures"))
            if not isinstance(total, int) or total < 1 or reported not in (None, total):
                raise RuntimeError("WFS count missing or changed during download")
            reported = total
            batch = data.get("features", [])
            if not batch:
                if len(features) != reported:
                    raise RuntimeError("WFS download ended before the reported count")
                break
            for feature in batch:
                identifier = feature.get("id")
                if identifier is None or identifier in ids:
                    raise RuntimeError("Duplicate/missing WFS feature identifier")
                ids.add(identifier)
            features.extend(batch)
            if len(features) >= reported:
                break
    if len(features) != reported or reported != expected_count:
        raise RuntimeError(f"Boundary count changed: expected {expected_count}, downloaded {len(features)}")
    output = {"type": "FeatureCollection", "features": features,
              "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3067"}}}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json_text(output))
    temporary.replace(path)
    manifest = input_manifest(path, endpoint, feature_type=typename, feature_count=reported,
                              retrieved_at=datetime.now(timezone.utc).isoformat(), crs="EPSG:3067", publisher="Statistics Finland", license="CC BY 4.0")
    path.with_suffix(path.suffix + ".source.json").write_text(json_text(manifest) + "\n")
    return manifest


def classification_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    result = {}
    for row in rows:
        code = area_code(row["municipality_code"], "municipality")
        if code in result:
            raise RuntimeError("Duplicate municipality in classification")
        result[code] = {**row, "municipality_code": code,
                       "region_code": area_code(row["region_code"], "region")}
    if not result:
        raise RuntimeError("Empty municipality-to-region classification")
    return result


def boundary_rows(path, level, vintage, code_field, names=("nimi", "namn", "name"), layer=None):
    frame = gpd.read_file(path, **({"layer": layer} if layer else {}))
    if frame.crs is None or code_field not in frame:
        raise RuntimeError("Boundary input needs a declared CRS and code column")
    if "vuosi" in frame and any(int(v) != vintage for v in frame["vuosi"]):
        raise RuntimeError("Boundary vintage mismatch")
    frame = frame.to_crs(3067)
    grouped, repairs = {}, []
    for _, row in frame.iterrows():
        code = area_code(row[code_field], level)
        geom = row.geometry
        if geom is None or geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
            raise RuntimeError("Empty or non-polygon boundary")
        geom = force_2d(geom)
        if not geom.is_valid:
            fixed = make_valid(geom)
            difference = abs(fixed.area - geom.area)
            if fixed.geom_type not in ("Polygon", "MultiPolygon") or difference > max(1, geom.area * 1e-6):
                raise RuntimeError("Boundary requires a material geometry repair")
            repairs.append({"level": level, "area_code": code, "area_difference_m2": difference})
            geom = fixed
        item = grouped.setdefault(code, {"parts": [], "names": []})
        item["parts"].append(geom)
        item["names"].append(tuple(str(row[n]) if n in row and row[n] is not None and str(row[n]) != "nan" else "" for n in names))
    result = []
    for code, item in sorted(grouped.items()):
        geom = union_all(item["parts"])
        if geom.geom_type == "Polygon":
            geom = MultiPolygon([geom])
        if geom.is_empty or not geom.is_valid or geom.geom_type != "MultiPolygon":
            raise RuntimeError("Invalid dissolved boundary")
        names = sorted(set(item["names"]))
        if len(names) != 1:
            raise RuntimeError("Conflicting names for one boundary code")
        result.append({"level": level, "area_code": code, "names": names[0], "wkt": geom.wkt})
    return result, repairs


def import_boundaries(session, *, postal_path, municipality_path, region_path,
                      classification_path=CLASSIFICATION, vintage=2026,
                      municipality_code_field="kunta", municipality_names=("nimi", "namn", "name"), municipality_layer=None,
                      municipality_source_url, municipality_source_version,
                      expected_counts=None, simplify_m=20, max_overlap_m2=1):
    if vintage < 2000 or not municipality_source_url or not municipality_source_version:
        raise ValueError("Explicit municipality source URL, release and vintage are required")
    if Path(classification_path).resolve() == CLASSIFICATION.resolve() and vintage != 2026:
        raise ValueError("The bundled classification is specifically the 2026 vintage")
    if simplify_m < 0 or max_overlap_m2 < 0:
        raise ValueError("Geometry tolerances must be nonnegative")
    mapping = classification_rows(classification_path)
    manifests = [
        input_manifest(postal_path, "https://geo.stat.fi/geoserver/postialue/wfs", level="postal_code", feature_type=f"postialue:pno_{vintage}", publisher="Statistics Finland", license="CC BY 4.0"),
        input_manifest(municipality_path, municipality_source_url, level="municipality",
                       source_version=municipality_source_version, scale="1:10000", layer=municipality_layer, publisher="National Land Survey of Finland", license="CC BY 4.0"),
        input_manifest(region_path, "https://geo.stat.fi/geoserver/tilastointialueet/wfs", level="region", feature_type=f"tilastointialueet:maakunta1000k_{vintage}", publisher="Statistics Finland", license="CC BY 4.0"),
        input_manifest(classification_path, "https://stat.fi/fi/luokitukset/tupa", level="classification", publisher="Statistics Finland", license="CC BY 4.0"),
    ]
    identity = {"inputs": [{k: v for k, v in m.items() if k not in ("retrieved_at", "imported_at", "file_modified_at", "upstream_manifest")} for m in manifests],
                "vintage": vintage, "version": INPUT_VERSION, "simplify_m": simplify_m,
                "max_overlap_m2": max_overlap_m2, "municipality_code_field": municipality_code_field,
                "municipality_names": municipality_names, "municipality_layer": municipality_layer}
    set_id = str(UUID(bytes=hashlib.sha256(json_text(identity).encode()).digest()[:16], version=5))
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('energymap_area_boundaries'))"))
    existing = session.execute(text("SELECT manifest FROM energymap_area_boundary_sets WHERE boundary_set_id=:id"), {"id": set_id}).scalar()
    if existing is not None:
        if expected_counts is not None and existing["counts"] != expected_counts:
            raise RuntimeError("Existing boundary counts do not meet the requested policy")
        return {"boundary_set_id": set_id, "reused": True, **existing}
    areas, repairs, counts = [], [], {}
    for path, level, code_field, names in (
        (postal_path, "postal_code", "posti_alue", ("nimi", "namn", "name")),
        (municipality_path, "municipality", municipality_code_field, municipality_names),
        (region_path, "region", "maakunta", ("nimi", "namn", "name")),
    ):
        rows, fixed = boundary_rows(path, level, vintage, code_field, names, layer=municipality_layer if level == "municipality" else None)
        counts[level] = len(rows)
        if expected_counts is not None and counts[level] != expected_counts[level]:
            raise RuntimeError(f"Unexpected {level} count")
        areas.extend(rows)
        repairs.extend(fixed)
    municipal_codes = {a["area_code"] for a in areas if a["level"] == "municipality"}
    region_codes = {a["area_code"] for a in areas if a["level"] == "region"}
    if municipal_codes != set(mapping) or region_codes != {v["region_code"] for v in mapping.values()}:
        raise RuntimeError("Boundary and classification code sets disagree")
    manifest = {"inputs": manifests, "vintage": vintage, "counts": counts, "repairs": repairs,
                "input_version": INPUT_VERSION, "simplify_m": simplify_m, "max_overlap_m2": max_overlap_m2}
    session.execute(text("INSERT INTO energymap_area_boundary_sets VALUES (:id,:v,CAST(:m AS jsonb),now())"),
                    {"id": set_id, "v": vintage, "m": json_text(manifest)})
    session.execute(text("CREATE TEMP TABLE area_boundary_import (level text, area_code text, name_fi text, name_sv text, name_en text, region_code text, wkt text) ON COMMIT DROP"))
    def records():
        for area in areas:
            code, level = area["area_code"], area["level"]
            names = area["names"]
            region = None
            if level == "municipality":
                row = mapping[code]
                names = tuple(row.get("name_" + lang) for lang in ("fi", "sv", "en"))
                region = row["region_code"]
            yield (level, code, names[0] or code, names[1], names[2], region, area["wkt"])
    copy_rows(session, "area_boundary_import", ("level", "area_code", "name_fi", "name_sv", "name_en", "region_code", "wkt"), records())
    session.execute(text("""
        INSERT INTO energymap_area_boundaries
        SELECT :id, level, area_code, name_fi, name_sv, name_en, region_code,
               ST_GeomFromText(wkt,3067),
               ST_Multi(ST_Transform(ST_SimplifyPreserveTopology(ST_GeomFromText(wkt,3067),:s),3857))
        FROM area_boundary_import
    """), {"id": set_id, "s": simplify_m})
    overlap = session.execute(text("""
        SELECT a.level,a.area_code,b.area_code
        FROM energymap_area_boundaries a JOIN energymap_area_boundaries b
          ON a.boundary_set_id=b.boundary_set_id AND a.level=b.level AND a.area_code<b.area_code
          AND a.geom && b.geom
        WHERE a.boundary_set_id=:id AND a.level IN ('postal_code','municipality')
          AND ST_Area(ST_Intersection(a.geom,b.geom))>:t LIMIT 1
    """), {"id": set_id, "t": max_overlap_m2}).first()
    if overlap:
        raise RuntimeError(f"Interior overlap in {overlap[0]} boundaries: {overlap[1]}, {overlap[2]}")
    session.execute(text("ANALYZE energymap_area_boundaries"))
    return {"boundary_set_id": set_id, "reused": False, **manifest}


def geography_record(row, export_srid):
    key = str(UUID(row["building_key"].strip()))
    municipality = row.get("municipality_number", "").strip()
    municipality = area_code(municipality, "municipality") if municipality else None
    x, y = row.get("X", "").strip(), row.get("Y", "").strip()
    wkt = None
    if x or y:
        if not x or not y or not all(math.isfinite(float(n)) for n in (x, y)):
            raise ValueError("Invalid populated registry location")
        if export_srid != 3067:
            raise ValueError("The Ryhti CSV importer requires declared EPSG:3067 export coordinates")
        wkt = f"POINT({float(x)} {float(y)})"
    provenance = {k: row.get(k) or None for k in
                  ("modified_timestamp_utc", "point_location_srid", "usage_status", "demolition_date")}
    return key, municipality, wkt, json_text(provenance)


def import_geography(session, path, *, source_url=RYHTI_URL, export_srid=3067, min_records=1_000_000):
    if min_records < 1 or export_srid != 3067:
        raise ValueError("Invalid geography import policy")
    source_manifest = input_manifest(path, source_url)
    checksum = source_manifest["sha256"]
    snapshot_id = hashlib.sha256(f"{checksum}:{INPUT_VERSION}:{export_srid}".encode()).hexdigest()
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('energymap_area_geography'))"))
    existing = session.execute(text("SELECT manifest FROM energymap_area_geography_snapshots WHERE snapshot_id=:id"), {"id": snapshot_id}).scalar()
    if existing is not None:
        if existing["unique_keys"] < min_records:
            raise RuntimeError("Existing geography snapshot does not meet the minimum population")
        return {"snapshot_id": snapshot_id, "reused": True, **existing}
    session.execute(text("CREATE TEMP TABLE area_geography_import (building_key text, municipality_code text, wkt text, provenance jsonb) ON COMMIT DROP"))
    opener = gzip.open if Path(path).suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not {"building_key", "municipality_number", "X", "Y"}.issubset(reader.fieldnames or []):
            raise RuntimeError("Missing required national Ryhti geography headers")
        source_rows = copy_rows(session, "area_geography_import",
                               ("building_key", "municipality_code", "wkt", "provenance"),
                               (geography_record(row, export_srid) for row in reader))
    if source_rows < min_records:
        raise RuntimeError("Refusing an incomplete national geography snapshot")
    conflict = session.execute(text("""
        SELECT building_key FROM area_geography_import GROUP BY building_key
        HAVING count(DISTINCT ROW(municipality_code,wkt,provenance))>1 LIMIT 1
    """)).first()
    if conflict:
        raise RuntimeError("Conflicting geography rows for one building key")
    counts = session.execute(text("""
        SELECT count(DISTINCT building_key) AS unique_keys,
            count(*) FILTER (WHERE wkt IS NULL) AS missing_location_rows,
            count(*) FILTER (WHERE municipality_code IS NULL) AS missing_municipality_rows
        FROM area_geography_import
    """)).mappings().one()
    if counts["unique_keys"] < min_records:
        raise RuntimeError("Too few unique building keys in geography snapshot")
    manifest = {**source_manifest, **dict(counts), "source_rows": source_rows,
                "identical_duplicate_rows": source_rows - counts["unique_keys"],
                "input_version": INPUT_VERSION, "export_crs": "EPSG:3067", "license": "CC BY 4.0"}
    session.execute(text("INSERT INTO energymap_area_geography_snapshots VALUES (:id,CAST(:m AS jsonb),now())"),
                    {"id": snapshot_id, "m": json_text(manifest)})
    session.execute(text("""
        INSERT INTO energymap_area_geography
        SELECT DISTINCT :id,building_key,municipality_code,ST_GeomFromText(wkt,3067),provenance
        FROM area_geography_import
    """), {"id": snapshot_id})
    session.execute(text("ANALYZE energymap_area_geography"))
    return {"snapshot_id": snapshot_id, "reused": False, **manifest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    download = sub.add_parser("download")
    download.add_argument("--directory", type=Path, default=Path("data/areas/2026"))
    boundary = sub.add_parser("boundaries")
    boundary.add_argument("--postal-file", type=Path, required=True)
    boundary.add_argument("--municipality-file", type=Path, required=True)
    boundary.add_argument("--region-file", type=Path, required=True)
    boundary.add_argument("--classification-file", type=Path, default=CLASSIFICATION)
    boundary.add_argument("--vintage", type=int, default=2026)
    boundary.add_argument("--municipality-code-field", default="kunta")
    boundary.add_argument("--municipality-layer")
    boundary.add_argument("--municipality-name-fields", nargs=3, default=["nimi", "namn", "name"])
    boundary.add_argument("--municipality-source-url", required=True)
    boundary.add_argument("--municipality-source-version", required=True)
    boundary.add_argument("--dry-run", action="store_true")
    geo = sub.add_parser("geography")
    geo.add_argument("--csv-path", type=Path, default=Path("data/ryhti-open-building.csv.gz"))
    geo.add_argument("--download", action="store_true")
    geo.add_argument("--source-url", default=RYHTI_URL)
    geo.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "download":
        for workspace, name, count in (("postialue", "pno_2026", 3018),
                                        ("tilastointialueet", "maakunta1000k_2026", 19)):
            print(json_text(download_wfs(args.directory / (name + ".geojson"),
                  f"https://geo.stat.fi/geoserver/{workspace}/wfs", f"{workspace}:{name}", count)))
        return
    if args.command == "geography" and args.download:
        download_snapshot(args.source_url, args.csv_path)
    with SessionLocal() as session:
        if args.command == "geography":
            report = import_geography(session, args.csv_path, source_url=args.source_url)
        else:
            report = import_boundaries(session, postal_path=args.postal_file,
                municipality_path=args.municipality_file, region_path=args.region_file,
                classification_path=args.classification_file, vintage=args.vintage,
                municipality_code_field=args.municipality_code_field, municipality_layer=args.municipality_layer,
                municipality_names=tuple(args.municipality_name_fields),
                municipality_source_url=args.municipality_source_url,
                municipality_source_version=args.municipality_source_version,
                expected_counts={"postal_code": 3018, "municipality": 308, "region": 19})
        session.rollback() if args.dry_run else session.commit()
    print(json_text(report))


if __name__ == "__main__":
    main()
