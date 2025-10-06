import pandas as pd

FILE_PATH = "./assets/municipality-by-region.csv"

def get_municipality_codes_by_region( region_name):
    # Read the CSV
    df = pd.read_csv(FILE_PATH, dtype=str)

    # Normalize text (lowercase and strip whitespace)
    region_name = region_name.strip().lower()

    # Filter rows where any of the region name columns match the user input
    mask = (
        df["CODE2_PREFLABEL_FI"].str.lower().eq(region_name)
        | df["CODE2_PREFLABEL_SV"].str.lower().eq(region_name)
        | df["CODE2_PREFLABEL_EN"].str.lower().eq(region_name)
    )

    # Extract the municipality codes (CODE1_CODEVALUE)
    codes = df.loc[mask, "CODE1_CODEVALUE"].dropna().tolist()

    return codes

def build_region_municipality_param(region_name):
    """
    Build the filter parameter for municipality codes based on region name.
    """
    codes = get_municipality_codes_by_region(region_name)
    if not codes:
        return None
    return "'" + "','".join(codes) + "'"


# Example usage:
if __name__ == "__main__":
    user_input = input("Enter region name (e.g., 'Uusimaa'): ")
    result = get_municipality_codes_by_region(user_input)
    print(
        f"Municipality codes in {user_input}: {', '.join(result) if result else 'None found'}"
    )
    print("Filter parameter:", build_region_municipality_param(user_input))
