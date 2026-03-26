import os
import pandas as pd
from datetime import date, time

from db.models import Building

# Get unique calculation column names from csv files
def get_unique_column_names():
    # read assets/energiankulutustaulukot - kerrostalot.csv & assets/energiankulutustaulukot - omakotitalot.csv
    assets_dir = "assets"
    files = [
        "energiankulutustaulukot - kerrostalot.csv",
        "energiankulutustaulukot - omakotitalot.csv"
    ]

    all_col_names = set()

    for file in files:
        file_path = os.path.join(assets_dir, file)
        if not os.path.exists(file_path):
            print(f"File not found: {file_path}")
            continue

        # Files has ; as separator
        # The first column doesn't have a header name in the first row (starts with ;)
        df = pd.read_csv(file_path, sep=';', index_col=0)

        # Calculations are in the index because we used index_col=0
        for val in df.index:
            if isinstance(val, str):
                # remove first 3 characters
                transformed = val[3:]
                all_col_names.add(transformed)

    # print to screen
    unique_col_names = sorted(list(all_col_names))
    for col_name in unique_col_names:
        print(col_name)

    print(f"Total unique column names: {len(unique_col_names)}")


if __name__ == "__main__":
    print("Start adding calculation to main table")
    start = time.time()

    # get the list of unique calculation columnn names to add to the main table
    calculation_cols = get_unique_column_names()
    # @todo: add these columns to main table if not exist

    # @todo: find the correct calculation
        # each building has main_purpose. 05 - use data from assets/energiankulutustaulukot - omakotitalot.csv; 06 = use data from assets/energiankulutustaulukot - kerrostalot.csv. Set all calculation value to null if main_purpose is not 05 or 06
        # each building has completion_date, use the completion_date to find the correct age group column in the csv file.
        # age group logic:
            # agegroup_1: 1975 and older
            # agegroup_2: 1976-1977
            # agegroup_3: 1978-1984
            # agegroup_4: 1985-2002
            # agegroup_5: 2003-2007
            # agegroup_6: 2008-2009
            # agegroup_7: 2010-2011
            # agegroup_8: 2012-2017
            # agegroup_9: 2018 and newer
        # example 1, a building with main_purpose = 05 and completion_date = 1977-01-01 should have:
            # wood_default_heat = 373 (value from assets/energiankulutustaulukot - omakotitalot.csv, agegroup_2, row dh_wood_default_heat)
            # gshp_windows_total = 156,0576923 (value from assets/energiankulutustaulukot - omakotitalot.csv, agegroup_2, row dh_gshp_windows_total)
        # example 2, a building with main_purpose = 06 and completion_date = 2005-01-01 should have:
            # wood_default_heat = null (no row for ab_wood_default_heat in assets/energiankulutustaulukot - kerrostalot.csv)
            # gshp_windows_total = 92,21007002 (value from assets/energiankulutustaulukot - kerrostalot.csv, agegroup_9, row ab_gshp_windows_total)


    print(f"Calculations added in {time.time() - start:.2f}s")
