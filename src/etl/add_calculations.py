import os
import pandas as pd
import time
from db.session import SessionLocal

def get_unique_column_names():
    """Extracts unique column names from the CSV files."""
    assets_dir = "assets"
    files = [
        "energiankulutustaulukot - kerrostalot.csv",
        "energiankulutustaulukot - omakotitalot.csv"
    ]

    all_col_names = set()

    for file in files:
        file_path = os.path.join(assets_dir, file)
        if not os.path.exists(file_path):
            continue

        # Files has ; as separator
        df = pd.read_csv(file_path, sep=';', index_col=0)

        for val in df.index:
            if isinstance(val, str):
                # remove first 3 characters (ab_ or dh_)
                transformed = val[3:]
                all_col_names.add(transformed)

    return sorted(list(all_col_names))

def ensure_columns_exist(column_names):
    """Adds missing columns to the main table."""
    with SessionLocal() as session:
        # Get existing columns
        result = session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'main'"))
        existing_cols = {row[0] for row in result}

        for col in column_names:
            if col not in existing_cols:
                print(f"Adding column {col} to main table...")
                session.execute(text(f"ALTER TABLE main ADD COLUMN \"{col}\" DOUBLE PRECISION"))

        session.commit()

def get_lookup_data(calculation_cols):
    """
    Parses CSVs and returns a list of dictionaries for the lookup table.
    Format: [{'main_purpose': '05', 'age_group': 'agegroup_1', 'col1': val1, ...}, ...]
    All rows will have all columns from calculation_cols, defaulting to None (NULL).
    """
    lookup_rows = []
    assets_dir = "assets"

    # Mapping purpose to file and prefix
    configs = [
        {'purpose': '06', 'file': 'energiankulutustaulukot - kerrostalot.csv', 'prefix': 'ab_'},
        {'purpose': '05', 'file': 'energiankulutustaulukot - omakotitalot.csv', 'prefix': 'dh_'}
    ]

    for cfg in configs:
        file_path = os.path.join(assets_dir, cfg['file'])
        if not os.path.exists(file_path):
            continue

        df = pd.read_csv(file_path, sep=';', index_col=0)
        # Convert all values to float (they use ',' as decimal separator)
        df = df.replace(',', '.', regex=True).astype(float)

        for age_col in df.columns: # agegroup_1, agegroup_2, ...
            row_data = {
                'main_purpose': cfg['purpose'],
                'age_group': age_col
            }
            # Initialize all calculation columns with None
            for col in calculation_cols:
                row_data[col] = None

            for idx in df.index:
                col_name = idx[3:] # remove ab_ or dh_
                if col_name in row_data:
                    row_data[col_name] = df.loc[idx, age_col]

            lookup_rows.append(row_data)

    return lookup_rows

def run_update(calculation_cols, lookup_data):
    """Performs the high-performance update using a temporary lookup table on the main table."""
    with SessionLocal() as session:
        # 1. Create temporary lookup table
        session.execute(text("DROP TABLE IF EXISTS temp_energy_lookup"))

        col_defs = ", ".join([f"\"{col}\" DOUBLE PRECISION" for col in calculation_cols])
        create_stmt = f"""
            CREATE TEMPORARY TABLE temp_energy_lookup (
                main_purpose VARCHAR,
                age_group VARCHAR,
                {col_defs}
            ) ON COMMIT DROP
        """
        session.execute(text(create_stmt))

        # 2. Insert lookup data, 2 building purposes * 9 age groups = 18 rows
        all_keys = ['main_purpose', 'age_group'] + calculation_cols
        cols_str = ", ".join([f"\"{k}\"" for k in all_keys])
        placeholders_str = ", ".join([f":{k}" for k in all_keys])
        insert_stmt = f"INSERT INTO temp_energy_lookup ({cols_str}) VALUES ({placeholders_str})"

        for row in lookup_data:
            session.execute(text(insert_stmt), row)

        # 3. Perform the mass update
        set_clause = ", ".join([f"\"{col}\" = l.\"{col}\"" for col in calculation_cols])

        update_stmt = f"""
            UPDATE main m
            SET {set_clause}
            FROM temp_energy_lookup l
            WHERE m.main_purpose = l.main_purpose
            AND l.age_group = (
                CASE
                    WHEN m.completion_date <= '1975-12-31' THEN 'agegroup_1'
                    WHEN m.completion_date <= '1977-12-31' THEN 'agegroup_2'
                    WHEN m.completion_date <= '1984-12-31' THEN 'agegroup_3'
                    WHEN m.completion_date <= '2002-12-31' THEN 'agegroup_4'
                    WHEN m.completion_date <= '2007-12-31' THEN 'agegroup_5'
                    WHEN m.completion_date <= '2009-12-31' THEN 'agegroup_6'
                    WHEN m.completion_date <= '2011-12-31' THEN 'agegroup_7'
                    WHEN m.completion_date <= '2017-12-31' THEN 'agegroup_8'
                    ELSE 'agegroup_9'
                END
            )
            AND m.main_purpose IN ('05', '06')
            AND m.completion_date IS NOT NULL
        """

        print("Executing mass update on main table...")
        session.execute(text(update_stmt))

        # 4. Nullify columns for buildings that don't match criteria
        null_clause = ", ".join([f"\"{col}\" = NULL" for col in calculation_cols])
        reset_stmt = f"""
            UPDATE main
            SET {null_clause}
            WHERE main_purpose NOT IN ('05', '06') OR completion_date IS NULL
        """
        print("Resetting non-eligible records to NULL...")
        session.execute(text(reset_stmt))

        session.commit()

if __name__ == "__main__":
    print("Start adding calculation to main table")
    start_time = time.time()

    # 1. Get the list of unique calculation column names
    calculation_cols = get_unique_column_names()
    print(f"Total unique column names: {len(calculation_cols)}")

    # 2. Add columns to DB if they don't exist
    ensure_columns_exist(calculation_cols)

    # 3. Get lookup data from CSVs
    lookup_data = get_lookup_data(calculation_cols)

    # 4. Run the high-performance update
    run_update(calculation_cols, lookup_data)

    print(f"Calculations added in {time.time() - start_time:.2f}s")
