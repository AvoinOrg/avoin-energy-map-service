import base64
import unittest
from unittest.mock import Mock, patch

from src.building_lookup import (
    LookupFailure, build_page, candidates_from_rows, decode_cursor, encode_cursor,
    lookup, read_main, _indexed_plan,
)
from tests.test_building_lookup_contract import request

SCOPE = 'a' * 64
NOW = 1_800_000_000


def row(key='000-key', **changes):
    return dict(building_key=key, permanent_building_identifier='0000000001',
        property_identifier='00012345678901', address_fin='Mäkelänkatu 1', postal_code='00550',
        postal_office_fin='Helsinki', geometry_present=False, oversized=False) | changes


class LookupTests(unittest.TestCase):
    def failure(self, code, func, *args, **kwargs):
        with self.assertRaises(LookupFailure) as caught:
            func(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_candidate_exact_keys_duplicates_multiplicity_no_geometry(self):
        keys = ['lookup', '.', '..', 'a/b', '0001', 'ä', 'a', 'a?b#c']
        rows = [row(k) for k in keys] + [row('a'), row('a', geometry_present=True), row('a', address_fin='B 2')]
        result = build_page(request(limit=50), rows, SCOPE, now=NOW)
        self.assertEqual([c.building_key for c in result.candidates], sorted(keys, key=lambda k:k.encode()))
        a = next(c for c in result.candidates if c.building_key == 'a')
        self.assertEqual(a.address_variant_count, 2)
        self.assertEqual(a.representative_address.address_fin, 'B 2')
        self.assertEqual(a.geometry_availability, 'present_unvalidated')
        self.assertIsNone(a.location)
        self.assertTrue(result.page.permanent_id_multiplicity)
        self.assertFalse(result.coverage.scope_complete)
        self.assertTrue(all(c.requires_canonical_confirmation for c in result.candidates))

    def test_whole_address_nulls_and_missing_geometry(self):
        rows = [row(address_fin=None, postal_code='00100', postal_office_fin='Z'),
                row(address_fin='A', postal_code=None, postal_office_fin=None)]
        candidate = candidates_from_rows(rows)[0]
        self.assertEqual(candidate.representative_address.postal_code, None)
        self.assertEqual(candidate.address_variant_count, 2)
        self.assertEqual(candidate.geometry_availability, 'missing_in_scope')
        self.assertEqual(candidates_from_rows([row(geometry_present=None)])[0].geometry_availability, 'unknown')
        candidate = candidates_from_rows([row(address_fin=None, postal_code=None, postal_office_fin=None)])[0]
        self.assertIsNone(candidate.representative_address)
        self.assertEqual(candidate.address_variant_count, 0)

    def test_conflicts_and_invalid_source_never_arbitrary_winner(self):
        for field, value in (('property_identifier','00012345678902'), ('permanent_building_identifier', 'other')):
            self.failure('lookup_identity_conflict', candidates_from_rows, [row(), row(**{field:value})])
        for key in (1, None, 'a' * 257, '\x00'):
            self.failure('lookup_source_contract_failure', candidates_from_rows, [row(key)])
        self.failure('lookup_source_contract_failure', candidates_from_rows, [row(oversized=True)])
        self.failure('lookup_source_contract_failure', candidates_from_rows, [row(address_fin=1)])
        c = candidates_from_rows([row(property_identifier='malformed')])[0]
        self.assertIsNone(c.property_identifier)
        self.assertEqual(c.property_identity_status, 'invalid')

    def test_partial_empty_is_not_global_no_match(self):
        page = build_page(request(), [], SCOPE, now=NOW)
        self.assertEqual(page.outcome, 'no_match_in_scope')
        self.assertEqual(page.page.total_candidates, 0)
        self.assertFalse(page.page.has_more)
        self.assertIsNone(page.page.next_cursor)
        self.assertEqual(page.source_version.status, 'unknown')

    def test_pages_no_missing_repeated_keys_and_fixed_expiry(self):
        rows = [row(str(i)) for i in range(5)]
        first = build_page(request(limit=2), rows, SCOPE, now=NOW)
        second = build_page(request(limit=2, cursor=first.page.next_cursor), list(reversed(rows)), SCOPE, now=NOW+10)
        third = build_page(request(limit=2, cursor=second.page.next_cursor), rows, SCOPE, now=NOW+20)
        self.assertEqual([c.building_key for p in (first,second,third) for c in p.candidates], ['0','1','2','3','4'])
        self.assertFalse(third.page.has_more)
        self.assertIsNone(third.page.next_cursor)
        self.assertEqual(decode_cursor(first.page.next_cursor).expires, decode_cursor(second.page.next_cursor).expires)
        self.assertFalse(build_page(request(limit=5), rows, SCOPE, now=NOW).page.has_more)

    def test_cursor_syntax_expiry_semantics_source_change(self):
        rows = [row('a'), row('b')]
        cursor = build_page(request(limit=1), rows, SCOPE, now=NOW).page.next_cursor
        for value in ('x', '!!!', base64.urlsafe_b64encode(b'{}').decode().rstrip('=')):
            self.failure('lookup_invalid_request', decode_cursor, value)
        for req, stamp in ((request(limit=2,cursor=cursor),NOW), (request(value='other',limit=1,cursor=cursor),NOW),
                           (request(limit=1,cursor=cursor),NOW+300), (request(limit=1,cursor=cursor),NOW-1)):
            self.failure('lookup_page_invalidated',build_page,req,rows,SCOPE,now=stamp)
        for changed in ([row('a')], [row('a'),row('c')], [row('a'),row('b',geometry_present=True)],
                        [row('a'),row('b',address_fin='changed')],
                        [row('a'),row('b',property_identifier='00012345678902')]):
            self.failure('lookup_page_invalidated',build_page,request(limit=1,cursor=cursor),changed,SCOPE,now=NOW)
        self.failure('lookup_page_invalidated',build_page,request(limit=1,cursor=cursor),rows,'b'*64,now=NOW)
        changed = decode_cursor(cursor).model_copy(update={'last_key':'missing'})
        self.failure('lookup_page_invalidated',build_page,request(limit=1,cursor=encode_cursor(changed)),rows,SCOPE,now=NOW)
        changed = decode_cursor(cursor).model_copy(update={'policy':'future-policy'})
        self.failure('lookup_page_invalidated',build_page,request(limit=1,cursor=encode_cursor(changed)),rows,SCOPE,now=NOW)
        changed = decode_cursor(cursor).model_copy(update={'version':2})
        self.failure('lookup_page_invalidated',build_page,request(limit=1,cursor=encode_cursor(changed)),rows,SCOPE,now=NOW)

    def test_raw_distinct_and_response_bounds(self):
        self.assertEqual(len(candidates_from_rows([row()] * 2048)),1)
        self.failure('lookup_query_too_broad',candidates_from_rows,[row()] * 2049)
        self.assertEqual(len(candidates_from_rows([row(str(i)) for i in range(512)])),512)
        self.failure('lookup_query_too_broad',candidates_from_rows,[row(str(i)) for i in range(513)])
        with patch('src.building_lookup.RESPONSE_BYTES', 1):
            self.failure('lookup_query_too_broad',build_page,request(),[row()],SCOPE,now=NOW)

    def test_unsupported_modes_and_bad_cursor_do_not_open_source(self):
        factory = Mock(side_effect=AssertionError('must not connect'))
        for mode,value in (('address','Mäkelänkatu 1'),('property_identifier','00012345678901')):
            self.failure('lookup_capability_unavailable',lookup,request(mode,value),factory)
        self.failure('lookup_invalid_request',lookup,request(cursor='x'),factory)
        factory.assert_not_called()

    def test_dirty_or_active_session_rejected_before_any_statement(self):
        for field in ('new','dirty','deleted','in_transaction'):
            session = Mock(new=[],dirty=[],deleted=[])
            session.in_transaction.return_value = field == 'in_transaction'
            if field != 'in_transaction': setattr(session,field,[object()])
            self.failure('lookup_source_unavailable',read_main,session,'0001',float('inf'))
            session.execute.assert_not_called()

    def test_only_early_cutoff_index_plan_is_supported(self):
        scan = {'Node Type':'Index Scan','Index Name':'pid','Relation Name':'main',
                'Index Cond':"permanent_building_identifier = 'x'"}
        plan = [{'Plan':{'Node Type':'Limit','Plans':[scan]}}]
        self.assertTrue(_indexed_plan(plan,{'pid'}))
        for node in ('Seq Scan','Bitmap Heap Scan','Sort','Aggregate'):
            self.assertFalse(_indexed_plan([{'Plan':{'Node Type':'Limit','Plans':[{**scan,'Node Type':node}]}}],{'pid'}))
