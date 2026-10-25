"""Effective class expressions shared by new builds and historical extensions."""


def energy_class_expressions(attributes="attributes"):
    # Older frozen snapshots predate the effective columns. Their raw source
    # fields reproduce the ETL's official-first rule; backfill verifies parity
    # against the actual published main projection before writing anything.
    official = f"nullif(upper(trim({attributes}->>'energy_certificate_class')),'')"
    effective = (f"CASE WHEN {attributes} ? 'energy_class' THEN {attributes}->>'energy_class' "
                 f"ELSE coalesce({official},{attributes}->>'modeled_energy_class') END")
    recognized = f"({effective}) IN ('A','B','C','D','E','F','G')"
    source = (f"CASE WHEN NOT coalesce({recognized},false) THEN 'unknown' "
              f"WHEN {attributes} ? 'energy_class' THEN CASE "
              f"WHEN {attributes}->>'is_energy_class_modeled'='true' THEN 'modeled' "
              f"WHEN {attributes}->>'is_energy_class_modeled'='false' THEN 'official' "
              f"ELSE 'unknown' END "
              f"WHEN {official} IS NOT NULL THEN 'official' ELSE 'modeled' END")
    return {"energy_class": f"CASE WHEN {recognized} THEN {effective} ELSE 'unknown' END",
            "energy_class_source": source}
