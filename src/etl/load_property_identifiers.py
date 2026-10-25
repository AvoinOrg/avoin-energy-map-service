"""Import the authoritative Ryhti property-ID projection without a main rebuild."""

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
from uuid import UUID

import requests
from sqlalchemy import text

from db.session import SessionLocal


DEFAULT_SOURCE_URL = (
    "https://paikkatiedot.ymparisto.fi/geoserver/www/open_building.csv.gz"
)
REQUIRED_HEADERS = {"building_key", "property_identifier"}
COPY_BATCH_SIZE = 10_000
logger = logging.getLogger("etl.property_identifiers")


def validated_projection(row: dict[str, str]) -> tuple[str, str] | None:
    """Keep IDs as strings; reject malformed populated values, skip unknown IDs."""
    key = row.get("building_key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Ryhti row has no building_key")
    try:
        normalized_key = str(UUID(key.strip()))
    except ValueError as exc:
        raise ValueError("Ryhti row has an invalid building_key") from exc
    if normalized_key != key.strip().lower():
        raise ValueError("Ryhti building_key must be a hyphenated UUID")

    identifier = row.get("property_identifier")
    if identifier is None:
        return None
    if not isinstance(identifier, str):
        raise ValueError("Ryhti property_identifier must remain a string")
    if not identifier.strip():
        return None
    identifier = identifier.strip()
    if len(identifier) != 14 or not identifier.isascii() or not identifier.isdigit():
        raise ValueError("Ryhti property_identifier must be a 14-digit string")
    if identifier == "0" * 14:
        return None
    return normalized_key, identifier


