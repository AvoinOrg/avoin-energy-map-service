"""Bounded, SELECT-only Main-scope permanent-ID discovery.

Only this source path has reviewed real-source evidence. Other query modes
validate but fail explicitly before acquiring a source connection. Never call
canonical resolution here: candidate discovery and confirmation are separate.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import re
import time

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.building_contract import building_key
from src.building_lookup_contract import (
    BuildingLookupPageV1, BuildingLookupRequestV1, CANDIDATE_KEYS, CURSOR_SECONDS,
    LookupAddressV1, LookupCandidateV1, LookupCursorV1, LookupEvidenceV1,
    LookupPageInfoV1, LookupSourceVersionV1, POLICY, RAW_ROWS, RESPONSE_BYTES,
    bounded_text, main_coverage, property_id,
)

MESSAGES = {
    'lookup_capability_unavailable': (503, 'This lookup source capability is unavailable'),
    'lookup_source_unavailable': (503, 'Lookup source is temporarily unavailable'),
    'lookup_source_contract_failure': (503, 'Lookup source does not satisfy the supported contract'),
    'lookup_identity_conflict': (409, 'Lookup source identity is conflicting'),
    'lookup_page_invalidated': (409, 'Lookup page changed or expired; restart at page one'),
    'lookup_query_too_broad': (422, 'Lookup exceeds bounded enumeration or response limits'),
    'lookup_invalid_request': (422, 'Invalid lookup request'),
    'lookup_payload_too_large': (413, 'Lookup body exceeds 16 KiB'),
    'lookup_unsupported_media_type': (415, 'Lookup requires application/json'),
    'lookup_internal_error': (500, 'Lookup request failed'),
}


class LookupFailure(Exception):
    def __init__(self, code, *, mode=None, component=None):
        self.code = code
        self.status, self.message = MESSAGES[code]
        self.mode, self.component = mode, component
        super().__init__(self.message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def query_document(query):
    # Omitted optional filters remain omitted; explicit null is forbidden.
    return query.model_dump(exclude_none=True)


def decode_cursor(value):
    if value is None:
        return None
    try:
        if not 1 <= len(value) <= 2048 or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
            raise ValueError()
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        cursor = LookupCursorV1.model_validate_json(raw)
        if encode_cursor(cursor) != value:
            raise ValueError()
        return cursor
    except (ValueError, ValidationError, UnicodeError):
        raise LookupFailure('lookup_invalid_request') from None


def encode_cursor(cursor):
    return base64.urlsafe_b64encode(encoded(cursor.model_dump())).rstrip(b'=').decode('ascii')


def validate_navigation(request, now):
    cursor = decode_cursor(request.cursor)
    if cursor and (cursor.version != 1 or cursor.policy != POLICY or cursor.query_hash != fingerprint(query_document(request.query)) or
                   cursor.limit != request.limit or cursor.issued > now or cursor.expires <= now or
                   cursor.expires != cursor.issued + CURSOR_SECONDS):
        raise LookupFailure('lookup_page_invalidated', mode=request.query.mode)
    return cursor


# Fixed, public-source allowlist. No identifiers, relation names or DSNs from requests.
TEXT_FIELDS = {
    'building_key': 256, 'permanent_building_identifier': 256,
    'property_identifier': 256, 'address_fin': 512,
    'postal_code': 64, 'postal_office_fin': 512,
}


def _execute(session, sql, params, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LookupFailure('lookup_source_unavailable', component='main')
    milliseconds = max(1, min(3000, int(remaining * 1000)))
    # Server-owned integer, never request input. Apply before every source statement.
    session.execute(text(f"SET LOCAL statement_timeout = '{milliseconds}ms'"))
    result = session.execute(text(sql), params)
    if time.monotonic() >= deadline:
        raise LookupFailure('lookup_source_unavailable', component='main')
    return result


def _metadata(session, deadline):
    rows = _execute(session, """
        SELECT c.oid::bigint AS oid,c.relname,c.relkind,a.attname,
               t.typname,tn.nspname AS type_schema,a.atttypmod,a.attcollation::bigint AS collation,
               COALESCE(co.collisdeterministic,true) AS deterministic,
               has_table_privilege(c.oid,'SELECT') AS readable
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
        JOIN pg_catalog.pg_namespace tn ON tn.oid=t.typnamespace
        LEFT JOIN pg_catalog.pg_collation co ON co.oid=a.attcollation
        WHERE n.nspname='public' AND c.relname=ANY(:relations)
        ORDER BY c.relname,a.attnum
    """, {'relations': ['main', 'buildings', 'addresses', 'polygons']}, deadline).mappings().all()
    shape = [dict(row) for row in rows]
    columns = {r['attname']: r for r in rows if r['relname'] == 'main'}
    if not columns or not all(r['readable'] for r in columns.values()):
        raise LookupFailure('lookup_source_unavailable', component='main')
    if not all(name in columns and columns[name]['typname'] in ('text', 'varchar') and
               columns[name]['type_schema'] == 'pg_catalog' and columns[name]['deterministic']
               for name in TEXT_FIELDS) or any(r['relkind'] != 'r' for r in columns.values()):
        raise LookupFailure('lookup_source_contract_failure', component='main')
    for name in ('point', 'polygon'):
        if name in columns and (columns[name]['typname'] != 'geometry' or columns[name]['type_schema'] != 'public'):
            raise LookupFailure('lookup_source_contract_failure', component='main')
    indexes = _execute(session, """
        SELECT ci.relname AS name,i.indexrelid::bigint AS oid,pg_get_indexdef(i.indexrelid) AS definition
        FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid=i.indrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_class ci ON ci.oid=i.indexrelid
        JOIN pg_catalog.pg_am am ON am.oid=ci.relam
        JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum=i.indkey[0]
        JOIN pg_catalog.pg_opclass op ON op.oid=i.indclass[0]
        JOIN pg_catalog.pg_namespace onsp ON onsp.oid=op.opcnamespace
        WHERE n.nspname='public' AND c.relname='main' AND am.amname='btree'
          AND a.attname='permanent_building_identifier' AND i.indisvalid AND i.indisready
          AND i.indpred IS NULL AND i.indexprs IS NULL
          AND i.indcollation[0]=a.attcollation AND op.opcname='text_ops' AND onsp.nspname='pg_catalog'
        ORDER BY ci.relname
    """, {}, deadline).mappings().all()
    if not indexes:
        raise LookupFailure('lookup_capability_unavailable', component='main')
    return columns, fingerprint({'policy': POLICY, 'shape': shape, 'indexes': [dict(r) for r in indexes]}), {r['name'] for r in indexes}


def _raw_query(columns):
    # Length checks avoid fetching unbounded source text or geometry. Raw-row
    # cutoff precedes all sorting, joins, aggregation and deduplication.
    parts = [f'CASE WHEN octet_length("{name}") <= {bound} THEN "{name}" END AS "{name}"'
             for name, bound in TEXT_FIELDS.items()]
    parts.append('(' + ' OR '.join(f'COALESCE(octet_length("{name}") > {bound},false)'
                                  for name, bound in TEXT_FIELDS.items()) + ') AS oversized')
    geometries = [f'"{name}" IS NOT NULL' for name in ('point', 'polygon') if name in columns]
    parts.append(('(' + ' OR '.join(geometries) + ')' if geometries else 'NULL::boolean') + ' AS geometry_present')
    return ('SELECT ' + ','.join(parts) + ' FROM public.main '
            'WHERE permanent_building_identifier = :identifier LIMIT :raw_limit')


def _indexed_plan(plan, indexes):
    # Bitmap creation, sort, aggregate and sequential scans can do unbounded
    # work before LIMIT. Only a direct index range under Limit is supported.
    node = plan[0]['Plan']
    if node.get('Node Type') != 'Limit' or len(node.get('Plans', [])) != 1:
        return False
    scan = node['Plans'][0]
    return (scan.get('Node Type') in ('Index Scan', 'Index Only Scan') and
            scan.get('Index Name') in indexes and scan.get('Relation Name') == 'main' and
            'permanent_building_identifier' in scan.get('Index Cond', '') and not scan.get('Plans'))


def read_main(session, identifier, deadline):
    """Own a fresh safe snapshot and roll it back, including failure paths."""
    if session.new or session.dirty or session.deleted or session.in_transaction():
        raise LookupFailure('lookup_source_unavailable', component='main')
    try:
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        session.execute(text("SET LOCAL lock_timeout = '1s'"))
        session.execute(text("SET LOCAL search_path = pg_catalog, public"))
        columns, scope, indexes = _metadata(session, deadline)
        query = _raw_query(columns)
        params = {'identifier': identifier, 'raw_limit': RAW_ROWS + 1}
        plan = _execute(session, 'EXPLAIN (FORMAT JSON) ' + query, params, deadline).scalar_one()
        if not _indexed_plan(plan, indexes):
            raise LookupFailure('lookup_capability_unavailable', component='main')
        rows = _execute(session, query, params, deadline).mappings().all()
        if len(rows) > RAW_ROWS:
            raise LookupFailure('lookup_query_too_broad')
        return rows, scope
    except SQLAlchemyError:
        # Never attach SQL/parameters/DSN to errors or logs.
        raise LookupFailure('lookup_source_unavailable', component='main') from None
    finally:
        session.rollback()


def _source_text(value, maximum):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return bounded_text(value, maximum)


def candidates_from_rows(rows):
    if len(rows) > RAW_ROWS:
        raise LookupFailure('lookup_query_too_broad')
    groups = defaultdict(list)
    try:
        for row in rows:
            if row['oversized']:
                raise LookupFailure('lookup_source_contract_failure', component='main')
            groups[building_key(row['building_key'])].append(row)
        if len(groups) > CANDIDATE_KEYS:
            raise LookupFailure('lookup_query_too_broad')
        result = []
        for key in sorted(groups, key=lambda k: k.encode('utf-8')):
            group = groups[key]
            identities = [{_source_text(r[name], 256) for r in group} - {None}
                          for name in ('permanent_building_identifier', 'property_identifier')]
            if any(len(values) > 1 for values in identities):
                raise LookupFailure('lookup_identity_conflict', component='main')
            permanent, prop = [next(iter(values), None) for values in identities]
            prop_status = 'available' if prop else 'missing'
            if prop:
                try:
                    property_id(prop)
                except ValueError:
                    prop, prop_status = None, 'invalid'
            addresses = {tuple(_source_text(r[name], TEXT_FIELDS[name]) for name in
                               ('address_fin', 'postal_code', 'postal_office_fin')) for r in group}
            addresses.discard((None, None, None))
            ordered = sorted(addresses, key=lambda a: tuple((s is None, (s or '').encode('utf-8')) for s in a))
            address = (LookupAddressV1(address_fin=ordered[0][0], postal_code=ordered[0][1],
                        postal_office_fin=ordered[0][2], municipality_number=None) if ordered else None)
            geometry = [r['geometry_present'] for r in group]
            state = ('present_unvalidated' if any(v is True for v in geometry) else
                     'unknown' if any(v is None for v in geometry) else 'missing_in_scope')
            result.append(LookupCandidateV1(building_key=key, permanent_building_identifier=permanent,
                property_identifier=prop, permanent_identity_status='available' if permanent else 'missing',
                property_identity_status=prop_status, representative_address=address,
                address_variant_count=len(addresses), evidence=[LookupEvidenceV1()], geometry_availability=state))
        return result
    except LookupFailure:
        raise
    except (ValueError, TypeError, ValidationError, KeyError):
        raise LookupFailure('lookup_source_contract_failure', component='main') from None


def build_page(request, rows, scope, *, now=None, cursor=None):
    now = int(time.time()) if now is None else now
    cursor = validate_navigation(request, now) if cursor is None else cursor
    candidates = candidates_from_rows(rows)
    digest = fingerprint([c.model_dump(mode='json') for c in candidates])
    start = 0
    if cursor:
        keys = [c.building_key for c in candidates]
        if cursor.scope_hash != scope or cursor.candidates_hash != digest or cursor.last_key not in keys:
            raise LookupFailure('lookup_page_invalidated', mode=request.query.mode)
        start = keys.index(cursor.last_key) + 1
    selected = candidates[start:start + request.limit]
    more = start + len(selected) < len(candidates)
    next_cursor = None
    if more:
        next_cursor = encode_cursor(LookupCursorV1(version=1, policy=POLICY,
            query_hash=fingerprint(query_document(request.query)), scope_hash=scope, candidates_hash=digest,
            last_key=selected[-1].building_key, limit=request.limit,
            issued=cursor.issued if cursor else now, expires=cursor.expires if cursor else now + CURSOR_SECONDS))
    result = BuildingLookupPageV1(query=request.query, candidates=selected,
        outcome='candidates' if candidates else 'no_match_in_scope', coverage=main_coverage(),
        page=LookupPageInfoV1(total_candidates=len(candidates), has_more=more, next_cursor=next_cursor,
                             permanent_id_multiplicity=len(candidates) > 1),
        observed_at=datetime.fromtimestamp(now, timezone.utc), source_version=LookupSourceVersionV1())
    # Count actual UTF-8 serialization, including cursor and coverage, before success.
    if len(result.model_dump_json(exclude_none=False).encode('utf-8')) > RESPONSE_BYTES:
        raise LookupFailure('lookup_query_too_broad')
    return result


def lookup(request: BuildingLookupRequestV1, session_factory):
    deadline = time.monotonic() + 8
    now = int(time.time())
    cursor = validate_navigation(request, now)  # no source access on malformed navigation
    if request.query.mode != 'permanent_building_identifier':
        raise LookupFailure('lookup_capability_unavailable', mode=request.query.mode)
    try:
        with session_factory() as session:
            rows, scope = read_main(session, request.query.permanent_building_identifier, deadline)
        page = build_page(request, rows, scope, now=now, cursor=cursor)
        if time.monotonic() >= deadline:
            raise LookupFailure('lookup_source_unavailable', component='main')
        return page
    except SQLAlchemyError:
        raise LookupFailure('lookup_source_unavailable', component='main') from None
