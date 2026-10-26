"""Build an isolated, validated release from one consistent main snapshot."""

import argparse
from datetime import date
import json
import time
from uuid import UUID, uuid4

from sqlalchemy import text

from src.db.session import SessionLocal
from src.etl.area_inputs import json_text
from src.geoserver.ventilation import SOURCE_COLUMNS, ventilation_expressions
from src.etl.area_energy_classes import energy_class_expressions

from src.area_contract import METRIC_VERSION, ATTRIBUTES, AREA_FIELDS, CATEGORIES, METRIC_DEFINITIONS, empty_metrics

def canonical_population(session, release_id, geography_id):
    columns = set(session.execute(text("SELECT attname FROM pg_attribute WHERE attrelid=to_regclass('main') AND attnum>0 AND NOT attisdropped")).scalars())
    if "building_key" not in columns:
        raise RuntimeError("main is missing building_key")
    missing = sorted(set(ATTRIBUTES) - columns)
    projection = ",".join(f"'{name}',{name}" if name in columns else f"'{name}',NULL" for name in ATTRIBUTES)
    session.execute(text(f"""
        CREATE TEMP TABLE area_canonical_main ON COMMIT DROP AS
        SELECT building_key, min(attributes::text)::jsonb AS attributes,
               count(*) AS source_rows, count(DISTINCT attributes) AS attribute_versions
        FROM (SELECT building_key,jsonb_build_object({projection}) AS attributes FROM main) source
        WHERE building_key IS NOT NULL AND length(trim(building_key))>0
        GROUP BY building_key
    """))
    # MIN is used only after proving all repeated attributes are equal.
    if session.execute(text("SELECT 1 FROM area_canonical_main WHERE attribute_versions<>1 LIMIT 1")).first():
        raise RuntimeError("Conflicting analytical attributes for one building key")
    count = session.execute(text("SELECT count(*) FROM area_canonical_main")).scalar_one()
    if not count:
        raise RuntimeError("Refusing an empty main population")
    session.execute(text("CREATE UNIQUE INDEX ON area_canonical_main(building_key)"))
    session.execute(text("ANALYZE area_canonical_main"))
    session.execute(text("""
        INSERT INTO energymap_area_buildings
        SELECT :release_id,c.building_key,c.attributes,g.point,
               CASE WHEN g.point IS NULL THEN 'missing' ELSE 'registry' END,g.municipality_code
        FROM area_canonical_main c LEFT JOIN energymap_area_geography g
          ON g.building_key=c.building_key AND g.snapshot_id=:geography_id
    """), {"release_id": release_id, "geography_id": geography_id})
    # Fresh national inserts need statistics before subsequent joins. Avoid
    # validating/scanning map geometry when registry locations cover every key.
    session.execute(text("ANALYZE energymap_area_buildings"))
    def missing_location():
        return session.execute(text("""
            SELECT EXISTS(SELECT 1 FROM energymap_area_buildings
              WHERE release_id=:id AND point IS NULL)
        """), {"id": release_id}).scalar_one()
    needed_fallback = missing_location()
    needs_fallback = needed_fallback
    if "polygon" in columns and needs_fallback:
        invalid = session.execute(text("""
            SELECT 1 FROM main m JOIN energymap_area_buildings b USING(building_key)
            WHERE b.release_id=:id AND b.point IS NULL AND m.polygon IS NOT NULL
              AND (NOT ST_IsValid(m.polygon) OR ST_SRID(m.polygon)<>3857
                   OR GeometryType(m.polygon) NOT IN ('POLYGON','MULTIPOLYGON')) LIMIT 1
        """), {"id": release_id}).first()
        if invalid:
            raise RuntimeError("Invalid map footprint needed for analytical location fallback")
        session.execute(text("""
            WITH footprints AS (
                SELECT m.building_key,ST_UnaryUnion(ST_Collect(m.polygon ORDER BY ST_AsEWKB(m.polygon))) AS geom
                FROM main m JOIN energymap_area_buildings b USING(building_key)
                WHERE b.release_id=:id AND b.point IS NULL AND m.polygon IS NOT NULL
                GROUP BY m.building_key
            )
            UPDATE energymap_area_buildings b
            SET point=ST_Force2D(ST_Transform(ST_PointOnSurface(f.geom),3067)),
                location_method='footprint_union_point_on_surface'
            FROM footprints f WHERE b.release_id=:id AND b.building_key=f.building_key
        """), {"id": release_id})
        needs_fallback = missing_location()
    if "point" in columns and needs_fallback:
        invalid = session.execute(text("""
            SELECT 1 FROM main m JOIN energymap_area_buildings b USING(building_key)
            WHERE b.release_id=:id AND b.point IS NULL AND m.point IS NOT NULL
              AND (NOT ST_IsValid(m.point) OR ST_SRID(m.point)<>3857
                   OR GeometryType(m.point) NOT IN ('POINT','MULTIPOINT')) LIMIT 1
        """), {"id": release_id}).first()
        if invalid:
            raise RuntimeError("Invalid map point needed for analytical location fallback")
        session.execute(text("""
            CREATE TEMP TABLE area_map_points ON COMMIT DROP AS
            SELECT m.building_key,min(encode(ST_AsEWKB(m.point),'hex')) AS geom,count(DISTINCT ST_AsEWKB(m.point)) AS versions
            FROM main m JOIN energymap_area_buildings b USING(building_key)
            WHERE b.release_id=:id AND b.point IS NULL AND m.point IS NOT NULL
            GROUP BY m.building_key
        """), {"id": release_id})
        if session.execute(text("SELECT 1 FROM area_map_points WHERE versions<>1 LIMIT 1")).first():
            raise RuntimeError("Conflicting fallback locations for one building key")
        session.execute(text("""
            UPDATE energymap_area_buildings b
            SET point=ST_Force2D(ST_Transform(ST_PointOnSurface(ST_GeomFromEWKB(decode(p.geom,'hex'))),3067)),
                location_method='map_point'
            FROM area_map_points p WHERE b.release_id=:id AND b.building_key=p.building_key
        """), {"id": release_id})
    if needed_fallback:
        session.execute(text("ANALYZE energymap_area_buildings"))
    signature = session.execute(text("""
        SELECT sum(source_rows)::bigint AS source_rows,count(*) AS building_count,
          sum(hashtextextended(building_key||attributes::text,0)::numeric)::text AS attribute_checksum
        FROM area_canonical_main
    """)).mappings().one()
    unmatched = session.execute(text("""
        SELECT count(*) FILTER (WHERE g.building_key IS NULL) AS missing_geography_keys
        FROM area_canonical_main c LEFT JOIN energymap_area_geography g
          ON c.building_key=g.building_key AND g.snapshot_id=:gid
    """), {"gid": geography_id}).scalar_one()
    extra = session.execute(text("""
        SELECT count(*) FROM energymap_area_geography g LEFT JOIN area_canonical_main c USING(building_key)
        WHERE g.snapshot_id=:gid AND c.building_key IS NULL
    """), {"gid": geography_id}).scalar_one()
    lifecycle = dict(session.execute(text("""
        SELECT coalesce(g.provenance->>'usage_status','unknown'),count(*)
        FROM area_canonical_main c LEFT JOIN energymap_area_geography g
          ON c.building_key=g.building_key AND g.snapshot_id=:gid
        GROUP BY coalesce(g.provenance->>'usage_status','unknown')
    """), {"gid": geography_id}).all())
    demolition = session.execute(text("""
        SELECT count(*) FROM area_canonical_main c JOIN energymap_area_geography g USING(building_key)
        WHERE g.snapshot_id=:gid AND nullif(g.provenance->>'demolition_date','') IS NOT NULL
    """), {"gid": geography_id}).scalar_one()
    excluded = session.execute(text(
        "SELECT count(*) FROM main WHERE building_key IS NULL OR length(trim(building_key))=0"
    )).scalar_one()
    session.execute(text("DROP TABLE area_canonical_main"))
    return {**dict(signature), "usage_status_counts": lifecycle, "recorded_demolition_date_count": demolition,
            "duplicate_map_rows": signature["source_rows"] - count,
            "missing_geography_keys": unmatched, "geography_keys_outside_map": extra,
            "missing_input_columns": missing,
            "excluded_empty_key_rows": excluded}


