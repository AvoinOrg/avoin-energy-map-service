import logging
import os
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text

from src.db.session import SessionLocal
from src.etl.energy_class_model import ReferenceValue, load_reference_values


MAIN_TABLE = "main"
MODEL_COLUMNS = {
    "number_of_storeys": "integer",
    "energy_certificate_class_year": "integer",
    "modeled_energy_class": "text",
    "modeled_energy_e_value": "double precision",
    "modeled_energy_class_reason": "text",
    "modeled_energy_class_area_source": "text",
    "energy_class": "text",
    "is_energy_class_modeled": "boolean",
    "energy_class_year": "integer",
}
REQUIRED_INPUT_COLUMNS = {
    "building_key",
    "completion_date",
    "main_purpose",
    "heating_method",
    "heating_energy_source",
    "floor_area",
    "total_area",
    "gross_floor_area",
    "energy_certificate_class",
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
)
logger = logging.getLogger("etl.augment_modeled_energy_classes")


def assert_main_table_ready(session: Any) -> None:
    columns = set(
        session.execute(
            text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = :table_name;
            """),
            {"table_name": MAIN_TABLE},
        ).scalars()
    )
    if not columns:
        raise RuntimeError("Cannot model energy classes: main table is missing.")

    missing = sorted(REQUIRED_INPUT_COLUMNS - columns)
    if missing:
        raise RuntimeError(
            "Cannot model energy classes: main is missing columns " + ", ".join(missing)
        )


def ensure_main_columns(session: Any) -> None:
    for column_name, column_type in MODEL_COLUMNS.items():
        session.execute(
            text(
                f"ALTER TABLE {MAIN_TABLE} ADD COLUMN IF NOT EXISTS "
                f"{column_name} {column_type};"
            )
        )


def sync_number_of_storeys(session: Any) -> int:
    result = session.execute(
        text("""
            UPDATE main m
            SET number_of_storeys = b.number_of_storeys
            FROM buildings b
            WHERE m.building_key = b.building_key
              AND m.number_of_storeys IS DISTINCT FROM b.number_of_storeys;
        """)
    )
    return result.rowcount or 0


def _reference_values_cte(
    references: Mapping[tuple[str, str, str], ReferenceValue],
) -> tuple[str, dict[str, Any]]:
    rows = []
    params: dict[str, Any] = {}
    for index, reference in enumerate(
        sorted(
            references.values(),
            key=lambda value: (
                value.building_type,
                value.heating_key,
                value.age_group,
            ),
        )
    ):
        rows.append(
            f"(:building_type_{index}, :heating_key_{index}, :age_group_{index}, "
            f":e_value_{index}, :class_scale_year_{index})"
        )
        params.update(
            {
                f"building_type_{index}": reference.building_type,
                f"heating_key_{index}": reference.heating_key,
                f"age_group_{index}": reference.age_group,
                f"e_value_{index}": reference.e_value_kwh_m2_year,
                f"class_scale_year_{index}": reference.class_scale_year,
            }
        )
    return ",\n                ".join(rows), params


def _detached_limit_sql(energy_class: str) -> str:
    formulas = {
        "A": ("110 - 0.2 * area", "83 - 0.02 * area", "70"),
        "B": ("215 - 0.6 * area", "131 - 0.04 * area", "106"),
        "C": ("252 - 0.6 * area", "173 - 0.07 * area", "130"),
        "D": ("332 - 0.6 * area", "253 - 0.07 * area", "210"),
        "E": ("462 - 0.6 * area", "383 - 0.07 * area", "340"),
        "F": ("532 - 0.6 * area", "453 - 0.07 * area", "410"),
    }
    small, medium, large = formulas[energy_class]
    return (
        "CASE WHEN area <= 150 THEN "
        f"{small} WHEN area <= 600 THEN {medium} ELSE {large} END"
    )


def _classification_sql() -> str:
    detached = "\n".join(
        f"WHEN rounded_e_value <= {_detached_limit_sql(energy_class)} "
        f"THEN '{energy_class}'"
        for energy_class in "ABCDEF"
    )
    apartment_limits = {
        "A": (80, 75),
        "B": (110, 100),
        "C": (150, 130),
        "D": (210, 160),
        "E": (340, 190),
        "F": (410, 240),
    }
    apartment = "\n".join(
        f"WHEN rounded_e_value <= CASE WHEN number_of_storeys <= 2 "
        f"THEN {low} ELSE {high} END THEN '{energy_class}'"
        for energy_class, (low, high) in apartment_limits.items()
    )
    return f"""
        CASE
            WHEN model_reason IS NOT NULL THEN NULL
            WHEN building_type = 'detached' THEN
                CASE
                    {detached}
                    ELSE 'G'
                END
            WHEN building_type = 'apartment' THEN
                CASE
                    {apartment}
                    ELSE 'G'
                END
            ELSE NULL
        END
    """


def augment_main(
    session: Any,
    references: Mapping[tuple[str, str, str], ReferenceValue] | None = None,
) -> int:
    references = references or load_reference_values()
    reference_values, params = _reference_values_cte(references)
    classification = _classification_sql()

    session.execute(
        text("""
            UPDATE main
            SET modeled_energy_class = NULL,
                modeled_energy_e_value = NULL,
                modeled_energy_class_reason = NULL,
                modeled_energy_class_area_source = NULL;
        """)
    )

    result = session.execute(
        text(f"""
            WITH reference (
                building_type,
                heating_key,
                age_group,
                e_value,
                class_scale_year
            ) AS (
                VALUES
                {reference_values}
            ),
            base AS (
                SELECT
                    m.ctid AS row_id,
                    CASE m.main_purpose
                        WHEN '05' THEN 'detached'
                        WHEN '06' THEN 'apartment'
                        ELSE NULL
                    END AS building_type,
                    EXTRACT(YEAR FROM m.completion_date)::integer AS completion_year,
                    CASE
                        WHEN m.heating_energy_source = '07' THEN 'wood'
                        WHEN m.heating_energy_source = '02' THEN 'oil'
                        WHEN m.heating_energy_source = '04' AND m.heating_method = '01'
                            THEN 'elecb'
                        WHEN m.heating_energy_source = '04' AND m.heating_method = '03'
                            THEN 'delec'
                        WHEN m.heating_energy_source = '01' THEN 'distr'
                        WHEN m.heating_energy_source = '1104' THEN 'awhp'
                        WHEN m.heating_energy_source IN ('09', '1101') THEN 'gshp'
                        ELSE NULL
                    END AS heating_key,
                    CASE
                        WHEN m.floor_area > 0 THEN m.floor_area
                        WHEN m.total_area > 0 THEN m.total_area
                        WHEN m.gross_floor_area > 0 THEN m.gross_floor_area
                        ELSE NULL
                    END::double precision AS area,
                    CASE
                        WHEN m.floor_area > 0 THEN 'floor_area'
                        WHEN m.total_area > 0 THEN 'total_area'
                        WHEN m.gross_floor_area > 0 THEN 'gross_floor_area'
                        ELSE NULL
                    END AS area_source,
                    m.number_of_storeys
                FROM main m
                WHERE NULLIF(TRIM(m.energy_certificate_class), '') IS NULL
            ),
            inputs AS (
                SELECT
                    base.*,
                    CASE
                        WHEN completion_year <= 1975 THEN 'agegroup_1'
                        WHEN completion_year <= 1977 THEN 'agegroup_2'
                        WHEN completion_year <= 1984 THEN 'agegroup_3'
                        WHEN completion_year <= 2002 THEN 'agegroup_4'
                        WHEN completion_year <= 2007 THEN 'agegroup_5'
                        WHEN completion_year <= 2009 THEN 'agegroup_6'
                        WHEN completion_year <= 2011 THEN 'agegroup_7'
                        WHEN completion_year <= 2017 THEN 'agegroup_8'
                        WHEN completion_year IS NOT NULL THEN 'agegroup_9'
                        ELSE NULL
                    END AS age_group,
                    CASE
                        WHEN building_type IS NULL THEN 'unsupported_building_purpose'
                        WHEN completion_year IS NULL THEN 'missing_completion_date'
                        WHEN heating_key IS NULL THEN 'unsupported_heating_source'
                        WHEN building_type = 'apartment'
                             AND heating_key IN ('wood', 'elecb')
                            THEN 'unsupported_heating_source'
                        WHEN building_type = 'detached' AND area IS NULL
                            THEN 'missing_heated_area'
                        WHEN building_type = 'detached' AND area < 50
                            THEN 'heated_area_below_50_m2'
                        WHEN building_type = 'apartment'
                             AND (number_of_storeys IS NULL OR number_of_storeys <= 0)
                            THEN 'missing_number_of_storeys'
                        ELSE NULL
                    END AS input_reason
                FROM base
            ),
            evaluated AS (
                SELECT
                    inputs.*,
                    reference.e_value,
                    CEIL(reference.e_value)::integer AS rounded_e_value,
                    reference.class_scale_year,
                    CASE
                        WHEN inputs.input_reason IS NOT NULL THEN inputs.input_reason
                        WHEN reference.e_value IS NULL THEN 'missing_reference_value'
                        ELSE NULL
                    END AS model_reason
                FROM inputs
                LEFT JOIN reference
                  ON reference.building_type = inputs.building_type
                 AND reference.heating_key = inputs.heating_key
                 AND reference.age_group = inputs.age_group
            ),
            classified AS (
                SELECT
                    evaluated.*,
                    {classification} AS modeled_class
                FROM evaluated
            )
            UPDATE main m
            SET modeled_energy_class = classified.modeled_class,
                modeled_energy_e_value = CASE
                    WHEN classified.model_reason IS NULL THEN classified.e_value
                    ELSE NULL
                END,
                modeled_energy_class_reason = classified.model_reason,
                modeled_energy_class_area_source = CASE
                    WHEN classified.building_type = 'detached'
                    THEN classified.area_source
                    ELSE NULL
                END
            FROM classified
            WHERE m.ctid = classified.row_id;
        """),
        params,
    )

    session.execute(
        text("""
            UPDATE main
            SET energy_class = COALESCE(
                    NULLIF(UPPER(TRIM(energy_certificate_class)), ''),
                    modeled_energy_class
                ),
                is_energy_class_modeled = CASE
                    WHEN NULLIF(TRIM(energy_certificate_class), '') IS NOT NULL THEN false
                    WHEN modeled_energy_class IS NOT NULL THEN true
                    ELSE NULL
                END,
                energy_class_year = CASE
                    WHEN NULLIF(TRIM(energy_certificate_class), '') IS NOT NULL
                        THEN energy_certificate_class_year
                    WHEN modeled_energy_class IS NOT NULL THEN 2018
                    ELSE NULL
                END;
        """)
    )
    return result.rowcount or 0


def report(session: Any) -> list[dict[str, Any]]:
    return list(
        session.execute(
            text("""
                SELECT
                    CASE
                        WHEN is_energy_class_modeled = false THEN 'official'
                        WHEN is_energy_class_modeled = true THEN 'modeled'
                        ELSE COALESCE(modeled_energy_class_reason, 'unavailable')
                    END AS outcome,
                    COUNT(*) AS row_count,
                    COUNT(DISTINCT building_key) AS building_count
                FROM main
                GROUP BY outcome
                ORDER BY outcome;
            """)
        ).mappings()
    )


def main() -> None:
    with SessionLocal() as session:
        assert_main_table_ready(session)
        ensure_main_columns(session)
        synced_storeys = sync_number_of_storeys(session)
        updated_rows = augment_main(session)
        outcomes = report(session)
        session.commit()

    logger.info(
        "Modeled energy class augmentation completed | updated_rows=%s | "
        "synced_storeys=%s",
        updated_rows,
        synced_storeys,
    )
    for outcome in outcomes:
        logger.info(
            "Energy class outcome | outcome=%s | rows=%s | buildings=%s",
            outcome["outcome"],
            outcome["row_count"],
            outcome["building_count"],
        )


if __name__ == "__main__":
    main()
