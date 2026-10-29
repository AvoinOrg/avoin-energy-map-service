"""Actual source-adapter proof in a doctor-provisioned, exact-name disposable.

No dotenv, defaults, sandbox endpoints or state connection is used. The test
launcher supplies a clean environment. Only fixtures in this test DB may reset.
"""
from contextlib import contextmanager
import os
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from src.building_lookup import LookupFailure, lookup, read_main, _raw_query
from tests.test_building_lookup_contract import request

DB = 'energymap_f004_building_lookup_test'
PID = '0000000001'


@unittest.skipUnless(os.getenv('RUN_BUILDING_LOOKUP_DB_TESTS') == '1', 'requires guarded disposable lookup DB')
class LookupDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Check all destructive-fixture guards BEFORE importing connection URLs.
        forbidden = [k for k in os.environ if (k.endswith('DATABASE_URL') or k.startswith(('SANDBOX_', 'ENERGY_MAP_STATE_', 'ENERGY_MAP_AUTH_'))
                     or k in ('PGOPTIONS','PGSERVICE','PGSERVICEFILE','PGPASSFILE','PGHOSTADDR')) and os.environ[k]]
        if (os.getenv('PGHOST') != '127.0.0.1' or os.getenv('PGPORT') != '55432' or
                os.getenv('PGDATABASE') != DB or forbidden):
            raise RuntimeError('Refusing fixtures outside doctor-provisioned exact-name loopback database')
        descriptor_path = Path(os.getenv('F004_BUILDING_LOOKUP_TEST_FIXTURE', '/missing-fixture'))
        if (descriptor_path.is_symlink() or descriptor_path.stat().st_mode & 0o777 != 0o600):
            raise RuntimeError('Disposable fixture descriptor is unavailable')
        descriptor = json.loads(descriptor_path.read_text())
        if any(descriptor.get(k) != v for k,v in {'format':'f004.1-disposable-building-lookup-fixture-v1',
                'mode':'disposable','host':'127.0.0.1','port':55432,'database':DB}.items()):
            raise RuntimeError('Disposable fixture descriptor mismatch')
        from src.db.session import local_db_url
        cls.engine = create_engine(local_db_url(), hide_parameters=True, connect_args={'connect_timeout':2})
        cls.sessions = sessionmaker(bind=cls.engine, autoflush=False)
        with cls.engine.connect() as connection:
            actual = connection.execute(text('SELECT current_database()')).scalar_one()
            if actual != DB:
                raise RuntimeError('Disposable identity mismatch')

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        with self.engine.begin() as c:
            c.execute(text('DROP TABLE IF EXISTS public.main'))
            c.execute(text('CREATE TABLE public.main (building_key text, permanent_building_identifier varchar, '
                'property_identifier text, address_fin varchar, postal_code varchar, postal_office_fin varchar,'
                'point geometry, polygon geometry)'))
            c.execute(text('CREATE INDEX lookup_fixture_pid ON public.main(permanent_building_identifier)'))
            c.execute(text("INSERT INTO public.main(building_key,permanent_building_identifier) "
                           "SELECT 'ballast-'||n::text,'unrelated-'||n::text FROM generate_series(1,20000) n"))
            c.execute(text('CREATE TABLE IF NOT EXISTS public.lookup_private_sentinel (secret text)'))
            c.execute(text("TRUNCATE public.lookup_private_sentinel"))
            c.execute(text("INSERT INTO public.lookup_private_sentinel VALUES ('PRIVATE_STATE_SENTINEL')"))
            # Building-only identities deliberately excluded and reported.
            c.execute(text('CREATE TABLE IF NOT EXISTS public.buildings(building_key text,permanent_building_identifier text)'))
            c.execute(text('TRUNCATE public.buildings'))
            c.execute(text("INSERT INTO public.buildings VALUES ('building-only',:pid)"), {'pid':PID})
        self.insert('a')
        self.insert('b', point=True)
        self.analyze()

    def insert(self, key, *, pid=PID, prop='00012345678901', address='Mäkelänkatu 1', point=False):
        with self.engine.begin() as c:
            c.execute(text('INSERT INTO public.main VALUES (:key,:pid,:prop,:address,\'00550\',\'Helsinki\','
                'CASE WHEN :point THEN ST_SetSRID(ST_MakePoint(24,60),4326) ELSE NULL END,NULL)'),
                {'key':key,'pid':pid,'prop':prop,'address':address,'point':point})

    def analyze(self):
        with self.engine.begin() as c:
            c.execute(text('CLUSTER public.main USING lookup_fixture_pid'))
            c.execute(text('ANALYZE public.main'))

    def checksum(self):
        with self.engine.connect() as c:
            return c.execute(text("SELECT md5(string_agg(v,',' ORDER BY v)) FROM (SELECT row_to_json(m)::text v FROM public.main m) q")).scalar_one()

    @contextmanager
    def audit(self):
        statements = []
        def before(conn,cursor,statement,parameters,context,executemany):
            upper = statement.lstrip().upper()
            self.assertTrue(upper.startswith(('SET ','SELECT ','EXPLAIN ')), upper[:50])
            self.assertNotIn('lookup_private_sentinel',statement)
            self.assertNotIn('energy_user_',statement)
            statements.append((statement,parameters))
        def no_commit(_conn): raise AssertionError('lookup must not commit')
        event.listen(self.engine,'before_cursor_execute',before)
        event.listen(self.engine,'commit',no_commit)
        try: yield statements
        finally:
            event.remove(self.engine,'before_cursor_execute',before)
            event.remove(self.engine,'commit',no_commit)

    def page(self, **kwargs):
        return lookup(request(**kwargs),self.sessions)

    def failure(self, code, func, *args, **kwargs):
        with self.assertRaises(LookupFailure) as caught: func(*args,**kwargs)
        self.assertEqual(caught.exception.code,code)

    def test_real_indexed_read_exact_keys_duplicate_address_no_geometry_and_no_writes(self):
        self.insert('a')
        self.insert('a',address='Secondary 2')
        self.insert('a',point=True)
        self.insert('opaque/slash')
        self.analyze()
        checksum = self.checksum()
        with self.audit() as statements:
            page = self.page()
        self.assertEqual([c.building_key for c in page.candidates],['a','b','opaque/slash'])
        self.assertEqual(page.candidates[0].address_variant_count,2)
        self.assertEqual(page.candidates[0].geometry_availability,'present_unvalidated')
        self.assertEqual(page.candidates[2].geometry_availability,'missing_in_scope')
        self.assertFalse(page.coverage.scope_complete)
        self.assertTrue(page.coverage.enumeration_complete)
        self.assertEqual(self.checksum(),checksum)
        actual = [(sql,params) for sql,params in statements if sql.startswith('SELECT CASE')]
        self.assertEqual(len(actual),1)
        self.assertEqual(actual[0][1]['identifier'],PID)
        self.assertNotIn(PID,actual[0][0])
        self.assertNotIn('ST_As',actual[0][0])
        self.assertNotIn('ORDER BY',actual[0][0])
        self.assertFalse(any('FROM public.buildings' in sql for sql,_ in statements))

    def test_partial_empty_and_exact_no_numeric_or_fuzzy_fallback(self):
        for value in ('0000000002', '1', ' '+PID, PID+' ', "x' OR true --", '%'):
            page = self.page(value=value)
            self.assertEqual(page.outcome,'no_match_in_scope')
            self.assertEqual(page.candidates,[])

    def test_actual_http_adapter_read_and_canonical_selection_is_separate(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.building_lookup import source_sessions
        app.dependency_overrides[source_sessions] = lambda:self.sessions
        try:
            with TestClient(app) as client, self.audit():
                response = client.post('/energy-data/building-lookup',json={'schema_version':1,
                    'query':{'mode':'permanent_building_identifier','permanent_building_identifier':PID}})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertEqual([c['building_key'] for c in response.json()['candidates']],['a','b'])
            self.assertIsNone(response.json()['candidates'][0]['location'])
        finally:
            app.dependency_overrides.pop(source_sessions,None)

    def test_unreadable_source_is_not_a_false_empty(self):
        # PostgreSQL's existing monitoring role has catalog reads but no fixture
        # table grants. SET ROLE applies only inside this owned connection.
        from src import building_lookup as service
        original = service._metadata
        def restricted(session,deadline):
            session.execute(text('SET LOCAL ROLE pg_monitor'))
            return original(session,deadline)
        with patch('src.building_lookup._metadata',side_effect=restricted):
            self.failure('lookup_source_unavailable',self.page)

    def test_address_property_remain_unavailable_even_if_new_indexes_exist(self):
        with self.engine.begin() as c:
            c.execute(text('CREATE INDEX property_idx ON public.main(property_identifier)'))
            c.execute(text('CREATE INDEX address_idx ON public.main(address_fin)'))
        for mode,value in [('address','Mäkelänkatu 1'),('property_identifier','00012345678901')]:
            with self.audit() as statements:
                self.failure('lookup_capability_unavailable',lookup,request(mode,value),self.sessions)
            self.assertEqual(statements,[])

    def test_identity_conflict_source_key_and_source_text_failures(self):
        self.insert('a',prop='00012345678902')
        self.analyze()
        self.failure('lookup_identity_conflict',self.page)
        with self.engine.begin() as c:
            c.execute(text("DELETE FROM public.main WHERE building_key='a'"))
        self.insert(None)
        self.failure('lookup_source_contract_failure',self.page)
        with self.engine.begin() as c:
            c.execute(text('DELETE FROM public.main WHERE building_key IS NULL'))
        self.insert('c',address='x'*513)
        self.failure('lookup_source_contract_failure',self.page)

    def test_missing_index_and_invalid_column_types_never_scan(self):
        with self.engine.begin() as c: c.execute(text('DROP INDEX lookup_fixture_pid'))
        with self.audit() as statements:
            self.failure('lookup_capability_unavailable',self.page)
        self.assertFalse(any(sql.startswith('SELECT CASE') for sql,_ in statements))
        with self.engine.begin() as c:
            c.execute(text('ALTER TABLE public.main ALTER COLUMN building_key TYPE integer USING 1'))
        self.failure('lookup_source_contract_failure',self.page)

    def test_missing_required_column_and_relation_are_not_empty(self):
        with self.engine.begin() as c: c.execute(text('ALTER TABLE public.main DROP COLUMN postal_code'))
        self.failure('lookup_source_contract_failure',self.page)
        with self.engine.begin() as c: c.execute(text('DROP TABLE public.main'))
        self.failure('lookup_source_unavailable',self.page)

    def test_pages_insert_delete_address_geometry_identity_and_relation_changes(self):
        changes = ["INSERT INTO public.main(building_key,permanent_building_identifier) VALUES ('c','0000000001')",
                   "DELETE FROM public.main WHERE building_key='b'",
                   "UPDATE public.main SET address_fin='changed' WHERE building_key='b'",
                   "UPDATE public.main SET point=NULL WHERE building_key='b'",
                   "UPDATE public.main SET property_identifier='00012345678902' WHERE building_key='b'",
                   'ALTER TABLE public.buildings ADD COLUMN page_shape_change text']
        for change in changes:
            cursor = self.page(limit=1).page.next_cursor
            self.assertIsNotNone(cursor)
            with self.engine.begin() as c: c.execute(text(change))
            self.failure('lookup_page_invalidated',self.page,limit=1,cursor=cursor)
            # Reset just this test's fixture between independent mutation cases.
            with self.engine.begin() as c:
                c.execute(text('ALTER TABLE public.buildings DROP COLUMN IF EXISTS page_shape_change'))
                c.execute(text('DELETE FROM public.main WHERE permanent_building_identifier=:pid'),{'pid':PID})
            self.insert('a');self.insert('b',point=True);self.analyze()

    def test_one_request_keeps_repeatable_snapshot_during_refresh(self):
        original = _raw_query
        def refresh(columns):
            # Metadata already established the snapshot. Concurrent committed
            # change must become visible only on the next request.
            self.insert('c')
            return original(columns)
        with patch('src.building_lookup._raw_query',side_effect=refresh):
            page = self.page()
        self.assertEqual([c.building_key for c in page.candidates],['a','b'])
        self.assertEqual([c.building_key for c in self.page().candidates],['a','b','c'])

    def test_actual_readonly_transaction_rejects_write_and_rolls_back(self):
        from src import building_lookup as service
        original = service._metadata
        proved = []
        def checked(session, deadline):
            settings = session.execute(text("SELECT current_setting('transaction_read_only'),current_setting('transaction_isolation')")).one()
            self.assertEqual(tuple(settings),('on','repeatable read'))
            with session.begin_nested():
                with self.assertRaises(DBAPIError):
                    session.execute(text("INSERT INTO public.main(building_key) VALUES ('forbidden')"))
                # Nested context cannot commit an aborted savepoint.
                session.get_nested_transaction().rollback()
            proved.append(True)
            return original(session,deadline)
        checksum = self.checksum()
        with patch('src.building_lookup._metadata',side_effect=checked): self.page()
        self.assertEqual(proved,[True])
        self.assertEqual(self.checksum(),checksum)

    def test_bounded_lock_timeout_and_deadline_failures_are_sanitized(self):
        with self.engine.connect() as blocker:
            transaction = blocker.begin()
            blocker.execute(text('LOCK public.main IN ACCESS EXCLUSIVE MODE'))
            started = time.monotonic()
            self.failure('lookup_source_unavailable',self.page)
            self.assertLess(time.monotonic()-started,4)
            transaction.rollback()
        with self.sessions() as session:
            self.failure('lookup_source_unavailable',read_main,session,PID,time.monotonic()-1)

    def test_raw_duplicate_and_distinct_caps_do_not_truncate(self):
        with self.engine.begin() as c:
            c.execute(text('DELETE FROM public.main WHERE permanent_building_identifier=:pid'),{'pid':PID})
            c.execute(text("INSERT INTO public.main(building_key,permanent_building_identifier) SELECT 'same',:pid FROM generate_series(1,2048)"),{'pid':PID})
        self.analyze()
        self.assertEqual(self.page().page.total_candidates,1)
        self.insert('same',prop=None,address=None)
        self.analyze()
        self.failure('lookup_query_too_broad',self.page)
        with self.engine.begin() as c:
            c.execute(text('DELETE FROM public.main WHERE permanent_building_identifier=:pid'),{'pid':PID})
            c.execute(text("INSERT INTO public.main(building_key,permanent_building_identifier) SELECT 'key-'||n::text,:pid FROM generate_series(1,512) n"),{'pid':PID})
        self.analyze()
        self.assertEqual(self.page().page.total_candidates,512)
        self.insert('extra')
        self.analyze()
        self.failure('lookup_query_too_broad',self.page)
