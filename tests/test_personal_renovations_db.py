"""Real PostgreSQL v4 transactions, synthetic source/auth, owned state fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import ANY, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from src.api.auth import ZitadelIntrospector
from src.api.building_examples import public_example
from src.api.buildings import building_session
from src.api.main import app
from src.building_contract import CanonicalBuildingV1
from src.building_resolver import Resolution
from src.db.state import state_session, personal_schema_capability
from src.hvac_catalog import HvacCatalogUnavailable
from src.state_storage.operations import apply, load_bundle
from tests.personal_state_fixture import state_pair, migrator_connection
from tests.test_state_storage_db import runtime_environment
from tests.test_personal_hvac import full_systems
from tests.test_personal_renovations import all_renovations
from tests import test_user_buildings as http_fixture
from tests.test_auth import settings
from state_migrations.versions.es0004_renovations_v4 import definition

PATH = '/energy-data/user/buildings/synthetic%2Fopaque'
V5 = PATH + '?personal_schema_version=5'


@unittest.skipUnless(os.getenv('RUN_PERSONAL_RENOVATIONS_DB_TESTS') == '1', 'requires explicit owned 55433 fixtures')
class RenovationsDatabaseTests(unittest.TestCase):
    def setUp(self):
        # Cleanup stack registered before provision; fixtures never clear old data.
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.target, _ = self.stack.enter_context(state_pair())
        apply(self.target)
        http_fixture.OwnerHttpBoundaryTests.setUp(self)
        self.enterContext(runtime_environment(self.target, load_bundle(self.target)))
        self.base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example())).model_copy(update={'building_key': 'synthetic/opaque'})
        self.enterContext(patch('src.api.user_buildings.observe', return_value=(self.base, 'available', None)))
        self.enterContext(patch('src.api.user_buildings.SessionLocal'))
        self.enterContext(patch('src.api.user_buildings.resolve_building_summaries', return_value={}))

    check = http_fixture.OwnerHttpBoundaryTests.check

    def saved(self):
        with state_session(read_only=True) as session:
            return [dict(row) for row in session.execute(text('SELECT * FROM energymap_state.energymap_user_buildings ORDER BY saved_id')).mappings()]

    def headers(self, tag=None):
        return {'Authorization': 'Bearer valid-fixture', **({'If-Match': tag} if tag else {})}

    def save(self, body=None, version=5):
        response = self.client.put(PATH + '?personal_schema_version=' + str(version),
            json=body or {'schema_version': version, 'scenario_inputs': {'renovations': all_renovations()}},
            headers=self.headers() | {'If-None-Match': '*'})
        self.check(response, 201)
        return response

    def change(self, body, tag):
        return self.client.patch(V5, json={'schema_version': 5, **body}, headers=self.headers(tag))

    def test_public_canonical_response_unchanged_across_personal_save_patch_reset(self):
        self.enterContext(patch.dict(app.dependency_overrides, {building_session: lambda: object()}))
        resolve = self.enterContext(patch('src.api.buildings.resolve_building',
            return_value=Resolution('available', self.base)))

        def public_read():
            response = self.client.get('/energy-data/buildings/synthetic%2Fopaque')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-cache')
            resolve.assert_called_with(ANY, 'synthetic/opaque')
            value = response.json()
            self.assertNotIn('scenario_inputs', value)
            self.assertNotIn('overrides', value)
            self.assertNotIn('renovations', response.text)
            self.assertTrue(all(p['origin'] != 'user' for p in value['field_provenance'].values()))
            return value

        # The synthetic source has fixed resolved_at metadata; compare it too.
        before = public_read()
        self.assertEqual(before, self.base.model_dump(mode='json'))
        saved = self.save()
        self.assertEqual(public_read(), before)
        changed = self.change({'set': {'scenario_inputs': {'renovations': {
            'facade': {'completed': False}, 'top_floor_insulation': {'completed': True, 'execution_year': 2025, 'added_insulation_thickness_mm': 0}
        }}}}, saved.headers['etag']); self.check(changed, 200)
        self.assertEqual(public_read(), before)
        reset = self.change({'unset': ['scenario_inputs.renovations']}, changed.headers['etag']); self.check(reset, 200)
        self.assertEqual(public_read(), before)

    def test_save_reopen_siblings_reset_put_and_private_source_independence(self):
        canonical_before = self.base.model_dump(mode='json')
        scenario = {'resident_count': 2, 'apartment_studio_count': 0, 'apartment_two_room_count': None,
                    'systems': full_systems(), 'renovations': all_renovations()}
        saved = self.save({'schema_version': 5, 'overrides': {'floor_area': 0, 'total_area': None}, 'scenario_inputs': scenario})
        tag = saved.headers['etag']
        self.assertEqual(tag, f'"ub1:{saved.json()["saved_id"]}:1"')
        with patch('src.api.user_buildings.observe', return_value=(None, 'source_unavailable', None)), \
             patch('src.personal_hvac_contract.load_hvac_catalog', side_effect=HvacCatalogUnavailable):
            view = self.client.get(V5, headers=self.headers()); self.check(view, 200)
            self.assertEqual(view.json()['scenario_inputs'], scenario)
            edited = self.change({'set': {'scenario_inputs': {'renovations': all_renovations(0)}}}, tag)
            self.check(edited, 200)
            self.assertEqual(edited.json()['scenario_inputs']['renovations'], all_renovations(0))
            renamed = self.change({'display_name': 'During outage'}, edited.headers['etag']); self.check(renamed, 200)
            reset = self.change({'unset': ['scenario_inputs.renovations']}, renamed.headers['etag']); self.check(reset, 200)
            self.assertEqual(reset.json()['scenario_inputs'], {k: v for k, v in scenario.items() if k != 'renovations'})
            self.assertEqual(reset.json()['document_schema_version'], 5)
        code = "from src.db.state import state_session\nfrom sqlalchemy import text\nwith state_session(read_only=True) as s:\n r=s.execute(text('SELECT schema_version,scenario_inputs FROM energymap_state.energymap_user_buildings')).one()\n assert r[0]==5 and 'renovations' not in r[1] and r[1]['resident_count']==2\n"
        result = subprocess.run([sys.executable, '-c', code], env=os.environ.copy(), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, 'separate-process private reopen failed')
        replaced = self.client.put(V5, json={'schema_version': 5}, headers=self.headers(reset.headers['etag'])); self.check(replaced, 200)
        self.assertEqual(replaced.json()['scenario_inputs'], {})
        self.assertEqual(replaced.json()['overrides'], {})
        self.assertEqual(self.base.model_dump(mode='json'), canonical_before)

    def test_thickness_committed_reopen_shared_and_separate_process(self):
        saved = self.save()
        path = 'scenario_inputs.renovations.top_floor_insulation.added_insulation_thickness_mm'
        for value in (230.5, 0, None, ...):
            with self.subTest(value=value):
                renovations = all_renovations(value)
                saved = self.change({'set': {'scenario_inputs': {'renovations': renovations}}}, saved.headers['etag'])
                self.check(saved, 200)
                read = self.client.get(V5, headers=self.headers()); self.check(read, 200)
                self.assertEqual(read.json()['scenario_inputs']['renovations'], renovations)
                expected = 'missing' if value is ... else 'user_unknown' if value is None else 'available'
                self.assertEqual(read.json()['field_provenance'][path]['status'], expected)
                share = self.client.post(PATH + '/share', json={}, headers=self.headers(read.headers['etag']))
                self.check(share, 201)
                shared = self.client.get(share.json()['resolver_path'] + '?personal_schema_version=5'); self.check(shared, 200)
                self.assertEqual(shared.json()['scenario_inputs']['renovations'], renovations)
                self.assertEqual(shared.json()['field_provenance'][path], read.json()['field_provenance'][path])
                # A new interpreter/connection checks the actual committed JSONB, not a model copy.
                code = ("import json\nfrom src.db.state import state_session\nfrom sqlalchemy import text\n"
                    "with state_session(read_only=True) as s:\n"
                    " r=s.execute(text('SELECT scenario_inputs FROM energymap_state.energymap_user_buildings')).scalar_one()\n"
                    " assert r['renovations']==json.loads(" + repr(json.dumps(renovations)) + ")\n")
                result = subprocess.run([sys.executable, '-c', code], env=os.environ.copy(), capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, 'separate-process thickness reopen failed')
                saved = share

    def test_invalid_and_exact_owner_do_not_mutate(self):
        saved = self.save({'schema_version': 5, 'scenario_inputs': {'systems': full_systems(), 'renovations': all_renovations()}})
        before = self.saved()
        for r in ({'top_floor_insulation': {'completed': False, 'execution_year': None}},
                  {'top_floor_insulation': {'completed': True, 'added_insulation_thickness_mm': -1}},
                  {'top_floor_insulation': {'completed': None, 'added_insulation_thickness_mm': None}},
                  {'top_floor_insulation': {'added_insulation_thickness_mm': 0}}, {'windows': {'completed': 0}},
                  {'roof': {'completed': True, 'execution_year': 2025}}):
            for method in ('put', 'patch'):
                body = {'scenario_inputs': {'renovations': r}}
                if method == 'patch': body = {'set': body}
                self.check(getattr(self.client, method)(V5, json={'schema_version': 5, **body},
                    headers=self.headers(saved.headers['etag'])), 422)
                self.assertEqual(self.saved(), before)
        changed = full_systems(); changed['heating']['sources'][0]['source_code'] = 'ryhti:heating_energy_source:07'
        for method in ('patch', 'put'):
            body = {'scenario_inputs': {'systems': changed}}
            if method == 'patch': body = {'set': body}
            response = getattr(self.client, method)(V5, json={'schema_version': 5, **body}, headers=self.headers(saved.headers['etag']))
            self.check(response, 422); self.assertEqual(response.json()['detail'][0]['type'], 'source_identity_reused')
            self.assertEqual(self.saved(), before)
        original = dict(self.body)
        self.body['sub'] = 'other-owner'
        for method, kwargs in (('get', {}), ('patch', {'json': {'schema_version': 5}}), ('delete', {})):
            self.check(getattr(self.client, method)(V5, headers=self.headers(saved.headers['etag']), **kwargs), 404)
        self.body = original | {'iss': 'https://another-issuer.invalid'}
        self.introspector = ZitadelIntrospector(settings(issuer=self.body['iss']), transport=self.introspector.transport, clock=lambda: 1000)
        self.check(self.client.get(V5, headers=self.headers()), 404)
        self.assertEqual(self.saved(), before)

    def test_actual_same_etag_race_reconcile_and_share_rotation(self):
        saved = self.save(); tag = saved.headers['etag']
        renovations = {'top_floor_insulation': {'completed': True, 'added_insulation_thickness_mm': 0}}
        bodies = ({'set': {'scenario_inputs': {'renovations': renovations}}},
                  {'set': {'scenario_inputs': {'apartment_studio_count': 0}}})
        barrier = threading.Barrier(2)
        def observe(*args):
            barrier.wait(timeout=10)
            return self.base, 'available', None
        with patch('src.api.user_buildings.observe', side_effect=observe), ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda body: self.change(body, tag), bodies))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 412])
        winner = next(r for r in responses if r.status_code == 200)
        latest = self.client.get(V5, headers=self.headers()); self.check(latest, 200)
        self.assertEqual(latest.headers['etag'], winner.headers['etag'])
        reconciled = self.change({'set': {'scenario_inputs': {'apartment_studio_count': 0,
            'renovations': renovations}}}, latest.headers['etag']); self.check(reconciled, 200)
        self.assertEqual(self.saved()[0]['scenario_inputs']['renovations'], renovations)
        self.assertEqual(self.saved()[0]['scenario_inputs']['apartment_studio_count'], 0)
        self.assertEqual(reconciled.json()['revision'], 3)
        tag = reconciled.headers['etag']
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.change, {'unset': ['scenario_inputs.renovations']}, tag),
                       pool.submit(self.client.post, PATH + '/share', json={}, headers=self.headers(tag))]
            results = [f.result() for f in futures]
        self.assertEqual(sorted(r.status_code for r in results), [200 if results[0].status_code == 200 else 201, 412])
        latest = self.client.get(V5, headers=self.headers()); self.check(latest, 200)
        share = self.client.post(PATH + '/share', json={}, headers=self.headers(latest.headers['etag'])); self.check(share, 201)
        self.check(self.client.get(share.json()['resolver_path'] + '?personal_schema_version=5'), 200)
        self.check(self.client.delete(PATH + '/share', headers=self.headers(share.headers['etag'])), 204)
        self.check(self.client.get(share.json()['resolver_path'] + '?personal_schema_version=5'), 404)
