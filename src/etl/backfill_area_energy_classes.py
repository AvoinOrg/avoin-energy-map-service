"""Add effective/model-inclusive histograms to an existing frozen area snapshot.

This additive extension preserves all previous metrics, memberships and source
rows. It is atomic and idempotent, and checks the current building projection
before extending a legacy snapshot that omitted effective-class columns.
"""

import argparse
from datetime import datetime, timezone
import time

from sqlalchemy import text

from src.area_contract import METRIC_DEFINITIONS, METRIC_VERSION
from src.db.session import SessionLocal
from src.etl.area_energy_classes import energy_class_expressions
from src.etl.area_inputs import json_text
from src.etl.build_area_aggregates import validate_release


def backfill(session, identifier=None):
    if session.in_transaction():
        raise RuntimeError("Backfill requires a fresh repeatable-read snapshot")
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    session.execute(text("SET LOCAL work_mem='64MB'"))
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('energymap_area_build'))"))
    row = session.execute(text("""
        SELECT release_id,manifest,validated FROM energymap_area_releases
        WHERE release_id=coalesce(:id,(SELECT release_id FROM energymap_area_releases WHERE active))
        FOR UPDATE
    """), {'id': identifier}).mappings().one()
    if not row['validated']:
        raise RuntimeError("Only a validated frozen snapshot can be extended")
    identifier = row['release_id']
    params = {'id': identifier}
    started = time.monotonic()
    expressions = energy_class_expressions()
    session.execute(text(f"""
        CREATE TEMP TABLE area_energy_class_facts ON COMMIT DROP AS
        SELECT building_key,{expressions['energy_class']} AS energy_class,
          {expressions['energy_class_source']} AS energy_class_source
        FROM energymap_area_buildings WHERE release_id=:id
    """), params)
    session.execute(text("CREATE UNIQUE INDEX ON area_energy_class_facts(building_key)"))
    session.execute(text("ANALYZE area_energy_class_facts"))
    # Compare every map row, including duplicated addresses/footprints. Nothing
    # is written if a legacy derivation differs from what users currently see.
    population = session.execute(text("SELECT count(*) FROM area_energy_class_facts")).scalar_one()
    comparison = session.execute(text("""
        SELECT count(DISTINCT f.building_key) AS matched_buildings,
          count(*) FILTER (WHERE f.energy_class IS DISTINCT FROM CASE WHEN m.energy_class IN ('A','B','C','D','E','F','G')
          THEN m.energy_class ELSE 'unknown' END
        OR f.energy_class_source IS DISTINCT FROM CASE
          WHEN NOT coalesce(m.energy_class IN ('A','B','C','D','E','F','G'),false) THEN 'unknown'
          WHEN m.is_energy_class_modeled::text='true' THEN 'modeled'
          WHEN m.is_energy_class_modeled::text='false' THEN 'official' ELSE 'unknown' END) AS mismatches
        FROM main m JOIN area_energy_class_facts f USING(building_key)
    """)).mappings().one()
    mismatches = comparison['mismatches']
    missing = comparison['matched_buildings'] != population
    if mismatches or missing:
        raise RuntimeError(f"Frozen effective classes disagree with current building projection: {mismatches} mismatched rows; missing keys={missing}")
    session.execute(text("""
        CREATE TEMP TABLE area_energy_class_counts ON COMMIT DROP AS
        SELECT g.level,g.area_code,f.energy_class,f.energy_class_source,count(*) AS n
        FROM area_energy_class_facts f JOIN energymap_building_area_membership m USING(building_key)
        CROSS JOIN LATERAL (VALUES
          ('postal_code',m.postal_code),('municipality',m.municipality_code),('region',m.region_code),
          ('postal_municipality',m.postal_code||':'||m.municipality_code)
        ) g(level,area_code)
        WHERE m.release_id=:id AND g.area_code IS NOT NULL
        GROUP BY g.level,g.area_code,f.energy_class,f.energy_class_source
    """), params)
    session.execute(text("DROP TABLE area_energy_class_facts"))
    session.execute(text("CREATE TEMP TABLE area_energy_class_metrics(level text,area_code text,metrics jsonb,PRIMARY KEY(level,area_code)) ON COMMIT DROP"))
    for category in ('energy_class', 'energy_class_source'):
        session.execute(text(f"""
            INSERT INTO area_energy_class_metrics
            WITH counts AS (
              SELECT level,area_code,{category} AS value,sum(n)::bigint AS n
              FROM area_energy_class_counts GROUP BY level,area_code,{category}
            ), histograms AS (
              SELECT level,area_code,jsonb_object_agg(value,n) AS counts,
                coalesce(sum(n) FILTER (WHERE value<>'unknown'),0)::bigint AS known,
                coalesce(sum(n) FILTER (WHERE value='unknown'),0)::bigint AS unknown
              FROM counts GROUP BY level,area_code
            )
            SELECT level,area_code,jsonb_build_object('{category}',jsonb_build_object(
              'counts',counts,'known_building_count',known,'unknown_building_count',unknown)) FROM histograms
            ON CONFLICT(level,area_code) DO UPDATE SET metrics=area_energy_class_metrics.metrics||excluded.metrics
        """))
    empty = {c: {'counts': {}, 'known_building_count': 0, 'unknown_building_count': 0}
             for c in ('energy_class', 'energy_class_source')}
    params['empty'] = json_text(empty)
    updated = {}
    for table, level, code in (
        ('energymap_area_stats', 's.level', 's.area_code'),
        ('energymap_postal_municipality_stats', "'postal_municipality'", "s.postal_code||':'||s.municipality_code"),
    ):
        updated[table] = session.execute(text(f"""
            WITH additions AS (
              SELECT s.release_id,{level} AS level,{code} AS area_code,
                coalesce(a.metrics,CAST(:empty AS jsonb)) AS metrics
              FROM {table} s LEFT JOIN area_energy_class_metrics a
                ON a.level={level} AND a.area_code={code} WHERE s.release_id=:id
            )
            UPDATE {table} s SET metrics=s.metrics||a.metrics FROM additions a
            WHERE s.release_id=a.release_id AND {level}=a.level AND {code}=a.area_code
              AND s.metrics IS DISTINCT FROM s.metrics||a.metrics
        """), params).rowcount
    validation = validate_release(session, identifier, population)
    totals = dict(session.execute(text("SELECT energy_class_source,sum(n)::bigint FROM area_energy_class_counts WHERE level='municipality' GROUP BY energy_class_source")).all())
    report = {'release_id': identifier, 'metric_version': METRIC_VERSION, 'population': population,
              'building_projection_mismatches': mismatches, 'updated_rows': updated,
              'municipality_assigned_provenance': totals, 'validated': validation['passed'],
              'seconds': round(time.monotonic() - started, 3)}
    manifest = dict(row['manifest'])
    definitions = dict(manifest.get('metric_definitions', {}))
    definitions.update({c: METRIC_DEFINITIONS[c] for c in ('energy_class', 'energy_class_source')})
    manifest['metric_definitions'] = definitions
    if any(updated.values()) or row['manifest'].get('summary_extensions', {}).get('energy_class') is None:
        extensions = dict(manifest.get('summary_extensions', {}))
        extensions['energy_class'] = {**report, 'verified_at': datetime.now(timezone.utc).isoformat(),
                                      'policy': 'published-building-effective-class-with-existing-modeled-fallback'}
        manifest['summary_extensions'] = extensions
        session.execute(text("UPDATE energymap_area_releases SET metric_version=:version,manifest=CAST(:manifest AS jsonb) WHERE release_id=:id"),
                        {'id': identifier, 'version': METRIC_VERSION, 'manifest': json_text(manifest)})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with SessionLocal() as session:
        result = backfill(session, args.release_id)
        session.commit() if args.apply else session.rollback()
    print(json_text({**result, 'apply': args.apply}))


if __name__ == '__main__':
    main()
