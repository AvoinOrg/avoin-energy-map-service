"""Certificate ventilation code list and the four-category map projection.

Labels are an upstream snapshot verified on 2026-09-15. Broad categories are
Energy Map policy, not classifications supplied by the certificate register.
No free-text interpretation, building-age inference or runtime API join is used.
"""

import re


CODE_LIST_URL = (
    "https://www.energiatodistusrekisteri.fi/api/public/"
    "energiatodistukset/ilmanvaihtotyyppi"
)
CODE_LIST_VERIFIED = "2026-09-15"
# id: (map category, official Finnish label, official Swedish label)
VENTILATION_TYPES = {
    "0": ("gravity", "Painovoimainen ilmanvaihtojärjestelmä", "Självdragsventilationssystem"),
    "1": ("mechanical", "Koneellinen poistoilmanvaihtojärjestelmä", "Maskinellt frånluftsventilationssystem"),
    "2": ("mechanical", "Koneellinen tulo- ja poistoilmanvaihtojärjestelmä", "Maskinellt till- och frånluftsventilationssystem"),
    "3": ("mechanical", "Poistoilmalämpöpumppu (vain käyttötarkoitusluokka 1)", "Frånluftsvärmepump (endast användningsklass 1)"),
    "4": ("other", "Painovoimainen/koneellinen poisto", "Naturlig/maskinell frånluftsventilation"),
    "5": ("other", "Painovoimainen/koneellinen tulo- ja poisto", "Naturlig/maskinell till- och frånluftsventilation"),
    "6": ("other", "Muu ilmanvaihtojärjestelmä", "Annat ventilationssystem"),
    "7": ("mechanical", "Koneellinen poistoilmanvaihtojärjestelmä lämmöntalteenotolla", "Mekaniskt frånluftsventilationssystem med värmeåtervinning"),
    "8": ("mechanical", "Koneellinen tulo- ja poistoilmanvaihtojärjestelmä lämmöntalteenotolla", "Mekaniskt till- och frånluftsventilationssystem med värmeåtervinning"),
}
TYPE_ID = "energy_certificate_ventilation_type_id"
DESCRIPTION_FI = "energy_certificate_ventilation_description_fi"
DESCRIPTION_SV = "energy_certificate_ventilation_description_sv"
SOURCE_COLUMNS = (TYPE_ID, DESCRIPTION_FI, DESCRIPTION_SV)
VENTILATION_ATTRIBUTES = {
    "ventilation_category": "java.lang.String",
    "energy_certificate_ventilation_type_label_fi": "java.lang.String",
    "energy_certificate_ventilation_type_label_sv": "java.lang.String",
}
START = "/* energy-map-ventilation:start */"
END = "/* energy-map-ventilation:end */"


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ventilation_expressions() -> dict[str, str]:
    """SQL expressions over main's existing certificate fields; no row writes."""
    type_id = f"NULLIF(TRIM({TYPE_ID}), '')"
    category_cases = " ".join(
        f"WHEN {sql_literal(code)} THEN {sql_literal(value[0])}"
        for code, value in VENTILATION_TYPES.items()
    )
    # Description-only / unknown-code records are known-but-unclassified, not
    # absent. Preserve their original fields; never infer a mechanical system.
    no_information = " AND ".join(
        f"NULLIF(TRIM({column}), '') IS NULL" for column in SOURCE_COLUMNS
    )
    category = (
        f"CASE {type_id} {category_cases} ELSE CASE WHEN {no_information} "
        "THEN 'no_info' ELSE 'other' END END"
    )
    result = {"ventilation_category": category}
    for index, language in ((1, "fi"), (2, "sv")):
        cases = " ".join(
            f"WHEN {sql_literal(code)} THEN {sql_literal(value[index])}"
            for code, value in VENTILATION_TYPES.items()
        )
        result[f"energy_certificate_ventilation_type_label_{language}"] = (
            f"CASE {type_id} {cases} ELSE NULL END"
        )
    return result


def ensure_ventilation_sql(sql: str, geometry_column: str) -> str:
    """Own one marked projection block; fail closed on an unfamiliar contract."""
    if sql.count(START) != sql.count(END) or sql.count(START) > 1:
        raise RuntimeError("Malformed ventilation projection markers")
    unmanaged = sql
    if START in sql:
        if sql.index(END) < sql.index(START):
            raise RuntimeError("Malformed ventilation projection marker order")
        unmanaged = sql[:sql.index(START)] + sql[sql.index(END) + len(END):]
    for column in SOURCE_COLUMNS:
        if re.search(rf"\b{column}\b", unmanaged) is None:
            raise RuntimeError(f"Ventilation source column is not published: {column}")
    for column in VENTILATION_ATTRIBUTES:
        if re.search(rf"\b{column}\b", unmanaged):
            raise RuntimeError(f"Unmanaged ventilation projection already exists: {column}")
    block = START + "".join(
        f", {expression} AS {column}"
        for column, expression in ventilation_expressions().items()
    ) + END
    if START in sql:
        return sql[:sql.index(START)] + block + sql[sql.index(END) + len(END):]
    needle = f", {geometry_column} AS geom"
    if sql.count(needle) != 1:
        raise RuntimeError("Cannot safely locate the building-layer SQL projection")
    return sql.replace(needle, block + needle, 1)


def ensure_ventilation_attributes(feature_type: dict) -> None:
    attributes = feature_type.setdefault("attributes", {}).setdefault("attribute", [])
    for name, binding in VENTILATION_ATTRIBUTES.items():
        matching = [attr for attr in attributes if attr.get("name") == name]
        if len(matching) > 1 or (matching and matching[0].get("binding") != binding):
            raise RuntimeError(f"Invalid ventilation attribute contract: {name}")
        if not matching:
            geom_index = next((i for i, attr in enumerate(attributes)
                               if attr.get("name") == "geom"), len(attributes))
            attributes.insert(geom_index, {
                "name": name, "minOccurs": 0, "maxOccurs": 1,
                "nillable": True, "binding": binding,
            })


def verify_code_list(rows: list[dict]) -> None:
    """Stop publication if the authoritative mapping has changed since review."""
    actual = {str(row["id"]): (row["label-fi"], row["label-sv"]) for row in rows}
    expected = {code: values[1:] for code, values in VENTILATION_TYPES.items()}
    if len(actual) != len(rows) or actual != expected:
        raise RuntimeError("Upstream ventilation codes/labels changed; review the mapping before publication")
