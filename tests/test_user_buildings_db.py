"""Real owner-state acceptance. Doctor owns provisioning and lifecycle.

Never sources an env file: the doctor injects explicit DSNs in a clean process.
All DDL/DML below is guarded by exact endpoint AND connected database identity.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from src.api.main import app
from src.api.auth import get_introspector, ZitadelIntrospector, AuthenticatedPrincipal
from src.api.buildings import building_session
from src.api.areas import area_session
from src.building_resolver import resolve_building, resolve_building_summaries, BuildingSourceUnavailable
from src.building_contract import personal_document
from src.db.state import state_engine, state_ready, state_session
from src.db.user_building_models import UserBuilding as Row
from src import user_buildings as service
from src.user_building_contract import decode_cursor
from src.geoserver.publish_area_aggregates import decode_mvt
from tests.test_auth import claims, settings, ISSUER

DATABASE = 'energymap_f001_owner_saved_buildings_test'
KEY = '000-owner-opaque/key'
PATH = '/energy-data/user/buildings/' + KEY
TABLE = 'energymap_state.energymap_user_buildings'


def guarded_urls():
    if os.getenv('RUN_OWNER_BUILDINGS_DB_TESTS') != '1' or os.getenv('DATABASE_URL'):
        raise RuntimeError('Explicit isolated owner fixture opt-in required')
    urls = [os.environ[k] for k in ('OWNER_TEST_ADMIN_URL', 'OWNER_TEST_READER_URL', 'ENERGY_MAP_STATE_DATABASE_URL')]
    for raw in urls:
        url = make_url(raw)
        if (url.database != DATABASE or url.host != '127.0.0.1' or url.port != 55432 or url.query or
                url.drivername not in ('postgresql', 'postgresql+psycopg2')):
            raise RuntimeError('Refusing owner fixtures outside exact disposable endpoint')
    if len({make_url(raw).username for raw in urls}) != 3:
        raise RuntimeError('Separate fixture principals required')
    return urls


@unittest.skipUnless(os.getenv('RUN_OWNER_BUILDINGS_DB_TESTS') == '1', 'requires dedicated disposable PostGIS and roles')
class OwnerDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        admin, reader, _ = guarded_urls()
        cls.admin_engine = create_engine(admin, hide_parameters=True, connect_args={'options':'-c default_transaction_read_only=off'})
        cls.reader_engine = create_engine(reader, hide_parameters=True, connect_args={'options':'-c default_transaction_read_only=on'})
        cls.Admin = sessionmaker(cls.admin_engine)
        cls.Reader = sessionmaker(cls.reader_engine)
        for engine in (cls.admin_engine, cls.reader_engine, state_engine()):
            with engine.connect() as connection:
                if connection.execute(text('SELECT current_database()')).scalar_one() != DATABASE:
                    raise RuntimeError('Connected database guard failed')
        cls.addClassCleanup(cls.admin_engine.dispose)
        cls.addClassCleanup(cls.reader_engine.dispose)

    def setUp(self):
        with self.Admin.begin() as session:
            session.execute(text(f'TRUNCATE energymap_state.energymap_user_building_shares, {TABLE}'))
            session.execute(text('TRUNCATE buildings,addresses,polygons,energy_certificates,code_maps'))
            session.execute(text("INSERT INTO buildings(building_key,permanent_building_identifier,property_identifier,completion_date,main_purpose,heating_method,heating_energy_source,floor_area,total_area,gross_floor_area,volume,number_of_storeys,apartment_count,point) VALUES (:key,'0000000001','00012345678901','1998-01-01','05','01','04',100,110,120,300,2,0,ST_SetSRID(ST_MakePoint(24,60),4326))"),{'key':KEY})
            session.execute(text("INSERT INTO addresses(address_key,building_key,address_fin,postal_code,postal_office_fin) VALUES ('a',:key,'A street 1','00100','City'),('b',:key,'B street 2','00100','City')"),{'key':KEY})
            session.execute(text("INSERT INTO polygons(polygon_key,building_key,polygon) VALUES ('p',:key,ST_Multi(ST_MakeEnvelope(24,60,24.001,60.001,4326)))"),{'key':KEY})
            session.execute(text("INSERT INTO energy_certificates(certificate_id,building_identifier,version,e_class,signed_at,valid_until,heated_net_area,source_row) VALUES ('1','0000000001',2018,'B','2026-01-01','2030-01-01',90,'{}')"))
            for kind, codes in {'main_purpose':('05','06'),'heating_method':('01','03'),'heating_energy_source':('01','04','09')}.items():
                for code in codes:
                    session.execute(text('INSERT INTO code_maps(type,code) VALUES(:t,:c)'),{'t':kind,'c':code})
        self.merge()
        self.patches = [patch('src.api.user_buildings.SessionLocal',self.Reader),
                        patch('src.api.main.SessionLocal',self.Reader)]
        for mocked in self.patches: mocked.start(); self.addCleanup(mocked.stop)
        app.dependency_overrides[building_session] = self.source_dependency
        app.dependency_overrides[area_session] = self.source_dependency
        self.provider_reply = lambda request: self.provider(request)
        def introspector():
            # Test seam chooses configured issuer, not an override of principal.
            return ZitadelIntrospector(settings(issuer=self.issuer), transport=httpx.MockTransport(lambda r:self.provider_reply(r)), clock=lambda:1000)
        self.issuer = ISSUER
        app.dependency_overrides[get_introspector] = introspector
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app); self.addCleanup(self.client.close)

    def source_dependency(self):
        with self.Reader() as session: yield session

    def provider(self, request):
        from urllib.parse import parse_qs
        token = parse_qs(request.content.decode())['token'][0]
        return httpx.Response(200,json={**claims(), 'iss':self.issuer,'sub':token.split('.')[0]})

    def merge(self):
        # Same production merge/augmentation code, fixture-only connection seam.
        from src.etl.merge import merge_table
        with patch('src.etl.merge.SessionLocal',self.Admin), redirect_stdout(io.StringIO()): merge_table()

    def auth(self, subject='A', **extra):
        return {'Authorization':'Bearer '+subject, **extra}

    def create(self, data=None, subject='A', key=KEY):
        return self.client.put('/energy-data/user/buildings/'+key,json=data or {},headers=self.auth(subject, **{'If-None-Match':'*'}))

    def save(self, data=None):
        result=self.create(data)
        self.assertEqual(result.status_code,201,result.text)
        return result

    def read(self,subject='A'):
        return self.client.get(PATH,headers=self.auth(subject))

    def count(self):
        with self.Admin() as session:return session.execute(text(f'SELECT count(*) FROM {TABLE}')).scalar_one()

    def checksum(self):
        with self.Admin() as session:
            return {name:session.execute(text(f"SELECT md5(COALESCE(string_agg(v,',' ORDER BY v),'')) FROM (SELECT to_jsonb(t)::text v FROM {name} t) q")).scalar_one() for name in ('main','buildings','addresses','polygons','energy_certificates','code_maps')}

    def stored(self):
        with self.Admin() as session:return [dict(r) for r in session.execute(text(f'SELECT * FROM {TABLE} ORDER BY saved_id')).mappings()]

    def test_owner_scoping_relogin_two_subjects_and_different_issuer(self):
        self.assertEqual(self.client.get('/energy-data/user/buildings',headers=self.auth()).json()['items'],[])
        self.assertEqual(self.count(),0)
        a=self.save({'display_name':'Private A','overrides':{'floor_area':0,'total_area':None}})
        self.assertEqual(self.read('A.new-login-token').json()['saved_id'],a.json()['saved_id'])
        for method in ('get','patch','delete'):
            kwargs={'json':{}} if method=='patch' else {}
            result=getattr(self.client,method)(PATH,headers=self.auth('B',**{'If-Match':a.headers['etag']}),**kwargs)
            self.assertEqual(result.status_code,404,result.text)
        b=self.create({'display_name':'Private B'},'B');self.assertEqual(b.status_code,201,b.text)
        self.issuer='https://second-issuer.example.invalid'
        self.assertEqual(self.read().status_code,404)
        other=self.create({'display_name':'Other issuer'});self.assertEqual(other.status_code,201,other.text)
        for issuer,subject,name in ((ISSUER,'A','Private A'),(ISSUER,'B','Private B'),(self.issuer,'A','Other issuer')):
            self.issuer=issuer
            page=self.client.get('/energy-data/user/buildings',headers=self.auth(subject)).json()
            self.assertEqual([r['display_name'] for r in page['items']],[name])
            self.assertNotIn('owner_',json.dumps(page))
        self.assertEqual(self.count(),3)

    def test_typed_roundtrip_all_leaves_null_zero_patch_and_full_replace(self):
        source_before=self.checksum()
        values={'address_fin':'Personal street','postal_code':'00100','postal_office_fin':'City','construction_year':2000,'main_purpose':'05','heating_method':'01','heating_energy_source':'04','number_of_storeys':0,'apartment_count':0,'floor_area':0,'total_area':100,'gross_floor_area':120,'heated_net_area':90,'volume':300,'ventilation_type_id':'0','ventilation_description':'Plain <text>'}
        response=self.save({'display_name':'Mine','overrides':values,'scenario_inputs':{'resident_count':2}})
        self.assertEqual(response.json()['overrides'],values)
        self.assertEqual(self.stored()[0]['overrides'],values)
        created=response.json()['created_at']
        response=self.client.patch(PATH,json={'set':{'overrides':values,'scenario_inputs':{'resident_count':2}}},headers=self.auth(**{'If-Match':response.headers['etag']}))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['overrides'],values)
        self.assertEqual(self.stored()[0]['overrides'],values)
        self.assertEqual(response.json()['display_name'],'Mine')
        self.assertEqual(response.json()['created_at'],created)
        self.assertEqual(response.json()['revision'],2)
        updated=self.client.patch(PATH,json={'display_name':None,'set':{'overrides':{'total_area':None},'scenario_inputs':{'resident_count':None}},'unset':['overrides.floor_area']},headers=self.auth(**{'If-Match':response.headers['etag']}))
        self.assertEqual(updated.status_code,200,updated.text)
        data=updated.json();self.assertIsNone(data['display_name']);self.assertEqual(data['revision'],3)
        self.assertNotIn('floor_area',data['overrides']);self.assertIsNone(data['overrides']['total_area'])
        self.assertEqual(data['effective']['details']['floor_area'],100)
        replaced=self.client.put(PATH,json={},headers=self.auth(**{'If-Match':updated.headers['etag']}))
        self.assertEqual(replaced.status_code,200,replaced.text)
        self.assertEqual(replaced.json()['overrides'],{});self.assertEqual(replaced.json()['scenario_inputs'],{})
        self.assertEqual(replaced.json()['revision'],4);self.assertEqual(self.count(),1)
        self.assertEqual(self.checksum(),source_before)

    def test_preconditions_delete_recreate_validation_and_atomic_rollback(self):
        self.assertEqual(self.client.put(PATH,json={},headers=self.auth()).status_code,428)
        self.assertEqual(self.client.put(PATH,json={},headers=self.auth(**{'If-Match':'"old"'})).status_code,412)
        response=self.save();tag=response.headers['etag'];before=self.stored()
        cases=[({'owner_subject':'forged'},422),({'overrides':{'unknown_column':'SQL SECRET'}},422),({'overrides':{'main_purpose':'99'}},422),({'overrides':{'floor_area':True}},422),({'display_name':'x'*121},422)]
        for body,status in cases:
            result=self.client.put(PATH,json=body,headers=self.auth(**{'If-Match':tag}))
            self.assertEqual(result.status_code,status,result.text)
            self.assertEqual(self.stored(),before)
        self.assertEqual(self.create().status_code,412)
        for method in ('put','patch','delete'):
            kwargs={'json':{}} if method!='delete' else {}
            self.assertEqual(getattr(self.client,method)(PATH,headers=self.auth(),**kwargs).status_code,428)
            self.assertEqual(getattr(self.client,method)(PATH,headers=self.auth(**{'If-Match':'"stale"'}),**kwargs).status_code,412)
        with patch('src.api.user_buildings.rendered',side_effect=RuntimeError('SECRET')):
            failed=self.client.patch(PATH,json={'display_name':'must rollback'},headers=self.auth(**{'If-Match':tag}))
        self.assertEqual(failed.status_code,500);self.assertNotIn('SECRET',failed.text)
        self.assertEqual(self.stored(),before)
        deleted=self.client.delete(PATH,headers=self.auth(**{'If-Match':tag}));self.assertEqual(deleted.status_code,204)
        self.assertEqual(deleted.headers['cache-control'],'no-store')
        recreated=self.save();self.assertNotEqual(tag,recreated.headers['etag'])
        self.assertEqual(self.client.patch(PATH,json={},headers=self.auth(**{'If-Match':tag})).status_code,412)

    def test_authentication_failures_do_not_write_database(self):
        self.save();before=self.stored();source=self.checksum()
        for change,status in (({'active':False},401),({'exp':999},401),({'aud':'wrong'},401),({'iss':'wrong'},401)):
            self.provider_reply=lambda request,change=change:httpx.Response(200,json={**claims(),**change})
            result=self.create(subject='B')
            self.assertEqual(result.status_code,status,result.text)
            self.assertEqual(self.stored(),before)
        self.provider_reply=lambda request:httpx.Response(503,text='SECRET')
        self.assertEqual(self.create(subject='B').status_code,503)
        self.assertEqual(self.stored(),before);self.assertEqual(self.checksum(),source)

    def test_summary_adapter_empty_reads_and_readiness_do_not_write(self):
        before=self.checksum()
        with self.Reader() as source:
            base=resolve_building(source,KEY).value
            summary=resolve_building_summaries(source,[KEY])[KEY].value
        captured=service.capture_summary(base,datetime.now(timezone.utc)).model_dump(mode='json');captured.pop('captured_at')
        self.assertEqual(captured,summary.model_dump(mode='json'))
        self.assertEqual(self.read().status_code,404)
        self.assertEqual(self.client.get('/energy-data/buildings/'+KEY).status_code,200)
        self.assertTrue(state_ready())
        self.assertEqual(self.client.get('/readyz/state').status_code,200)
        self.assertEqual(self.client.get('/energy-data/user/buildings',headers=self.auth()).json()['items'],[])
        self.assertEqual(self.count(),0);self.assertEqual(before,self.checksum())

    def test_source_loss_outage_ambiguity_and_catalog_rules(self):
        first=self.save({'overrides':{'main_purpose':'05','floor_area':0}})
        before=self.stored()
        with self.Admin.begin() as session:
            session.execute(text('DELETE FROM main WHERE building_key=:k'),{'k':KEY})
            session.execute(text('DELETE FROM buildings WHERE building_key=:k'),{'k':KEY})
            session.execute(text("DELETE FROM code_maps WHERE type='main_purpose' AND code='05'"))
        read=self.read();self.assertEqual(read.status_code,200,read.text)
        self.assertEqual(read.json()['base_unavailable_reason'],'missing')
        self.assertEqual(read.json()['last_known_summary']['address_fin'],'A street 1')
        self.assertIsNone(read.json()['effective']['details']['address_fin'])
        self.assertEqual(self.stored(),before)
        page=self.client.get('/energy-data/user/buildings',headers=self.auth()).json()['items'][0]
        self.assertIsNone(page['current_summary']);self.assertEqual(page['base_unavailable_reason'],'missing')
        self.assertEqual(self.create(subject='B').status_code,404)
        patched=self.client.patch(PATH,json={'display_name':'Still mine'},headers=self.auth(**{'If-Match':first.headers['etag']}))
        self.assertEqual(patched.status_code,200,patched.text);self.assertEqual(patched.json()['overrides']['main_purpose'],'05')
        invalid=self.client.put(PATH,json={'overrides':{'main_purpose':'05'}},headers=self.auth(**{'If-Match':patched.headers['etag']}))
        self.assertEqual(invalid.status_code,422)
        with patch('src.api.user_buildings.resolve_building',side_effect=BuildingSourceUnavailable):
            read=self.read();self.assertEqual(read.json()['base_unavailable_reason'],'source_unavailable')
            reset=self.client.patch(PATH,json={'unset':['overrides.main_purpose','overrides.floor_area']},headers=self.auth(**{'If-Match':patched.headers['etag']}))
            self.assertEqual(reset.status_code,200,reset.text)
            self.assertEqual(reset.json()['overrides'],{})
            self.assertEqual(self.client.delete(PATH,headers=self.auth(**{'If-Match':reset.headers['etag']})).status_code,204)

    def test_real_ambiguous_and_unavailable_source_existing_writes_and_catalog_outage(self):
        saved=self.save();snapshot=saved.json()['last_known_summary']
        with self.Admin.begin() as session:
            session.execute(text("UPDATE main SET permanent_building_identifier='different' WHERE address_key='b'"))
        self.assertEqual(self.read().json()['base_unavailable_reason'],'ambiguous')
        self.assertEqual(self.create(subject='B').status_code,409)
        replaced=self.client.put(PATH,json={'display_name':'During ambiguity'},headers=self.auth(**{'If-Match':saved.headers['etag']}))
        self.assertEqual(replaced.status_code,200,replaced.text)
        self.assertEqual(replaced.json()['last_known_summary'],snapshot)
        with self.Admin.begin() as session:session.execute(text('ALTER TABLE main RENAME TO fixture_unavailable_main'))
        try:
            self.assertEqual(self.read().json()['base_unavailable_reason'],'source_unavailable')
            self.assertEqual(self.create(subject='B').status_code,503)
            result=self.client.patch(PATH,json={'set':{'overrides':{'floor_area':0}}},headers=self.auth(**{'If-Match':replaced.headers['etag']}))
            self.assertEqual(result.status_code,200,result.text)
            with self.Admin.begin() as session:session.execute(text('ALTER TABLE code_maps RENAME TO fixture_unavailable_codes'))
            try:
                before=self.stored()
                failure=self.client.patch(PATH,json={'set':{'overrides':{'main_purpose':'05'}}},headers=self.auth(**{'If-Match':result.headers['etag']}))
                self.assertEqual(failure.status_code,503,failure.text);self.assertEqual(self.stored(),before)
                reset=self.client.patch(PATH,json={'unset':['overrides.floor_area']},headers=self.auth(**{'If-Match':result.headers['etag']}))
                self.assertEqual(reset.status_code,200,reset.text)
            finally:
                with self.Admin.begin() as session:session.execute(text('ALTER TABLE fixture_unavailable_codes RENAME TO code_maps'))
        finally:
            with self.Admin.begin() as session:session.execute(text('ALTER TABLE fixture_unavailable_main RENAME TO main'))

    def test_long_principal_and_update_delete_sql_exact_owner_predicates(self):
        subject='fixture-subject-'+'x'*6000
        self.provider_reply=lambda request:httpx.Response(200,json=claims()|{'sub':subject})
        saved=self.save();queries=[]
        def before(conn,cursor,statement,parameters,context,many):
            if statement.startswith(('UPDATE ','DELETE ')):queries.append(statement)
        event.listen(state_engine(),'before_cursor_execute',before)
        try:
            result=self.client.patch(PATH,json={'display_name':'Long principal'},headers=self.auth(**{'If-Match':saved.headers['etag']}))
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(self.client.delete(PATH,headers=self.auth(**{'If-Match':result.headers['etag']})).status_code,204)
        finally:event.remove(state_engine(),'before_cursor_execute',before)
        self.assertEqual(len(queries),2)
        for statement in queries:
            predicates=statement.split(' WHERE ')[1]
            self.assertIn('owner_issuer',predicates);self.assertIn('owner_subject',predicates)

    def test_concurrent_put_and_delete_same_tag(self):
        saved=self.save();barrier=threading.Barrier(2)
        # Synchronize before both lock attempts; they use independent writers.
        original=service.load_owned
        def synchronized(*args,**kwargs):
            if kwargs.get('lock'):barrier.wait(timeout=10)
            return original(*args,**kwargs)
        with patch('src.api.user_buildings.service.load_owned',synchronized),ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda i:self.client.put(PATH,json={'display_name':str(i)},headers=self.auth(**{'If-Match':saved.headers['etag']})),range(2)))
        self.assertEqual(sorted(r.status_code for r in results),[200,412],[r.text for r in results])
        tag=self.read().headers['etag'];barrier=threading.Barrier(2)
        with patch('src.api.user_buildings.service.load_owned',synchronized),ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:self.client.delete(PATH,headers=self.auth(**{'If-Match':tag})),range(2)))
        self.assertEqual(sorted(r.status_code for r in results),[204,404],[r.text for r in results])
        self.assertEqual(self.count(),0)

    def test_concurrent_creates_and_same_tag_updates(self):
        barrier=threading.Barrier(2)
        original=service.create_owned
        def synchronized(*args,**kwargs): barrier.wait(timeout=10);return original(*args,**kwargs)
        with patch('src.api.user_buildings.service.create_owned',synchronized), ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:self.create(),range(2)))
        self.assertEqual(sorted(r.status_code for r in results),[201,412],[r.text for r in results]);self.assertEqual(self.count(),1)
        tag=self.read().headers['etag'];barrier=threading.Barrier(2)
        original_lock=service.locked_owned
        def locked(*args,**kwargs):barrier.wait(timeout=10);return original_lock(*args,**kwargs)
        with patch('src.api.user_buildings.service.locked_owned',locked), ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda i:self.client.patch(PATH,json={'display_name':str(i)},headers=self.auth(**{'If-Match':tag})),range(2)))
        self.assertEqual(sorted(r.status_code for r in results),[200,412],[r.text for r in results])
        self.assertEqual(self.read().json()['revision'],2)

    def test_waiting_writer_rechecks_current_row_and_lock_timeout_is_sanitized(self):
        saved=self.save();tag=saved.headers['etag']
        principal=AuthenticatedPrincipal(ISSUER,'A')
        with state_session() as blocker, ThreadPoolExecutor(1) as pool:
            row=service.load_owned(blocker,principal,KEY,lock=True)
            future=pool.submit(lambda:self.client.patch(PATH,json={'display_name':'waiting'},headers=self.auth(**{'If-Match':tag})))
            time.sleep(.2);self.assertFalse(future.done())
            service.bump_revision(row);blocker.commit()
            result=future.result(timeout=10)
        self.assertEqual(result.status_code,412,result.text)
        tag=self.read().headers['etag']
        with state_session() as blocker, ThreadPoolExecutor(1) as pool:
            service.load_owned(blocker,principal,KEY,lock=True)
            future=pool.submit(lambda:self.client.delete(PATH,headers=self.auth(**{'If-Match':tag})))
            result=future.result(timeout=10)
            blocker.rollback()
        self.assertEqual(result.status_code,503,result.text)
        self.assertNotIn('SQL',result.text);self.assertEqual(self.count(),1)

    def test_narrow_roles_inherited_pgoptions_unique_indexes_and_privileges(self):
        with patch.dict(os.environ,{'PGOPTIONS':'-c default_transaction_read_only=on'}):
            state_engine().dispose();self.assertEqual(self.save().status_code,201)
            with self.Reader() as source:self.assertEqual(source.execute(text('SHOW transaction_read_only')).scalar_one(),'on')
        with state_session() as state:
            self.assertEqual(state.execute(text('SHOW transaction_read_only')).scalar_one(),'off')
        statements=["UPDATE public.main SET floor_area=0", "INSERT INTO public.buildings(building_key) VALUES ('denied')", 'DELETE FROM public.energy_certificates', 'TRUNCATE public.main', 'ALTER TABLE public.main ADD COLUMN forbidden int', 'CREATE TABLE energymap_state.forbidden(i int)', "UPDATE energymap_state.energymap_user_buildings SET owner_subject='forged'", 'DELETE FROM public.energymap_area_releases']
        for statement in statements:
            with self.subTest(statement=statement), state_session() as state:
                with self.assertRaises(DBAPIError) as failure:state.execute(text(statement))
                self.assertEqual(failure.exception.orig.pgcode,'42501');state.rollback()
        # PostgreSQL reports an ineffective GRANT as a warning, not an error.
        # Assert that no privileges were delegated (and roll it back regardless).
        with state_session() as state:
            state.execute(text(f'GRANT ALL ON {TABLE} TO PUBLIC'))
            self.assertFalse(state.execute(text("SELECT EXISTS(SELECT 1 FROM pg_class c,aclexplode(c.relacl) a WHERE c.oid=to_regclass(:t) AND a.grantee=0)"),{'t':TABLE}).scalar_one())
            state.rollback()
        with self.Reader() as reader:
            with self.assertRaises(DBAPIError) as failure:reader.execute(text(f'SELECT * FROM {TABLE}'))
            self.assertEqual(failure.exception.orig.pgcode,'42501')
        with self.Admin() as admin:
            indexes=admin.execute(text("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='energymap_state'")).all()
            self.assertIn('updated_at DESC, saved_id DESC',dict(indexes)['ix_user_buildings_owner_updated'])
            self.assertEqual(admin.execute(text("SELECT count(*) FROM information_schema.table_constraints WHERE table_schema='energymap_state' AND table_name='energymap_user_buildings' AND constraint_type='FOREIGN KEY'")).scalar_one(),0)
            row=admin.execute(select(Row)).scalar_one();clone={c.name:getattr(row,c.name) for c in Row.__table__.columns};clone['saved_id']=uuid4()
            with self.assertRaises(IntegrityError):admin.execute(Row.__table__.insert().values(**clone));admin.flush()
            admin.rollback()

    def test_pagination_101_ties_owner_cursor_mutations_and_batch_budget(self):
        self.save()
        with self.Admin.begin() as session:
            row=session.execute(select(Row)).scalar_one()
            values={c.name:getattr(row,c.name) for c in Row.__table__.columns}
            for i in range(1,105):
                document={**values,'saved_id':UUID(int=i),'building_key':f'missing-{i}'}
                document['last_known_summary']={**values['last_known_summary'],'building_key':document['building_key']}
                session.execute(Row.__table__.insert().values(**document))
        queries=[];batch=[];source_queries=[]
        def before(conn,cursor,statement,parameters,context,many):queries.append(statement)
        def source_before(conn,cursor,statement,parameters,context,many):source_queries.append((statement,parameters))
        original=resolve_building_summaries
        def summaries(source,keys):batch.append(keys);return original(source,keys)
        event.listen(state_engine(),'before_cursor_execute',before)
        event.listen(self.reader_engine,'before_cursor_execute',source_before)
        try:
            with patch('src.api.user_buildings.resolve_building_summaries',summaries):
                page=self.client.get('/energy-data/user/buildings?limit=100',headers=self.auth())
        finally:
            event.remove(state_engine(),'before_cursor_execute',before)
            event.remove(self.reader_engine,'before_cursor_execute',source_before)
        self.assertEqual(page.status_code,200,page.text);self.assertEqual(len(page.json()['items']),100)
        self.assertEqual(len(batch),1);self.assertEqual(len(batch[0]),100)
        selects=[q for q in queries if q.lstrip().startswith('SELECT') and 'FROM energymap_state.energymap_user_buildings' in q]
        self.assertEqual(len(selects),1);self.assertNotIn('.overrides',selects[0]);self.assertNotIn('.scenario_inputs',selects[0])
        source_selects=[(q,p) for q,p in source_queries if q.lstrip().upper().startswith(('SELECT','WITH'))]
        self.assertLessEqual(len(source_selects),7)
        self.assertFalse(any('FROM energy_certificates' in q for q,p in source_selects))
        cursor=page.json()['next_cursor'];all_items=page.json()['items']
        second=self.client.get('/energy-data/user/buildings',params={'limit':100,'cursor':cursor},headers=self.auth()).json()
        all_items+=second['items'];self.assertEqual(len(all_items),105);self.assertEqual(len({r['saved_id'] for r in all_items}),105)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len(self.client.get('/energy-data/user/buildings',headers=self.auth()).json()['items']),20)
        self.assertEqual(self.client.get('/energy-data/user/buildings',params={'cursor':cursor},headers=self.auth('B')).json()['items'],[])
        for query in ('limit=0','limit=101','cursor=bad','owner=A','limit=1&limit=2'):
            self.assertEqual(self.client.get('/energy-data/user/buildings?'+query,headers=self.auth()).status_code,422)
        boundary=decode_cursor(cursor)
        with self.Admin.begin() as session:
            session.execute(text(f'DELETE FROM {TABLE} WHERE saved_id=:id'),{'id':boundary.saved_id})
        remaining=self.client.get('/energy-data/user/buildings',params={'cursor':cursor},headers=self.auth()).json()
        self.assertEqual(len(remaining['items']),5)
        moved=self.client.get('/energy-data/user/buildings/missing-1',headers=self.auth())
        self.assertEqual(moved.status_code,200,moved.text)
        changed=self.client.patch('/energy-data/user/buildings/missing-1',json={'display_name':'Moved between pages'},headers=self.auth(**{'If-Match':moved.headers['etag']}))
        self.assertEqual(changed.status_code,200,changed.text)
        after=self.client.get('/energy-data/user/buildings',params={'cursor':cursor},headers=self.auth()).json()
        self.assertEqual(len(after['items']),4)
        self.assertEqual(self.client.get('/energy-data/user/buildings',headers=self.auth()).json()['items'][0]['saved_id'],str(UUID(int=1)))
        # Record source batch SQL/EXPLAIN without parameters or private values.
        main_sql,parameters=next((q,p) for q,p in source_selects if 'FROM main WHERE building_key=ANY' in q)
        with self.reader_engine.connect() as source:
            explain=source.exec_driver_sql('EXPLAIN '+main_sql,parameters).scalars().all()
        evidence=Path('.codex-orch/features/F001.3-owner-saved-buildings/evidence/summary-explain.txt')
        evidence.write_text('\n'.join(explain)+'\n100 bounded mixed keys; one state scalar page query, two fixed metadata probes and one canonical batch call.\n\nGenerated source SQL (parameters omitted):\n'+'\n'.join(q for q,p in source_selects))

    def test_real_merge_source_refresh_public_tiles_and_saved_state_survive(self):
        public=lambda:self.client.get('/energy-data/buildings/'+KEY).json()
        def comparable():
            value=public();value.pop('resolved_at');return value
        tile=lambda:decode_mvt(self.client.get('/energy-data/tiles/11/1160/594.pbf').content)
        before=self.checksum();projection=comparable();native=tile()
        saved=self.save({'overrides':{'floor_area':0,'total_area':None},'scenario_inputs':{'resident_count':None}})
        self.assertEqual(self.checksum(),before);self.assertEqual(comparable(),projection);self.assertEqual(tile(),native)
        stored=self.stored()
        with self.Admin.begin() as session:session.execute(text('UPDATE buildings SET volume=555 WHERE building_key=:k'),{'k':KEY})
        self.merge()
        current=self.read().json()
        self.assertEqual(current['saved_id'],saved.json()['saved_id']);self.assertEqual(current['revision'],1)
        self.assertEqual(current['effective']['details']['volume'],555);self.assertEqual(current['overrides'],{'floor_area':0.0,'total_area':None})
        self.assertEqual(self.stored(),stored)
        rebuilt=comparable();rebuilt_tiles=tile()
        state_engine().dispose();self.assertEqual(self.read('A.new-session').json()['saved_id'],saved.json()['saved_id'])
        self.assertEqual(self.client.delete(PATH,headers=self.auth(**{'If-Match':saved.headers['etag']})).status_code,204)
        self.merge();self.assertEqual(comparable(),rebuilt);self.assertEqual(tile(),rebuilt_tiles)

    def test_official_area_details_aggregates_and_decoded_tiles_unchanged(self):
        from src.etl.area_inputs import import_boundaries, import_geography
        from src.etl.build_area_aggregates import build_release
        from src.etl.area_releases import promote_release
        from src.geoserver.publish_area_aggregates import verify_delivery, record_publication
        key=str(UUID(int=1000))
        with self.Admin.begin() as session:
            for table in ('buildings','addresses','polygons'):
                session.execute(text(f'UPDATE {table} SET building_key=:new WHERE building_key=:old'),{'new':key,'old':KEY})
            geometry=json.loads(session.execute(text('SELECT ST_AsGeoJSON(ST_Transform(ST_MakeEnvelope(23.9,59.9,24.1,60.1,4326),3067))')).scalar_one())
            x,y=session.execute(text('SELECT ST_X(g),ST_Y(g) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(24,60),4326),3067) g) q')).one()
        self.merge()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            for name,field,code in (('postals','posti_alue','00100'),('municipalities','kunta','091'),('regions','maakunta','01')):
                data={'type':'FeatureCollection','crs':{'type':'name','properties':{'name':'EPSG:3067'}},'features':[{'type':'Feature','geometry':geometry,'properties':{field:code,'vuosi':2026,'nimi':'Fixture','namn':'Fixture','name':'Fixture'}}]}
                (path/(name+'.geojson')).write_text(json.dumps(data))
            (path/'classification.csv').write_text('municipality_code,region_code,name_fi,name_sv,name_en\n091,01,Fixture,Fixture,Fixture\n')
            (path/'geography.csv').write_text(f'building_key,municipality_number,X,Y\n{key},091,{x},{y}\n')
            with self.Admin.begin() as session:
                boundaries=import_boundaries(session,postal_path=path/'postals.geojson',municipality_path=path/'municipalities.geojson',region_path=path/'regions.geojson',classification_path=path/'classification.csv',municipality_source_url='https://example.invalid/fixture',municipality_source_version='fixture',expected_counts={'postal_code':1,'municipality':1,'region':1},simplify_m=0)
                geography=import_geography(session,path/'geography.csv',min_records=1)
            with self.Admin() as session:
                release=build_release(session,boundary_set_id=boundaries['boundary_set_id'],geography_snapshot_id=geography['snapshot_id'],as_of=date(2026,10,23))
                session.commit()
        def fetch(url,**kwargs):
            kwargs.pop('timeout',None);return self.client.get(url,**kwargs)
        with self.Admin.begin() as session:
            publication=verify_delivery(session,release['release_id'],'http://testserver',get=fetch)
            record_publication(session,release['release_id'],publication)
            promote_release(session,release['release_id'])
        urls=['/energy-data/areas/municipality/091','/energy-data/areas/postal_code/00100','/energy-data/areas/region/01']
        details=[self.client.get(url).json() for url in urls]
        tile_url='/energy-data/areas/municipality/tiles/11/1160/594.pbf'
        tiles=decode_mvt(self.client.get(tile_url).content)
        self.assertTrue(tiles)
        before=self.checksum()
        with self.Admin() as session:
            stats=session.execute(text('SELECT level,area_code,building_count,valid_official_class_count,metrics FROM energymap_area_stats WHERE release_id=:id ORDER BY level,area_code'),{'id':release['release_id']}).all()
        saved=self.create({'overrides':{'floor_area':999999,'heating_energy_source':None}},key=key)
        self.assertEqual(saved.status_code,201,saved.text)
        share_path='/energy-data/user/buildings/'+key+'/share'
        shared=self.client.post(share_path,headers=self.auth(**{'If-Match':saved.headers['etag']}))
        self.assertEqual(shared.status_code,201)
        rotated=self.client.post(share_path,headers=self.auth(**{'If-Match':shared.headers['etag']}))
        self.assertEqual(rotated.status_code,201)
        self.assertEqual(self.client.get(shared.json()['resolver_path']).status_code,404)
        self.assertEqual(self.client.get(rotated.json()['resolver_path']).status_code,200)
        revoked=self.client.delete(share_path,headers=self.auth(**{'If-Match':rotated.headers['etag']}))
        self.assertEqual(revoked.status_code,204)
        self.assertEqual(self.client.get(rotated.json()['resolver_path']).status_code,404)
        self.assertEqual(self.checksum(),before)
        self.assertEqual([self.client.get(url).json() for url in urls],details)
        self.assertEqual(decode_mvt(self.client.get(tile_url).content),tiles)
        with self.Admin() as session:
            self.assertEqual(session.execute(text('SELECT level,area_code,building_count,valid_official_class_count,metrics FROM energymap_area_stats WHERE release_id=:id ORDER BY level,area_code'),{'id':release['release_id']}).all(),stats)
        self.assertEqual(self.client.delete('/energy-data/user/buildings/'+key,headers=self.auth(**{'If-Match':revoked.headers['etag']})).status_code,204)
        self.assertEqual([self.client.get(url).json() for url in urls],details)

    def test_readiness_schema_grant_failure_and_corrupt_state(self):
        self.assertTrue(state_ready());saved=self.save()
        with self.Admin.begin() as session:session.execute(text(f'REVOKE DELETE ON {TABLE} FROM energymap_f001_owner_writer'))
        try:
            self.assertFalse(state_ready())
            self.assertEqual(self.read().status_code,503)
        finally:
            with self.Admin.begin() as session:session.execute(text(f'GRANT DELETE ON {TABLE} TO energymap_f001_owner_writer'))
        with self.Admin.begin() as session:session.execute(text(f'UPDATE {TABLE} SET overrides=CAST(:value AS jsonb)'),{'value':json.dumps({'forged':1})})
        result=self.read();self.assertEqual(result.status_code,503,result.text);self.assertNotIn('forged',result.text)
        self.assertEqual(self.client.get('/healthz').status_code,200)
        self.assertEqual(self.client.get('/energy-data/buildings/'+KEY).status_code,200)

    def test_missing_schema_disabled_and_wrong_connection_are_private_503(self):
        self.save();stored=self.stored();source=self.checksum()
        with self.Admin.begin() as session:
            session.execute(text(f'ALTER TABLE {TABLE} RENAME TO fixture_unavailable_saved_buildings'))
        try:
            self.assertFalse(state_ready())
            result=self.read();self.assertEqual(result.status_code,503,result.text)
            self.assertEqual(result.headers['cache-control'],'no-store')
            self.assertEqual(self.client.get('/energy-data/buildings/'+KEY).status_code,200)
        finally:
            with self.Admin.begin() as session:
                session.execute(text('ALTER TABLE energymap_state.fixture_unavailable_saved_buildings RENAME TO energymap_user_buildings'))
        wrong=make_url(os.environ['ENERGY_MAP_STATE_DATABASE_URL']).set(username='energymap_fixture_absent_writer').render_as_string(hide_password=False)
        for change in ({'ENERGY_MAP_STATE_ENABLED':'false'},{'ENERGY_MAP_STATE_DATABASE_URL':wrong},
                       {'ENERGY_MAP_STATE_DATABASE_URL':os.environ['OWNER_TEST_ADMIN_URL']}):
            with patch.dict(os.environ,change):
                self.assertFalse(state_ready())
                result=self.read();self.assertEqual(result.status_code,503,result.text)
                self.assertEqual(result.headers['cache-control'],'no-store')
                self.assertNotIn('absent_writer',result.text)
                self.assertEqual(self.client.get('/healthz').status_code,200)
                self.assertEqual(self.client.get('/energy-data/buildings/'+KEY).status_code,200)
        self.assertEqual(self.stored(),stored);self.assertEqual(self.checksum(),source)
