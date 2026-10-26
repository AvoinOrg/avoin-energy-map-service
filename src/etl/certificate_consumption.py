"""Normalize certificate-calculated purchased energy, never meter readings."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import math
import re
from typing import Any, Mapping


CONSUMPTION_COLUMN = "energy_certificate_consumption"
SOURCE_PREFIX = "Tulokset / Kaytettavat-energiamuodot / "
CARRIERS = {
    "electricity": "Sahko",
    "district_heat": "Kaukolampo",
    "district_cooling": "Kaukojaahdytys",
    "fossil_fuel": "Fossiilinen-polttoaine",
    "renewable_fuel": "Uusiutuva-polttoaine",
}


def nonnegative_number(value: Any) -> float | None:
    """Accept source decimal commas; never turn blanks or booleans into zero."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"\+?\d+(?:[.,]\d+)?", value):
            return None
        value = value.replace(",", ".")
    elif not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def source_text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def iso_date(value: Any) -> str | None:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    value = source_text(value)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return None


def consumption_payload(certificate: Mapping[str, Any]) -> str | None:
    """One versioned MVT String with quantities and provenance from one row.

    Missing standard carriers are unknown, not zero. Empty optional 'Muu'
    slots are unused; a named or populated invalid slot makes totals partial.
    Unknown certificate versions are not assumed to share these semantics.
    """
    if certificate.get("version") not in (2013, 2018):
        return None
    source = certificate.get("source_row")
    if not isinstance(source, dict):
        return None
    certificate_id = source_text(certificate.get("certificate_id"))
    building_identifier = source_text(certificate.get("building_identifier"))
    if certificate_id is None or building_identifier is None:
        return None

    components = []
    complete = True
    for carrier, suffix in CARRIERS.items():
        value = nonnegative_number(source.get(SOURCE_PREFIX + suffix))
        if value is None:
            complete = False
        else:
            components.append({"carrier": carrier, "annual_kwh": value})
    for index in range(3):
        prefix = f"{SOURCE_PREFIX}Muu / {index} / "
        name = source_text(source.get(prefix + "Nimi"))
        raw = source.get(prefix + "Ostoenergia")
        value = nonnegative_number(raw)
        unused = name is None and (raw is None or isinstance(raw, str) and not raw.strip())
        if unused:
            continue
        if value is None:
            complete = False
            continue
        components.append({"carrier": "other", "source_slot": index,
                           "name": name, "annual_kwh": value})
    if not components:
        return None

    # Decimal summation avoids presenting 0.1 + 0.2 as 0.30000000000000004.
    try:
        total = float(sum(Decimal(str(item["annual_kwh"])) for item in components))
    except (InvalidOperation, OverflowError):
        total = math.inf
    complete = complete and math.isfinite(total)
    area = nonnegative_number(certificate.get("heated_net_area"))
    payload = {
        "schema_version": 1,
        "basis": "standardized_calculation",
        "unit": "kWh/year",
        "building_identifier": building_identifier,
        "certificate_id": certificate_id,
        "certificate_version": certificate["version"],
        "signed_at": iso_date(certificate.get("signed_at")),
        "valid_until": iso_date(certificate.get("valid_until")),
        "heated_net_area_m2": area if area is not None and area > 0 else None,
        "components": components,
        "completeness": "complete" if complete else "partial",
        "total_kwh_year": total if complete else None,
    }
    return json.dumps(payload, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))
