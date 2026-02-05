import os, argparse
from typing import Any, Dict, Optional

from etl.base_loader import BaseLoader
from db.models import Building
from utils.common import code_from_uri, parse_date

class BuildingLoader(BaseLoader):
    """
    Load building to database
    """
    API_URL = os.getenv("BUILDINGS_API_URL", "").strip()
    API_PAGE_SIZE = 2999 # for some reasons API failed for page size > 3000

    def __init__(self, **kwargs):
        super().__init__(
            self.API_URL,
            model_class=Building,
            index_columns=(Building.building_key,),
            api_page_size=kwargs.pop("api_page_size", self.API_PAGE_SIZE),
            name="etl.bulidings",
        )

    def initial_params(self) -> Dict[str, Any]:
        params = super().initial_params()
        return params

    def map_feature_to_row(self, feature: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or None

        coordinates_str = None
        # Expect a point geometry [x, y]
        if isinstance(coordinates, (list, tuple)) and len(coordinates) == 2:
            coordinates_str = f"POINT({coordinates[0]} {coordinates[1]})"

        row = {
            "building_key": properties.get("building_key"),
            "permanent_building_identifier": properties.get(
                "permanent_building_identifier"
            ),
            "property_identifier": properties.get("property_identifier"),
            "completion_date": parse_date(properties.get("completion_date")),
            "demolition_date": parse_date(properties.get("demolition_date")),
            "main_purpose": code_from_uri(properties.get("main_purpose")),
            "municipality_number": properties.get("municipality_number"),
            "usage_status": code_from_uri(properties.get("usage_status")),
            "facade_material": code_from_uri(properties.get("facade_material")),
            "heating_method": code_from_uri(properties.get("heating_method")),
            "heating_energy_source": code_from_uri(
                properties.get("heating_energy_source")
            ),
            "material_of_load_bearing_structures": code_from_uri(
                properties.get("material_of_load_bearing_structures")
            ),
            "construction_method": code_from_uri(properties.get("construction_method")),
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
        return row

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

    loader = BuildingLoader()
    loader.run(base_params={}, max_records=args.limit)
