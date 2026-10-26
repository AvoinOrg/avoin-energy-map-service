"""Backfill only main's certificate-consumption field from retained source rows."""

import argparse
from collections import Counter
import gzip
import json
import logging
from pathlib import Path

from sqlalchemy import text
from psycopg2.extras import execute_values

from src.etl.certificate_consumption import CONSUMPTION_COLUMN, consumption_payload

logger = logging.getLogger(__name__)


def augment_consumption(session, *, apply=True, clear_stale=True, backup_path=None):
    """Stage the latest certificate per ID and update just the owned column.

    The caller owns the transaction. Never rebuild main from sparse buildings.
    Selection matches the existing certificate augmentation, including lexical
    certificate_id tie-breaking and no older-row fallback for missing data.
    """
    if session.execute(text("SELECT count(*) FROM energy_certificates")).scalar_one() == 0:
        raise RuntimeError("Refusing consumption refresh from an empty certificate table")
    session.execute(text("""
        CREATE TEMP TABLE staged_certificate_consumption (
            building_identifier text PRIMARY KEY, payload text
        ) ON COMMIT DROP
    """))
    rows = session.execute(text("""
        SELECT DISTINCT ON (building_identifier)
            building_identifier, certificate_id, version, signed_at,
            valid_until, heated_net_area, source_row
        FROM energy_certificates
        WHERE NULLIF(TRIM(building_identifier), '') IS NOT NULL
        ORDER BY building_identifier, signed_at DESC NULLS LAST, certificate_id DESC
    """)).mappings()
    counts = Counter()
    batch = []
    for row in rows:
        payload = consumption_payload(row)
        counts["selected_certificates"] += 1
        if payload is not None:
            value = json.loads(payload)
            counts["with_consumption"] += 1
            counts[value["completeness"]] += 1
        batch.append((row["building_identifier"], payload))
        if len(batch) == 1000:
            with session.connection().connection.driver_connection.cursor() as cursor:
                execute_values(cursor, "INSERT INTO staged_certificate_consumption VALUES %s", batch, page_size=1000)
            batch.clear()
            if counts["selected_certificates"] % 40000 == 0:
                logger.info("Staged %s latest certificates", counts["selected_certificates"])
    if batch:
        with session.connection().connection.driver_connection.cursor() as cursor:
            execute_values(cursor, "INSERT INTO staged_certificate_consumption VALUES %s", batch, page_size=1000)
    if counts["selected_certificates"] == 0:
        raise RuntimeError("No certificate building identifiers; refusing to clear publication")
    session.execute(text("ANALYZE staged_certificate_consumption"))
    logger.info("Certificate staging complete; checking building joins and changed rows")
    ambiguous = session.execute(text("""
        SELECT s.building_identifier
        FROM staged_certificate_consumption s JOIN main m
          ON m.permanent_building_identifier = s.building_identifier
        GROUP BY s.building_identifier
        HAVING count(DISTINCT m.building_key) > 1 LIMIT 1
    """)).first()
    if ambiguous:
        raise RuntimeError("A certificate building identifier maps to multiple building keys")
    exists = session.execute(text("""
        SELECT data_type FROM information_schema.columns
        WHERE table_schema='public' AND table_name='main' AND column_name=:column
    """), {"column": CONSUMPTION_COLUMN}).scalar_one_or_none()
    if exists is not None and exists != "text":
        raise RuntimeError("Existing consumption publication column has an unexpected type")
    if apply and exists is None:
        session.execute(text(f"ALTER TABLE main ADD COLUMN {CONSUMPTION_COLUMN} text"))
    old = f"m.{CONSUMPTION_COLUMN}" if exists is not None or apply else "NULL::text"
    stale_filter = "true" if clear_stale else "s.building_identifier IS NOT NULL"
    changed = f"""
        FROM main m LEFT JOIN staged_certificate_consumption s
          ON m.permanent_building_identifier = s.building_identifier
        WHERE {stale_filter} AND {old} IS DISTINCT FROM s.payload
    """
    counts["changed_main_rows"] = session.execute(text("SELECT count(*) " + changed)).scalar_one()
    if backup_path is not None:
        with gzip.open(backup_path, "xt", encoding="utf-8") as handle:
            for row in session.execute(text(
                f"SELECT m.building_key, m.permanent_building_identifier, m.address_key, {old} AS old_payload, s.payload AS new_payload " + changed
            )).mappings():
                handle.write(json.dumps(dict(row), default=str) + "\n")
    if apply and counts["changed_main_rows"]:
        logger.info("Applying %s changed map rows", counts["changed_main_rows"])
        result = session.execute(text(f"""
            UPDATE main m SET {CONSUMPTION_COLUMN}=s.payload
            FROM staged_certificate_consumption s
            WHERE m.permanent_building_identifier=s.building_identifier
              AND m.{CONSUMPTION_COLUMN} IS DISTINCT FROM s.payload
        """))
        counts["updated_main_rows"] = result.rowcount
        if clear_stale and counts["changed_main_rows"] > counts["updated_main_rows"]:
            result = session.execute(text(f"""
                UPDATE main m SET {CONSUMPTION_COLUMN}=NULL
                WHERE m.{CONSUMPTION_COLUMN} IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM staged_certificate_consumption s
                    WHERE s.building_identifier=m.permanent_building_identifier
                )
            """))
            counts["cleared_stale_rows"] = result.rowcount
    if apply:
        counts.setdefault("updated_main_rows", 0)
        counts.setdefault("cleared_stale_rows", 0)
    # Allow repeated calls in the same transaction, including integration tests.
    session.execute(text("DROP TABLE staged_certificate_consumption"))
    return dict(counts)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit; default is read-only dry run")
    parser.add_argument("--backup-path", type=Path, help="New .jsonl.gz file of changed field values")
    args = parser.parse_args()
    if args.apply and args.backup_path is None:
        parser.error("--apply requires --backup-path")
    from src.db.session import SessionLocal
    if args.apply:
        # Short schema transaction: avoid holding ACCESS EXCLUSIVE throughout
        # the national backfill and blocking live map reads unnecessarily.
        with SessionLocal() as schema_session:
            schema_session.execute(text("SET LOCAL lock_timeout = '5s'"))
            schema_session.execute(text(f"ALTER TABLE main ADD COLUMN IF NOT EXISTS {CONSUMPTION_COLUMN} text"))
            schema_session.commit()
    with SessionLocal() as session:
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(text("SET LOCAL statement_timeout = '300s'"))
        session.execute(text("SET LOCAL jit = off"))
        # Keep certificate selection stable while the staged projection is used.
        session.execute(text("LOCK TABLE energy_certificates IN SHARE MODE"))
        counts = augment_consumption(session, apply=args.apply, backup_path=args.backup_path)
        if args.apply:
            session.commit()
        else:
            session.rollback()
        print(json.dumps({"applied": args.apply, **counts}, sort_keys=True))


if __name__ == "__main__":
    main()