def snapshot_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_snapshot(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with requests.get(url, stream=True, timeout=(20, 120)) as response:
            response.raise_for_status()
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
                temporary_path = Path(target.name)
                for chunk in response.iter_content(1024 * 1024):
                    target.write(chunk)
        temporary_path.replace(path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def apply_property_identifiers(session) -> dict[str, int]:
    """Update only matching rows and only this column, never clear unknown data."""
    projection_exists = session.execute(
        text("SELECT to_regclass('public.ryhti_property_identifiers') IS NOT NULL")
    ).scalar_one()
    if not projection_exists:
        return {"updated_buildings": 0, "updated_main": 0}
    logger.info("Applying the property-only projection to existing buildings")
    updated_buildings = session.execute(text("""
        UPDATE buildings b
        SET property_identifier = p.property_identifier
        FROM ryhti_property_identifiers p
        WHERE b.building_key = p.building_key
          AND b.property_identifier IS DISTINCT FROM p.property_identifier;
    """)).rowcount
    logger.info("Updated %s staging rows; applying the property-only projection to main", updated_buildings)
    updated_main = session.execute(text("""
        UPDATE main m
        SET property_identifier = p.property_identifier
        FROM ryhti_property_identifiers p
        WHERE m.building_key = p.building_key
          AND m.property_identifier IS DISTINCT FROM p.property_identifier;
    """)).rowcount
    logger.info("Updated %s map rows", updated_main)
    return {"updated_buildings": updated_buildings, "updated_main": updated_main}


def import_snapshot(
    path: Path,
    *,
    source_url: str = DEFAULT_SOURCE_URL,
    dry_run: bool = False,
    min_records: int = 1_000_000,
) -> dict:
    checksum = snapshot_checksum(path)
    source_rows = valid_rows = missing_rows = 0
    with SessionLocal() as session:
        session.execute(text("SET LOCAL lock_timeout = '30s'"))
        locked = session.execute(text("""
            SELECT pg_try_advisory_xact_lock(hashtext('ryhti_property_identifier_import'))
        """)).scalar_one()
        if not locked:
            raise RuntimeError("Another property identifier import is running")
        session.execute(text("""
            CREATE TEMP TABLE property_identifier_import (
                building_key text NOT NULL,
                property_identifier text NOT NULL
            ) ON COMMIT DROP;
        """))
        connection = session.connection().connection.driver_connection
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")

        def copy_batch() -> None:
            buffer.seek(0)
            with connection.cursor() as cursor:
                cursor.copy_expert(
                    "COPY property_identifier_import FROM STDIN WITH (FORMAT CSV)",
                    buffer,
                )
            buffer.seek(0)
            buffer.truncate(0)

        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not REQUIRED_HEADERS.issubset(reader.fieldnames or []):
                raise RuntimeError("Snapshot is missing required Ryhti projection headers")
            for row in reader:
                source_rows += 1
                projection = validated_projection(row)
                if projection is None:
                    missing_rows += 1
                    continue
                writer.writerow(projection)
                valid_rows += 1
                if valid_rows % COPY_BATCH_SIZE == 0:
                    copy_batch()
                    if valid_rows % 250_000 == 0:
                        logger.info("Validated and staged %s source rows", source_rows)
        if buffer.tell():
            copy_batch()
        if valid_rows < min_records:
            raise RuntimeError(
                f"Refusing a small/incomplete national snapshot: {valid_rows} valid rows"
            )
        logger.info("Staged %s valid source rows; checking duplicate-key consistency", valid_rows)
        conflicts = session.execute(text("""
            SELECT building_key
            FROM property_identifier_import
            GROUP BY building_key
            HAVING count(DISTINCT property_identifier) > 1
            LIMIT 1;
        """)).first()
        if conflicts:
            raise RuntimeError("Conflicting property identifiers for one Ryhti building_key")
        unique_keys = session.execute(text("""
            SELECT count(DISTINCT building_key) FROM property_identifier_import
        """)).scalar_one()
        report = {
            "source_url": source_url,
            "snapshot_sha256": checksum,
            "source_rows": source_rows,
            "valid_rows": valid_rows,
            "missing_property_identifiers": missing_rows,
            "unique_keys": unique_keys,
            "identical_duplicate_rows": valid_rows - unique_keys,
            "dry_run": dry_run,
        }
        if dry_run:
            session.rollback()
            return report
        logger.info("Validated %s unique building keys; publishing the dedicated snapshot", unique_keys)
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS ryhti_property_identifiers (
                building_key text PRIMARY KEY,
                property_identifier text NOT NULL
                    CHECK (property_identifier ~ '^[0-9]{14}$'
                           AND property_identifier <> '00000000000000')
            );
            CREATE TABLE IF NOT EXISTS ryhti_property_identifier_imports (
                snapshot_sha256 text PRIMARY KEY,
                source_url text NOT NULL,
                imported_at timestamptz NOT NULL DEFAULT now(),
                source_rows bigint NOT NULL,
                unique_keys bigint NOT NULL,
                missing_property_identifiers bigint NOT NULL
            );
            TRUNCATE TABLE ryhti_property_identifiers;
            INSERT INTO ryhti_property_identifiers (building_key, property_identifier)
            SELECT DISTINCT building_key, property_identifier
            FROM property_identifier_import;
            ANALYZE ryhti_property_identifiers;
        """))
        report.update(apply_property_identifiers(session))
        if report["updated_main"]:
            session.execute(text("ANALYZE main (property_identifier)"))
        session.execute(text("""
            INSERT INTO ryhti_property_identifier_imports (
                snapshot_sha256, source_url, source_rows, unique_keys,
                missing_property_identifiers
            ) VALUES (:snapshot_sha256, :source_url, :source_rows, :unique_keys,
                      :missing_property_identifiers)
            ON CONFLICT (snapshot_sha256) DO NOTHING;
        """), report)
        session.commit()
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-path", type=Path, default=Path(os.getenv(
        "RYHTI_BUILDINGS_SNAPSHOT_PATH", "data/ryhti-open-building.csv.gz"
    )))
    parser.add_argument("--source-url", default=os.getenv(
        "RYHTI_BUILDINGS_SNAPSHOT_URL", DEFAULT_SOURCE_URL
    ))
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-records", type=int, default=1_000_000,
                        help="Minimum valid national snapshot size; lower only for fixtures")
    args = parser.parse_args()
    if args.min_records < 1:
        parser.error("--min-records must be positive")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.download:
        download_snapshot(args.source_url, args.csv_path)
    report = import_snapshot(args.csv_path, source_url=args.source_url,
                             dry_run=args.dry_run, min_records=args.min_records)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
