"""Real v5 PostgreSQL/HTTP proof, synthetic auth/source, existing guarded service."""
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
from tests.test_personal_utilities import utilities
from tests import test_user_buildings as http_fixture
from tests.test_auth import settings
from state_migrations.versions.es0005_utilities_v5 import definition

PATH = '/energy-data/user/buildings/synthetic%2Fopaque'
V5 = PATH + '?personal_schema_version=5'


@unittest.skipUnless(os.getenv('RUN_PERSONAL_UTILITIES_DB_TESTS') == '1', 'requires explicit owned 55433 fixtures')
class UtilitiesDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.target, _ = self.stack.enter_context(state_pair())
        apply(self.target)
        http_fixture.OwnerHttpBoundaryTests.setUp(self)
        self.enterContext(runtime_environment(self.target, load_bundle(self.target)))
        self.base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example())).model_copy(update={'building_key': 'synthetic/opaque'})
        self.enterContext(patch('src.api.user_buildings.observe', return_value=(self.base, 'available', None)))
        self.enterContext(patch('src.api.building_shares.observe', side_effect=lambda key, *args: (self.base.model_copy(update={'building_key': key}), 'available', None)))
        self.enterContext(patch('src.api.user_buildings.SessionLocal'))
        self.enterContext(patch('src.api.user_buildings.resolve_building_summaries', return_value={}))

    check = http_fixture.OwnerHttpBoundaryTests.check

    def saved(self):
        with state_session(read_only=True) as session:
            return [dict(row) for row in session.execute(text('SELECT * FROM energymap_state.energymap_user_buildings ORDER BY saved_id')).mappings()]

    def headers(self, tag=None):
        return {'Authorization': 'Bearer valid-fixture', **({'If-Match': tag} if tag else {})}

    def save(self, body=None, version=5, path=PATH):
        response = self.client.put(path + '?personal_schema_version=' + str(version),
            json=body or {'schema_version': version, 'scenario_inputs': {'utilities': utilities()}},
            headers=self.headers() | {'If-None-Match': '*'})
        self.check(response, 201)
        return response

    def change(self, body, tag):
        return self.client.patch(V5, json={'schema_version': 5, **body}, headers=self.headers(tag))

    def test_committed_reopen_provenance_sparse_quantities_and_sibling_preservation(self):
        scenario = {'resident_count': 2, 'apartment_studio_count': 0, 'apartment_two_room_count': None,
            'systems': full_systems(), 'renovations': all_renovations(0), 'utilities': utilities()}
        saved = self.save({'schema_version': 5, 'overrides': {'floor_area': 0, 'total_area': None}, 'scenario_inputs': scenario})
        for u in (utilities(), utilities(0), utilities(None), {'electricity': {}, 'water': None},
                  {'electricity': {'grid_import_kwh': 0}, 'water': {'calendar_year': None}}, None, {}):
            saved = self.change({'set': {'scenario_inputs': {'utilities': u}}}, saved.headers['etag']); self.check(saved, 200)
            read = self.client.get(V5, headers=self.headers()); self.check(read, 200)
            self.assertEqual(read.json()['scenario_inputs'], dict(scenario, utilities=u))
            self.assertEqual(read.json()['effective']['scenario_inputs'], dict(scenario, utilities=u))
            self.assertEqual(read.json()['overrides'], {'floor_area': 0, 'total_area': None})
            share = self.client.post(PATH + '/share', json={}, headers=self.headers(read.headers['etag'])); self.check(share, 201)
            public = self.client.get(share.json()['resolver_path'] + '?personal_schema_version=5'); self.check(public, 200)
            self.assertEqual(public.json()['scenario_inputs'], read.json()['scenario_inputs'])
            self.assertEqual(public.json()['field_provenance'], read.json()['field_provenance'])
            # New interpreter and connection, not merely a projected model/cache.
            code = ("import json\nfrom src.db.state import state_session\nfrom sqlalchemy import text\n"
                "with state_session(read_only=True) as s:\n"
                " r=s.execute(text('SELECT schema_version,scenario_inputs FROM energymap_state.energymap_user_buildings')).one()\n"
                " assert r[0]==5 and r[1]==json.loads(" + repr(json.dumps(dict(scenario, utilities=u))) + ")\n")
            result = subprocess.run([sys.executable, '-c', code], env=os.environ.copy(), capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, 'separate-process utilities reopen failed')
            saved = share
        with patch('src.api.user_buildings.observe', return_value=(None, 'source_unavailable', None)), \
             patch('src.personal_hvac_contract.load_hvac_catalog', side_effect=HvacCatalogUnavailable):
            edited = self.change({'set': {'scenario_inputs': {'utilities': utilities(0)}}}, saved.headers['etag']); self.check(edited, 200)
            renamed = self.change({'display_name': 'Outage draft'}, edited.headers['etag']); self.check(renamed, 200)
            reset = self.change({'unset': ['scenario_inputs.utilities']}, renamed.headers['etag']); self.check(reset, 200)
            self.assertEqual(reset.json()['scenario_inputs'], {k: v for k, v in scenario.items() if k != 'utilities'})
            read = self.client.get(V5, headers=self.headers()); self.check(read, 200)
            self.assertEqual(read.json()['field_provenance']['scenario_inputs.utilities']['status'], 'missing')
        readded = self.change({'set': {'scenario_inputs': {'utilities': {'water': {}}}}}, reset.headers['etag']); self.check(readded, 200)
        self.assertEqual(readded.json()['scenario_inputs']['utilities'], {'water': {}})
        sibling = self.change({'set': {'scenario_inputs': {'renovations': all_renovations(None)}}}, readded.headers['etag']); self.check(sibling, 200)
        self.assertEqual(sibling.json()['scenario_inputs']['utilities'], {'water': {}})
        replaced = self.client.put(V5, json={'schema_version': 5}, headers=self.headers(sibling.headers['etag'])); self.check(replaced, 200)
        self.assertEqual(replaced.json()['scenario_inputs'], {}); self.assertEqual(replaced.json()['overrides'], {})

    def test_all_versions_projection_promotion_summary_and_old_writer_delete_guards(self):
        with patch('src.api.user_buildings.observe', side_effect=lambda key, *args: (self.base.model_copy(update={'building_key': key}), 'available', None)):
            for version in range(1, 6):
                path = '/energy-data/user/buildings/mixed-' + str(version)
                scenario = {'resident_count': None}
                if version >= 2: scenario['apartment_studio_count'] = 0
                if version >= 3: scenario['systems'] = full_systems()
                if version >= 4: scenario['renovations'] = all_renovations()
                if version == 5: scenario['utilities'] = utilities()
                initial = self.save({'schema_version': version, 'scenario_inputs': scenario}, version, path)
                read = self.client.get(path + '?personal_schema_version=5', headers=self.headers()); self.check(read, 200)
                self.assertEqual(read.json()['scenario_inputs'], scenario)
                self.assertEqual(read.json()['document_schema_version'], version)
            page = self.client.get('/energy-data/user/buildings?personal_schema_version=5', headers=self.headers()); self.check(page, 200)
            self.assertEqual(sorted(i['document_schema_version'] for i in page.json()['items']), [1, 2, 3, 4, 5])
            for view in (2, 3, 4): self.check(self.client.get('/energy-data/user/buildings?personal_schema_version=' + str(view), headers=self.headers()), 409)
            for version in range(1, 6):
                path = '/energy-data/user/buildings/mixed-' + str(version)
                initial = self.client.get(path + '?personal_schema_version=5', headers=self.headers()); self.check(initial, 200)
                scenario = initial.json()['scenario_inputs']
                before = self.saved()
                read = self.client.get(path + '?personal_schema_version=5', headers=self.headers()); self.check(read, 200)
                self.assertEqual(read.json()['scenario_inputs'], scenario)
                self.assertEqual(read.json()['document_schema_version'], version); self.assertEqual(self.saved(), before)
                share = self.client.post(path + '/share', json={}, headers=self.headers(initial.headers['etag'])); self.check(share, 201)
                promoted = self.client.patch(path + '?personal_schema_version=5', json={'schema_version': 5}, headers=self.headers(share.headers['etag'])); self.check(promoted, 200)
                for key in ('saved_id', 'created_at', 'overrides', 'scenario_inputs'): self.assertEqual(promoted.json()[key], read.json()[key])
                self.assertEqual(promoted.json()['revision'], 3); self.assertTrue(promoted.json()['share_enabled'])
                for view in range(1, 6):
                    suffix = '?personal_schema_version=' + str(view)
                    expected = 409 if view in (2, 3, 4) else 200
                    self.check(self.client.get(path + suffix, headers=self.headers()), expected)
                    shared = self.client.get(share.json()['resolver_path'] + suffix); self.check(shared, expected)
                    if expected == 200:
                        def keys(value):
                            if isinstance(value, dict): return set(value) | set().union(*(keys(v) for v in value.values()), set())
                            if isinstance(value, list): return set().union(*(keys(v) for v in value), set())
                            return set()
                        self.assertFalse({'saved_id', 'owner_issuer', 'owner_subject', 'token_digest'} & keys(shared.json()))
                        self.assertEqual(shared.json()['scenario_inputs'], scenario if view == 5 else {'resident_count': None})
                before = self.saved()
                for old in range(1, 5):
                    for method, kwargs in (('put', {'json': {'schema_version': old}}), ('patch', {'json': {'schema_version': old}}), ('delete', {})):
                        self.check(getattr(self.client, method)(path + '?personal_schema_version=' + str(old), headers=self.headers(promoted.headers['etag']), **kwargs), 409)
                        self.assertEqual(self.saved(), before)
        page = self.client.get('/energy-data/user/buildings?personal_schema_version=5', headers=self.headers()); self.check(page, 200)
        self.assertEqual(len(page.json()['items']), 5)
        self.assertTrue(all('scenario_inputs' not in i for i in page.json()['items']))
        for view in (2, 3, 4): self.check(self.client.get('/energy-data/user/buildings?personal_schema_version=' + str(view), headers=self.headers()), 409)
        self.check(self.client.get('/energy-data/user/buildings', headers=self.headers()), 200)

    def test_invalid_owner_hvac_identity_and_failure_atomicity(self):
        saved = self.save({'schema_version': 5, 'scenario_inputs': {'utilities': utilities(), 'renovations': all_renovations(), 'systems': full_systems()}})
        share = self.client.post(PATH + '/share', json={}, headers=self.headers(saved.headers['etag'])); self.check(share, 201)
        before = self.saved()
        for u in ({'water': {'supplied_volume_m3': -1}}, {'electricity': {'calendar_year': 9999}}, {'water': {'calendar_year': True}}, {'water': {'billing': 'private-canary'}}):
            for method in ('put', 'patch'):
                body = {'scenario_inputs': {'utilities': u}}
                if method == 'patch': body = {'set': body}
                self.check(getattr(self.client, method)(V5, json={'schema_version': 5, **body}, headers=self.headers(share.headers['etag'])), 422)
                self.assertEqual(self.saved(), before)
        changed = full_systems(); changed['heating']['sources'][0]['source_code'] = 'ryhti:heating_energy_source:07'
        for method in ('put', 'patch'):
            body = {'scenario_inputs': {'systems': changed}}
            if method == 'patch': body = {'set': body}
            response = getattr(self.client, method)(V5, json={'schema_version': 5, **body}, headers=self.headers(share.headers['etag']))
            self.check(response, 422); self.assertEqual(response.json()['detail'][0]['type'], 'source_identity_reused')
            self.assertEqual(self.saved(), before)
        self.check(self.change({}, saved.headers['etag']), 412)
        self.check(self.change({}, None), 428)
        self.check(self.client.put(V5, content=' ' * 32769, headers=self.headers(share.headers['etag'])), 413)
        # Exercise the merged-document budget through the existing size-check seam.
        from src.building_contract import check_payload_size
        limit = len(json.dumps({'schema_version': 5, 'set': {'scenario_inputs': {'utilities': utilities(0)}}}, separators=(',', ':')).encode()) + 20
        def merged_budget(value):
            with patch('src.building_contract.PERSONAL_PAYLOAD_BYTES', limit):
                check_payload_size(value)
        with patch('src.personal_utilities_contract.check_payload_size', side_effect=merged_budget):
            result = self.change({'set': {'scenario_inputs': {'utilities': utilities(0)}}}, share.headers['etag'])
            self.check(result, 413)
        self.assertEqual(self.saved(), before)
        original = dict(self.body); self.body['sub'] = 'other-owner'
        for method, kwargs in (('get', {}), ('patch', {'json': {'schema_version': 5}}), ('delete', {})):
            self.check(getattr(self.client, method)(V5, headers=self.headers(share.headers['etag']), **kwargs), 404)
        self.body = original | {'iss': 'https://another-issuer.invalid'}
        self.introspector = ZitadelIntrospector(settings(issuer=self.body['iss']), transport=self.introspector.transport, clock=lambda: 1000)
        self.check(self.client.get(V5, headers=self.headers()), 404); self.assertEqual(self.saved(), before)

    def test_real_races_same_tag_sibling_and_share_reset_then_reconcile(self):
        saved = self.save(); tag = saved.headers['etag']
        barrier = threading.Barrier(2)
        def observe(*args):
            barrier.wait(timeout=10); return self.base, 'available', None
        bodies = ({'set': {'scenario_inputs': {'utilities': utilities(0)}}}, {'set': {'scenario_inputs': {'renovations': all_renovations(None)}}})
        with patch('src.api.user_buildings.observe', side_effect=observe), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda body: self.change(body, tag), bodies))
        self.assertEqual(sorted(r.status_code for r in results), [200, 412])
        latest = self.client.get(V5, headers=self.headers()); self.check(latest, 200)
        reconciled = self.change({'set': {'scenario_inputs': {'utilities': utilities(0), 'renovations': all_renovations(None)}}}, latest.headers['etag']); self.check(reconciled, 200)
        self.assertEqual(reconciled.json()['revision'], 3)
        with patch('src.api.user_buildings.observe', side_effect=observe), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda body: self.change(body, reconciled.headers['etag']), (
                {'set': {'scenario_inputs': {'utilities': utilities(None)}}}, {'unset': ['scenario_inputs.utilities']})))
        self.assertEqual(sorted(r.status_code for r in results), [200, 412])
        latest = self.client.get(V5, headers=self.headers()); self.check(latest, 200)
        tag = latest.headers['etag']
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.change, {'unset': ['scenario_inputs.utilities']}, tag),
                pool.submit(self.client.post, PATH + '/share', json={}, headers=self.headers(tag))]
            responses = [f.result(timeout=20) for f in jobs]
        self.assertEqual(sum(r.status_code == 412 for r in responses), 1)
        latest = self.client.get(V5, headers=self.headers()); self.check(latest, 200)
        reset = self.change({'unset': ['scenario_inputs.utilities']}, latest.headers['etag']); self.check(reset, 200)
        self.assertEqual(reset.json()['scenario_inputs']['renovations'], all_renovations(None))
        for old in range(1, 5):
            for method, kwargs in (('patch', {'json': {'schema_version': old}}), ('put', {'json': {'schema_version': old}}), ('delete', {})):
                self.check(getattr(self.client, method)(PATH + '?personal_schema_version=' + str(old), headers=self.headers(reset.headers['etag']), **kwargs), 409)
        share = self.client.post(PATH + '/share', json={}, headers=self.headers(reset.headers['etag'])); self.check(share, 201)
        rotated = self.client.post(PATH + '/share', json={}, headers=self.headers(share.headers['etag'])); self.check(rotated, 201)
        self.check(self.client.get(share.json()['resolver_path'] + '?personal_schema_version=5'), 404)
        self.check(self.client.delete(PATH + '/share', headers=self.headers(rotated.headers['etag'])), 204)
        self.check(self.client.get(rotated.json()['resolver_path'] + '?personal_schema_version=5'), 404)

    def test_old_writer_paused_before_promotion_and_incarnation_delete(self):
        saved = self.save({'schema_version': 4, 'scenario_inputs': {'renovations': all_renovations(0)}}, version=4)
        arrived, resume = threading.Event(), threading.Event()
        def observe(*args):
            if not arrived.is_set():
                arrived.set()
                if not resume.wait(10): raise RuntimeError('bounded race timeout')
            return self.base, 'available', None
        with patch('src.api.user_buildings.observe', side_effect=observe), ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(self.client.put, PATH + '?personal_schema_version=4', json={'schema_version': 4}, headers=self.headers(saved.headers['etag']))
            try:
                self.assertTrue(arrived.wait(10))
                promoted = self.change({'set': {'scenario_inputs': {'utilities': utilities()}}}, saved.headers['etag']); self.check(promoted, 200)
            finally: resume.set()
            self.check(old.result(timeout=15), 412)
        self.assertEqual(promoted.json()['scenario_inputs']['renovations'], all_renovations(0))
        share = self.client.post(PATH + '/share', json={}, headers=self.headers(promoted.headers['etag'])); self.check(share, 201)
        self.check(self.client.delete(V5, headers=self.headers(share.headers['etag'])), 204)
        self.check(self.client.get(share.json()['resolver_path'] + '?personal_schema_version=5'), 404)
        recreated = self.save(); self.assertNotEqual(recreated.json()['saved_id'], saved.json()['saved_id'])
        self.check(self.change({}, promoted.headers['etag']), 412)

    def test_readiness_additive_definition_and_unvalidated_constraint_fail_closed(self):
        with migrator_connection(self.target) as connection:
            with Operations.context(MigrationContext.configure(connection)): definition().downgrade()
            connection.exec_driver_sql("UPDATE energymap_state_meta.alembic_version SET version_num='es0004_renovations_v4'")
        self.check(self.client.get('/readyz/state?personal_schema_version=5'), 503)
        response = self.client.put(V5, json={'schema_version': 5}, headers=self.headers() | {'If-None-Match': '*'})
        self.check(response, 503); self.assertEqual(response.json()['detail']['code'], 'personal_schema_upgrade_required')
        with migrator_connection(self.target) as connection:
            with Operations.context(MigrationContext.configure(connection)): definition().upgrade()
            connection.exec_driver_sql("UPDATE energymap_state_meta.alembic_version SET version_num='es0005_utilities_v5'")
        self.check(self.client.get('/readyz/state?personal_schema_version=5'), 200)
        self.save({'schema_version': 5}); before = self.saved()
        with self.assertRaisesRegex(RuntimeError, 'downgrade refused'), migrator_connection(self.target) as connection:
            with Operations.context(MigrationContext.configure(connection)): definition().downgrade()
        self.assertEqual(self.saved(), before)
        with state_session(read_only=True) as session: self.assertEqual(personal_schema_capability(session), 5)
        with migrator_connection(self.target) as c:
            c.exec_driver_sql('ALTER TABLE energymap_state.energymap_user_buildings DROP CONSTRAINT user_buildings_version')
            c.exec_driver_sql('ALTER TABLE energymap_state.energymap_user_buildings ADD CONSTRAINT user_buildings_version CHECK (schema_version IN (1,2,3,4,5)) NOT VALID')
        self.check(self.client.get('/readyz/state?personal_schema_version=5'), 503)
        with migrator_connection(self.target) as c:
            c.exec_driver_sql('ALTER TABLE energymap_state.energymap_user_buildings VALIDATE CONSTRAINT user_buildings_version')
        self.check(self.client.get('/readyz/state?personal_schema_version=5'), 503)


    def test_public_response_unchanged_and_new_source_required(self):
        self.enterContext(patch.dict(app.dependency_overrides, {building_session: lambda: object()}))
        resolve = self.enterContext(patch('src.api.buildings.resolve_building', return_value=Resolution('available', self.base)))
        def public():
            response = self.client.get('/energy-data/buildings/synthetic%2Fopaque'); self.assertEqual(response.status_code, 200)
            resolve.assert_called_with(ANY, 'synthetic/opaque')
            self.assertEqual(response.json(), self.base.model_dump(mode='json'))
            self.assertNotIn('utilities', response.text)
            return response.json()
        before = public(); saved = self.save(); self.assertEqual(before, public())
        edited = self.change({'set': {'scenario_inputs': {'utilities': utilities(0)}}}, saved.headers['etag']); self.check(edited, 200)
        self.assertEqual(before, public())
        reset = self.change({'unset': ['scenario_inputs.utilities']}, edited.headers['etag']); self.check(reset, 200); self.assertEqual(before, public())
        self.check(self.client.delete(V5, headers=self.headers(reset.headers['etag'])), 204); self.assertEqual(before, public())
        for reason, status in (('source_unavailable', 503), ('missing', 404), ('ambiguous', 409)):
            with patch('src.api.user_buildings.observe', return_value=(None, reason, None)):
                self.check(self.client.put(V5, json={'schema_version': 5}, headers=self.headers() | {'If-None-Match': '*'}), status)
        self.assertEqual(self.saved(), [])
