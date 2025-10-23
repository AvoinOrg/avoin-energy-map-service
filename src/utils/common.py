
def code_from_uri(uri: str | None) -> str | None:
    """
    Extract the code from the URI. Only keep two last characters as the code
    Example: URI "http://uri.suomi.fi/codelist/rytj/lammitystapa/code/04"
    will return "04"
    """
    if not uri:
        return None
    parts = uri.rstrip("/").split("/")
    if len(parts) >= 2:
        return parts[-1]
    return None


if __name__ == "__main__":
    # Example usage
    test_uris = [
        "http://uri.suomi.fi/codelist/rytj/lammitystapa/code/04",
        "https://koodistot.suomi.fi/codelist-api/api/v1/coderegistries/rytj/codeschemes/lammitystapa/codes/07/",
        None,
        "invalid_uri",
    ]
    for uri in test_uris:
        print(f"URI: {uri} -> Code: {code_from_uri(uri)}")