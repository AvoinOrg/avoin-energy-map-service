import os, argparse
from typing import Any, Dict, Optional

from etl.base_loader import BaseLoader
from db.models import Address

class AddressLoader(BaseLoader):
    """
    Load address to database
    """
    API_URL = os.getenv("ADDRESSES_API_URL", "").strip()
    API_PAGE_SIZE = 2999 # for some reasons API failed for page size > 3000
    
    def __init__(self, **kwargs):
        super().__init__(
            self.API_URL,
            model_class=Address,
            index_columns=(Address.address_key,),
            api_page_size=kwargs.pop("api_page_size", self.API_PAGE_SIZE),
            name="etl.addresses",
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
            "address_key": properties.get("address_key"),
            "building_key": properties.get("building_key"),
            "address_fin": properties.get("address_fin"),
            "postal_office_fin": properties.get("postal_office_fin"),
            "municipality_number": properties.get("municipality_number"),
            "postal_code": properties.get("postal_code"),
            "modified_timestamp_utc": properties.get("modified_timestamp_utc"),
            "point": coordinates_str,
        }
        return row
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract address data from original dataset API into local Postgres"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of features to process (for testing). Default: fetch all.",
    )

    args = parser.parse_args()
    
    loader = AddressLoader()
    municipality_codes = '616'  # Pukkila 2k records only for testing        
    base_params = {
        "filter": "municipality_number IN (%s)" % municipality_codes
    }
    loader.run(base_params=base_params, max_records=args.limit)