def assign_membership(session, release_id, boundary_set_id):
    # Keep the spatial working set narrow and process a constant polygon against
    # its indexed points. The point-first left join repeatedly prepares complex
    # national boundaries and reads wide attribute rows for every candidate.
    session.execute(text("""
        CREATE TEMP TABLE area_locations ON COMMIT DROP AS
        SELECT building_key,registry_municipality_code,point
        FROM energymap_area_buildings WHERE release_id=:id
    """), {"id": release_id})
    session.execute(text("CREATE INDEX ON area_locations USING gist(point)"))
    session.execute(text("ANALYZE area_locations"))
    session.execute(text("""
        CREATE TEMP TABLE area_coverage ON COMMIT DROP AS
        SELECT p.building_key,a.level,count(*) AS candidates,
               count(*) FILTER (WHERE p.interior) AS interiors,
               min(a.area_code) AS edge_code,
               min(a.area_code) FILTER (WHERE p.interior) AS interior_code,
               bool_or(a.area_code=p.registry_municipality_code) AS registry_covered
        FROM energymap_area_boundaries a
        CROSS JOIN LATERAL (
            SELECT b.building_key,b.registry_municipality_code,
                   ST_Contains(a.geom,b.point) AS interior
            FROM area_locations b
            WHERE b.point && a.geom AND ST_Covers(a.geom,b.point)
            OFFSET 0
        ) p
        WHERE a.boundary_set_id=:bid AND a.level IN ('postal_code','municipality')
        GROUP BY p.building_key,a.level
    """), {"bid": boundary_set_id})
    session.execute(text("CREATE UNIQUE INDEX ON area_coverage(building_key,level)"))
    session.execute(text("ANALYZE area_coverage"))
    if session.execute(text("SELECT 1 FROM area_coverage WHERE interiors>1 LIMIT 1")).first():
        raise RuntimeError("Building falls in overlapping boundary interiors")
    session.execute(text("""
        INSERT INTO energymap_building_area_membership
        WITH resolved AS (
          SELECT b.building_key,b.registry_municipality_code,b.point IS NULL AS missing_point,
            m.registry_covered,
            CASE WHEN p.interiors=1 THEN p.interior_code
                 WHEN p.candidates>0 THEN p.edge_code END AS postal_code,
            CASE WHEN b.point IS NULL THEN 'missing_location'
                 WHEN p.interiors=1 THEN 'spatial_interior'
                 WHEN p.candidates>1 THEN 'shared_edge_code_tiebreak'
                 WHEN p.candidates=1 THEN 'spatial_boundary'
                 ELSE 'outside_boundaries' END AS postal_method,
            CASE WHEN m.interiors=1 THEN m.interior_code
                 WHEN m.candidates>0 THEN m.edge_code END AS municipality_spatial_code,
            CASE WHEN b.point IS NULL THEN 'missing_location'
                 WHEN m.interiors=1 THEN 'spatial_interior'
                 WHEN m.candidates>1 THEN 'shared_edge_code_tiebreak'
                 WHEN m.candidates=1 THEN 'spatial_boundary'
                 ELSE 'outside_boundaries' END AS municipality_spatial_method
          FROM area_locations b
          LEFT JOIN area_coverage p ON p.building_key=b.building_key AND p.level='postal_code'
          LEFT JOIN area_coverage m ON m.building_key=b.building_key AND m.level='municipality'
        ), chosen AS (
          SELECT c.building_key,c.postal_code,c.postal_method,
            CASE WHEN r.area_code IS NOT NULL AND c.missing_point THEN r.area_code
                 WHEN r.area_code IS NOT NULL AND c.registry_covered THEN r.area_code
                 WHEN r.area_code IS NOT NULL THEN NULL
                 ELSE c.municipality_spatial_code END AS municipality_code,
            CASE WHEN r.area_code IS NOT NULL AND c.missing_point THEN 'registry_only'
                 WHEN r.area_code IS NOT NULL AND c.registry_covered THEN 'registry_verified'
                 WHEN r.area_code IS NOT NULL THEN 'registry_spatial_conflict'
                 ELSE c.municipality_spatial_method END AS municipality_method
          FROM resolved c
          LEFT JOIN energymap_area_boundaries r ON r.boundary_set_id=:bid
            AND r.level='municipality' AND r.area_code=c.registry_municipality_code
        )
        SELECT :id,c.building_key,:bid,c.postal_code,c.municipality_code,r.region_code,
               c.postal_method,c.municipality_method
        FROM chosen c LEFT JOIN energymap_area_boundaries r
          ON r.boundary_set_id=:bid AND r.level='municipality' AND r.area_code=c.municipality_code
    """), {"id": release_id, "bid": boundary_set_id})
    session.execute(text("DROP TABLE area_coverage,area_locations"))
    session.execute(text("ANALYZE energymap_building_area_membership"))


