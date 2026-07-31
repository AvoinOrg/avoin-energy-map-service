import argparse
import logging
import os
import time
from dataclasses import dataclass

from sqlalchemy import text

from src.db.session import SessionLocal


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
)
logger = logging.getLogger("etl.density_grids")


@dataclass(frozen=True)
class DensityGridConfig:
    key: str
    table_name: str
    cell_size_m: int
    min_zoom: int
    max_zoom: int


DENSITY_GRIDS = {
    "z5_8": DensityGridConfig(
        key="z5_8",
        table_name="energymap_density_grid_z5_8",
        cell_size_m=25_000,
        min_zoom=5,
        max_zoom=8,
    ),
    "z9_12": DensityGridConfig(
        key="z9_12",
        table_name="energymap_density_grid_z9_12",
        cell_size_m=2_500,
        min_zoom=9,
        max_zoom=12,
    ),
}


def assert_main_table_ready() -> None:
    with SessionLocal() as session:
        main_exists = session.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'main'
                );
            """)
        ).scalar_one()
        if not main_exists:
            raise RuntimeError("Cannot build density grids: main table is missing.")

        missing_columns = session.execute(
            text("""
                SELECT required.column_name
                FROM (
                    VALUES
                        ('polygon'),
                        ('point'),
                        ('energy_certificate_class')
                ) AS required(column_name)
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns c
                    WHERE c.table_schema = 'public'
                      AND c.table_name = 'main'
                      AND c.column_name = required.column_name
                );
            """)
        ).scalars().all()
        if missing_columns:
            raise RuntimeError(
                "Cannot build density grids: main is missing columns "
                f"{', '.join(missing_columns)}"
            )


def grant_readonly_select(table_name: str) -> None:
    with SessionLocal() as session:
        session.execute(
            text(f"""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sandbox_energymap_readonly') THEN
                        EXECUTE 'GRANT SELECT ON TABLE public.{table_name} TO sandbox_energymap_readonly';
                    END IF;
                END
                $$;
            """)
        )
        session.commit()


def build_density_grid(config: DensityGridConfig) -> None:
    logger.info(
        "Building density grid %s | table=%s | cell_size_m=%s | zooms=%s-%s",
        config.key,
        config.table_name,
        config.cell_size_m,
        config.min_zoom,
        config.max_zoom,
    )
    started_at = time.time()

    insert_sql = text(f"""
        WITH source AS (
            SELECT
                FLOOR(
                    (
                        CASE
                            WHEN point IS NOT NULL THEN ST_X(point)
                            ELSE (ST_XMin(polygon) + ST_XMax(polygon)) / 2.0
                        END
                    ) / :cell_size_m
                )::bigint AS cell_x,
                FLOOR(
                    (
                        CASE
                            WHEN point IS NOT NULL THEN ST_Y(point)
                            ELSE (ST_YMin(polygon) + ST_YMax(polygon)) / 2.0
                        END
                    ) / :cell_size_m
                )::bigint AS cell_y,
                CASE
                    WHEN UPPER(NULLIF(TRIM(energy_certificate_class), '')) IN
                        ('A', 'B', 'C', 'D', 'E', 'F', 'G')
                    THEN UPPER(NULLIF(TRIM(energy_certificate_class), ''))
                    ELSE NULL
                END AS certificate_class
            FROM main
            WHERE polygon IS NOT NULL OR point IS NOT NULL
        ),
        aggregated AS (
            SELECT
                cell_x,
                cell_y,
                COUNT(*)::integer AS building_count,
                COUNT(*) FILTER (WHERE certificate_class IS NOT NULL)::integer
                    AS certified_building_count,
                COUNT(*) FILTER (WHERE certificate_class = 'A')::integer AS class_a_count,
                COUNT(*) FILTER (WHERE certificate_class = 'B')::integer AS class_b_count,
                COUNT(*) FILTER (WHERE certificate_class = 'C')::integer AS class_c_count,
                COUNT(*) FILTER (WHERE certificate_class = 'D')::integer AS class_d_count,
                COUNT(*) FILTER (WHERE certificate_class = 'E')::integer AS class_e_count,
                COUNT(*) FILTER (WHERE certificate_class = 'F')::integer AS class_f_count,
                COUNT(*) FILTER (WHERE certificate_class = 'G')::integer AS class_g_count,
                COUNT(*) FILTER (WHERE certificate_class IS NULL)::integer
                    AS class_unknown_count
            FROM source
            GROUP BY cell_x, cell_y
        ),
        dominant_classes AS (
            SELECT
                cell_x,
                cell_y,
                certificate_class,
                class_count
            FROM (
                SELECT
                    cell_x,
                    cell_y,
                    certificate_class,
                    COUNT(*)::integer AS class_count,
                    ROW_NUMBER() OVER (
                        PARTITION BY cell_x, cell_y
                        ORDER BY COUNT(*) DESC, certificate_class
                    ) AS rank
                FROM source
                WHERE certificate_class IS NOT NULL
                GROUP BY cell_x, cell_y, certificate_class
            ) ranked
            WHERE rank = 1
        )
        INSERT INTO {config.table_name} (
            cell_id,
            cell_size_m,
            min_zoom,
            max_zoom,
            cell_x,
            cell_y,
            building_count,
            certified_building_count,
            certificate_coverage,
            class_a_count,
            class_b_count,
            class_c_count,
            class_d_count,
            class_e_count,
            class_f_count,
            class_g_count,
            class_unknown_count,
            dominant_energy_certificate_class,
            dominant_energy_certificate_class_count,
            geom
        )
        SELECT
            :grid_key || ':' || a.cell_x || ':' || a.cell_y AS cell_id,
            :cell_size_m AS cell_size_m,
            :min_zoom AS min_zoom,
            :max_zoom AS max_zoom,
            a.cell_x,
            a.cell_y,
            a.building_count,
            a.certified_building_count,
            CASE
                WHEN a.building_count > 0
                THEN a.certified_building_count::double precision / a.building_count
                ELSE NULL
            END AS certificate_coverage,
            a.class_a_count,
            a.class_b_count,
            a.class_c_count,
            a.class_d_count,
            a.class_e_count,
            a.class_f_count,
            a.class_g_count,
            a.class_unknown_count,
            d.certificate_class AS dominant_energy_certificate_class,
            COALESCE(d.class_count, 0) AS dominant_energy_certificate_class_count,
            ST_MakeEnvelope(
                a.cell_x * :cell_size_m,
                a.cell_y * :cell_size_m,
                (a.cell_x + 1) * :cell_size_m,
                (a.cell_y + 1) * :cell_size_m,
                3857
            )::geometry(Polygon, 3857) AS geom
        FROM aggregated a
        LEFT JOIN dominant_classes d
            ON a.cell_x = d.cell_x
           AND a.cell_y = d.cell_y;
    """)

    params = {
        "grid_key": config.key,
        "cell_size_m": config.cell_size_m,
        "min_zoom": config.min_zoom,
        "max_zoom": config.max_zoom,
    }

    with SessionLocal() as session:
        session.execute(text("SET jit = off;"))
        session.execute(text(f"TRUNCATE TABLE {config.table_name};"))
        session.execute(insert_sql, params)
        session.commit()

        row_count = session.execute(
            text(f"SELECT COUNT(*) FROM {config.table_name};")
        ).scalar_one()

    grant_readonly_select(config.table_name)
    logger.info(
        "Built density grid %s | rows=%s | elapsed=%.2fs",
        config.key,
        row_count,
        time.time() - started_at,
    )


def selected_grids(names: list[str]) -> list[DensityGridConfig]:
    if "all" in names:
        return list(DENSITY_GRIDS.values())

    unknown = [name for name in names if name not in DENSITY_GRIDS]
    if unknown:
        raise RuntimeError(
            f"Unknown density grid(s): {', '.join(unknown)}. "
            f"Expected one of: all, {', '.join(DENSITY_GRIDS)}"
        )
    return [DENSITY_GRIDS[name] for name in names]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build precomputed energymap density grids")
    parser.add_argument(
        "--grid",
        action="append",
        choices=["all", *DENSITY_GRIDS.keys()],
        default=None,
        help="Grid to build. Can be passed more than once.",
    )
    args = parser.parse_args()

    assert_main_table_ready()
    for config in selected_grids(args.grid or ["all"]):
        build_density_grid(config)


if __name__ == "__main__":
    main()
