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
V4 = PATH + '?personal_schema_version=4'


@unittest.skipUnless(os.getenv('RUN_PERSONAL_RENOVATIONS_DB_TESTS') == '1', 'requires explicit owned 55433 fixtures')
class RenovationsDatabaseTests(unittest.TestCase):
    def setUp(self):
        # Cleanup stack registered before provision; fixtures never clear old data.
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.target, _ = self.stack.enter_context(state_pair())
        # This historical suite exercises the exact v4 profile, not latest head.
        from src.state_storage import operations
        upgrade = operations.command.upgrade
        with patch('src.state_storage.operations.command.upgrade',
                side_effect=lambda c, r: upgrade(c, 'es0004_renovations_v4')):
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

    def save(self, body=None, version=4):
        response = self.client.put(PATH + '?personal_schema_version=' + str(version),
            json=body or {'schema_version': version, 'scenario_inputs': {'renovations': all_renovations()}},
            headers=self.headers() | {'If-None-Match': '*'})
        self.check(response, 201)
        return response

    def change(self, body, tag):
        return self.client.patch(V4, json={'schema_version': 4, **body}, headers=self.headers(tag))

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
        saved = self.save({'schema_version': 4, 'overrides': {'floor_area': 0, 'total_area': None}, 'scenario_inputs': scenario})
        tag = saved.headers['etag']
        self.assertEqual(tag, f'"ub1:{saved.json()["saved_id"]}:1"')
        with patch('src.api.user_buildings.observe', return_value=(None, 'source_unavailable', None)), \
             patch('src.personal_hvac_contract.load_hvac_catalog', side_effect=HvacCatalogUnavailable):
            view = self.client.get(V4, headers=self.headers()); self.check(view, 200)
            self.assertEqual(view.json()['scenario_inputs'], scenario)
            edited = self.change({'set': {'scenario_inputs': {'renovations': all_renovations(0)}}}, tag)
            self.check(edited, 200)
            self.assertEqual(edited.json()['scenario_inputs']['renovations'], all_renovations(0))
            renamed = self.change({'display_name': 'During outage'}, edited.headers['etag']); self.check(renamed, 200)
            reset = self.change({'unset': ['scenario_inputs.renovations']}, renamed.headers['etag']); self.check(reset, 200)
            self.assertEqual(reset.json()['scenario_inputs'], {k: v for k, v in scenario.items() if k != 'renovations'})
            self.assertEqual(reset.json()['document_schema_version'], 4)
        code = "from src.db.state import state_session\nfrom sqlalchemy import text\nwith state_session(read_only=True) as s:\n r=s.execute(text('SELECT schema_version,scenario_inputs FROM energymap_state.energymap_user_buildings')).one()\n assert r[0]==4 and 'renovations' not in r[1] and r[1]['resident_count']==2\n"
        result = subprocess.run([sys.executable, '-c', code], env=os.environ.copy(), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, 'separate-process private reopen failed')
        replaced = self.client.put(V4, json={'schema_version': 4}, headers=self.headers(reset.headers['etag'])); self.check(replaced, 200)
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
                read = self.client.get(V4, headers=self.headers()); self.check(read, 200)
                self.assertEqual(read.json()['scenario_inputs']['renovations'], renovations)
                expected = 'missing' if value is ... else 'user_unknown' if value is None else 'available'
                self.assertEqual(read.json()['field_provenance'][path]['status'], expected)
                share = self.client.post(PATH + '/share', json={}, headers=self.headers(read.headers['etag']))
                self.check(share, 201)
                shared = self.client.get(share.json()['resolver_path'] + '?personal_schema_version=4'); self.check(shared, 200)
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

    def test_views_promotion_shared_privacy_lists_and_old_writer_delete_protection(self):
        initial = self.save({'schema_version': 3, 'overrides': {'floor_area': 0, 'total_area': None},
            'scenario_inputs': {'resident_count': 2, 'apartment_studio_count': 0, 'systems': full_systems()}}, version=3)
        share = self.client.post(PATH + '/share', json={}, headers=self.headers(initial.headers['etag'])); self.check(share, 201)
        promoted = self.change({}, share.headers['etag']); self.check(promoted, 200)
        for key in ('saved_id', 'created_at', 'scenario_inputs', 'overrides'):
            self.assertEqual(promoted.json()[key], initial.json()[key])
        self.assertEqual(promoted.json()['revision'], 3)
        self.assertTrue(promoted.json()['share_enabled'])
        saved = self.change({'set': {'scenario_inputs': {'renovations': all_renovations()}}}, promoted.headers['etag']); self.check(saved, 200)
        for version in (1, 2, 3, 4):
            suffix = '?personal_schema_version=' + str(version)
            expected = 409 if version in (2, 3) else 200
            self.check(self.client.get(PATH + suffix, headers=self.headers()), expected)
            public = self.client.get(share.json()['resolver_path'] + suffix); self.check(public, expected)
            page = self.client.get('/energy-data/user/buildings' + suffix, headers=self.headers()); self.check(page, expected)
            if expected == 200:
                data = public.json()
                def keys(value):
                    if isinstance(value, dict): return set(value) | set().union(*(keys(v) for v in value.values()), set())
                    if isinstance(value, list): return set().union(*(keys(v) for v in value), set())
                    return set()
                self.assertFalse({'saved_id', 'owner_issuer', 'owner_subject', 'token_digest'} & keys(data))
                self.assertEqual('renovations' in data['scenario_inputs'], version == 4)
                if version == 4:
                    self.assertEqual(data['scenario_inputs']['renovations'], all_renovations())
                self.assertNotIn('scenario_inputs', page.json()['items'][0])
        before = self.saved()
        for version in (1, 2, 3):
            path = PATH + '?personal_schema_version=' + str(version)
            for method, kwargs in (('patch', {'json': {'schema_version': version}}),
                                   ('put', {'json': {'schema_version': version}}), ('delete', {})):
                self.check(getattr(self.client, method)(path, headers=self.headers(saved.headers['etag']), **kwargs), 409)
                self.assertEqual(self.saved(), before)
        reset = self.change({'unset': ['scenario_inputs.renovations']}, saved.headers['etag']); self.check(reset, 200)
        before = self.saved()
        for version in (1, 2, 3):
            for method, kwargs in (('put', {'json': {'schema_version': version}}),
                                   ('patch', {'json': {'schema_version': version}}), ('delete', {})):
                self.check(getattr(self.client, method)(PATH + '?personal_schema_version=' + str(version),
                    headers=self.headers(reset.headers['etag']), **kwargs), 409)
                self.assertEqual(self.saved(), before)

    def test_mixed_old_rows_v4_reads_and_summary_pages_never_synthesize_history(self):
        with patch('src.api.user_buildings.observe', side_effect=lambda key, *args: (
                self.base.model_copy(update={'building_key': key}), 'available', None)):
            for version in (1, 2, 3, 4):
                path = '/energy-data/user/buildings/mixed-' + str(version)
                scenario = {'resident_count': None}
                if version >= 2: scenario['apartment_studio_count'] = 0
                if version >= 3: scenario['systems'] = full_systems()
                if version == 4: scenario['renovations'] = all_renovations()
                response = self.client.put(path + '?personal_schema_version=' + str(version),
                    json={'schema_version': version, 'scenario_inputs': scenario},
                    headers=self.headers() | {'If-None-Match': '*'})
                self.check(response, 201)
                before = self.saved()
                read = self.client.get(path + '?personal_schema_version=4', headers=self.headers()); self.check(read, 200)
                self.assertEqual(read.json()['schema_version'], 4)
                self.assertEqual(read.json()['document_schema_version'], version)
                self.assertEqual(read.json()['scenario_inputs'], scenario)
                self.assertEqual(self.saved(), before)
        page = self.client.get('/energy-data/user/buildings?personal_schema_version=4', headers=self.headers()); self.check(page, 200)
        self.assertEqual(sorted(i['document_schema_version'] for i in page.json()['items']), [1, 2, 3, 4])
        self.assertTrue(all('scenario_inputs' not in i and 'renovations' not in i for i in page.json()['items']))
        for version in (2, 3):
            self.check(self.client.get('/energy-data/user/buildings?personal_schema_version=' + str(version), headers=self.headers()), 409)
        self.check(self.client.get('/energy-data/user/buildings', headers=self.headers()), 200)
        with patch('src.api.user_buildings.observe', side_effect=lambda key, *args: (
                self.base.model_copy(update={'building_key': key}), 'available', None)):
            # Explicitly promote each old stored format through authenticated handlers.
            for version in (1, 2, 3):
                path = '/energy-data/user/buildings/mixed-' + str(version)
                before = self.client.get(path + '?personal_schema_version=4', headers=self.headers())
                self.check(before, 200)
                share = self.client.post(path + '/share', json={}, headers=self.headers(before.headers['etag']))
                self.check(share, 201)
                promoted = self.client.patch(path + '?personal_schema_version=4', json={'schema_version': 4},
                    headers=self.headers(share.headers['etag'])); self.check(promoted, 200)
                for key in ('saved_id', 'created_at', 'overrides', 'scenario_inputs'):
                    self.assertEqual(promoted.json()[key], before.json()[key])
                self.assertEqual(promoted.json()['document_schema_version'], 4)
                self.assertEqual(promoted.json()['revision'], before.json()['revision'] + 2)
                shared = self.client.get(share.json()['resolver_path'] + '?personal_schema_version=4')
                self.check(shared, 200)
                self.assertEqual(shared.json()['scenario_inputs'], before.json()['scenario_inputs'])

    def test_invalid_and_exact_owner_do_not_mutate(self):
        saved = self.save({'schema_version': 4, 'scenario_inputs': {'systems': full_systems(), 'renovations': all_renovations()}})
        before = self.saved()
        for r in ({'top_floor_insulation': {'completed': False, 'execution_year': None}},
                  {'top_floor_insulation': {'completed': True, 'added_insulation_thickness_mm': -1}},
                  {'top_floor_insulation': {'completed': None, 'added_insulation_thickness_mm': None}},
                  {'top_floor_insulation': {'added_insulation_thickness_mm': 0}}, {'windows': {'completed': 0}},
                  {'roof': {'completed': True, 'execution_year': 2025}}):
            for method in ('put', 'patch'):
                body = {'scenario_inputs': {'renovations': r}}
                if method == 'patch': body = {'set': body}
                self.check(getattr(self.client, method)(V4, json={'schema_version': 4, **body},
                    headers=self.headers(saved.headers['etag'])), 422)
                self.assertEqual(self.saved(), before)
        changed = full_systems(); changed['heating']['sources'][0]['source_code'] = 'ryhti:heating_energy_source:07'
        for method in ('patch', 'put'):
            body = {'scenario_inputs': {'systems': changed}}
            if method == 'patch': body = {'set': body}
            response = getattr(self.client, method)(V4, json={'schema_version': 4, **body}, headers=self.headers(saved.headers['etag']))
            self.check(response, 422); self.assertEqual(response.json()['detail'][0]['type'], 'source_identity_reused')
            self.assertEqual(self.saved(), before)
        original = dict(self.body)
        self.body['sub'] = 'other-owner'
        for method, kwargs in (('get', {}), ('patch', {'json': {'schema_version': 4}}), ('delete', {})):
            self.check(getattr(self.client, method)(V4, headers=self.headers(saved.headers['etag']), **kwargs), 404)
        self.body = original | {'iss': 'https://another-issuer.invalid'}
        self.introspector = ZitadelIntrospector(settings(issuer=self.body['iss']), transport=self.introspector.transport, clock=lambda: 1000)
        self.check(self.client.get(V4, headers=self.headers()), 404)
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
        latest = self.client.get(V4, headers=self.headers()); self.check(latest, 200)
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
        latest = self.client.get(V4, headers=self.headers()); self.check(latest, 200)
        share = self.client.post(PATH + '/share', json={}, headers=self.headers(latest.headers['etag'])); self.check(share, 201)
        self.check(self.client.get(share.json()['resolver_path'] + '?personal_schema_version=4'), 200)
        self.check(self.client.delete(PATH + '/share', headers=self.headers(share.headers['etag'])), 204)
        self.check(self.client.get(share.json()['resolver_path'] + '?personal_schema_version=4'), 404)

    def test_old_write_preflight_racing_promotion_is_rechecked_under_lock(self):
        initial = self.save({'schema_version': 3}, version=3)
        arrived, resume = threading.Event(), threading.Event()
        def observe(key, supplied_codes=False):
            if threading.current_thread().name.startswith('old-writer') or not arrived.is_set():
                # TestClient runs the endpoint on an AnyIO worker: only the first
                # observed request is delayed, before the v4 request starts.
                arrived.set()
                if not resume.wait(10): raise RuntimeError('bounded race timeout')
            return self.base, 'available', None
        with patch('src.api.user_buildings.observe', side_effect=observe), ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(self.client.put, PATH + '?personal_schema_version=3', json={'schema_version': 3}, headers=self.headers(initial.headers['etag']))
            try:
                self.assertTrue(arrived.wait(10))
                promoted = self.change({}, initial.headers['etag']); self.check(promoted, 200)
            finally:
                resume.set()
            self.check(old.result(timeout=15), 412)
        self.assertEqual(self.saved()[0]['schema_version'], 4)
        self.assertEqual(self.saved()[0]['revision'], 2)

    def test_combined_migration_readiness_refusal_and_retry(self):
        # Same shared definition as the combined lineage, in owned state DB.
        with migrator_connection(self.target) as connection:
            with Operations.context(MigrationContext.configure(connection)): definition().downgrade()
            connection.exec_driver_sql("UPDATE energymap_state_meta.alembic_version SET version_num='es0003_personal_hvac_v3'")
        self.check(self.client.get('/readyz/state?personal_schema_version=4'), 503)
        self.check(self.client.put(V4, json={'schema_version': 4}, headers=self.headers() | {'If-None-Match': '*'}), 503)
        with migrator_connection(self.target) as connection:
            with Operations.context(MigrationContext.configure(connection)): definition().upgrade()
            connection.exec_driver_sql("UPDATE energymap_state_meta.alembic_version SET version_num='es0004_renovations_v4'")
        self.check(self.client.get('/readyz/state?personal_schema_version=4'), 200)
        saved = self.save({'schema_version': 4})
        before = self.saved()
        with self.assertRaisesRegex(RuntimeError, 'downgrade refused'), migrator_connection(self.target) as connection:
            with Operations.context(MigrationContext.configure(connection)): definition().downgrade()
        self.assertEqual(self.saved(), before)
        with state_session(read_only=True) as session:
            self.assertEqual(personal_schema_capability(session), 4)
