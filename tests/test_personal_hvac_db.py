"""Real private v3 API on exact disposable F001 fixture; synthetic provider/source."""
from concurrent.futures import ThreadPoolExecutor
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from src.db.state import personal_schema_capability
from src.hvac_catalog import HvacCatalogUnavailable
from tests.test_personal_hvac import IDS, full_systems, systems
from tests import test_user_buildings_db as fixture

V3=fixture.PATH+'?personal_schema_version=3'


def definition():
    path=Path(__file__).resolve().parents[1]/'db_migrations/versions/f6a213bde901_013_personal_hvac_v3.py'
    spec=spec_from_file_location('hvac_combined_migration',path)
    module=module_from_spec(spec);spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.getenv('RUN_OWNER_BUILDINGS_DB_TESTS')=='1','requires guarded doctor-owned F001 fixture')
class HvacDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(fixture.OwnerDatabaseTests.setUpClass.__func__)
    source_dependency=fixture.OwnerDatabaseTests.source_dependency
    provider=fixture.OwnerDatabaseTests.provider
    fixture_purpose_catalog=fixture.OwnerDatabaseTests.fixture_purpose_catalog
    merge=fixture.OwnerDatabaseTests.merge
    auth=fixture.OwnerDatabaseTests.auth
    checksum=fixture.OwnerDatabaseTests.checksum
    stored=fixture.OwnerDatabaseTests.stored

    def setUp(self):
        fixture.OwnerDatabaseTests.setUp(self)
        with self.Admin.begin() as session:
            if personal_schema_capability(session)<3:
                with Operations.context(MigrationContext.configure(session.connection())): definition().upgrade()
                session.execute(text("UPDATE alembic_version SET version_num='f6a213bde901'"))

    @classmethod
    def tearDownClass(cls):
        # Final lifecycle test deletes its row. Refuse cleanup if any v3 row
        # remains; never erase data to force a downgrade. Leave F001 on v2.
        with cls.Admin.begin() as session:
            with Operations.context(MigrationContext.configure(session.connection())): definition().downgrade()
            session.execute(text("UPDATE alembic_version SET version_num='e5f102acd890'"))

    def status(self,response,status):
        self.assertEqual(response.status_code,status,response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        return response

    def save3(self,value=None):
        return self.status(self.client.put(V3,json={'schema_version':3,'scenario_inputs':{'systems':value or systems({'kind':'selected','code':'0'})}},
            headers=self.auth(**{'If-None-Match':'*'})),201)

    def patch3(self,body,tag):
        return self.client.patch(V3,json={'schema_version':3,**body},headers=self.auth(**{'If-Match':tag}))

    def test_real_partial_save_reopen_outage_and_source_preservation(self):
        before=self.checksum()
        saved=self.save3(full_systems());tag=saved.headers['etag']
        self.assertEqual(saved.json()['systems_defaults']['coefficients']['status'],'deferred')
        self.assertEqual(saved.json()['scenario_inputs']['systems'], full_systems())
        changed=self.status(self.patch3({'set':{'overrides':{'ventilation_type_id':'0','ventilation_description':'Legacy note'},
            'scenario_inputs':{'apartment_studio_count':0,'apartment_two_room_count':None}}},tag),200)
        expected=changed.json()['scenario_inputs']
        with patch('src.api.user_buildings.observe',return_value=(None,'source_unavailable',None)):
            view=self.status(self.client.get(V3,headers=self.auth()),200)
            self.assertEqual(view.json()['scenario_inputs'],expected)
        code="from src.db.state import state_session;from sqlalchemy import text;\nwith state_session(read_only=True) as s:\n d=s.execute(text('SELECT schema_version,scenario_inputs FROM energymap_state.energymap_user_buildings')).one();assert d[0]==3 and d[1]=="+repr(expected)
        result=subprocess.run([sys.executable,'-c',code],env=os.environ.copy(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
        self.assertEqual(result.returncode,0,'separate process reopen failed')
        self.assertEqual(self.checksum(),before)
        reset=self.status(self.patch3({'set':{'scenario_inputs':{'systems':systems(None)}}},changed.headers['etag']),200)
        self.assertIsNone(reset.json()['scenario_inputs']['systems']['ventilation'])
        removed=self.status(self.patch3({'unset':['scenario_inputs.systems']},reset.headers['etag']),200)
        self.assertNotIn('systems',removed.json()['scenario_inputs'])
        self.assertEqual(removed.json()['document_schema_version'],3)

    def test_version_views_old_writers_mixed_listing_and_promotion(self):
        old=self.status(self.client.put(fixture.PATH+'?personal_schema_version=2',json={'schema_version':2,
            'overrides':{'total_area':None},'scenario_inputs':{'resident_count':2,'apartment_studio_count':0}},headers=self.auth(**{'If-None-Match':'*'})),201)
        share=self.status(self.client.post(fixture.PATH+'/share',json={},headers=self.auth(**{'If-Match':old.headers['etag']})),201)
        changed=self.status(self.patch3({'set':{'scenario_inputs':{'systems':systems({'kind':'selected','code':'8'})}}},share.headers['etag']),200)
        self.assertEqual(changed.json()['saved_id'],old.json()['saved_id']);self.assertEqual(changed.json()['created_at'],old.json()['created_at'])
        self.assertEqual(changed.json()['scenario_inputs']['apartment_studio_count'],0)
        for version in (1,2,3):
            suffix='?personal_schema_version='+str(version)
            expected=409 if version==2 else 200
            self.status(self.client.get(fixture.PATH+suffix,headers=self.auth()),expected)
            shared=self.status(self.client.get(share.json()['resolver_path']+suffix),expected)
            self.status(self.client.get('/energy-data/user/buildings'+suffix,headers=self.auth()),expected)
            if expected==200:
                self.assertNotIn('saved_id', shared.json())
                def private_keys(value):
                    if isinstance(value, dict):
                        return set(value) | set().union(*(private_keys(v) for v in value.values()), set())
                    if isinstance(value, list):
                        return set().union(*(private_keys(v) for v in value), set())
                    return set()
                self.assertFalse({'owner_issuer', 'owner_subject', 'token_digest', 'saved_id'} & private_keys(shared.json()))
                self.assertEqual('systems' in shared.json()['scenario_inputs'],version==3)
        before=self.stored()
        for version in (1,2):
            path=fixture.PATH+'?personal_schema_version='+str(version)
            for method,body in (('put',{'schema_version':version}),('patch',{'schema_version':version}),('delete',None)):
                kwargs={} if body is None else {'json':body}
                self.status(getattr(self.client,method)(path,headers=self.auth(**{'If-Match':old.headers['etag']}),**kwargs),412)
                self.status(getattr(self.client,method)(path,headers=self.auth(**{'If-Match':changed.headers['etag']}),**kwargs),409)
                self.assertEqual(self.stored(),before)
        # Mixed summary-only pagination must not omit v3 or fail a v2 page
        # that contains only supported older rows.
        older=[]
        with self.Admin.begin() as session:
            for key in ('hvac-mixed-v1','hvac-mixed-v2'):
                session.execute(text('INSERT INTO buildings(building_key) VALUES (:key)'),{'key':key})
        for version in (1,2):
            path='/energy-data/user/buildings/hvac-mixed-v'+str(version)+'?personal_schema_version='+str(version)
            response=self.status(self.client.put(path,json={'schema_version':version},headers=self.auth(**{'If-None-Match':'*'})),201)
            older.append((path,response.headers['etag']))
        page=self.status(self.client.get('/energy-data/user/buildings?personal_schema_version=3',headers=self.auth()),200).json()
        self.assertEqual(sorted(x['document_schema_version'] for x in page['items']),[1,2,3])
        cursor=None
        for expected in (200,200,409):
            params={'personal_schema_version':2,'limit':1}
            if cursor: params['cursor']=cursor
            response=self.status(self.client.get('/energy-data/user/buildings',params=params,headers=self.auth()),expected)
            if expected==200: cursor=response.json()['next_cursor']
        self.assertEqual(len(self.status(self.client.get('/energy-data/user/buildings',headers=self.auth()),200).json()['items']),3)
        for path,tag in older:
            self.status(self.client.delete(path,headers=self.auth(**{'If-Match':tag})),204)
        self.status(self.client.delete(V3,headers=self.auth(**{'If-Match':changed.headers['etag']})),204)
        self.status(self.client.get(share.json()['resolver_path']),404)

    def test_historical_choices_numeric_edits_and_source_rebuild_preserve_answers(self):
        from src.hvac_catalog import load_hvac_catalog
        saved = self.save3(full_systems())
        # Product source rebuild is exercised against the guarded synthetic source.
        before = self.stored(); fingerprint = self.checksum()
        self.merge()
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.checksum(), fingerprint)
        catalog = load_hvac_catalog().model_copy(deep=True)
        choice = next(c for c in catalog.heating_choices if c.code == 'ryhti:heating_energy_source:01')
        choice.selectable = False; choice.lifecycle = 'deprecated'  # synthetic lifecycle fixture
        with patch('src.personal_hvac_contract.load_hvac_catalog', return_value=catalog):
            view = self.status(self.client.get(V3, headers=self.auth()), 200)
            self.assertEqual(view.json()['systems_source_provenance'][0]['source_code']['status'], 'unsupported')
            self.assertEqual(view.json()['systems_source_provenance'][0]['share_percent']['status'], 'available')
            answers = full_systems(); answers['heating']['sources'][0]['share_percent'] = -11.25
            edited = self.status(self.patch3({'set': {'scenario_inputs': {'systems': answers}}}, saved.headers['etag']), 200)
            self.assertEqual(edited.json()['scenario_inputs']['systems'], answers)
            self.assertEqual(self.checksum(), fingerprint)
            before = self.stored()
            # A newly created identity still needs current choice validation.
            answers['heating']['sources'][0]['source_id'] = '44444444-4444-4444-8444-444444444444'
            answers['heating']['primary_source_id'] = answers['heating']['sources'][0]['source_id']
            self.status(self.patch3({'set': {'scenario_inputs': {'systems': answers}}}, edited.headers['etag']), 422)
            self.assertEqual(self.stored(), before)
        with patch('src.api.user_buildings.observe', return_value=(None, 'source_unavailable', None)):
            unavailable = self.status(self.client.get(V3, headers=self.auth()), 200)
            self.assertEqual(unavailable.json()['scenario_inputs']['systems']['heating']['sources'][0]['share_percent'], -11.25)
            self.assertEqual(unavailable.json()['field_provenance']['scenario_inputs.systems.heated_quantities.total_area_m2']['status'], 'available')
        self.status(self.client.delete(V3, headers=self.auth(**{'If-Match': edited.headers['etag']})), 204)

    def test_v1_to_empty_v3_preserves_share_incarnation_and_cannot_downgrade(self):
        old=self.status(self.client.put(fixture.PATH,json={'overrides':{'floor_area':0,'total_area':None},
            'scenario_inputs':{'resident_count':2}},headers=self.auth(**{'If-None-Match':'*'})),201)
        share=self.status(self.client.post(fixture.PATH+'/share',json={},headers=self.auth(**{'If-Match':old.headers['etag']})),201)
        promoted=self.status(self.patch3({},share.headers['etag']),200)
        for key in ('saved_id','created_at','overrides','scenario_inputs'):
            self.assertEqual(promoted.json()[key],old.json()[key])
        self.assertTrue(promoted.json()['share_enabled'])
        self.status(self.client.get(share.json()['resolver_path']+'?personal_schema_version=3'),200)
        self.status(self.client.get(fixture.PATH+'?personal_schema_version=2',headers=self.auth()),409)
        self.status(self.client.delete(V3,headers=self.auth(**{'If-Match':promoted.headers['etag']})),204)

    def test_real_concurrency_exact_identity_and_invalid_no_mutation(self):
        saved=self.save3(full_systems());before=self.stored()
        for issuer,subject in ((self.issuer,'B'),('https://other.example.invalid','A')):
            previous=self.issuer;self.issuer=issuer
            self.status(self.client.get(V3,headers=self.auth(subject)),404)
            self.issuer=previous
        self.status(self.patch3({'set':{'scenario_inputs':{'systems':systems({'kind':'selected','code':'unknown'})}}},saved.headers['etag']),422)
        self.assertEqual(before,self.stored())
        # Same-ETag collection edits: reorder/primary and a source-share change.
        def write(index):
            value = full_systems()
            if index == 0:
                value['heating']['sources'].reverse()
                value['heating']['primary_source_id'] = IDS[1]
            else:
                value['heating']['sources'][0]['share_percent'] = -25.5
            return self.patch3({'set': {'scenario_inputs': {'systems': value}}}, saved.headers['etag'])
        with ThreadPoolExecutor(max_workers=2) as pool: responses=list(pool.map(write,(0,1)))
        self.assertEqual(sorted(r.status_code for r in responses),[200,412])
        self.assertEqual(self.stored()[0]['revision'],2)
        current=next(r for r in responses if r.status_code==200)
        reconciled = current.json()['scenario_inputs']['systems']
        reconciled['heating']['primary_source_id'] = IDS[1]
        next(s for s in reconciled['heating']['sources'] if s['source_id'] == IDS[0])['share_percent'] = -25.5
        current = self.status(self.patch3({'set': {'scenario_inputs': {'systems': reconciled}}}, current.headers['etag']), 200)
        self.assertEqual(self.stored()[0]['revision'], 3)
        with patch('src.personal_hvac_contract.load_hvac_catalog',side_effect=HvacCatalogUnavailable):
            self.status(self.client.get(V3,headers=self.auth()),200)
            self.status(self.patch3({'set':{'scenario_inputs':{'systems':systems({'kind':'selected','code':'2'})}}},current.headers['etag']),503)
            renamed=self.status(self.patch3({'display_name':'During outage'},current.headers['etag']),200)
            self.status(self.patch3({'unset':['scenario_inputs.systems']},renamed.headers['etag']),200)

    def test_full_collections_replacement_errors_sharing_reset_and_unknown(self):
        saved = self.save3(full_systems())
        tag = saved.headers['etag']
        # Rejected coefficients/speculative equipment never mutate the accepted row.
        before = self.stored()
        for extra in ({'coefficients': {'electricity': 0}}, {'attribution': 'unsupported'}, {'equipment': {'battery_capacity': 1}}):
            self.status(self.patch3({'set': {'scenario_inputs': {'systems': full_systems() | extra}}}, tag), 422)
            self.assertEqual(self.stored(), before)
        changed = full_systems(); changed['heating']['sources'][0]['source_code'] = 'ryhti:heating_energy_source:07'
        for method in ('patch', 'put'):
            body = {'schema_version': 3, 'scenario_inputs': {'systems': changed}}
            if method == 'patch': body = {'schema_version': 3, 'set': {'scenario_inputs': {'systems': changed}}}
            response = getattr(self.client, method)(V3, json=body, headers=self.auth(**{'If-Match': tag}))
            self.status(response, 422)
            self.assertEqual(response.json()['detail'][0]['type'], 'source_identity_reused')
            self.assertEqual(self.stored(), before)
        share = self.status(self.client.post(fixture.PATH + '/share', json={}, headers=self.auth(**{'If-Match': tag})), 201)
        tag = share.headers['etag']
        shared = self.status(self.client.get(share.json()['resolver_path'] + '?personal_schema_version=3'), 200)
        self.assertEqual(shared.json()['scenario_inputs']['systems'], full_systems())
        self.assertEqual(shared.json()['systems_source_provenance'], saved.json()['systems_source_provenance'])
        self.assertNotIn('saved_id', shared.json())
        read_before = self.stored()
        self.status(self.client.get('/energy-data/building-hvac-choices'), 200)
        self.status(self.client.get(V3, headers=self.auth()), 200)
        self.assertEqual(self.stored(), read_before)
        # Remove primary and one answer; re-add without resurrecting the removed share.
        answers = full_systems(); answers['heating']['sources'] = answers['heating']['sources'][1:]
        answers['heating']['primary_source_id'] = None
        removed = self.status(self.patch3({'set': {'scenario_inputs': {'systems': answers}}}, tag), 200)
        answers['heating']['sources'].append({'source_id': '44444444-4444-4444-8444-444444444444', 'source_code': 'ryhti:heating_energy_source:01'})
        readded = self.status(self.patch3({'set': {'scenario_inputs': {'systems': answers}}}, removed.headers['etag']), 200)
        self.assertNotIn('share_percent', readded.json()['scenario_inputs']['systems']['heating']['sources'][-1])
        with patch('src.personal_hvac_contract.load_hvac_catalog', side_effect=HvacCatalogUnavailable):
            self.status(self.patch3({'set': {'scenario_inputs': {'systems': systems() | {'heated_quantities': {'total_area_m2': -1.5}, 'equipment': {'geothermal_installed': False}}}}}, readded.headers['etag']), 200)
        current = self.status(self.client.get(V3, headers=self.auth()), 200)
        reset = self.status(self.patch3({'set': {'scenario_inputs': {'systems': None}}}, current.headers['etag']), 200)
        self.assertIsNone(reset.json()['scenario_inputs']['systems'])
        self.status(self.client.delete(V3, headers=self.auth(**{'If-Match': reset.headers['etag']})), 204)
        self.status(self.client.get(share.json()['resolver_path']), 404)

    def test_combined_upgrade_downgrade_gates_preserve_rows(self):
        # Exact combined migration exercised transactionally, no fixture lifecycle.
        with self.Admin.begin() as session:
            with Operations.context(MigrationContext.configure(session.connection())): definition().downgrade()
            session.execute(text("UPDATE alembic_version SET version_num='e5f102acd890'"))
        self.status(self.client.get('/readyz/state?personal_schema_version=3'),503)
        self.status(self.client.put(V3,json={'schema_version':3},headers=self.auth(**{'If-None-Match':'*'})),503)
        with self.Admin.begin() as session:
            with Operations.context(MigrationContext.configure(session.connection())): definition().upgrade()
            session.execute(text("UPDATE alembic_version SET version_num='f6a213bde901'"))
        self.status(self.client.get('/readyz/state?personal_schema_version=3'),200)
        self.save3();before=self.stored()
        with self.Admin.begin() as session:
            with Operations.context(MigrationContext.configure(session.connection())):
                with self.assertRaisesRegex(RuntimeError,'downgrade refused'): definition().downgrade()
        self.assertEqual(before,self.stored())
