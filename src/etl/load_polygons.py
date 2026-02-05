import logging, os, requests, time
from io import BytesIO
from typing import Any, Dict, Optional

import geopandas as gpd
import pandas as pd

from sqlalchemy import text, func
from sqlalchemy.dialects.postgresql import insert
from db.session import SessionLocal
from db.models import Polygon

"""
Load polygons to database
Since the polygon endpoint returned XML instead of GeoJSON, this loader is not using the same BaseLoader as others.
"""
BASE_URL = "https://inspire-wfs.maanmittauslaitos.fi/inspire-wfs/bu_mtk_polygon"
DEFAULT_PARAMS = {
    "service": "wfs",
    "version": "2.0.0",
    "request": "GetFeature",
    "typenames": "bu-core2d:Building",
    "srsname": "EPSG:4326",
    # "bbox": "24.9454703,60.1697815,24.9511014,60.1678721,EPSG:4326"
    # "bbox": "22.659,59.611,26.656,60.840,EPSG:4326" # Uusimaa bounding box for MVP
}
BATCH_SIZE = 1000
LIMIT = None # limit record to fetch. for testing purposes

pd.set_option('display.max_columns', None)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
)
logger = logging.getLogger("etl.load_polygons")

def map_gdf_to_db_row(row: Any) -> Optional[Dict[str, Any]]:
    return {
        "id": row.get("gml_id"),
        "building_key": None, # this endpoint does not provide building_key, set it to None and match it with building later
        "modified_timestamp_utc": row.get("beginLifespanVersion"),
        "geometry": row.get("geometry").wkt if row.get("geometry") else None
    }

def load_polygons():
    start_time = time.perf_counter()

    params = DEFAULT_PARAMS.copy()
    params["count"] = BATCH_SIZE

    start_index = 0
    total_upserted = 0

    while True:
        params["startIndex"] = start_index
        logger.info(f"Fetching batch starting at index {start_index}...")

        try:
            response = requests.get(BASE_URL, params=params, timeout=30)
            response.raise_for_status()

            gdf = gpd.read_file(BytesIO(response.content))
        except Exception:
            logger.info("Could not parse GML")

        if gdf.empty:
            logger.info("No more data found for this batch, exiting.")
            break

        logger.info(f"Fetched {len(gdf)} polygons")

        rows = []
        for _, row in gdf.iterrows():
            mapped_row = map_gdf_to_db_row(row)
            if mapped_row:
                rows.append(mapped_row)

        if rows:
            logger.info(f"Saving {len(rows)} rows to DB...")
            with SessionLocal() as session:
                try:
                    for row in rows:
                        stmt = insert(Polygon).values(
                            polygon_key=row["id"],
                            building_key=row["building_key"],
                            modified_timestamp_utc=row["modified_timestamp_utc"],
                            polygon=row["geometry"],
                        ).on_conflict_do_update(
                            index_elements=[Polygon.polygon_key],
                            set_={
                                "building_key": row["building_key"],
                                "modified_timestamp_utc": row["modified_timestamp_utc"],
                                "polygon": row["geometry"],
                                "updated_at": func.now(),
                            },
                        )
                        session.execute(stmt)
                    session.commit()
                    total_upserted += len(rows)
                    logger.info(f"Saved {len(rows)} rows to DB...")
                except Exception as e:
                    logger.info(f"Error writing batch to DB: {e}")
                    session.rollback()

        # Check for end of stream
        if len(gdf) < BATCH_SIZE:
            logger.info("Last batch received.")
            break

        if LIMIT and total_upserted >= LIMIT:
            logger.info(f"Reached limit of {LIMIT} rows, stopping.")
            break

        # Move to next page
        start_index += len(gdf)

    elapsed = time.perf_counter() - start_time
    logger.info(f"Completed in {elapsed:.2f} seconds")


def link_buildings_to_polygons():
    print("Add building_key to polygons")
    start_time = time.time()

    with SessionLocal() as db:
        print("Run Spatial UPDATE to link polygons to buildings...")

        sql = text("""
            UPDATE polygons p
            SET building_key = b.building_key, updated_at = now()
            FROM buildings b
            WHERE ST_Intersects(p.polygon, b.point)
            AND p.building_key IS NULL
            AND p.updated_at = current_date;
        """)

        result = db.execute(sql)
        db.commit()

    elapsed = time.time() - start_time
    print(f"Linked {result.rowcount} polygons in {elapsed:.2f} seconds.")

def save_unlinked_polygons():
    print("Find bulidings without polygon match and try fuzzy matching based on proximity...")
    start_time = time.time()

    with SessionLocal() as db:
        sql = text("""
            WITH fallback_matches AS (
                SELECT p.polygon_key, b.building_key
                FROM polygons p
                CROSS JOIN LATERAL (
                    SELECT b.building_key
                    FROM buildings b
                    -- search within 25 meters (approx 0.00025 degrees if using 4326,
                    WHERE ST_DWithin(p.polygon, b.point, 25)
                    ORDER BY p.polygon <-> b.point ASC
                    LIMIT 1
                ) b
                WHERE p.building_key IS NULL -- only target unlinked polygons
            )
            UPDATE polygons p
            SET building_key = fm.building_key
            FROM fallback_matches fm
            WHERE p.polygon_key = fm.polygon_key;
        """)
        result = db.execute(sql)
        db.commit()

    elapsed = time.time() - start_time
    print(f"Added {result.rowcount} building keys to unlinked polygons in {elapsed:.2f} seconds.")


if __name__ == "__main__":
    load_polygons()
    link_buildings_to_polygons()
    save_unlinked_polygons()