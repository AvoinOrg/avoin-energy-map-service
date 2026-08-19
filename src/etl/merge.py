from sqlalchemy import text
from src.db.session import SessionLocal
from src.etl.augment_modeled_energy_classes import augment_main as augment_modeled_energy_classes
import time

def validate_energy_certificate_join(db):
    duplicate_sql = text("""
        SELECT permanent_building_identifier, COUNT(*) AS building_count
        FROM buildings
        WHERE permanent_building_identifier IS NOT NULL
        GROUP BY permanent_building_identifier
        HAVING COUNT(*) > 1
        LIMIT 10;
    """)
    duplicates = db.execute(duplicate_sql).mappings().all()

    if duplicates:
        sample = ", ".join(
            f"{row['permanent_building_identifier']} ({row['building_count']})"
            for row in duplicates
        )
        raise RuntimeError(
            "Cannot cleanly join energy certificates: duplicate "
            f"buildings.permanent_building_identifier values found: {sample}"
        )

def merge_table():
    print("Start merging table into one")
    start = time.time()

    sql_commands = [
        "DROP TABLE IF EXISTS main;",
        # populate main table with merged data & processed geometries
        """
        CREATE TABLE main AS
        WITH ranked_energy_certificates AS (
            SELECT
                building_identifier,
                e_class,
                version,
                valid_until,
                recommendations_fi,
                recommendations_sv,
                heated_net_area,
                ventilation_type_id,
                ventilation_description_fi,
                ventilation_description_sv,
                LEAD(e_class) OVER (
                    PARTITION BY building_identifier
                    ORDER BY signed_at DESC NULLS LAST, certificate_id DESC
                ) AS previous_class,
                ROW_NUMBER() OVER (
                    PARTITION BY building_identifier
                    ORDER BY signed_at DESC NULLS LAST, certificate_id DESC
                ) AS history_position
            FROM energy_certificates
            WHERE building_identifier IS NOT NULL
        ),
        latest_energy_certificates AS (
            SELECT *
            FROM ranked_energy_certificates
            WHERE history_position = 1
        ),
        processed_buildings AS (
            -- join buildings, addresses & polygons
            -- set final geometry data for building
            SELECT
                b.building_key,
                b.permanent_building_identifier,
                b.property_identifier,
                b.heating_method,
                b.heating_energy_source,
                b.main_purpose,
                b.completion_date,
                b.floor_area,
                b.gross_floor_area,
                b.total_area,
                b.volume,
                b.number_of_storeys,

                -- keep the raw 4326 geometries for polygon overlapping check later
                b.point as raw_point_4326,

                -- process & convert geometry to EPSG:3857
                -- polygon logic: from polygon table if exist -> convex hull of points if multiple points -> NULL
                ST_Transform(
                    COALESCE(
                        p.polygon,
                        CASE WHEN ST_NumGeometries(b.point) > 2 THEN ST_ConvexHull(b.point) ELSE NULL END
                    ),
                    3857
                ) as polygon,

                -- point logic: take only the centroid of points in case of multipoint. Convert to EPSG:3857
                ST_Transform(ST_Centroid(b.point), 3857) as point_3857

            FROM buildings b
            LEFT JOIN polygons p ON b.building_key = p.building_key
            -- ensures prioritizing the row where polygon is NOT NULL
            ORDER BY b.building_key, p.polygon IS NOT NULL DESC
        )
        SELECT
            a.address_key,
            a.address_fin,
            a.postal_code,
            a.postal_office_fin,

            pb.building_key,
            pb.permanent_building_identifier,
            pb.property_identifier,
            pb.heating_method,
            pb.heating_energy_source,
            pb.main_purpose,
            pb.completion_date,
            pb.floor_area,
            pb.gross_floor_area,
            pb.total_area,
            pb.volume,
            pb.number_of_storeys,
            ec.e_class as energy_certificate_class,
            CASE
                WHEN NULLIF(TRIM(ec.e_class), '') IS NOT NULL THEN ec.version
                ELSE NULL
            END as energy_certificate_class_year,
            ec.valid_until as energy_certificate_valid_until,
            ec.previous_class as energy_certificate_previous_class,
            ec.recommendations_fi as energy_certificate_recommendations_fi,
            ec.recommendations_sv as energy_certificate_recommendations_sv,
            ec.heated_net_area as energy_certificate_heated_net_area,
            ec.ventilation_type_id as energy_certificate_ventilation_type_id,
            ec.ventilation_description_fi as energy_certificate_ventilation_description_fi,
            ec.ventilation_description_sv as energy_certificate_ventilation_description_sv,
            NULL::text as modeled_energy_class,
            NULL::double precision as modeled_energy_e_value,
            NULL::text as modeled_energy_class_reason,
            NULL::text as modeled_energy_class_area_source,
            NULLIF(UPPER(TRIM(ec.e_class)), '') as energy_class,
            CASE
                WHEN NULLIF(TRIM(ec.e_class), '') IS NOT NULL THEN false
                ELSE NULL
            END as is_energy_class_modeled,
            CASE
                WHEN NULLIF(TRIM(ec.e_class), '') IS NOT NULL THEN ec.version
                ELSE NULL
            END as energy_class_year,
            pb.polygon,

            -- remove point if polygon exist to avoid duplicates in MVT tiles
            CASE
                WHEN pb.polygon IS NOT NULL THEN NULL

                -- clean point on top of polygon of other buildings
                WHEN EXISTS (
                    SELECT 1
                    FROM polygons p_neighbor
                    WHERE p_neighbor.building_key != pb.building_key
                    AND ST_Intersects(pb.raw_point_4326, p_neighbor.polygon)
                ) THEN NULL

                ELSE pb.point_3857
            END as point

        FROM processed_buildings pb
        LEFT JOIN addresses a ON pb.building_key = a.building_key
        LEFT JOIN latest_energy_certificates ec
            ON pb.permanent_building_identifier = ec.building_identifier;
        """,

        # add spatial index for MVT queries
        "CREATE INDEX idx_polygon_geom ON main USING GIST (polygon);",
        "CREATE INDEX idx_point_geom ON main USING GIST (point);",
    ]

    with SessionLocal() as db:
        validate_energy_certificate_join(db)
        for cmd in sql_commands:
            print(f"  - Executing: {cmd[:50]}...")
            db.execute(text(cmd))
        modeled_rows = augment_modeled_energy_classes(db)
        db.commit()

    print(f"Modeled energy classes evaluated for {modeled_rows} main rows")
    print(f"Table merging completed in {time.time() - start:.2f}s")

if __name__ == "__main__":
    merge_table()
