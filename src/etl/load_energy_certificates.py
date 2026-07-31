import argparse
import csv
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

import requests
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert

from db.models import EnergyCertificate
from db.session import SessionLocal


SOURCE_URL = os.getenv(
    "ENERGY_CERTIFICATES_CSV_URL",
    "https://www.energiatodistusrekisteri.fi/api/public/energiatodistukset/csv/energiatodistukset.csv",
)
DEFAULT_CSV_PATH = os.getenv(
    "ENERGY_CERTIFICATES_CSV_PATH", "data/energiatodistukset.csv"
)
BATCH_SIZE = int(os.getenv("ENERGY_CERTIFICATES_BATCH_SIZE", "1000"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
)
logger = logging.getLogger("etl.energy_certificates")


def text_value(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def int_value(value: str | None) -> int | None:
    value = text_value(value)
    if value is None:
        return None
    try:
        return int(float(value.replace(",", ".")))
    except ValueError:
        return None


def float_value(value: str | None) -> float | None:
    value = text_value(value)
    if value is None:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def datetime_value(value: str | None) -> datetime | None:
    value = text_value(value)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except ValueError:
        return None


def date_value(value: str | None) -> date | None:
    value = text_value(value)
    if value is None:
        return None
    try:
        return date.fromisoformat(value.split("T", 1)[0])
    except ValueError:
        return None


CSV_TO_MODEL: Dict[str, tuple[str, Callable[[str | None], Any]]] = {
    "Id": ("certificate_id", text_value),
    "Versio": ("version", int_value),
    "Allekirjoitusaika": ("signed_at", datetime_value),
    "Voimassaolo-paattymisaika": ("valid_until", datetime_value),
    "Perustiedot / Kieli": ("language", text_value),
    "Perustiedot / Laatimisvaihe": ("drafting_stage", text_value),
    "Perustiedot / Havainnointikaynti": ("inspection_date", date_value),
    "Perustiedot / Nimi-fi": ("name_fi", text_value),
    "Perustiedot / Nimi-sv": ("name_sv", text_value),
    "Perustiedot / Valmistumisvuosi": ("completion_year", int_value),
    "Perustiedot / Katuosoite-fi": ("street_address_fi", text_value),
    "Perustiedot / Katuosoite-sv": ("street_address_sv", text_value),
    "Perustiedot / Postinumero": ("postal_code", text_value),
    "Perustiedot / Rakennustunnus": ("building_identifier", text_value),
    "Perustiedot / Kayttotarkoitus": ("use_purpose", text_value),
    "Perustiedot / Alakayttotarkoitus-fi": ("sub_use_purpose_fi", text_value),
    "Tulokset / E-luku": ("e_value", float_value),
    "Tulokset / E-luokka": ("e_class", text_value),
    "Tulokset / E-luokka-rajat / Raja-uusi-2018": ("e_class_limit_2018", float_value),
    "Tulokset / E-luokka-rajat / Kayttotarkoitus / Label-fi": ("e_class_purpose_label_fi", text_value),
    "Perustiedot / Keskeiset-suositukset-fi": ("recommendations_fi", text_value),
    "Perustiedot / Keskeiset-suositukset-sv": ("recommendations_sv", text_value),
    "Lahtotiedot / Lammitetty-nettoala": ("heated_net_area", float_value),
    "Lahtotiedot / Ilmanvaihto / Tyyppi-id": ("ventilation_type_id", text_value),
    "Lahtotiedot / Ilmanvaihto / Kuvaus-fi": ("ventilation_description_fi", text_value),
    "Lahtotiedot / Ilmanvaihto / Kuvaus-sv": ("ventilation_description_sv", text_value),
    "Lahtotiedot / Lammitys / Lammitysmuoto-1 / Id": ("heating_form_1_id", text_value),
    "Lahtotiedot / Lammitys / Lammitysmuoto-2 / Id": ("heating_form_2_id", text_value),
    "Lahtotiedot / Lammitys / Lammitysmuoto-1 / Kuvaus-fi": ("heating_form_1_description_fi", text_value),
    "Lahtotiedot / Lammitys / Lammitysmuoto-1 / Kuvaus-sv": ("heating_form_1_description_sv", text_value),
    "Lahtotiedot / Lammitys / Lammitysmuoto-2 / Kuvaus-fi": ("heating_form_2_description_fi", text_value),
    "Lahtotiedot / Lammitys / Lammitysmuoto-2 / Kuvaus-sv": ("heating_form_2_description_sv", text_value),
    "Lahtotiedot / Lammitys / Lammonjako / Id": ("heat_distribution_id", text_value),
    "Lahtotiedot / Lammitys / Lammonjako / Kuvaus-fi": ("heat_distribution_description_fi", text_value),
    "Lahtotiedot / Lammitys / Lammonjako / Kuvaus-sv": ("heat_distribution_description_sv", text_value),
}

REQUIRED_HEADERS = set(CSV_TO_MODEL)


def source_row(row: Dict[str, str | None]) -> Dict[str, str | None]:
    return {key: text_value(value) for key, value in row.items() if key is not None}


def map_csv_row(row: Dict[str, str | None]) -> Optional[Dict[str, Any]]:
    mapped: Dict[str, Any] = {}
    for csv_column, (model_column, parser) in CSV_TO_MODEL.items():
        mapped[model_column] = parser(row.get(csv_column))

    if not mapped["certificate_id"]:
        return None

    mapped["source_row"] = source_row(row)
    return mapped


def download_csv(source_url: str, csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading energy certificates from %s", source_url)
    with requests.get(source_url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with csv_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    logger.info("Downloaded energy certificates to %s", csv_path)


def iter_certificate_rows(
    csv_path: Path, max_records: int | None = None
) -> Iterable[Dict[str, Any]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        headers = set(reader.fieldnames or [])
        missing_headers = sorted(REQUIRED_HEADERS - headers)
        if missing_headers:
            raise ValueError(
                "Energy certificate CSV is missing required columns: "
                + ", ".join(missing_headers)
            )

        for row_number, row in enumerate(reader, start=1):
            if max_records is not None and row_number > max_records:
                break

            mapped = map_csv_row(row)
            if mapped:
                yield mapped


def upsert_batch(rows: list[Dict[str, Any]]) -> int:
    if not rows:
        return 0

    with SessionLocal() as session:
        stmt = insert(EnergyCertificate).values(rows)
        update_fields = {
            col.name: getattr(stmt.excluded, col.name)
            for col in EnergyCertificate.__table__.columns
            if col.name not in ("certificate_id", "created_at", "updated_at")
        }
        update_fields["updated_at"] = func.now()
        stmt = stmt.on_conflict_do_update(
            index_elements=[EnergyCertificate.certificate_id],
            set_=update_fields,
        )
        session.execute(stmt)
        session.commit()
        return len(rows)


def load_csv(csv_path: Path, max_records: int | None = None) -> int:
    total = 0
    batch: list[Dict[str, Any]] = []

    for record in iter_certificate_rows(csv_path, max_records=max_records):
        batch.append(record)
        if len(batch) >= BATCH_SIZE:
            total += upsert_batch(batch)
            logger.info("Upserted %s energy certificates", total)
            batch = []

    total += upsert_batch(batch)
    logger.info("Energy certificate load completed | upserted=%s", total)
    return total


def assert_clean_building_identifier_join() -> None:
    sql = text("""
        SELECT permanent_building_identifier, COUNT(*) AS building_count
        FROM buildings
        WHERE permanent_building_identifier IS NOT NULL
        GROUP BY permanent_building_identifier
        HAVING COUNT(*) > 1
        LIMIT 10;
    """)

    with SessionLocal() as session:
        duplicates = session.execute(sql).mappings().all()

    if duplicates:
        sample = ", ".join(
            f"{row['permanent_building_identifier']} ({row['building_count']})"
            for row in duplicates
        )
        raise RuntimeError(
            "Cannot cleanly join energy certificates: duplicate "
            f"buildings.permanent_building_identifier values found: {sample}"
        )


def print_join_report() -> None:
    sql = text("""
        WITH latest_certificates AS (
            SELECT DISTINCT ON (building_identifier)
                building_identifier
            FROM energy_certificates
            WHERE building_identifier IS NOT NULL
            ORDER BY building_identifier, signed_at DESC NULLS LAST, certificate_id DESC
        )
        SELECT
            (SELECT COUNT(*) FROM energy_certificates) AS certificate_count,
            (SELECT COUNT(*) FROM energy_certificates WHERE building_identifier IS NOT NULL) AS certificates_with_identifier,
            (SELECT COUNT(*) FROM latest_certificates) AS unique_certificate_buildings,
            (
                SELECT COUNT(*)
                FROM latest_certificates ec
                JOIN buildings b
                    ON b.permanent_building_identifier = ec.building_identifier
            ) AS matched_loaded_buildings;
    """)

    with SessionLocal() as session:
        row = session.execute(sql).mappings().one()

    logger.info(
        "Energy certificate join report | certificates=%s | with_identifier=%s | "
        "unique_certificate_buildings=%s | matched_loaded_buildings=%s",
        row["certificate_count"],
        row["certificates_with_identifier"],
        row["unique_certificate_buildings"],
        row["matched_loaded_buildings"],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download and load public energy certificate CSV data"
    )
    parser.add_argument(
        "--csv-path",
        default=DEFAULT_CSV_PATH,
        help="Path to energiatodistukset.csv inside the service workspace.",
    )
    parser.add_argument(
        "--source-url",
        default=SOURCE_URL,
        help="Public energy certificate CSV URL.",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download the CSV before loading it.",
    )
    parser.add_argument(
        "--no-load",
        action="store_true",
        help="Download only; do not load rows into Postgres.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of CSV records to load for testing.",
    )
    parser.add_argument(
        "--skip-join-check",
        action="store_true",
        help="Skip duplicate permanent-building-identifier validation.",
    )

    args = parser.parse_args()
    csv_path = Path(args.csv_path)

    if args.download:
        download_csv(args.source_url, csv_path)

    if args.no_load:
        raise SystemExit(0)

    if not csv_path.exists():
        raise FileNotFoundError(
            f"{csv_path} does not exist. Run with --download first."
        )

    load_csv(csv_path, max_records=args.limit)

    if not args.skip_join_check:
        assert_clean_building_identifier_join()
        print_join_report()
