"""Actual personal-v2 API/persistence on the exact doctor-owned F001 fixture."""
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

from src.building_contract import CodeCatalog
from src.db.state import personal_schema_capability
from src.purpose_catalog import PurposeCatalogUnavailable, load_purpose_catalog
from tests import test_user_buildings_db as owner_fixture
from tests.purpose_catalog_fixture import adopted_pin
from tests.test_user_buildings_db import KEY, PATH, TABLE

V2 = PATH + '?personal_schema_version=2'


def definition():
    path=Path(__file__).resolve().parents[1]/'db_migrations/versions/e5f102acd890_012_personal_v2.py'
    spec=spec_from_file_location('personal_v2_combined',path);module=module_from_spec(spec);spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.getenv('RUN_OWNER_BUILDINGS_DB_TESTS') == '1', 'requires exact doctor-owned owner fixture')
class PersonalPropertyDatabaseTests(unittest.TestCase):
    setUpClass=classmethod(owner_fixture.OwnerDatabaseTests.setUpClass.__func__)
    def setUp(self):
        owner_fixture.OwnerDatabaseTests.setUp(self)
        self.patches[0].stop()
    fixture_purpose_catalog=owner_fixture.OwnerDatabaseTests.fixture_purpose_catalog
    source_dependency=owner_fixture.OwnerDatabaseTests.source_dependency
    provider=owner_fixture.OwnerDatabaseTests.provider
    merge=owner_fixture.OwnerDatabaseTests.merge
    auth=owner_fixture.OwnerDatabaseTests.auth
    create=owner_fixture.OwnerDatabaseTests.create
    save=owner_fixture.OwnerDatabaseTests.save
    read=owner_fixture.OwnerDatabaseTests.read
    count=owner_fixture.OwnerDatabaseTests.count
    checksum=owner_fixture.OwnerDatabaseTests.checksum
    stored=owner_fixture.OwnerDatabaseTests.stored

    def assert_status(self, response, status):
        self.assertEqual(response.status_code,status,response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        return response

    def patch2(self, body, tag, subject='A'):
        return self.client.patch(V2,json={'schema_version':2,**body},headers=self.auth(subject,**{'If-Match':tag}))

    def test_00_old_schema_v1_saved_share_migration_exact_preservation_and_readiness(self):
        # Reusable fixture, serial execution. Begin from the true historical
        # constraint with no rows, then seed actual v1 through HTTP before upgrade.
        with self.Admin.begin() as session:
            connection=session.connection()
            if personal_schema_capability(session)==2:
                with Operations.context(MigrationContext.configure(connection)):definition().downgrade()
            session.execute(text("UPDATE alembic_version SET version_num='d4e8f021bc63'"))
        saved=self.save({'display_name':'Preserve', 'overrides':{'main_purpose':None,'total_area':None},'scenario_inputs':{'resident_count':3}})
        share=self.assert_status(self.client.post(PATH+'/share',json={},headers=self.auth(**{'If-Match':saved.headers['etag']})),201)
        before=self.stored();sources=self.checksum()
        self.assert_status(self.client.get('/readyz/state'),200)
        self.assert_status(self.client.get('/readyz/state?personal_schema_version=2'),503)
        result=self.assert_status(self.patch2({},share.headers['etag']),503)
        self.assertEqual(result.json()['detail']['code'],'personal_schema_upgrade_required')
        self.assertEqual(self.stored(),before)
        view=self.assert_status(self.client.get(V2,headers=self.auth()),200)
        self.assertEqual(view.json()['document_schema_version'],1)
        self.assertEqual(self.stored(),before)
        with self.Admin.begin() as session:
            with Operations.context(MigrationContext.configure(session.connection())):definition().upgrade()
            session.execute(text("UPDATE alembic_version SET version_num='e5f102acd890'"))
        self.assertEqual(self.stored(),before)
        self.assertEqual(self.checksum(),sources)
        self.assert_status(self.client.get('/readyz/state?personal_schema_version=2'),200)
        self.assert_status(self.client.get(share.json()['resolver_path']),200)
        self.assert_status(self.client.get(share.json()['resolver_path']+'?personal_schema_version=2'),200)

    def test_01_sparse_roundtrip_reopen_and_safe_v1_projection(self):
        data={'resident_count':2,'personal_energy_class':{'grade':'C','scale':2018},
              'personal_energy_class_date':{'precision':'year','year':2020},
              'annual_maintenance_charge_eur_cents':123456,'apartment_studio_count':0,
              'apartment_two_room_count':None,'apartment_three_room_count':1_000_000,
              'apartment_four_plus_room_count':1}
        source_before=self.checksum()
        saved=self.assert_status(self.client.put(V2,json={'schema_version':2,'display_name':'Counts',
            'overrides':{'apartment_count':0},'scenario_inputs':data},headers=self.auth(**{'If-None-Match':'*'})),201)
        self.assertEqual(saved.json()['scenario_inputs'],data)
        self.assertEqual(saved.json()['effective']['scenario_inputs'],data)
        with self.Admin() as session:
            self.assertEqual(session.execute(text(f'SELECT scenario_inputs FROM {TABLE}')).scalar_one(),data)
        self.assertEqual(self.assert_status(self.client.get(V2,headers=self.auth()),200).json()['scenario_inputs'],data)
        # Actual separate interpreter/session; output only the public shape assertion.
        code="from src.db.state import state_session;from sqlalchemy import text;\nwith state_session(read_only=True) as s:\n d=s.execute(text('SELECT schema_version,scenario_inputs FROM energymap_state.energymap_user_buildings')).one();assert d[0]==2 and d[1]=="+repr(data)
        process=subprocess.run([sys.executable,'-c',code],env=os.environ.copy(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
        self.assertEqual(process.returncode,0,'separate-process read failed')
        old=self.assert_status(self.read(),200).json()
        self.assertEqual(old['scenario_inputs'],{'resident_count':2})
        self.assertEqual(self.checksum(),source_before)
        for field in data:
            if field != 'resident_count':
                provenance=saved.json()['field_provenance']['scenario_inputs.'+field]
                self.assertEqual(provenance['origin'],'user')
                self.assertIsNone(provenance['source_observed_at'])
        self.assertNotIn('document_schema_version',old);self.assertNotIn('input_status',old)
        self.assertFalse(any('apartment_studio' in k for k in old['field_provenance']))
        for version in (1,2):
            page=self.assert_status(self.client.get('/energy-data/user/buildings?personal_schema_version='+str(version),headers=self.auth()),200).json()
            self.assertEqual(page['schema_version'],version)
            self.assertEqual(len(page['items']),1)
            self.assertEqual('document_schema_version' in page['items'][0],version==2)

    def test_02_no_loss_older_mutations_preconditions_and_owner_precedence(self):
        saved=self.assert_status(self.client.put(V2,json={'schema_version':2,'scenario_inputs':{'apartment_studio_count':7}},headers=self.auth(**{'If-None-Match':'*'})),201)
        share=self.assert_status(self.client.post(PATH+'/share',json={},headers=self.auth(**{'If-Match':saved.headers['etag']})),201)
        tag=share.headers['etag'];before=self.stored()
        with self.Admin() as session: digest=session.execute(text('SELECT token_digest FROM energymap_state.energymap_user_building_shares')).scalar_one()
        for method,body in (('put',{}),('patch',{'set':{'overrides':{'floor_area':120}}}),('delete',None)):
            kwargs={} if body is None else {'json':body}
            for subject,headers,expected in (('A',{},428),('A',{'If-Match':saved.headers['etag']},412),('B',{'If-Match':tag},412 if method=='put' else 404),('A',{'If-Match':tag},409)):
                response=self.assert_status(getattr(self.client,method)(PATH,headers=self.auth(subject,**headers),**kwargs),expected)
                if expected==409:self.assertEqual(response.json()['detail']['code'],'personal_document_version_conflict')
                self.assertEqual(self.stored(),before)
        with self.Admin() as session:self.assertEqual(session.execute(text('SELECT token_digest FROM energymap_state.energymap_user_building_shares')).scalar_one(),digest)
        self.assert_status(self.client.delete(V2,headers=self.auth(**{'If-Match':tag})),204)
        self.assert_status(self.client.get(share.json()['resolver_path']),404)

    def test_03_promotion_share_continuity_reset_and_source_loss(self):
        saved=self.save({'scenario_inputs':{'resident_count':5},'overrides':{'main_purpose':None}})
        share=self.assert_status(self.client.post(PATH+'/share',json={},headers=self.auth(**{'If-Match':saved.headers['etag']})),201)
        old=self.stored()[0];old_tag=share.headers['etag']
        promoted=self.assert_status(self.patch2({},old_tag),200)
        self.assertEqual(promoted.json()['saved_id'],saved.json()['saved_id'])
        self.assertEqual(promoted.json()['created_at'],saved.json()['created_at'])
        self.assertEqual(promoted.json()['revision'],old['revision']+1)
        changed=self.assert_status(self.patch2({'set':{'scenario_inputs':{'apartment_studio_count':0,'apartment_two_room_count':None}}},promoted.headers['etag']),200)
        before=self.checksum()
        with patch('src.api.user_buildings.observe',return_value=(None,'source_unavailable',None)):
            result=self.assert_status(self.patch2({'unset':['scenario_inputs.apartment_studio_count']},changed.headers['etag']),200)
            self.assertEqual(result.json()['scenario_inputs'],{'resident_count':5,'apartment_two_room_count':None})
            self.assertEqual(result.json()['field_provenance']['scenario_inputs.apartment_two_room_count']['status'],'user_unknown')
        self.assertEqual(self.checksum(),before)
        for version in (1,2):
            view=self.assert_status(self.client.get(share.json()['resolver_path']+'?personal_schema_version='+str(version)),200).json()
            self.assertEqual(view['schema_version'],version)
            self.assertNotIn('saved_id',view)
            self.assertEqual('apartment_two_room_count' in view['scenario_inputs'],version==2)
        self.assert_status(self.client.patch(PATH,json={},headers=self.auth(**{'If-Match':old_tag})),412)
        reset=self.assert_status(self.client.put(V2,json={'schema_version':2},headers=self.auth(**{'If-Match':result.headers['etag']})),200)
        self.assertEqual(reset.json()['scenario_inputs'],{});self.assertEqual(reset.json()['document_schema_version'],2)
        self.assert_status(self.client.delete(PATH+'/share',headers=self.auth(**{'If-Match':reset.headers['etag']})),204)
        self.assert_status(self.client.get(share.json()['resolver_path']),404)

    def test_04_two_writers_and_version_transition_race(self):
        saved=self.save({'scenario_inputs':{'resident_count':2}});tag=saved.headers['etag']
        def newer():return self.patch2({'set':{'scenario_inputs':{'apartment_four_plus_room_count':8}}},tag)
        def older():return self.client.patch(PATH,json={'set':{'overrides':{'floor_area':5}}},headers=self.auth(**{'If-Match':tag}))
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(lambda f:f(),(newer,older)))
        self.assertEqual(sorted(r.status_code for r in results),[200,412])
        latest=self.assert_status(self.client.get(V2,headers=self.auth()),200)
        retry=self.assert_status(self.patch2({'set':{'scenario_inputs':{'apartment_four_plus_room_count':8}}},latest.headers['etag']),200)
        self.assertEqual(retry.json()['scenario_inputs']['resident_count'],2)
        self.assertEqual(retry.json()['scenario_inputs']['apartment_four_plus_room_count'],8)

    def test_05_catalog_outage_historical_purpose_and_input_validation(self):
        with patch('src.api.user_buildings.purpose_validation_catalog',return_value=CodeCatalog({'main_purpose':frozenset({'old'})})):
            saved=self.save({'overrides':{'main_purpose':'old'}})
        result=self.assert_status(self.patch2({'set':{'scenario_inputs':{'apartment_three_room_count':0}}},saved.headers['etag']),200)
        self.assertEqual(result.json()['overrides']['main_purpose'],'old')
        before=self.stored()
        with patch('src.api.user_buildings.purpose_validation_catalog', side_effect=PurposeCatalogUnavailable):
            failed=self.assert_status(self.patch2({'set':{'overrides':{'main_purpose':'05'}}},result.headers['etag']),503)
        self.assertEqual(failed.json()['detail']['code'],'purpose_catalog_unavailable')
        self.assertEqual(self.stored(),before)
        for value in (True,-1,1.5,'1',1_000_001):
            self.assert_status(self.patch2({'set':{'scenario_inputs':{'apartment_three_room_count':value}}},result.headers['etag']),422)
            self.assertEqual(self.stored(),before)
        reset=self.assert_status(self.patch2({'unset':['overrides.main_purpose']},result.headers['etag']),200)
        self.assertNotIn('main_purpose',reset.json()['overrides'])

    def test_07_full_personal_shared_reopen_source_loss_and_atomic_resets(self):
        fields={'personal_energy_class':{'grade':'G','scale':2013},
                'personal_energy_class_date':{'precision':'year','year':2020},
                'annual_maintenance_charge_eur_cents':0, 'apartment_studio_count':0,
                'apartment_two_room_count':3,'apartment_three_room_count':None,
                'apartment_four_plus_room_count':1,'resident_count':2}
        public_response=self.client.get('/energy-data/buildings/'+KEY)
        self.assertEqual(public_response.status_code,200,public_response.text)
        public=public_response.json()
        saved=self.assert_status(self.client.put(V2,json={'schema_version':2,'scenario_inputs':fields},
            headers=self.auth(**{'If-None-Match':'*'})),201)
        official=saved.json()['effective']['certificate'];model=saved.json()['effective']['model']
        self.assertEqual(official,public['certificate']);self.assertEqual(model,public['model'])
        self.assertEqual((official['energy_class'],official['class_scale_year']),('B',2018))
        source_before=self.checksum()
        share=self.assert_status(self.client.post(PATH+'/share',json={},headers=self.auth(**{'If-Match':saved.headers['etag']})),201)
        tag=share.headers['etag']
        for name in ('personal_energy_class','personal_energy_class_date','annual_maintenance_charge_eur_cents'):
            unknown=self.assert_status(self.patch2({'set':{'scenario_inputs':{name:None}}},tag),200)
            self.assertEqual(unknown.json()['scenario_inputs'],{**fields,name:None})
            self.assertEqual(unknown.json()['field_provenance']['scenario_inputs.'+name]['status'],'user_unknown')
            restored=self.assert_status(self.patch2({'set':{'scenario_inputs':{name:fields[name]}}},unknown.headers['etag']),200)
            reset=self.assert_status(self.patch2({'unset':['scenario_inputs.'+name]},restored.headers['etag']),200)
            self.assertEqual(reset.json()['scenario_inputs'],{k:v for k,v in fields.items() if k!=name})
            self.assertEqual(reset.json()['field_provenance']['scenario_inputs.'+name]['reason'],'no_personal_default')
            restored=self.assert_status(self.patch2({'set':{'scenario_inputs':{name:fields[name]}}},reset.headers['etag']),200)
            tag=restored.headers['etag']
        # Read full new fields through a real capability without private metadata.
        view=self.assert_status(self.client.get(share.json()['resolver_path']+'?personal_schema_version=2'),200).json()
        self.assertEqual(view['scenario_inputs'],fields)
        self.assertEqual(view['effective']['scenario_inputs'],fields)
        self.assertNotIn('saved_id',view)
        self.assertNotIn('identity.address_key',view['field_provenance'])
        self.assertEqual(view['effective']['certificate'],official)
        self.assertEqual(view['effective']['model'],model)
        with patch('src.api.user_buildings.observe',return_value=(None,'source_unavailable',None)):
            changed=self.assert_status(self.patch2({'display_name':'Still saveable'},tag),200)
            self.assertEqual(changed.json()['scenario_inputs'],fields)
            self.assertTrue(changed.json()['input_status']['property']['saveable'])
        with patch('src.api.building_shares.observe',return_value=(None,'source_unavailable',None)):
            lost=self.assert_status(self.client.get(share.json()['resolver_path']+'?personal_schema_version=2'),200).json()
            self.assertEqual(lost['scenario_inputs'],fields)
            self.assertIsNone(lost['effective']['certificate'])
            self.assertEqual(lost['field_provenance']['scenario_inputs.personal_energy_class']['origin'],'user')
        self.assertEqual(self.checksum(),source_before)
        # A structurally valid saved year must not become a generic outage or be
        # revalidated as new input on unrelated writes or capability reads.
        historical={**fields,'personal_energy_class_date':{'precision':'year','year':9999}}
        with self.Admin.begin() as session:
            session.execute(text(f'UPDATE {TABLE} SET scenario_inputs=CAST(:document AS jsonb)'),{'document':json.dumps(historical)})
        current=self.assert_status(self.client.get(V2,headers=self.auth()),200)
        self.assertEqual(current.json()['scenario_inputs'],historical)
        self.assert_status(self.read(),200)
        self.assertEqual(self.assert_status(self.client.get(share.json()['resolver_path']+'?personal_schema_version=2'),200).json()['scenario_inputs'],historical)
        retained=self.assert_status(self.patch2({'display_name':'Year preserved'},current.headers['etag']),200)
        self.assertEqual(retained.json()['scenario_inputs'],historical)
        before=self.stored()
        rejected=self.assert_status(self.patch2({'set':{'scenario_inputs':{'personal_energy_class_date':{'precision':'year','year':9999}}}},retained.headers['etag']),422)
        self.assertEqual(rejected.json()['detail'][0]['loc'],['body','set','scenario_inputs','personal_energy_class_date','year'])
        self.assertEqual(self.stored(),before)

    def test_08_adopted_synthetic_draft_pin_discovery_write_parity_and_completeness(self):
        with adopted_pin():
            catalog=self.assert_status(self.client.get('/energy-data/building-purpose-choices'),200).json()
            self.assertEqual(catalog['scheme_status'],'DRAFT')
            old=self.save({'overrides':{'main_purpose':'01'}})
            tag=old.headers['etag']
            for choice in catalog['choices']:
                before=self.stored()
                result=self.assert_status(self.client.patch(PATH,json={'set':{'overrides':{'main_purpose':choice['code']}}},
                    headers=self.auth(**{'If-Match':tag})),200 if choice['selectable'] else 422)
                if choice['selectable']:tag=result.headers['etag']
                else:self.assertEqual(self.stored(),before)
            saved=self.assert_status(self.patch2({},tag),200)
            tag=saved.headers['etag']
            self.assertEqual(saved.json()['input_status']['property']['completeness'],'complete')
            for choice in catalog['choices']:
                before=self.stored()
                result=self.assert_status(self.patch2({'set':{'overrides':{'main_purpose':choice['code']}}},tag),200 if choice['selectable'] else 422)
                if choice['selectable']:
                    tag=result.headers['etag']
                    self.assertEqual(result.json()['effective']['details']['main_purpose'],choice['code'])
                else:
                    self.assertEqual(self.stored(),before)
                    self.assertEqual(result.json()['detail'][0]['loc'],['body','set','overrides','main_purpose'])
        # The bundled catalog supports all seven choices, including the two
        # retired only in the synthetic fixture above.
        current=self.assert_status(self.client.get(V2,headers=self.auth()),200)
        self.assertEqual(current.json()['input_status']['property']['completeness'],'complete')
        partial=self.assert_status(self.patch2({'set':{'overrides':{'construction_year':None}}},current.headers['etag']),200)
        self.assertEqual(partial.json()['input_status']['property']['missing_paths'],['details.construction_year'])

    def test_10_bundled_pin_v1_v2_put_patch_read_reset_and_outage(self):
        catalog=self.assert_status(self.client.get('/energy-data/building-purpose-choices'),200).json()
        self.assertEqual(catalog['catalog_version'],load_purpose_catalog().catalog_version)
        self.assertEqual(catalog['product_support'],'adopted')
        self.assertEqual([c['code'] for c in catalog['choices']],[f'{n:02}' for n in range(1,8)])
        self.assertTrue(all(c['selectable'] and c['upstream_status']=='DRAFT' for c in catalog['choices']))
        source_before=self.checksum()
        for version in (1,2):
            with self.subTest(version=version):
                path=PATH if version==1 else V2
                scenario={'resident_count':2}
                if version==2:scenario['apartment_studio_count']=0
                body={'schema_version':version,'display_name':'Purpose parity',
                      'overrides':{'main_purpose':'05'},'scenario_inputs':scenario}
                saved=self.assert_status(self.client.put(path,json=body,
                    headers=self.auth(**{'If-None-Match':'*'})),201)
                share=self.assert_status(self.client.post(PATH+'/share',json={},
                    headers=self.auth(**{'If-Match':saved.headers['etag']})),201)
                tag=share.headers['etag']
                shared_path=share.json()['resolver_path']+'?personal_schema_version='+str(version)
                for choice in catalog['choices']:
                    code=choice['code']
                    # Includes v1 full resubmission of a saved non-null purpose,
                    # the precise failure reproduced by functional review F1.
                    body['overrides']['main_purpose']=code
                    for method,payload in (('put',body),('patch',{'schema_version':version,
                            'set':{'overrides':{'main_purpose':code}}})):
                        changed=self.assert_status(getattr(self.client,method)(path,json=payload,
                            headers=self.auth(**{'If-Match':tag})),200)
                        tag=changed.headers['etag']
                        self.assertEqual(changed.json()['overrides']['main_purpose'],code)
                        self.assertEqual(changed.json()['scenario_inputs'],scenario)
                        self.assertEqual(self.stored()[0]['overrides']['main_purpose'],code)
                        self.assertEqual(self.stored()[0]['schema_version'],version)
                    for read_path,headers in ((path,self.auth()),(shared_path,{})):
                        view=self.assert_status(self.client.get(read_path,headers=headers),200).json()
                        self.assertEqual(view['effective']['details']['main_purpose'],code)
                        self.assertEqual(view['field_provenance']['details.main_purpose']['origin'],'user')
                        if version==2:
                            self.assertEqual(view['input_status']['property']['completeness'],'complete')
                before=self.stored()
                for bad in ('5','00','08','historical'):
                    invalid=self.assert_status(self.client.patch(path,json={'schema_version':version,
                        'set':{'overrides':{'main_purpose':bad}}},headers=self.auth(**{'If-Match':tag})),422)
                    self.assertEqual(invalid.json()['detail'][0]['loc'],['body','set','overrides','main_purpose'])
                    self.assertEqual(self.stored(),before)
                with patch('src.api.user_buildings.purpose_validation_catalog',side_effect=PurposeCatalogUnavailable):
                    for method,payload in (('put',body),('patch',{'schema_version':version,
                            'set':{'overrides':{'main_purpose':'05'}}})):
                        failed=self.assert_status(getattr(self.client,method)(path,json=payload,
                            headers=self.auth(**{'If-Match':tag})),503)
                        self.assertEqual(failed.json()['detail']['code'],'purpose_catalog_unavailable')
                        self.assertEqual(self.stored(),before)
                    # Reads and independent saves do not revalidate old codes;
                    # null and unset remain available during a real pin outage.
                    self.assert_status(self.client.get(path,headers=self.auth()),200)
                    for patch_body in ({'display_name':'Draft during outage'},
                            {'set':{'overrides':{'main_purpose':None}}},
                            {'unset':['overrides.main_purpose']}):
                        changed=self.assert_status(self.client.patch(path,json={'schema_version':version,**patch_body},
                            headers=self.auth(**{'If-Match':tag})),200)
                        tag=changed.headers['etag']
                        self.assertEqual(changed.json()['scenario_inputs'],scenario)
                        if 'set' in patch_body:
                            self.assertIsNone(changed.json()['overrides']['main_purpose'])
                        if 'unset' in patch_body:
                            self.assertNotIn('main_purpose',changed.json()['overrides'])
                            self.assertEqual(changed.json()['effective']['details']['main_purpose'],'05')
                self.assert_status(self.client.delete(path,headers=self.auth(**{'If-Match':tag})),204)
                self.assert_status(self.client.get(shared_path),404)
        self.assertEqual(self.checksum(),source_before)

    def test_09_mixed_old_count_only_full_v2_pagination_without_read_mutation(self):
        with self.Admin.begin() as session:
            for key in ('property-count-only','property-full'):
                session.execute(text('INSERT INTO buildings(building_key) VALUES (:key)'),{'key':key})
        self.save({'scenario_inputs':{'resident_count':1}})
        for key,scenario in (('property-count-only',{'apartment_studio_count':0}),
                             ('property-full',{'personal_energy_class':{'grade':'D','scale':2018},
                                'personal_energy_class_date':{'precision':'year','year':2020},
                                'annual_maintenance_charge_eur_cents':1})):
            self.assert_status(self.client.put('/energy-data/user/buildings/'+key+'?personal_schema_version=2',
                json={'schema_version':2,'scenario_inputs':scenario},headers=self.auth(**{'If-None-Match':'*'})),201)
        before=self.stored()
        for version in (1,2):
            items=[];cursor=None
            while True:
                query={'personal_schema_version':version,'limit':1}
                if cursor:query['cursor']=cursor
                page=self.assert_status(self.client.get('/energy-data/user/buildings',params=query,headers=self.auth()),200).json()
                items.extend(page['items']);cursor=page['next_cursor']
                if cursor is None:break
            self.assertEqual(len(items),3)
            self.assertEqual(len({item['saved_id'] for item in items}),3)
            if version==2:self.assertEqual(sorted(item['document_schema_version'] for item in items),[1,2,2])
            else:self.assertTrue(all('document_schema_version' not in item for item in items))
        self.assertEqual(self.stored(),before)
        self.assertEqual(self.assert_status(self.client.get('/energy-data/user/buildings',headers=self.auth('B')),200).json()['items'],[])

    def test_06_downgrade_refuses_v2_without_erasure(self):
        result=self.assert_status(self.client.put(V2,json={'schema_version':2},headers=self.auth(**{'If-None-Match':'*'})),201)
        before=self.stored()
        with self.Admin.begin() as session:
            with Operations.context(MigrationContext.configure(session.connection())):
                with self.assertRaisesRegex(RuntimeError,'downgrade refused'):definition().downgrade()
        self.assertEqual(self.stored(),before)
        self.assert_status(self.client.get(V2,headers=self.auth()),200)
