import argparse
import logging
import os
from collections import Counter
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text

from db.models import Building
from db.session import SessionLocal
from etl.base_loader import BaseLoader
from utils.common import code_from_uri


APARTMENT_PURPOSE_CODE = "06"
APARTMENT_PURPOSE_URI = (
    "http://uri.suomi.fi/codelist/rytj/"
    "avoin_rakennusluokitus/code/06"
)
APARTMENT_STOREY_FILTER = (
    f"main_purpose = '{APARTMENT_PURPOSE_URI}' "
    "AND number_of_storeys IS NOT NULL"
)
PROJECTED_PROPERTIES = (
    "building_key",
    "number_of_storeys",
    "main_purpose",
    "modified_timestamp_utc",
)

logger = logging.getLogger("etl.load_apartment_storeys")


def normalize_storey_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if normalized <= 0:
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    if isinstance(value, str):
        try:
            if float(value) != normalized:
                return None
        except ValueError:
            return None
    return normalized


class ApartmentStoreyLoader(BaseLoader):
    """Load the narrow Ryhti apartment-storey projection into buildings."""

    API_PAGE_SIZE = 2999

    def __init__(self, base_url: str | None = None, **kwargs: Any):
        api_url = (base_url or os.getenv("BUILDINGS_API_URL", "")).strip()
        if not api_url:
            raise RuntimeError("BUILDINGS_API_URL is required")
        super().__init__(
            api_url,
            model_class=Building,
            index_columns=(Building.building_key,),
            api_page_size=kwargs.pop("api_page_size", self.API_PAGE_SIZE),
            name="etl.apartment_storeys",
            select_properties=list(PROJECTED_PROPERTIES),
            **kwargs,
        )
        self.source_rows = 0
        self.duplicate_rows = 0
        self.invalid_rows: Counter[str] = Counter()
        self._seen_storeys: dict[str, int] = {}

    def initial_params(self) -> dict[str, Any]:
        params = super().initial_params()
        params.update(
            {
                "f": "application/json",
                "filter": APARTMENT_STOREY_FILTER,
            }
        )
        return params

    def _validated_values(
        self, feature: dict[str, Any]
    ) -> tuple[str, int, dict[str, Any]] | None:
        properties = feature.get("properties") or {}
        building_key = properties.get("building_key")
        if not isinstance(building_key, str) or not building_key.strip():
            self.invalid_rows["missing_building_key"] += 1
            return None

        purpose = code_from_uri(properties.get("main_purpose"))
        if purpose != APARTMENT_PURPOSE_CODE:
            self.invalid_rows["unexpected_main_purpose"] += 1
            return None

        storeys = normalize_storey_count(properties.get("number_of_storeys"))
        if storeys is None:
            self.invalid_rows["invalid_number_of_storeys"] += 1
            return None

        return building_key.strip(), storeys, properties

    def iterate_features(
        self, base_params: dict[str, Any] | None = None
    ) -> Iterable[dict[str, Any]]:
        for feature in super().iterate_features(base_params=base_params):
            self.source_rows += 1
            validated = self._validated_values(feature)
            if validated is None:
                continue
            building_key, storeys, _ = validated
            previous = self._seen_storeys.get(building_key)
            if previous is not None:
                self.duplicate_rows += 1
                if previous != storeys:
                    raise RuntimeError(
                        "Conflicting Ryhti storey counts for building_key "
                        f"{building_key}: {previous} and {storeys}"
                    )
                continue
            self._seen_storeys[building_key] = storeys
            yield feature

    def map_feature_to_row(self, feature: dict[str, Any]) -> dict[str, Any] | None:
        validated = self._validated_values(feature)
        if validated is None:
            return None
        building_key, storeys, properties = validated
        return {
            "building_key": building_key,
            "number_of_storeys": storeys,
            "main_purpose": APARTMENT_PURPOSE_CODE,
            "modified_timestamp_utc": properties.get("modified_timestamp_utc"),
        }

    def run(
        self,
        *,
        base_params: dict[str, Any] | None = None,
        max_records: int | None = None,
    ) -> int:
        written = super().run(base_params=base_params, max_records=max_records)
        expected = len(self._seen_storeys)
        if written != expected:
            raise RuntimeError(
                "Apartment storey import did not reconcile: "
                f"expected {expected} writes, completed {written}"
            )
        logger.info(
            "Ryhti apartment storey source validated | source_rows=%s | "
            "distinct_keys=%s | duplicate_rows=%s | invalid_rows=%s",
            self.source_rows,
            expected,
            self.duplicate_rows,
            dict(sorted(self.invalid_rows.items())),
        )
        return written


def report_database_coverage() -> dict[str, int | None]:
    with SessionLocal() as session:
        main_has_storeys = bool(
            session.execute(
                text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'main'
                          AND column_name = 'number_of_storeys'
                    );
                """)
            ).scalar_one()
        )
        result = dict(
            session.execute(
                text("""
                    SELECT
                        (
                            SELECT COUNT(*)
                            FROM buildings b
                            WHERE b.main_purpose = :purpose
                        )::bigint AS staged_rows,
                        (
                            SELECT COUNT(DISTINCT b.building_key)
                            FROM buildings b
                            WHERE b.main_purpose = :purpose
                        )::bigint AS staged_keys,
                        (
                            SELECT COUNT(*)
                            FROM buildings b
                            WHERE b.main_purpose = :purpose
                              AND (
                                  b.number_of_storeys IS NULL
                                  OR b.number_of_storeys <= 0
                              )
                        )::bigint AS invalid_staged_storeys,
                        (
                            SELECT COUNT(*)
                            FROM main m
                            JOIN buildings b ON b.building_key = m.building_key
                            WHERE b.main_purpose = :purpose
                        )::bigint AS matched_main_rows,
                        (
                            SELECT COUNT(DISTINCT b.building_key)
                            FROM main m
                            JOIN buildings b ON b.building_key = m.building_key
                            WHERE b.main_purpose = :purpose
                        )::bigint AS matched_main_keys,
                        (
                            SELECT MIN(b.number_of_storeys)
                            FROM buildings b
                            WHERE b.main_purpose = :purpose
                        )::integer AS min_storeys,
                        (
                            SELECT MAX(b.number_of_storeys)
                            FROM buildings b
                            WHERE b.main_purpose = :purpose
                        )::integer AS max_storeys;
                """),
                {"purpose": APARTMENT_PURPOSE_CODE},
            ).mappings().one()
        )
        if main_has_storeys:
            result["matched_main_with_storeys"] = int(
                session.execute(
                    text("""
                        SELECT COUNT(*)
                        FROM main m
                        JOIN buildings b ON b.building_key = m.building_key
                        WHERE b.main_purpose = :purpose
                          AND m.number_of_storeys = b.number_of_storeys;
                    """),
                    {"purpose": APARTMENT_PURPOSE_CODE},
                ).scalar_one()
            )
        else:
            result["matched_main_with_storeys"] = None
        return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Load the authoritative Ryhti building_key -> number_of_storeys "
            "projection for apartment buildings"
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum valid features to write; intended only for testing.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=ApartmentStoreyLoader.API_PAGE_SIZE,
        help="Ryhti request page size.",
    )
    args = parser.parse_args()

    loader = ApartmentStoreyLoader(api_page_size=args.page_size)
    loader.run(max_records=args.limit)
    logger.info("Apartment storey database coverage | %s", report_database_coverage())


if __name__ == "__main__":
    main()
