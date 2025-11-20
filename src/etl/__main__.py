import argparse

from etl.load_buildings import BuildingLoader
from etl.load_addresses import AddressLoader

from utils.regions import build_region_municipality_param


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract data from original dataset API into local Postgres"
    )
    parser.add_argument(
        "--test",
        choices=["region", "municipality"],
        help="Fetch a sample region for testing. Leave empty to fetch all data",
    )
    
    args = parser.parse_args()
    
    test_municipality_codes = None
    test_params = None
    if args.test == "municipality":
        test_municipality_codes = "'616'"  # Pukkila 2k records only for testing
    elif args.test == "region":
        test_municipality_codes = build_region_municipality_param('Uusimaa')  # ~500k records
        # test_municipality_codes = build_region_municipality_param('Ahvenanmaa')  # 30k records
       
    if (test_municipality_codes):
        test_params = {
            "filter": "municipality_number IN (%s)" % test_municipality_codes
        }
    
    building_loader = BuildingLoader()
    building_loader.run(base_params=test_params)
    
    address_loader = AddressLoader()
    address_loader.run(base_params=test_params)
    