def number_expression(field):
    # Source main fields may be PostgreSQL NaN/Infinity; neither is a known area.
    return (f"CASE WHEN attributes->>'{field}' ~ '^[0-9]+([.][0-9]+)?([eE][+-]?[0-9]+)?$' "
            f"THEN CASE WHEN (attributes->>'{field}')::numeric>0 "
            f"THEN (attributes->>'{field}')::numeric END END")


def calculate_stats(session, release_id, boundary_set_id, as_of):
    ventilation = ventilation_expressions()["ventilation_category"]
    source_columns = ",".join(f"attributes->>'{c}' AS {c}" for c in SOURCE_COLUMNS)
    areas = ",".join(number_expression(c) + " AS " + c for c in AREA_FIELDS)
    effective_classes = ",".join(f"{value} AS {name}" for name, value in energy_class_expressions().items())
    category_columns = ",".join(
        f"coalesce(nullif(trim(attributes->>'{c}'),''),'unknown') AS {c}"
        for c in ("heating_method", "heating_energy_source", "main_purpose"))
    session.execute(text(f"""
        CREATE TEMP TABLE area_metric_facts ON COMMIT DROP AS
        WITH source AS (
          SELECT building_key,attributes,{source_columns},
            upper(nullif(trim(attributes->>'energy_certificate_class'),'')) AS cls,
            CASE WHEN attributes->>'energy_certificate_class_year' IN ('2013','2018')
              THEN attributes->>'energy_certificate_class_year' ELSE 'unknown_scale' END AS scale,
            CASE WHEN attributes->>'energy_certificate_valid_until' ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
              THEN CASE WHEN left(attributes->>'energy_certificate_valid_until',10)::date >= :as_of
                   THEN 'valid' ELSE 'expired' END ELSE 'unknown_validity' END AS validity,
            CASE WHEN attributes->>'completion_date' ~ '^[0-9]{{4}}-'
              THEN left(attributes->>'completion_date',4)::integer END AS completion_year
          FROM energymap_area_buildings WHERE release_id=:id
        )
        SELECT building_key,{areas},{category_columns},{effective_classes},
          {ventilation} AS ventilation,
          CASE WHEN cls IN ('A','B','C','D','E','F','G') THEN scale||':'||validity||':'||cls
               WHEN cls IS NULL THEN 'missing' ELSE 'invalid_class' END AS official_class,
          cls IN ('A','B','C','D','E','F','G') AND scale<>'unknown_scale' AND validity='valid' AS valid_official,
          CASE WHEN attributes->>'modeled_energy_class' IN ('A','B','C','D','E','F','G')
               THEN attributes->>'modeled_energy_class' ELSE 'unknown' END AS modeled_class,
          coalesce(nullif(attributes->>'modeled_energy_class_reason',''),'unknown') AS modeled_reason,
          CASE WHEN completion_year<1 OR completion_year>:year OR completion_year IS NULL THEN 'unknown'
               WHEN completion_year<1940 THEN 'before_1940' WHEN completion_year<1960 THEN '1940_1959'
               WHEN completion_year<1980 THEN '1960_1979' WHEN completion_year<2000 THEN '1980_1999'
               WHEN completion_year<2020 THEN '2000_2019' ELSE '2020_onward' END AS construction_period
        FROM source
    """), {"id": release_id, "as_of": as_of, "year": as_of.year})
    # Building identity is needed for the join, not repeated in four metric rows.
    fact_columns = ",".join(f"f.{c}" for c in (*AREA_FIELDS, *CATEGORIES, "valid_official"))
    session.execute(text(f"""
        CREATE TEMP TABLE area_metric_groups ON COMMIT DROP AS
        SELECT g.level,g.area_code,{fact_columns}
        FROM area_metric_facts f JOIN energymap_building_area_membership m USING(building_key)
        CROSS JOIN LATERAL (VALUES
          ('postal_code',m.postal_code),('municipality',m.municipality_code),('region',m.region_code),
          ('postal_municipality',m.postal_code||':'||m.municipality_code)
        ) g(level,area_code) WHERE m.release_id=:id AND g.area_code IS NOT NULL
    """), {"id": release_id})
    session.execute(text("DROP TABLE area_metric_facts"))
    session.execute(text("ANALYZE area_metric_groups"))
    sums = ",".join(f"count({c}) AS {c}_known,sum({c}) AS {c}_sum" for c in AREA_FIELDS)
    session.execute(text(f"""
        CREATE TEMP TABLE area_summary ON COMMIT DROP AS
        SELECT level,area_code,count(*) AS building_count,
            count(*) FILTER (WHERE valid_official) AS valid_official_class_count,
            {sums},'{{}}'::jsonb AS metrics
        FROM area_metric_groups GROUP BY level,area_code
    """))
    for category in CATEGORIES:
        session.execute(text(f"""
            WITH counts AS (
              SELECT level,area_code,{category} AS value,count(*) AS n
              FROM area_metric_groups GROUP BY level,area_code,{category}
            ), histograms AS (
              SELECT level,area_code,jsonb_object_agg(value,n) AS counts,
                sum(n) FILTER (WHERE value NOT IN ('unknown','no_info','missing','invalid_class')) AS known
              FROM counts GROUP BY level,area_code
            )
            UPDATE area_summary s SET metrics=metrics||jsonb_build_object(
              '{category}',jsonb_build_object('counts',h.counts,'known_building_count',coalesce(h.known,0),
                      'unknown_building_count',s.building_count-coalesce(h.known,0)))
            FROM histograms h WHERE s.level=h.level AND s.area_code=h.area_code
        """))
    session.execute(text("""
        WITH counts AS (
          SELECT level,area_code,split_part(official_class,':',1) AS scale,
            split_part(official_class,':',3) AS class,count(*) AS n
          FROM area_metric_groups WHERE valid_official GROUP BY level,area_code,scale,class
        ), distributions AS (
          SELECT level,area_code,scale,jsonb_object_agg(class,n) AS counts
          FROM counts GROUP BY level,area_code,scale
        ), by_scale AS (
          SELECT level,area_code,jsonb_object_agg(scale,counts) AS counts
          FROM distributions GROUP BY level,area_code
        )
        UPDATE area_summary s SET metrics=jsonb_set(metrics,'{official_class}',metrics->'official_class'||jsonb_build_object(
            'valid_counts_by_scale',coalesce(d.counts,'{}'::jsonb),'eligible_building_count',s.valid_official_class_count))
        FROM (SELECT s.level,s.area_code,b.counts FROM area_summary s LEFT JOIN by_scale b USING(level,area_code)) d
        WHERE s.level=d.level AND s.area_code=d.area_code
    """))
    for field in AREA_FIELDS:
        session.execute(text(f"""
            UPDATE area_summary SET metrics=metrics||jsonb_build_object(
              '{field}',jsonb_build_object('unit','m2','sum',{field}_sum,
                'known_building_count',{field}_known,'unknown_building_count',building_count-{field}_known))
        """))
    session.execute(text("""
        INSERT INTO energymap_area_stats
        SELECT :id,:bid,a.level,a.area_code,coalesce(s.building_count,0),
          coalesce(s.valid_official_class_count,0),
          s.valid_official_class_count::double precision/nullif(s.building_count,0),
          coalesce(s.metrics,CAST(:empty AS jsonb))
        FROM energymap_area_boundaries a LEFT JOIN area_summary s USING(level,area_code)
        WHERE a.boundary_set_id=:bid
    """), {"id": release_id, "bid": boundary_set_id, "empty": json_text(empty_metrics())})
    session.execute(text("""
        INSERT INTO energymap_postal_municipality_stats
        SELECT :id,split_part(area_code,':',1),split_part(area_code,':',2),building_count,
               valid_official_class_count,valid_official_class_count::double precision/building_count,metrics
        FROM area_summary WHERE level='postal_municipality'
    """), {"id": release_id})
    session.execute(text("DROP TABLE area_metric_groups,area_summary"))


