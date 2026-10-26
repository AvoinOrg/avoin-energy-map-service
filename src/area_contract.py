"""Shared, lightweight contract for building area summaries."""

from src.geoserver.ventilation import SOURCE_COLUMNS
import os
import re
from urllib.parse import quote, urlsplit

METRIC_VERSION = "building-area-summary-v2"
ATTRIBUTES = (
    "permanent_building_identifier", "heating_method", "heating_energy_source",
    "main_purpose", "completion_date", "floor_area", "gross_floor_area", "total_area",
    "energy_certificate_class", "energy_certificate_class_year", "energy_certificate_valid_until",
    "energy_certificate_heated_net_area", *SOURCE_COLUMNS,
    "modeled_energy_class", "modeled_energy_class_reason", "energy_certificate_consumption",
    "energy_class", "is_energy_class_modeled", "energy_class_year",
)
AREA_FIELDS = ("floor_area", "gross_floor_area", "total_area", "energy_certificate_heated_net_area")
CATEGORIES = ("official_class", "modeled_class", "modeled_reason", "energy_class", "energy_class_source", "heating_method",
              "heating_energy_source", "ventilation", "main_purpose", "construction_period")
METRIC_DEFINITIONS = {
    "population": "Distinct nonempty building_key values in the frozen main snapshot",
    "official_class": "counts are scale:validity:class diagnostics; valid_counts_by_scale is the default A-G distribution on each recognized scale; validity is as of release date",
    "certificate_coverage": "Valid A-G official classes on the 2013 or 2018 scale / all buildings",
    "modeled_class": "Diagnostic distribution only; source main does not record model version",
    "energy_class": "Map-effective A-G classes, including existing modeled fallbacks once per unique building; official-first precedence matches main.energy_class, including its existing expiry policy",
    "energy_class_source": "Provenance of the map-effective class: official, modeled, or unknown; modeled values are estimates, not official certificates",
    "floor_area": "Each area definition is separate; nonpositive/nonfinite registry area is unavailable",
    "construction_period": "before_1940,1940_1959,1960_1979,1980_1999,2000_2019,2020_onward,unknown",
    "filters": ["postal_code within municipality"],
}

LEVEL_WIDTH = {"postal_code": 5, "municipality": 3, "region": 2}
STABLE_AREA_VIEWS = {level: f"energymap_{level}_areas" for level in LEVEL_WIDTH}


def latest_layers():
    """Public layer names stay fixed; release selection belongs to the server."""
    base = os.getenv("ENERGY_MAP_GEOSERVER_URL", "").rstrip("/")
    workspace = os.getenv("ENERGY_MAP_AREA_WORKSPACE", "sandbox_energiakartta")
    if base:
        parsed = urlsplit(base)
        if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Expected a public GeoServer URL without credentials")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", workspace):
            raise ValueError("Invalid area workspace")
    return {level: {
        "url": (f"{base}/gwc/service/tms/1.0.0/{quote(workspace + ':' + name, safe='')}@EPSG%3A900913@pbf/{{z}}/{{x}}/{{y}}.pbf"
                if base else f"/energy-data/areas/{level}/tiles/{{z}}/{{x}}/{{y}}.pbf"),
        "source_layer": name if base else "areas", "scheme": "tms" if base else "xyz",
        "minzoom": 0, "maxzoom": 14,
    } for level, name in STABLE_AREA_VIEWS.items()}


def empty_metrics():
    return {
        **{c: {"counts": {}, "known_building_count": 0, "unknown_building_count": 0} for c in CATEGORIES},
        "official_class": {"counts": {}, "known_building_count": 0, "unknown_building_count": 0, "valid_counts_by_scale": {}, "eligible_building_count": 0},
        **{c: {"sum": None, "unit": "m2", "known_building_count": 0, "unknown_building_count": 0} for c in AREA_FIELDS},
    }
