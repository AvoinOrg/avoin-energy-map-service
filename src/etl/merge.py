from sqlalchemy import text
from src.db.session import SessionLocal
import time

def merge_table():
    print("Start merging table into one")
    start = time.time()

    sql_commands = [
        "DROP TABLE IF EXISTS main;",
        # populate main table with merged data & processed geometries
        """
        CREATE TABLE main AS
        WITH processed_buildings AS (
            -- join buildings, addresses & polygons
            -- set final geometry data for building
            SELECT
                b.building_key,
                b.permanent_building_identifier,
                b.heating_method,
                b.heating_energy_source,
                b.main_purpose,
                b.completion_date,
                b.floor_area,
                b.gross_floor_area,
                b.total_area,
                b.volume,

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
            pb.heating_method,
            pb.heating_energy_source,
            pb.main_purpose,
            pb.completion_date,
            pb.floor_area,
            pb.gross_floor_area,
            pb.total_area,
            pb.volume,
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
        LEFT JOIN addresses a ON pb.building_key = a.building_key;
        """,

        # add spatial index for MVT queries
        "CREATE INDEX idx_polygon_geom ON main USING GIST (polygon);",
        "CREATE INDEX idx_point_geom ON main USING GIST (point);",
    ]

    with SessionLocal() as db:
        for cmd in sql_commands:
            print(f"  - Executing: {cmd[:50]}...")
            db.execute(text(cmd))
        db.commit()

    print(f"Table merging completed in {time.time() - start:.2f}s")

if __name__ == "__main__":
    merge_table()