def validate_release(session, release_id, expected_population):
    report = {"building_count": expected_population, "levels": {}, "location_methods": {}}
    count = session.execute(text("SELECT count(*) FROM energymap_building_area_membership WHERE release_id=:id"), {"id": release_id}).scalar_one()
    if count != expected_population:
        raise RuntimeError("Membership does not conserve the canonical population")
    for level, field in (("postal_code", "postal_code"), ("municipality", "municipality_code"), ("region", "region_code")):
        assigned = session.execute(text(f"SELECT count({field}) FROM energymap_building_area_membership WHERE release_id=:id"), {"id": release_id}).scalar_one()
        total = session.execute(text("SELECT coalesce(sum(building_count),0) FROM energymap_area_stats WHERE release_id=:id AND level=:level"), {"id": release_id, "level": level}).scalar_one()
        if assigned != total:
            raise RuntimeError(f"{level} counts do not reconcile")
        report["levels"][level] = {"assigned": assigned, "unassigned": count-assigned}
    # Conserving the national total alone is insufficient: each region must equal its municipalities.
    mismatch = session.execute(text("""
        SELECT 1 FROM energymap_area_stats r LEFT JOIN (
          SELECT s.release_id,b.region_code,sum(s.building_count) AS n
          FROM energymap_area_stats s JOIN energymap_area_boundaries b
            USING(boundary_set_id,level,area_code)
          WHERE s.release_id=:id AND s.level='municipality'
          GROUP BY s.release_id,b.region_code
        ) m ON r.release_id=m.release_id AND r.area_code=m.region_code
        WHERE r.release_id=:id AND r.level='region' AND r.building_count<>coalesce(m.n,0) LIMIT 1
    """), {"id": release_id}).first()
    if mismatch:
        raise RuntimeError("Municipality/region totals disagree")
    for field in ("location_method",):
        rows = session.execute(text(f"SELECT {field},count(*) FROM energymap_area_buildings WHERE release_id=:id GROUP BY {field}"), {"id": release_id}).all()
        report["location_methods"] = dict(rows)
    for field in ("postal_method", "municipality_method"):
        report[field] = dict(session.execute(text(f"SELECT {field},count(*) FROM energymap_building_area_membership WHERE release_id=:id GROUP BY {field}"), {"id": release_id}).all())
    cross = session.execute(text("SELECT coalesce(sum(building_count),0) FROM energymap_postal_municipality_stats WHERE release_id=:id"), {"id": release_id}).scalar_one()
    both = session.execute(text("SELECT count(*) FROM energymap_building_area_membership WHERE release_id=:id AND postal_code IS NOT NULL AND municipality_code IS NOT NULL"), {"id": release_id}).scalar_one()
    if cross != both:
        raise RuntimeError("Postal/municipality crosswalk does not reconcile")
    for category in CATEGORIES:
        invalid = session.execute(text("""
            WITH summaries AS (
              SELECT building_count,metrics FROM energymap_area_stats WHERE release_id=:id
              UNION ALL
              SELECT building_count,metrics FROM energymap_postal_municipality_stats WHERE release_id=:id
            )
            SELECT 1 FROM summaries s
            WHERE (metrics->:category->>'known_building_count')::bigint
                    +(metrics->:category->>'unknown_building_count')::bigint IS DISTINCT FROM building_count
              OR coalesce((SELECT sum(value::bigint) FROM jsonb_each_text(s.metrics->:category->'counts')),0)<>building_count
            LIMIT 1
        """), {"id": release_id, "category": category}).first()
        if invalid:
            raise RuntimeError(f"{category} histogram does not conserve the population")
    for field in AREA_FIELDS:
        invalid = session.execute(text("""
            SELECT 1 FROM energymap_area_stats WHERE release_id=:id AND (
              (metrics->:field->>'known_building_count')::bigint
                +(metrics->:field->>'unknown_building_count')::bigint<>building_count
              OR ((metrics->:field->>'known_building_count')::bigint=0)
                   <> (metrics->:field->>'sum' IS NULL)) LIMIT 1
        """), {"id": release_id, "field": field}).first()
        if invalid:
            raise RuntimeError(f"{field} denominators do not reconcile")
    invalid = session.execute(text("""
        SELECT 1 FROM energymap_area_stats WHERE release_id=:id AND
          (certificate_coverage IS DISTINCT FROM valid_official_class_count::double precision/nullif(building_count,0)) LIMIT 1
    """), {"id": release_id}).first()
    if invalid:
        raise RuntimeError("Certificate coverage disagrees with its numerator and denominator")
    report["histograms_and_denominators_checked"] = True
    report["postal_municipality_assigned"] = both
    report["passed"] = True
    return report


