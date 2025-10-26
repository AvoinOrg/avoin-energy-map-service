import os, argparse
from typing import Any, Dict, Optional

from base_loader import BaseLoader
from db.models import Address

class AddressLoader(BaseLoader):
    """
    Load address to database
    """
    API_URL = os.getenv("ADDRESSES_API_URL", "").strip()
    API_PAGE_SIZE = 2999 # for some reason address API failed for page size > 3000
    
    def __init__(self, base_url: str = API_URL, **kwargs):
        super().__init__(
            base_url or "",
            model_class=Address,
            index_columns=(Address.address_key,),
            api_page_size=kwargs.pop("api_page_size", self.API_PAGE_SIZE),
            name="etl.addresses",
        )
    
    def initial_params(self) -> Dict[str, Any]:
        params = super().initial_params()
        return params
    
    def map_feature_to_row(self, feature: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        props = feature.get("properties") or {}
        geom = feature.get("geometry") or {}
        coords = geom.get("coordinates") or None

        coordinates_str = None
        # Expect a point geometry [x, y]
        if isinstance(coords, (list, tuple)) and len(coords) == 2:
            coordinates_str = f"POINT({coords[0]} {coords[1]})"

        row = {
            "address_key": props.get("address_key"),
            "building_key": props.get("building_key"),
            "address_fin": props.get("address_fin"),
            "property_identifier": props.get("property_identifier"),
            "postal_office_fin": props.get("postal_office_fin"),
            "municipality_number": props.get("municipality_number"),
            "postal_code": props.get("postal_code"),
            "modified_timestamp_utc": props.get("modified_timestamp_utc"),
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