"""Flat numeric thematic attributes shared by native and GeoServer MVTs."""

CLASS_FIELDS = {f"energy_class_{c.lower()}_count": c for c in "ABCDEFG"}
HEATING_FIELDS = {"heating_geothermal_count": "09", "heating_district_count": "01",
                  "heating_electricity_count": "04", "heating_solar_count": "10"}
VENTILATION_FIELDS = {f"ventilation_{c}_count": c for c in ("mechanical", "gravity", "other", "no_info")}
TILE_FIELDS = (*CLASS_FIELDS, "energy_class_unknown_count", "energy_class_official_count",
               "energy_class_modeled_count", *HEATING_FIELDS, "heating_other_count",
               *VENTILATION_FIELDS, "thematic_contract_version")


def tile_projection(alias="s"):
    metrics = f"{alias}.metrics"
    expressions = {}
    for field, category in CLASS_FIELDS.items():
        expressions[field] = f"coalesce(({metrics}->'energy_class'->'counts'->>'{category}')::bigint,0)"
    expressions["energy_class_unknown_count"] = f"({metrics}->'energy_class'->>'unknown_building_count')::bigint"
    for source in ("official", "modeled"):
        expressions[f"energy_class_{source}_count"] = f"coalesce(({metrics}->'energy_class_source'->'counts'->>'{source}')::bigint,0)"
    for field, category in HEATING_FIELDS.items():
        expressions[field] = f"coalesce(({metrics}->'heating_energy_source'->'counts'->>'{category}')::bigint,0)"
    # The building layer's 'other' bucket includes missing and unrecognized codes.
    expressions["heating_other_count"] = f"{alias}.building_count-" + "-".join(expressions[f] for f in HEATING_FIELDS)
    for field, category in VENTILATION_FIELDS.items():
        expressions[field] = f"coalesce(({metrics}->'ventilation'->'counts'->>'{category}')::bigint,0)"
    expressions["thematic_contract_version"] = "1"
    # Old candidates must not appear to contain verified zero distributions.
    return ",".join(f"CASE WHEN {metrics} ? 'energy_class' THEN {expressions[field]} END AS {field}"
                    for field in TILE_FIELDS)


def tile_values(metrics, building_count):
    if "energy_class" not in metrics:
        return {}
    classes = metrics["energy_class"]["counts"]
    sources = metrics["energy_class_source"]["counts"]
    heating = metrics["heating_energy_source"]["counts"]
    ventilation = metrics["ventilation"]["counts"]
    values = {field: classes.get(category, 0) for field, category in CLASS_FIELDS.items()}
    values.update(energy_class_unknown_count=metrics["energy_class"]["unknown_building_count"],
                  energy_class_official_count=sources.get("official", 0),
                  energy_class_modeled_count=sources.get("modeled", 0))
    values.update({field: heating.get(category, 0) for field, category in HEATING_FIELDS.items()})
    values["heating_other_count"] = building_count - sum(values[field] for field in HEATING_FIELDS)
    values.update({field: ventilation.get(category, 0) for field, category in VENTILATION_FIELDS.items()})
    values["thematic_contract_version"] = 1
    return values
