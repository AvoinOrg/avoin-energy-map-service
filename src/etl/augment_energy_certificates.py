import argparse
import logging
import os

from sqlalchemy import text

from db.session import SessionLocal


MAIN_TABLE = "main"
BUILDING_IDENTIFIER_COLUMN = "permanent_building_identifier"
BUILDING_KEY_COLUMN = "building_key"
CERTIFICATE_CLASS_COLUMN = "energy_certificate_class"
CERTIFICATE_CLASS_YEAR_COLUMN = "energy_certificate_class_year"
ENERGY_CLASS_COLUMNS = {
    CERTIFICATE_CLASS_COLUMN: "text",
    CERTIFICATE_CLASS_YEAR_COLUMN: "integer",
    "modeled_energy_class": "text",
    "modeled_energy_e_value": "double precision",
    "modeled_energy_class_reason": "text",
    "modeled_energy_class_area_source": "text",
    "energy_class": "text",
    "is_energy_class_modeled": "boolean",
    "energy_class_year": "integer",
}

OLD_MAIN_CERTIFICATE_COLUMNS = [
    "energy_certificate_id",
    "energy_certificate_version",
    "energy_certificate_signed_at",
    "energy_certificate_valid_until",
    "energy_certificate_e_value",
    "energy_certificate_e_class",
    "energy_certificate_e_class_limit_2018",
    "energy_certificate_heated_net_area",
    "energy_certificate_use_purpose",
    "energy_certificate_sub_use_purpose_fi",
    "energy_certificate_ventilation_type_id",
    "energy_certificate_ventilation_description_fi",
    "energy_certificate_heating_form_1_id",
    "energy_certificate_heating_form_2_id",
    "energy_certificate_heating_form_1_description_fi",
    "energy_certificate_heating_form_2_description_fi",
    "energy_certificate_heat_distribution_id",
    "energy_certificate_heat_distribution_description_fi",
]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
)
logger = logging.getLogger("etl.augment_energy_certificates")


def assert_main_join_is_clean(skip_join_check: bool = False) -> None:
    with SessionLocal() as session:
        main_exists = session.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = :table_name
                );
            """),
            {"table_name": MAIN_TABLE},
        ).scalar_one()
        if not main_exists:
            raise RuntimeError("Cannot augment energy certificates: main table is missing.")

        columns = set(
            session.execute(
                text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = :table_name
                      AND column_name IN (
                          'permanent_building_identifier',
                          'building_key'
                      );
                """),
                {"table_name": MAIN_TABLE},
            ).scalars()
        )

        if BUILDING_IDENTIFIER_COLUMN not in columns:
            raise RuntimeError(
                "Cannot augment energy certificates: main.permanent_building_identifier "
                "is missing."
            )

        if not skip_join_check and BUILDING_KEY_COLUMN in columns:
            ambiguous = session.execute(
                text("""
                    SELECT permanent_building_identifier, COUNT(DISTINCT building_key) AS building_keys
                    FROM main
                    WHERE permanent_building_identifier IS NOT NULL
                    GROUP BY permanent_building_identifier
                    HAVING COUNT(DISTINCT building_key) > 1
                    LIMIT 10;
                """)
            ).mappings().all()

            if ambiguous:
                sample = ", ".join(
                    f"{row['permanent_building_identifier']} ({row['building_keys']})"
                    for row in ambiguous
                )
                raise RuntimeError(
                    "Cannot cleanly join energy certificates: duplicate "
                    f"main.permanent_building_identifier values point to multiple "
                    f"building_key values: {sample}"
                )

        certificate_count = session.execute(
            text("SELECT COUNT(*) FROM energy_certificates;")
        ).scalar_one()
        if certificate_count == 0:
            raise RuntimeError(
                "Cannot augment energy certificates: energy_certificates is empty. "
                "Run src.etl.load_energy_certificates first."
            )


def ensure_main_columns() -> None:
    with SessionLocal() as session:
        for column_name, column_type in ENERGY_CLASS_COLUMNS.items():
            session.execute(
                text(
                    f"ALTER TABLE {MAIN_TABLE} "
                    f"ADD COLUMN IF NOT EXISTS {column_name} {column_type};"
                )
            )
        session.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS idx_main_permanent_building_identifier "
                f"ON {MAIN_TABLE} ({BUILDING_IDENTIFIER_COLUMN});"
            )
        )
        session.commit()


