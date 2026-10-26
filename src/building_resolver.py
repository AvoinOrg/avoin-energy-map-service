"""Read-only canonical building resolution against a consistent PostgreSQL snapshot.

All identifiers below are server-owned allowlists. Values are always bound.
The caller supplies a fresh, clean Session; this module starts a repeatable-read
read-only transaction and never flushes/commits it. Close/rollback after use.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
import re
from typing import Generic, Literal, Sequence, TypeVar

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.building_contract import (
    AvailabilityV1, BuildingSummaryV1, CanonicalBuildingV1, CertificateConsumptionV1,
    CodeLabelV1, CodeLabelsV1, PublishedCertificateV1,
    CertificateV1, CodeCatalog, CatalogUnavailable, DetailsV1, EDITABLE_FIELDS,
    FieldProvenanceV1, GEOMETRY_BYTES, GeometryV1, IdentityV1, LocationV1, ModelV1,
    PREFIXES, MEASURES, QUANTITIES, SCENARIO_COLUMNS, SOURCE_TEXT_CHARS,
    ScenarioQuantityV1, SelectionV1, SourceV1, SourcesV1, building_key as validate_key,
    ventilation_category,
)
from src.etl.certificate_consumption import consumption_payload, nonnegative_number, CARRIERS, SOURCE_PREFIX
from src.geoserver.ventilation import VENTILATION_TYPES

REGISTRY = ('permanent_building_identifier', 'property_identifier', 'completion_date',
            'main_purpose', 'heating_method', 'heating_energy_source', 'number_of_storeys',
            'floor_area', 'total_area', 'gross_floor_area', 'volume')
ADDRESS = ('address_key', 'address_fin', 'postal_code', 'postal_office_fin')
CERT_MAIN = ('energy_certificate_class', 'energy_certificate_previous_class', 'energy_certificate_class_year',
             'energy_certificate_valid_until', 'energy_certificate_heated_net_area',
             'energy_certificate_ventilation_type_id', 'energy_certificate_ventilation_description_fi',
             'energy_certificate_ventilation_description_sv', 'energy_certificate_recommendations_fi',
             'energy_certificate_recommendations_sv', 'energy_certificate_consumption')
MODEL_MAIN = ('modeled_energy_class', 'modeled_energy_e_value', 'modeled_energy_class_reason',
              'modeled_energy_class_area_source', 'energy_class', 'is_energy_class_modeled', 'energy_class_year')
MAIN_INVARIANTS = REGISTRY + CERT_MAIN + MODEL_MAIN + SCENARIO_COLUMNS
CERT_COLUMNS = ('certificate_id', 'version', 'building_identifier', 'signed_at', 'valid_until',
                'inspection_date', 'e_class', 'heated_net_area', 'ventilation_type_id',
                'ventilation_description_fi', 'ventilation_description_sv', 'recommendations_fi',
                'recommendations_sv', 'updated_at')
T = TypeVar('T')


class BuildingSourceUnavailable(Exception):
    """Sanitized dependency failure. Do not return/log the underlying SQL error."""


@dataclass(frozen=True)
class Resolution(Generic[T]):
    status: Literal['available', 'missing', 'ambiguous']
    value: T | None = None


SummaryResolutionV1 = Resolution[BuildingSummaryV1]


def _snapshot(session):
    if session.new or session.dirty or session.deleted:
        raise BuildingSourceUnavailable('Resolver requires a clean read-only snapshot')
    if not session.in_transaction():
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
    else:
        # Reuse an explicitly established safe snapshot (e.g. later state reader).
        settings = session.execute(text("SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only')")).one()
        if settings[0] not in ('repeatable read', 'serializable') or settings[1] != 'on':
            raise BuildingSourceUnavailable('Resolver requires a clean read-only snapshot')
    session.execute(text("SET LOCAL statement_timeout = '10s'"))
    session.execute(text("SET LOCAL lock_timeout = '2s'"))


def _metadata(session):
    rows = session.execute(text("""
        SELECT c.relname,a.attname,has_table_privilege(c.oid,'SELECT') AS readable
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        WHERE n.nspname=current_schema() AND c.relname=ANY(:tables)
    """), {'tables': ['main', 'buildings', 'addresses', 'polygons', 'energy_certificates', 'code_maps']}).mappings()
    result = {}
    for row in rows:
        if row['readable']:
            result.setdefault(row['relname'], set()).add(row['attname'])
    if 'building_key' not in result.get('main', set()) or 'building_key' not in result.get('buildings', set()):
        raise BuildingSourceUnavailable('Required building source unavailable')
    return result


def _value(column, columns):
    if column not in columns:
        return 'NULL::text'
    expression = f'NULLIF(btrim("{column}"::text),\'\')'
    if column in ('permanent_building_identifier', 'property_identifier', 'address_key'):
        expression = f'CASE WHEN NULLIF(btrim("{column}"::text),\'\') IS NOT NULL THEN "{column}"::text END'
    if column.endswith('_class') or column in ('energy_class',):
        expression = f'upper({expression})'
    return expression


def _object(columns, available):
    # PostgreSQL function arguments are bounded; concatenate closed chunks.
    chunks = []
    for offset in range(0, len(columns), 40):
        chunks.append('jsonb_build_object(' + ','.join(
            f"'{c}',{_value(c, available)}" for c in columns[offset:offset + 40]) + ')')
    return ' || '.join(chunks)


def _address_order():
    return '''CASE WHEN NULLIF(btrim(address_fin),'') IS NOT NULL AND length(address_fin)<=16384 THEN 0
                  WHEN COALESCE(NULLIF(btrim(postal_code),''),NULLIF(btrim(postal_office_fin),'')) IS NOT NULL THEN 1 ELSE 2 END,
              address_key COLLATE "C" ASC NULLS LAST,address_fin COLLATE "C" ASC NULLS LAST,
              postal_code COLLATE "C" ASC NULLS LAST,postal_office_fin COLLATE "C" ASC NULLS LAST'''


def _main(session, keys, columns, *, detail=True):
    address = ','.join(f'{_value(c, columns)} AS {c}' for c in ADDRESS)
    returned_facts = 'facts' if detail else "jsonb_build_object('permanent_building_identifier',facts->'permanent_building_identifier') AS facts"
    sql = f'''
        WITH filtered AS MATERIALIZED (
          SELECT building_key,{address},{_object(MAIN_INVARIANTS, columns)} AS facts
          FROM main WHERE building_key=ANY(:keys)
        ), counts AS (
          SELECT building_key,count(DISTINCT facts::text) AS fact_variants,
            count(DISTINCT jsonb_build_array(address_key,address_fin,postal_code,postal_office_fin)) AS address_variants
          FROM filtered GROUP BY building_key
        ), ranked AS (
          SELECT *,row_number() OVER(PARTITION BY building_key ORDER BY {_address_order()}) AS rn FROM filtered
        )
        SELECT building_key,{returned_facts},address_key,address_fin,postal_code,postal_office_fin,fact_variants,address_variants
        FROM ranked JOIN counts USING(building_key) WHERE rn=1
    '''
    return {r['building_key']: dict(r) for r in session.execute(text(sql), {'keys': keys}).mappings()}


def _buildings(session, keys, columns):
    names = REGISTRY + ('apartment_count', 'modified_timestamp_utc')
    sql = 'SELECT building_key,' + ','.join(f'{_value(c, columns)} AS "{c}"' for c in names) + ' FROM buildings WHERE building_key=ANY(:keys)'
    return {r['building_key']: dict(r) for r in session.execute(text(sql), {'keys': keys}).mappings()}


def _identity_conflicts(session, identifiers):
    if not identifiers:
        return set()
    rows = session.execute(text('''
        SELECT identifier FROM (
          SELECT permanent_building_identifier AS identifier,building_key
          FROM main WHERE permanent_building_identifier=ANY(:ids)
          UNION ALL
          SELECT permanent_building_identifier,building_key
          FROM buildings WHERE permanent_building_identifier=ANY(:ids)
        ) x GROUP BY identifier HAVING count(DISTINCT building_key)>1
    '''), {'ids': list(identifiers)}).scalars()
    return set(rows)


def _addresses(session, keys, chosen_keys, meta):
    columns = meta.get('addresses', set())
    if not {'building_key', *ADDRESS}.issubset(columns):
        return {}
    sql = f'''
        WITH filtered AS MATERIALIZED (
          SELECT building_key,{','.join(f'{_value(c, columns)} AS {c}' for c in ADDRESS)},
            {_value('modified_timestamp_utc', columns)} AS modified_timestamp_utc
          FROM addresses WHERE building_key=ANY(:keys)
        ), ranked AS (
          SELECT *, count(*) OVER(PARTITION BY building_key) AS variants,
            row_number() OVER(PARTITION BY building_key ORDER BY
              CASE WHEN address_key=ANY(:chosen) THEN 0 ELSE 1 END,{_address_order()}) AS rn
          FROM filtered
        ) SELECT * FROM ranked WHERE rn=1
    '''
    return {r['building_key']: dict(r) for r in session.execute(text(sql), {'keys': keys, 'chosen': chosen_keys}).mappings()}


def geometry_query(meta, *, detail):
    main_columns = meta.get('main', set())
    geometry_values = [(name, f'main.{name}') for name in ('polygon', 'point') if name in main_columns]
    parts = []
    if geometry_values:
        values = ','.join(f"({name},'{source}')" for name, source in geometry_values)
        selected = ','.join(name for name, _ in geometry_values)
        parts.append(f"SELECT m.building_key,v.g,v.source::text AS source,3857 AS expected,NULL::text AS stamp FROM (SELECT building_key,{selected} FROM main WHERE building_key=ANY(:main_keys)) m CROSS JOIN LATERAL (VALUES {values}) v(g,source) WHERE v.g IS NOT NULL")
    for table, column, expected, keyset, stamp in (
        ('polygons', 'polygon', 4326, 'fallback_keys', True), ('buildings', 'point', 4326, 'fallback_keys', True),
    ):
        if column in meta.get(table, set()):
            timestamp = _value('modified_timestamp_utc', meta[table]) if stamp else 'NULL::text'
            parts.append(f"SELECT building_key,{column} AS g,'{table}.{column}'::text AS source,{expected} AS expected,{timestamp} AS stamp FROM {table} WHERE building_key=ANY(:{keyset}) AND {column} IS NOT NULL")
    if not parts:
        return None
    geometry = 'ST_AsGeoJSON(g,17,0)::jsonb' if detail else 'NULL::jsonb'
    return f'''
      WITH filtered AS MATERIALIZED ({' UNION ALL '.join(parts)}),
      variants AS MATERIALIZED (
        SELECT DISTINCT ON (building_key,ST_AsEWKB(g),source COLLATE "C",stamp COLLATE "C") *
        FROM filtered ORDER BY building_key,ST_AsEWKB(g),source COLLATE "C",stamp COLLATE "C" NULLS LAST
      ), checked AS MATERIALIZED (
        SELECT *, CASE WHEN ST_SRID(g)=expected AND NOT ST_IsEmpty(g) AND ST_IsValid(g)
          AND GeometryType(g) IN ('POINT','POLYGON','MULTIPOLYGON')
          AND ST_ZMin(g::box3d) BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308
          AND ST_ZMax(g::box3d) BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308
          AND ((expected=4326 AND ST_XMin(g::box3d)>=-180 AND ST_XMax(g::box3d)<=180
                                AND ST_YMin(g::box3d)>=-90 AND ST_YMax(g::box3d)<=90)
            OR (expected=3857 AND ST_XMin(g::box3d)>=-20037508.35 AND ST_XMax(g::box3d)<=20037508.35
                                AND ST_YMin(g::box3d)>=-20048966.11 AND ST_YMax(g::box3d)<=20048966.11))
          THEN CASE WHEN octet_length(ST_AsGeoJSON(g,17,0))<=:max_bytes THEN 'available' ELSE 'source_value_too_large' END
          ELSE 'invalid_geometry' END AS state
        FROM variants
      ), counts AS (SELECT building_key,count(DISTINCT ST_AsEWKB(g)) AS variants FROM variants GROUP BY building_key),
      ranked AS (
        SELECT *,row_number() OVER(PARTITION BY building_key ORDER BY
          CASE WHEN state='available' THEN 0 ELSE 1 END,
          CASE WHEN GeometryType(g) IN ('POLYGON','MULTIPOLYGON') THEN 0 ELSE 1 END,
          ST_AsEWKB(g),source COLLATE "C",stamp COLLATE "C" NULLS LAST) AS rn FROM checked
      )
      SELECT building_key,source,expected,state,stamp,variants,
        CASE WHEN state='available' THEN {geometry} END AS geometry,
        CASE WHEN state='available' THEN ST_X(ST_Transform(ST_PointOnSurface(g),4326)) END AS longitude,
        CASE WHEN state='available' THEN ST_Y(ST_Transform(ST_PointOnSurface(g),4326)) END AS latitude
      FROM ranked JOIN counts USING(building_key) WHERE rn=1
    '''


def _geometries(session, keys, main, meta, *, detail):
    sql = geometry_query(meta, detail=detail)
    if sql is None:
        return {}
    return {r['building_key']: dict(r) for r in session.execute(text(sql), {
        'main_keys': list(main), 'fallback_keys': [k for k in keys if k not in main], 'max_bytes': GEOMETRY_BYTES,
    }).mappings()}


def _text(value):
    if value is None or isinstance(value, str) and not value.strip():
        return None, 'missing', None
    if not isinstance(value, str):
        return None, 'invalid', 'invalid_source_type'
    if len(value) > SOURCE_TEXT_CHARS:
        return None, 'invalid', 'source_value_too_large'
    if any(ord(c) == 0 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        return None, 'invalid', 'invalid_source_text'
    return value, 'available', None


def _calendar(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?', value):
        return None
    try:
        return datetime.fromisoformat(value).date() if len(value) > 10 else date.fromisoformat(value)
    except ValueError:
        return None


def _timestamp(value):
    if value is None or value == '':
        return None, 'missing'
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)?', value):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None, 'invalid'
    else:
        return None, 'invalid'
    # These source columns explicitly declare UTC. Naive strings use that
    # declaration; certificate signed_at is never passed through this function.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc), 'known'


def _source(row, *, available=True):
    dt, state = _timestamp(row.get('modified_timestamp_utc') if row else None)
    return SourceV1(status='available' if row else 'missing' if available else 'source_unavailable',
                    upstream_modified_at=dt, timestamp_status=state)


def _normalized(value, kind):
    value, state, reason = _text(value) if isinstance(value, str) or value is None else (value, 'available', None)
    if state != 'available':
        return value, state, reason
    if kind == 'date':
        result = _calendar(value)
    elif kind in ('number', 'int', 'year'):
        result = nonnegative_number(value)
        if result is None and isinstance(value, str) and re.fullmatch(r'\+?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', value):
            parsed = float(value)
            result = parsed if math.isfinite(parsed) and parsed >= 0 else None
        if result is not None and kind in ('int', 'year'):
            result = int(result) if result.is_integer() else None
    elif kind == 'class':
        result = value.strip().upper() if isinstance(value, str) else None
        if result not in tuple('ABCDEFG'):
            result = None
    elif kind == 'bool':
        result = value if type(value) is bool else {'true': True, 'false': False}.get(value)
    else:
        result = value if isinstance(value, str) else None
    return (result, 'available', None) if result is not None else (None, 'invalid', 'invalid_source_value')


def _bundle(session, keys, *, detail):
    _snapshot(session)
    resolved_at = datetime.now(timezone.utc)
    meta = _metadata(session)
    main = _main(session, keys, meta['main'], detail=detail)
    buildings = _buildings(session, keys, meta['buildings'])
    ids = {r['facts'].get('permanent_building_identifier') for r in main.values()}
    ids.update(r.get('permanent_building_identifier') for r in buildings.values())
    ids.discard(None)
    conflicts = _identity_conflicts(session, ids)
    outcomes = {}
    for key in keys:
        m, b = main.get(key), buildings.get(key)
        if m is None and b is None:
            outcomes[key] = 'missing'
            continue
        identities = {v for v in (m['facts'].get('permanent_building_identifier') if m else None,
                                  b.get('permanent_building_identifier') if b else None) if v}
        outcomes[key] = 'ambiguous' if len(identities) > 1 or identities & conflicts or m and m['fact_variants'] != 1 else 'available'
    available_keys = [k for k, state in outcomes.items() if state == 'available']
    available_main = {k: v for k, v in main.items() if k in available_keys}
    addresses = _addresses(session, available_keys, [r['address_key'] for r in available_main.values() if r['address_key']], meta) if available_keys else {}
    geometry = _geometries(session, available_keys, available_main, meta, detail=detail) if available_keys else {}
    return meta, main, buildings, addresses, geometry, outcomes, resolved_at


def _public_address(main, address):
    # main owns its published address, including null. Never fill parts from a
    # different staging row. Source metadata is separate.
    return {c: _text((main or address or {}).get(c))[0] for c in ADDRESS}


def _location(g):
    if not g or g['state'] != 'available':
        return None
    try:
        return LocationV1(longitude=float(g['longitude']), latitude=float(g['latitude']))
    except (TypeError, ValueError):
        return None


def _summary(key, main, address, geometry):
    parts = _public_address(main, address)
    postal_invalid = parts['postal_code'] is not None and not re.fullmatch(r'[0-9]{5}', parts['postal_code'])
    if postal_invalid:
        parts['postal_code'] = None
    availability = {c: AvailabilityV1(status=_text((main or address or {}).get(c))[1], reason=_text((main or address or {}).get(c))[2]) for c in ADDRESS if c != 'address_key'}
    if postal_invalid:
        availability['postal_code'] = AvailabilityV1(status='invalid', reason='invalid_postal_code')
    availability['location'] = AvailabilityV1(status='available' if _location(geometry) else 'invalid' if geometry else 'missing', reason=geometry['state'] if geometry and geometry['state'] != 'available' else None)
    availability['main'] = AvailabilityV1(status='available' if main else 'missing')
    availability['source_observed_at'] = AvailabilityV1(status='missing', reason='unknown_source_freshness')
    return BuildingSummaryV1(building_key=key, location=_location(geometry), source_availability=availability,
        **{k: v for k, v in parts.items() if k != 'address_key'})


def resolve_building_summaries(session, keys: Sequence[str]) -> dict[str, SummaryResolutionV1]:
    if isinstance(keys, (str, bytes)):
        raise ValueError('Expected a sequence of building keys')
    distinct = list(dict.fromkeys(validate_key(k) for k in keys))
    if len(distinct) > 100:
        raise ValueError('At most 100 distinct building keys are supported')
    if not distinct:
        return {}
    try:
        meta, main, buildings, addresses, geometry, outcomes, _ = _bundle(session, distinct, detail=False)
        return {k: Resolution(state, _summary(k, main.get(k), addresses.get(k), geometry.get(k)) if state == 'available' else None)
                for k, state in outcomes.items()}
    except SQLAlchemyError:
        raise BuildingSourceUnavailable('Building source temporarily unavailable') from None


def load_code_catalog(session) -> CodeCatalog:
    """One bounded query; callers must already be in their safe read snapshot."""
    try:
        return _catalog(session)
    except SQLAlchemyError:
        raise CatalogUnavailable('Code catalog temporarily unavailable') from None


def _catalog(session):
    rows = session.execute(text('SELECT type,code,label_fi,label_sv,label_en FROM code_maps WHERE type=ANY(:types) LIMIT 10001'),
                           {'types': ['main_purpose', 'heating_method', 'heating_energy_source']}).all()
    codes = {kind: frozenset(r[1] for r in rows if r[0] == kind) for kind in ('main_purpose', 'heating_method', 'heating_energy_source')}
    if len(rows) > 10000 or any(not values for values in codes.values()):
        raise CatalogUnavailable('Code catalog temporarily unavailable')
    labels = {kind: {} for kind in codes}
    for kind, code, fi, sv, en in rows:
        labels[kind][code] = CodeLabelV1(label_fi=_text(fi)[0], label_sv=_text(sv)[0], label_en=_text(en)[0])
    return CodeCatalog(codes, labels)


def _certificates(session, identifier, meta):
    columns = meta.get('energy_certificates', set())
    if not {'certificate_id', 'building_identifier', 'signed_at'}.issubset(columns):
        return None, 0
    # Extract only consumption fields, never fetch or serialize the raw blob.
    source_keys = [SOURCE_PREFIX + suffix for suffix in CARRIERS.values()] + [
        f'{SOURCE_PREFIX}Muu / {i} / {part}' for i in range(3) for part in ('Nimi', 'Ostoenergia')]
    extracted = ('(SELECT jsonb_object_agg(k,source_row->k) FROM unnest(CAST(:source_keys AS text[])) AS k)' if 'source_row' in columns else 'NULL::jsonb')
    projection = ','.join(f'"{c}"' if c in columns else f'NULL AS "{c}"' for c in CERT_COLUMNS)
    rows = session.execute(text(f'''
        SELECT {projection},{extracted} AS source_row,count(*) OVER() AS history_count
        FROM energy_certificates WHERE building_identifier=:id
        ORDER BY signed_at DESC NULLS LAST,certificate_id COLLATE "C" DESC LIMIT 2
    '''), {'id': identifier, 'source_keys': source_keys}).mappings().all() if identifier else []
    return [dict(r) for r in rows], rows[0]['history_count'] if rows else 0


def _certificate(rows, history_count, main):
    if not rows:
        return None
    row = rows[0]
    values, availability = {}, {}
    mapping = {k: k for k in CERT_COLUMNS if k not in ('e_class', 'updated_at')}
    mapping['energy_class'] = 'e_class'
    mapping['previous_class'] = 'e_class'
    for name, source in mapping.items():
        raw = rows[1].get('e_class') if name == 'previous_class' and len(rows) > 1 else None if name == 'previous_class' else row.get(source)
        kind = 'class' if name in ('energy_class', 'previous_class') else 'date' if name in ('signed_at', 'valid_until', 'inspection_date') else 'int' if name == 'version' else 'number' if name == 'heated_net_area' else 'text'
        value, status, reason = _normalized(raw, kind)
        if name == 'ventilation_type_id' and value is not None:
            # Match the pinned publication policy's source-code TRIM, without
            # making personal input validation tolerant.
            value = value.strip()
        values[name] = value
        if name == 'previous_class' and len(rows) == 1:
            reason = 'no_older_imported_certificate'
        availability[name] = AvailabilityV1(status=status, reason=reason)
    code = values['ventilation_type_id']
    if code is not None and code not in VENTILATION_TYPES:
        availability['ventilation_type_id'] = AvailabilityV1(status='unsupported', reason='unsupported_source_code')
    labels = VENTILATION_TYPES.get(code)
    values['ventilation_type_label_fi'] = labels[1] if labels else None
    values['ventilation_type_label_sv'] = labels[2] if labels else None
    values['class_scale_year'] = values['version'] if values['version'] in (2013, 2018) else None
    availability['class_scale_year'] = AvailabilityV1(status='available' if values['class_scale_year'] else 'missing' if values['version'] is None else 'unsupported',
        reason='unsupported_class_scale' if values['version'] is not None and not values['class_scale_year'] else None)
    for lang in ('fi', 'sv'):
        availability[f'ventilation_type_label_{lang}'] = AvailabilityV1(status='available' if labels else 'unsupported' if code else 'missing')
    if values['heated_net_area'] == 0:
        availability['heated_net_area'] = AvailabilityV1(status='invalid', reason='nonpositive_certificate_denominator')
    imported, stamp_state = _timestamp(row.get('updated_at'))
    values['imported_at'] = imported if isinstance(row.get('updated_at'), datetime) and row['updated_at'].tzinfo else None
    values['history_count'] = history_count
    payload = consumption_payload(row)
    consumption = None
    if payload:
        data = json.loads(payload)
        for name in ('signed_at', 'valid_until'):
            data[name] = _calendar(data[name])
        try:
            consumption = CertificateConsumptionV1.model_validate(data)
        except ValidationError:
            pass
    if consumption is not None and (consumption.certificate_id != values['certificate_id'] or
                                    consumption.building_identifier != values['building_identifier']):
        consumption = None
    availability['consumption'] = AvailabilityV1(status='available' if consumption else 'invalid' if payload else 'missing',
        reason=None if consumption else 'no_usable_selected_certificate_consumption')
    values['consumption'] = consumption
    facts = main['facts'] if main else {}
    comparisons = [('energy_certificate_class', values['energy_class']),
                   ('energy_certificate_heated_net_area', values['heated_net_area']),
                   ('energy_certificate_ventilation_type_id', values['ventilation_type_id'])]
    compared = [(facts.get(k), v, k) for k, v in comparisons if main and k in facts]
    agree = all(_normalized(raw, 'number' if k.endswith('heated_net_area') else 'class' if k.endswith('_class') else 'text')[0] == val for raw, val, k in compared)
    values['publication_comparison'] = 'agrees' if compared and agree else 'differs' if compared else 'unknown'
    return CertificateV1(**values, field_availability=availability)


def _model(main, columns, purpose):
    facts = main['facts'] if main else {}
    values, availability = {}, {}
    for name, column, kind in (
        ('modeled_energy_class', 'modeled_energy_class', 'class'), ('modeled_energy_e_value', 'modeled_energy_e_value', 'number'),
        ('reason', 'modeled_energy_class_reason', 'text'), ('area_source', 'modeled_energy_class_area_source', 'text'),
        ('energy_class', 'energy_class', 'class'), ('is_energy_class_modeled', 'is_energy_class_modeled', 'bool'),
        ('energy_class_year', 'energy_class_year', 'int'),
    ):
        val, state, reason = _normalized(facts.get(column), kind)
        values[name] = val
        availability[name] = AvailabilityV1(status=state if main and column in columns else 'not_projected')
    scenarios = {}
    for p in PREFIXES:
        scenarios[p] = {}
        for m in MEASURES:
            scenarios[p][m] = {}
            excluded = purpose not in ('05', '06') or purpose == '06' and (p in ('wood', 'elecb') or m == 'aahp') or m == 'aahp' and p in ('awhp', 'gshp')
            for q in QUANTITIES:
                column = f'{p}_{m}_{q}'
                val, state, reason = _normalized(facts.get(column), 'number')
                if not main or column not in columns:
                    state, reason = 'not_projected', 'source_column_not_projected'
                elif excluded:
                    state, reason = 'unsupported', 'outside_reference_coverage'
                scenarios[p][m][q] = ScenarioQuantityV1(value=val, status=state, reason=reason)
    return ModelV1(**values, consumption_scenarios=scenarios, field_availability=availability)


def _published_certificate(main, columns):
    if not main:
        return None
    values, availability = {}, {}
    for name, source, kind in (
        ('energy_class', 'class', 'class'), ('previous_class', 'previous_class', 'class'),
        ('class_scale_year', 'class_year', 'int'), ('valid_until', 'valid_until', 'date'),
        ('heated_net_area', 'heated_net_area', 'number'), ('ventilation_type_id', 'ventilation_type_id', 'text'),
        ('ventilation_description_fi', 'ventilation_description_fi', 'text'),
        ('ventilation_description_sv', 'ventilation_description_sv', 'text'),
        ('recommendations_fi', 'recommendations_fi', 'text'), ('recommendations_sv', 'recommendations_sv', 'text'),
    ):
        column = 'energy_certificate_' + source
        value, state, reason = _normalized(main['facts'].get(column), kind)
        if name == 'class_scale_year' and value not in (None, 2013, 2018):
            value, state, reason = None, 'unsupported', 'unsupported_class_scale'
        values[name] = value
        availability[name] = AvailabilityV1(status=state if column in columns else 'not_projected', reason=reason)
    return PublishedCertificateV1(**values, field_availability=availability)


def resolve_building(session, building_key: str) -> Resolution[CanonicalBuildingV1]:
    key = validate_key(building_key)
    try:
        meta, mains, buildings, addresses, geometries, outcomes, resolved_at = _bundle(session, [key], detail=True)
        if outcomes[key] != 'available':
            return Resolution(outcomes[key])
        main, building, address, geom = mains.get(key), buildings.get(key), addresses.get(key), geometries.get(key)
        facts = main['facts'] if main else building or {}
        identifier = _text(facts.get('permanent_building_identifier'))[0]
        cert_rows, count = _certificates(session, identifier, meta)
        cert = _certificate(cert_rows, count, main)
        try:
            catalog = _catalog(session) if 'code_maps' in meta else None
        except CatalogUnavailable:
            catalog = None
        provenance, details = {}, {}
        public_address = _public_address(main, address)
        for leaf, definition in EDITABLE_FIELDS.items():
            origin = 'ryhti'
            source_root = 'main' if main else 'buildings'
            column = definition.source
            raw = facts.get(column)
            projected = column in meta.get(source_root, set())
            if leaf in ('address_fin', 'postal_code', 'postal_office_fin'):
                raw = (main or address or {}).get(leaf)
                source_root = 'main' if main else 'addresses'
                projected = leaf in meta.get(source_root, set())
            elif leaf == 'apartment_count':
                raw = (building or {}).get(leaf)
                source_root = 'buildings'
                projected = leaf in meta['buildings']
            elif leaf in ('heated_net_area', 'ventilation_type_id', 'ventilation_description'):
                origin = 'certificate'
                if cert:
                    source_root = 'energy_certificates'
                    if leaf == 'ventilation_description':
                        language = 'fi' if cert.ventilation_description_fi else 'sv'
                        column = f'ventilation_description_{language}'
                    raw = getattr(cert, column)
                    projected = column in meta.get('energy_certificates', set())
                else:
                    # Explicitly published certificate-derived detail fallback;
                    # no selected certificate identity is attributed to it.
                    source_root = 'main'
                    column = f'energy_certificate_{leaf}'
                    if leaf == 'ventilation_description':
                        language = 'fi' if facts.get('energy_certificate_ventilation_description_fi') else 'sv'
                        column += '_' + language
                    raw = facts.get(column)
                    projected = bool(main) and column in meta['main']
            kind = 'date' if leaf == 'construction_year' else definition.kind
            value, state, reason = _normalized(raw, kind)
            if leaf == 'construction_year':
                completion = value
                value = value.year if value else None
            if leaf in ('main_purpose', 'heating_method', 'heating_energy_source') and value is not None and catalog and value not in catalog.codes[leaf]:
                state, reason = 'unsupported', 'unsupported_source_code'
            if leaf == 'ventilation_type_id' and value is not None and value not in VENTILATION_TYPES:
                state, reason = 'unsupported', 'unsupported_source_code'
            if leaf == 'postal_code' and value is not None and not re.fullmatch(r'[0-9]{5}', value):
                value, state, reason = None, 'invalid', 'invalid_postal_code'
            if not projected:
                state = 'not_projected'
            details[leaf] = value
            provenance[f'details.{leaf}'] = FieldProvenanceV1(origin=origin, source=f'{source_root}.{column}', status=state, reason=reason,
                source_language=language if leaf == 'ventilation_description' else None)
        details['completion_date'] = completion
        provenance['details.completion_date'] = provenance['details.construction_year'].model_copy(deep=True)
        details['ventilation_category'] = ventilation_category(details['ventilation_type_id'], details['ventilation_description'])
        provenance['details.ventilation_category'] = FieldProvenanceV1(origin='derived', source='ventilation_policy_v1', status='available',
            dependencies=['details.ventilation_type_id', 'details.ventilation_description'])
        identity_values = {'permanent_building_identifier': _text(identifier)[0], 'address_key': public_address['address_key']}
        property_value, property_state, reason = _text(facts.get('property_identifier'))
        if property_value is not None and (not re.fullmatch(r'[0-9]{14}', property_value) or property_value == '0' * 14):
            property_value, property_state, reason = None, 'invalid', 'invalid_property_identifier'
        identity_values['property_identifier'] = property_value
        for name in identity_values:
            state = property_state if name == 'property_identifier' else 'available' if identity_values[name] else 'missing'
            provenance[f'identity.{name}'] = FieldProvenanceV1(origin='ryhti', source=('main' if main else 'buildings') + '.' + name,
                status=state, reason=reason if name == 'property_identifier' else None)
        source_address = address if address and address.get('address_key') == public_address['address_key'] else None
        source_polygon = {'modified_timestamp_utc': geom['stamp']} if geom and geom['source'] == 'polygons.polygon' else None
        sources = SourcesV1(main=SourceV1(status='available' if main else 'missing'), ryhti_building=_source(building),
            ryhti_address=_source(source_address, available='addresses' in meta),
            ryhti_polygon=SourceV1(status='not_projected', reason='main_geometry_has_no_source_polygon_key') if main else _source(source_polygon, available='polygons' in meta),
            certificate=SourceV1(status='source_unavailable' if cert_rows is None else 'available' if cert else 'missing',
                reason='certificate_relation_unavailable' if cert_rows is None else 'no_matching_imported_certificate' if not cert else None,
                imported_at=cert.imported_at if cert else None),
            code_catalog=SourceV1(status='available' if catalog else 'source_unavailable'))
        # Source modification belongs only to its own exact row, never main.
        if not main:
            for item in provenance.values():
                if item.source and item.source.startswith('buildings.'):
                    item.upstream_modified_at = sources.ryhti_building.upstream_modified_at
                elif item.source and item.source.startswith('addresses.'):
                    item.upstream_modified_at = sources.ryhti_address.upstream_modified_at
        geometry_status = AvailabilityV1(status='available' if _location(geom) else 'invalid' if geom else 'missing',
            reason=geom['state'] if geom and geom['state'] != 'available' else None)
        geometry = None
        if geom and geom['geometry'] is not None and geometry_status.status == 'available':
            try:
                geometry = GeometryV1(srid=geom['expected'], source=geom['source'], geometry=geom['geometry'])
            except ValidationError:
                geometry_status = AvailabilityV1(status='invalid', reason='invalid_geometry')
        availability = {k: AvailabilityV1(status=v.status, reason=v.reason) for k, v in provenance.items()}
        availability['geometry'] = geometry_status
        availability['source_observed_at'] = AvailabilityV1(status='missing', reason='unknown_source_freshness')
        labels = CodeLabelsV1(**{kind: (catalog.labels or {}).get(kind, {}).get(details[kind]) for kind in ('main_purpose', 'heating_method', 'heating_energy_source')}) if catalog else CodeLabelsV1()
        return Resolution('available', CanonicalBuildingV1(building_key=key, identity=IdentityV1(**identity_values),
            code_labels=labels, published_certificate=_published_certificate(main, meta['main']),
            details=DetailsV1(**details), geometry=geometry, location=_location(geom),
            selection=SelectionV1(address_variants=main['address_variants'] if main else address['variants'] if address else 0,
                geometry_variants=geom['variants'] if geom else 0, geometry_source=geom['source'] if geom else None,
                geometry_status=geometry_status), certificate=cert, model=_model(main, meta['main'], details['main_purpose']),
            sources=sources, resolved_at=resolved_at, field_provenance=provenance, source_availability=availability))
    except SQLAlchemyError:
        raise BuildingSourceUnavailable('Building source temporarily unavailable') from None
