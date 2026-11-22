"""Personal v5 HVAC API on exact disposable F001 fixture; synthetic provider/source."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from sqlalchemy import text

from src.hvac_catalog import HvacCatalogUnavailable
from tests.test_personal_hvac import IDS, full_systems, systems
from tests import test_user_buildings_db as fixture

V5=fixture.PATH+'?personal_schema_version=5'


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

    setUp = fixture.OwnerDatabaseTests.setUp

    def status(self,response,status):
        self.assertEqual(response.status_code,status,response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        return response

    def save_systems(self,value=None):
        return self.status(self.client.put(V5,json={'schema_version':5,'scenario_inputs':{'systems':value or systems({'kind':'selected','code':'0'})}},
            headers=self.auth(**{'If-None-Match':'*'})),201)

    def change_systems(self,body,tag):
        return self.client.patch(V5,json={'schema_version':5,**body},headers=self.auth(**{'If-Match':tag}))

    def test_real_partial_save_reopen_outage_and_source_preservation(self):
        before=self.checksum()
        saved=self.save_systems(full_systems());tag=saved.headers['etag']
        self.assertEqual(saved.json()['systems_defaults']['coefficients']['status'],'deferred')
        self.assertEqual(saved.json()['scenario_inputs']['systems'], full_systems())
        changed=self.status(self.change_systems({'set':{'overrides':{'ventilation_type_id':'0','ventilation_description':'Legacy note'},
            'scenario_inputs':{'apartment_studio_count':0,'apartment_two_room_count':None}}},tag),200)
        expected=changed.json()['scenario_inputs']
        with patch('src.api.user_buildings.observe',return_value=(None,'source_unavailable',None)):
            view=self.status(self.client.get(V5,headers=self.auth()),200)
            self.assertEqual(view.json()['scenario_inputs'],expected)
        code="from src.db.state import state_session;from sqlalchemy import text;\nwith state_session(read_only=True) as s:\n d=s.execute(text('SELECT schema_version,scenario_inputs FROM energymap_state.energymap_user_buildings')).one();assert d[0]==5 and d[1]=="+repr(expected)
        result=subprocess.run([sys.executable,'-c',code],env=os.environ.copy(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
        self.assertEqual(result.returncode,0,'separate process reopen failed')
        self.assertEqual(self.checksum(),before)
        reset=self.status(self.change_systems({'set':{'scenario_inputs':{'systems':systems(None)}}},changed.headers['etag']),200)
        self.assertIsNone(reset.json()['scenario_inputs']['systems']['ventilation'])
        removed=self.status(self.change_systems({'unset':['scenario_inputs.systems']},reset.headers['etag']),200)
        self.assertNotIn('systems',removed.json()['scenario_inputs'])
        self.assertEqual(removed.json()['document_schema_version'],5)

    def test_historical_choices_numeric_edits_and_source_rebuild_preserve_answers(self):
        from src.hvac_catalog import load_hvac_catalog
        saved = self.save_systems(full_systems())
        # Product source rebuild is exercised against the guarded synthetic source.
        before = self.stored(); fingerprint = self.checksum()
        self.merge()
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.checksum(), fingerprint)
        catalog = load_hvac_catalog().model_copy(deep=True)
        choice = next(c for c in catalog.heating_choices if c.code == 'ryhti:heating_energy_source:01')
        choice.selectable = False; choice.lifecycle = 'deprecated'  # synthetic lifecycle fixture
        with patch('src.personal_hvac_contract.load_hvac_catalog', return_value=catalog):
            view = self.status(self.client.get(V5, headers=self.auth()), 200)
            self.assertEqual(view.json()['systems_source_provenance'][0]['source_code']['status'], 'unsupported')
            self.assertEqual(view.json()['systems_source_provenance'][0]['share_percent']['status'], 'available')
            answers = full_systems(); answers['heating']['sources'][0]['share_percent'] = -11.25
            edited = self.status(self.change_systems({'set': {'scenario_inputs': {'systems': answers}}}, saved.headers['etag']), 200)
            self.assertEqual(edited.json()['scenario_inputs']['systems'], answers)
            self.assertEqual(self.checksum(), fingerprint)
            before = self.stored()
            # A newly created identity still needs current choice validation.
            answers['heating']['sources'][0]['source_id'] = '44444444-4444-4444-8444-444444444444'
            answers['heating']['primary_source_id'] = answers['heating']['sources'][0]['source_id']
            self.status(self.change_systems({'set': {'scenario_inputs': {'systems': answers}}}, edited.headers['etag']), 422)
            self.assertEqual(self.stored(), before)
        with patch('src.api.user_buildings.observe', return_value=(None, 'source_unavailable', None)):
            unavailable = self.status(self.client.get(V5, headers=self.auth()), 200)
            self.assertEqual(unavailable.json()['scenario_inputs']['systems']['heating']['sources'][0]['share_percent'], -11.25)
            self.assertEqual(unavailable.json()['field_provenance']['scenario_inputs.systems.heated_quantities.total_area_m2']['status'], 'available')
        self.status(self.client.delete(V5, headers=self.auth(**{'If-Match': edited.headers['etag']})), 204)

    def test_real_concurrency_exact_identity_and_invalid_no_mutation(self):
        saved=self.save_systems(full_systems());before=self.stored()
        for issuer,subject in ((self.issuer,'B'),('https://other.example.invalid','A')):
            previous=self.issuer;self.issuer=issuer
            self.status(self.client.get(V5,headers=self.auth(subject)),404)
            self.issuer=previous
        self.status(self.change_systems({'set':{'scenario_inputs':{'systems':systems({'kind':'selected','code':'unknown'})}}},saved.headers['etag']),422)
        self.assertEqual(before,self.stored())
        # Same-ETag collection edits: reorder/primary and a source-share change.
        def write(index):
            value = full_systems()
            if index == 0:
                value['heating']['sources'].reverse()
                value['heating']['primary_source_id'] = IDS[1]
            else:
                value['heating']['sources'][0]['share_percent'] = -25.5
            return self.change_systems({'set': {'scenario_inputs': {'systems': value}}}, saved.headers['etag'])
        with ThreadPoolExecutor(max_workers=2) as pool: responses=list(pool.map(write,(0,1)))
        self.assertEqual(sorted(r.status_code for r in responses),[200,412])
        self.assertEqual(self.stored()[0]['revision'],2)
        current=next(r for r in responses if r.status_code==200)
        reconciled = current.json()['scenario_inputs']['systems']
        reconciled['heating']['primary_source_id'] = IDS[1]
        next(s for s in reconciled['heating']['sources'] if s['source_id'] == IDS[0])['share_percent'] = -25.5
        current = self.status(self.change_systems({'set': {'scenario_inputs': {'systems': reconciled}}}, current.headers['etag']), 200)
        self.assertEqual(self.stored()[0]['revision'], 3)
        with patch('src.personal_hvac_contract.load_hvac_catalog',side_effect=HvacCatalogUnavailable):
            self.status(self.client.get(V5,headers=self.auth()),200)
            self.status(self.change_systems({'set':{'scenario_inputs':{'systems':systems({'kind':'selected','code':'2'})}}},current.headers['etag']),503)
            renamed=self.status(self.change_systems({'display_name':'During outage'},current.headers['etag']),200)
            self.status(self.change_systems({'unset':['scenario_inputs.systems']},renamed.headers['etag']),200)

    def test_full_collections_replacement_errors_sharing_reset_and_unknown(self):
        saved = self.save_systems(full_systems())
        tag = saved.headers['etag']
        # Rejected coefficients/speculative equipment never mutate the accepted row.
        before = self.stored()
        for extra in ({'coefficients': {'electricity': 0}}, {'attribution': 'unsupported'}, {'equipment': {'battery_capacity': 1}}):
            self.status(self.change_systems({'set': {'scenario_inputs': {'systems': full_systems() | extra}}}, tag), 422)
            self.assertEqual(self.stored(), before)
        changed = full_systems(); changed['heating']['sources'][0]['source_code'] = 'ryhti:heating_energy_source:07'
        for method in ('patch', 'put'):
            body = {'schema_version': 5, 'scenario_inputs': {'systems': changed}}
            if method == 'patch': body = {'schema_version': 5, 'set': {'scenario_inputs': {'systems': changed}}}
            response = getattr(self.client, method)(V5, json=body, headers=self.auth(**{'If-Match': tag}))
            self.status(response, 422)
            self.assertEqual(response.json()['detail'][0]['type'], 'source_identity_reused')
            self.assertEqual(self.stored(), before)
        share = self.status(self.client.post(fixture.PATH + '/share', json={}, headers=self.auth(**{'If-Match': tag})), 201)
        tag = share.headers['etag']
        shared = self.status(self.client.get(share.json()['resolver_path'] + '?personal_schema_version=5'), 200)
        self.assertEqual(shared.json()['scenario_inputs']['systems'], full_systems())
        self.assertEqual(shared.json()['systems_source_provenance'], saved.json()['systems_source_provenance'])
        self.assertNotIn('saved_id', shared.json())
        read_before = self.stored()
        self.status(self.client.get('/energy-data/building-hvac-choices'), 200)
        self.status(self.client.get(V5, headers=self.auth()), 200)
        self.assertEqual(self.stored(), read_before)
        # Remove primary and one answer; re-add without resurrecting the removed share.
        answers = full_systems(); answers['heating']['sources'] = answers['heating']['sources'][1:]
        answers['heating']['primary_source_id'] = None
        removed = self.status(self.change_systems({'set': {'scenario_inputs': {'systems': answers}}}, tag), 200)
        answers['heating']['sources'].append({'source_id': '44444444-4444-4444-8444-444444444444', 'source_code': 'ryhti:heating_energy_source:01'})
        readded = self.status(self.change_systems({'set': {'scenario_inputs': {'systems': answers}}}, removed.headers['etag']), 200)
        self.assertNotIn('share_percent', readded.json()['scenario_inputs']['systems']['heating']['sources'][-1])
        with patch('src.personal_hvac_contract.load_hvac_catalog', side_effect=HvacCatalogUnavailable):
            self.status(self.change_systems({'set': {'scenario_inputs': {'systems': systems() | {'heated_quantities': {'total_area_m2': -1.5}, 'equipment': {'geothermal_installed': False}}}}}, readded.headers['etag']), 200)
        current = self.status(self.client.get(V5, headers=self.auth()), 200)
        reset = self.status(self.change_systems({'set': {'scenario_inputs': {'systems': None}}}, current.headers['etag']), 200)
        self.assertIsNone(reset.json()['scenario_inputs']['systems'])
        self.status(self.client.delete(V5, headers=self.auth(**{'If-Match': reset.headers['etag']})), 204)
        self.status(self.client.get(share.json()['resolver_path']), 404)