def augment_main(clear_stale: bool = True) -> int:
    update_sql = text(f"""
        WITH latest_energy_certificates AS (
            SELECT DISTINCT ON (building_identifier)
                building_identifier,
                e_class,
                version
            FROM energy_certificates
            WHERE building_identifier IS NOT NULL
            ORDER BY building_identifier, signed_at DESC NULLS LAST, certificate_id DESC
        )
        UPDATE main m
        SET {CERTIFICATE_CLASS_COLUMN} = ec.e_class,
            {CERTIFICATE_CLASS_YEAR_COLUMN} = CASE
                WHEN NULLIF(TRIM(ec.e_class), '') IS NOT NULL THEN ec.version
                ELSE NULL
            END
        FROM latest_energy_certificates ec
        WHERE m.permanent_building_identifier = ec.building_identifier;
    """)

    with SessionLocal() as session:
        result = session.execute(update_sql)
        updated_rows = result.rowcount or 0

        if clear_stale:
            session.execute(
                text(f"""
                    UPDATE main m
                    SET {CERTIFICATE_CLASS_COLUMN} = NULL,
                        {CERTIFICATE_CLASS_YEAR_COLUMN} = NULL
                    WHERE m.{CERTIFICATE_CLASS_COLUMN} IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM energy_certificates ec
                          WHERE ec.building_identifier = m.permanent_building_identifier
                      );
                """)
            )

        session.execute(
            text(f"""
                UPDATE main
                SET modeled_energy_class = NULL,
                    modeled_energy_e_value = NULL,
                    modeled_energy_class_reason = NULL,
                    modeled_energy_class_area_source = NULL
                WHERE NULLIF(TRIM({CERTIFICATE_CLASS_COLUMN}), '') IS NOT NULL;
            """)
        )
        session.execute(
            text(f"""
                UPDATE main
                SET energy_class = COALESCE(
                        NULLIF(UPPER(TRIM({CERTIFICATE_CLASS_COLUMN})), ''),
                        modeled_energy_class
                    ),
                    is_energy_class_modeled = CASE
                        WHEN NULLIF(TRIM({CERTIFICATE_CLASS_COLUMN}), '') IS NOT NULL
                            THEN false
                        WHEN modeled_energy_class IS NOT NULL THEN true
                        ELSE NULL
                    END,
                    energy_class_year = CASE
                        WHEN NULLIF(TRIM({CERTIFICATE_CLASS_COLUMN}), '') IS NOT NULL
                            THEN {CERTIFICATE_CLASS_YEAR_COLUMN}
                        WHEN modeled_energy_class IS NOT NULL THEN 2018
                        ELSE NULL
                    END;
            """)
        )

        session.commit()
        return updated_rows


def print_report(updated_rows: int) -> None:
    logger.info(
        "Energy certificate main augmentation completed | updated_main_rows=%s | "
        "columns=%s,%s",
        updated_rows,
        CERTIFICATE_CLASS_COLUMN,
        CERTIFICATE_CLASS_YEAR_COLUMN,
    )


def drop_old_main_certificate_columns() -> None:
    with SessionLocal() as session:
        for column_name in OLD_MAIN_CERTIFICATE_COLUMNS:
            session.execute(
                text(f"ALTER TABLE {MAIN_TABLE} DROP COLUMN IF EXISTS {column_name};")
            )
        session.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Add latest official energy class and class-scale year to main"
    )
    parser.add_argument(
        "--skip-join-check",
        action="store_true",
        help="Skip ambiguous permanent-building-identifier validation.",
    )
    parser.add_argument(
        "--keep-stale",
        action="store_true",
        help="Do not clear existing energy certificate fields when no matching certificate remains.",
    )
    parser.add_argument(
        "--keep-old-columns",
        action="store_true",
        help="Do not drop older multi-field energy certificate columns from main.",
    )

    args = parser.parse_args()

    assert_main_join_is_clean(skip_join_check=args.skip_join_check)
    ensure_main_columns()
    updated = augment_main(clear_stale=not args.keep_stale)
    if not args.keep_old_columns:
        drop_old_main_certificate_columns()
    logger.info("Updated %s main rows from latest energy certificates", updated)
    print_report(updated)
