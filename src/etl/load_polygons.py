import logging, os, requests, time
from io import BytesIO
from typing import Any, Dict, Optional

import geopandas as gpd
import pandas as pd

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
    "bbox": "22.659,59.611,26.656,60.840,EPSG:4326" # Uusimaa bounding box for MVP
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

if __name__ == "__main__":
    load_polygons()