def build_release(session, *, boundary_set_id, geography_snapshot_id, as_of=None, release_id=None, min_buildings=1):
    if session.in_transaction():
        raise RuntimeError("Release build requires a fresh session for a consistent snapshot")
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('energymap_area_build'))"))
    release_id = str(UUID(release_id)) if release_id else str(uuid4())
    as_of = as_of or date.today()
    boundary = session.execute(text("SELECT manifest FROM energymap_area_boundary_sets WHERE boundary_set_id=:id"), {"id": boundary_set_id}).scalar()
    geography = session.execute(text("SELECT manifest FROM energymap_area_geography_snapshots WHERE snapshot_id=:id"), {"id": geography_snapshot_id}).scalar()
    if boundary is None or geography is None:
        raise RuntimeError("Boundary/geography snapshot not found")
    started = time.monotonic()
    manifest = {"population_scope": METRIC_DEFINITIONS["population"], "metric_definitions": METRIC_DEFINITIONS,
                "boundary_inputs": boundary, "geography_input": geography}
    session.execute(text("""
        INSERT INTO energymap_area_releases
        (release_id,boundary_set_id,geography_snapshot_id,as_of,metric_version,manifest)
        VALUES (:id,:bid,:gid,:as_of,:mv,CAST(:manifest AS jsonb))
    """), {"id": release_id, "bid": boundary_set_id, "gid": geography_snapshot_id,
           "as_of": as_of, "mv": METRIC_VERSION, "manifest": json_text(manifest)})
    population = canonical_population(session, release_id, geography_snapshot_id)
    if population["building_count"] < min_buildings:
        raise RuntimeError("Refusing an incomplete national main population")
    assign_membership(session, release_id, boundary_set_id)
    calculate_stats(session, release_id, boundary_set_id, as_of)
    validation = validate_release(session, release_id, population["building_count"])
    manifest.update(population)
    validation["build_seconds"] = round(time.monotonic() - started, 3)
    session.execute(text("""
        UPDATE energymap_area_releases SET manifest=CAST(:m AS jsonb),
          validation=CAST(:v AS jsonb),validated=true WHERE release_id=:id
    """), {"id": release_id, "m": json_text(manifest), "v": json_text(validation)})
    session.execute(text("ANALYZE energymap_area_stats"))
    return {"release_id": release_id, "manifest": manifest, "validation": validation}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundary-set-id", required=True)
    parser.add_argument("--geography-snapshot-id", required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--release-id")
    parser.add_argument("--min-buildings", type=int, default=1_000_000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as session:
        result = build_release(session, boundary_set_id=args.boundary_set_id,
            geography_snapshot_id=args.geography_snapshot_id, as_of=args.as_of, release_id=args.release_id, min_buildings=args.min_buildings)
        session.rollback() if args.dry_run else session.commit()
    print(json_text(result))


if __name__ == "__main__":
    main()
