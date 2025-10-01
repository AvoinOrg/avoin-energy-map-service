import os, sys, argparse, logging, time
from typing import Any, Dict, Iterable, Optional
from datetime import datetime

from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from db.session import SessionLocal
from db.models import Building

BUILDINGS_API_URL = os.getenv("BUILDINGS_API_URL", "").strip()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("etl.buildings")

def _parse_date(date_str: str | None) -> Any:
    """
    Date-only fields in the data set end with 'Z'
    Replace 'Z' with '+00:00' for UTC
    """
    if not date_str:
        return None
    try:
        if not date_str.endswith("Z"):
            return None
        
        date_str = date_str[:-1] + "+00:00"  
        dt = datetime.fromisoformat(s)
        return dt.date()
    except Exception:
        return None

def _code_from_uri(uri: str | None) -> str | None:
    """
    Extract the code from the URI. Only keep two last characters as the code
    """
    if not uri:
        return None
    parts = uri.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] == "codes":
        return parts[-1]
    return None

# Retry connections after 4 failed attempts, the waiting time for each retry is 1,2,4,8 seconds respectively
@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=1, max=8))
def http_get_json(url: str, query_parameters: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.get(url, params=query_parameters, timeout=30)
    response.raise_for_status()  # raise if status >= 400
    return response.json()

def iterate_buildings(base_url: str, base_params: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """
    Generator that yields building features from the paginated API.
    Args:
        base_url: The base URL of the buildings API endpoint.
        base_params: Query parameters to include in the initial request.
    Yields:
        Each building feature as a dict.
    """
    params = dict(base_params)
    url = base_url
    
    if "limit" not in params:
        params["limit"] = 10000  # maximum page size supported by the API
        
    while True:
        data = http_get_json(url, params)
        features = data.get("features", [])
        for feature in features:
            yield feature

        next_href = None
        for link in data.get("links", []):
            if link.get("rel") == "next":
                next_href = link.get("href")
                break

        if not next_href:
            break
        
        # politeness delay every 0.1s to avoid hammering the API
        time.sleep(0.1)
        
        # move to next page
        url = next_href
        params = {}  # 'next' already contains query params

def upsert_building(database_session: Session, feature: Dict[str, Any]) -> bool:
    """
    Insert or update a single building feature into the database.

    Args:
        database_session: Active SQLAlchemy session.
        feature: A GeoJSON feature dict representing a building.

    Returns:
        True if the feature was processed and written, False if skipped (e.g., missing ID).
    """
    properties = feature.get("properties") or {}
    
    # Extract coordinates
    coordinates = feature.get("geometry").get("coordinates")
    coordinates_str = None
    if len(coordinates) == 2:
        coordinates_str = f"POINT({coordinates[0]} {coordinates[1]})"

    # Prepare row fields
    building_record = {
        "id": feature.get("id"),
        "building_key": properties.get("building_key"),
        "permanent_building_identifier": properties.get("permanent_building_identifier"),
        "property_identifier": properties.get("property_identifier"),

        "completion_date": _parse_date(properties.get("completion_date")),
        "demolition_date": _parse_date(properties.get("demolition_date")),

        "main_purpose": _code_from_uri(properties.get("main_purpose")),
        "municipality_number": properties.get("municipality_number"),
        "usage_status": _code_from_uri(properties.get("usage_status")),
        "facade_material": _code_from_uri(properties.get("facade_material")),
        "heating_method": _code_from_uri(properties.get("heating_method")),
        "heating_energy_source": _code_from_uri(properties.get("heating_energy_source")),
        "material_of_load_bearing_structures": _code_from_uri(properties.get("material_of_load_bearing_structures")),
        "construction_method": _code_from_uri(properties.get("construction_method")),
        "voting_district_number": properties.get("voting_district_number"),

        "volume": properties.get("volume"),
        "number_of_storeys": properties.get("number_of_storeys"),
        "gross_floor_area": properties.get("gross_floor_area"),
        "total_area": properties.get("total_area"),
        "floor_area": properties.get("floor_area"),
        "apartment_count": properties.get("apartment_count"),

        "point_location_srid": properties.get("point_location_srid"),
        "point": coordinates_str,

        "modified_timestamp_utc": properties.get("modified_timestamp_utc"),
    }

    if not building_record["id"]:
        # skip features with no usable identifier
        return False

    # Postgres UPSERT (insert or update on conflict)
    statement = (
        insert(Building)
        .values(**building_record)
        .on_conflict_do_update(
            index_elements=[Building.id],
            set_={column: building_record[column] for column in building_record if column != "id"}
        )
    )
    database_session.execute(statement)
    return True

def load_buildings(limit: Optional[int] = None) -> int:
    """
    Extract building data from the upstream API and load into local Postgres database.
    Args:
        limit: Optional maximum number of features to process (for testing). Default is None (process all).
    Returns:
        Total number of features written to the database.
    """
    if not BUILDINGS_API_URL:
        raise RuntimeError("BUILDINGS_API_URL is not set")

    query_parameters: Dict[str, Any] = {"limit": 100}

    total_seen, total_written = 0, 0
    # write to db in batches of 200
    BATCH_SIZE = 200
    
    logger.info(
        "Building extract started limit=%s | batch_size=%s",
        limit, BATCH_SIZE
    )
    start_time = time.perf_counter()

    try:
        with SessionLocal() as session:
            for feature in iterate_buildings(BUILDINGS_API_URL, query_parameters):
                was_written = upsert_building(session, feature)
                total_seen += 1
                if was_written:
                    total_written += 1
                
                # live progress logging
                if total_seen % BATCH_SIZE == 0:
                    elapsed = time.perf_counter() - start_time
                    logger.info(
                        "Progress: seen=%d written=%d | elapsed=%.2fs",
                        total_seen, total_written, elapsed
                    )

                if total_seen % BATCH_SIZE == 0:
                    session.commit()

                if limit and total_seen >= limit:
                    break

            session.commit()
    except Exception as exc:
        elapsed = time.perf_counter() - start_time
        logger.exception(
            "Building extract failed after %.2fs | seen=%d written=%d | error=%s",
            elapsed, total_seen, total_written, exc
        )
        raise

    elapsed = time.perf_counter() - start_time
    logger.info(
        "Building extract completed | seen=%d written=%d | duration=%.2fs",
        total_seen, total_written, elapsed
    )
    return total_written

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract building data from original dataset API into local Postgres"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of features to process (for testing). Default: fetch all.",
    )

    args = parser.parse_args()

    try:
        load_buildings(limit=args.limit)
    except Exception as exc:
        print(f"ETL failed: {exc}", file=sys.stderr)
        sys.exit(1)