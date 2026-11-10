"""Historical/mixed-v4 archives and NEW-target recovery on owned PostgreSQL pairs."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.api.auth import AuthenticatedPrincipal
from src.api.building_examples import public_example
from src.building_contract import CanonicalBuildingV1, PersonalBuildingPutV1, personal_document, sparse
from src.personal_property_contract import PersonalBuildingPutV2
from src.personal_hvac_contract import PersonalBuildingPutV3
from src.personal_renovations_contract import PersonalBuildingPutV4
from src.building_share_contract import shared_projection
from src.db.state import state_session, state_ready
from src import user_buildings as owner, building_shares as shares
from src.state_storage import operations, recovery
from src.state_storage.catalog import (HISTORICAL_VERSION, PROPERTY_VERSION, HVAC_VERSION, VERSION,
    audit_schema, audit_acl, rows, scalar)
from src.state_storage.private import StorageError, read_json
from tests.personal_state_fixture import state_pair, migrator_connection
from tests.test_state_storage_db import runtime_environment
from tests.test_personal_hvac import full_systems
from tests.test_personal_renovations import all_renovations
from state_migrations.versions.es0004_renovations_v4 import definition

PROFILES = (HISTORICAL_VERSION, PROPERTY_VERSION, HVAC_VERSION, VERSION)
PRINCIPAL = AuthenticatedPrincipal('https://synthetic.invalid', 'renovations-owner')


def seed(target, profile):
    models = (PersonalBuildingPutV1, PersonalBuildingPutV2, PersonalBuildingPutV3, PersonalBuildingPutV4)
    base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
    tokens = []
    with runtime_environment(target, operations.load_bundle(target)), state_session() as session:
        selected = models[:PROFILES.index(profile) + 1]
        if profile == VERSION: selected += (PersonalBuildingPutV4,) * 3
        for index, model in enumerate(selected, 1):
            version = min(index, 4)
            scenario = {'resident_count': 2}
            if version >= 2: scenario |= {'apartment_studio_count': 0, 'apartment_two_room_count': None}
            if version >= 3: scenario['systems'] = full_systems()
            if version >= 4: scenario['renovations'] = all_renovations((230.5, 0, None, ...)[index - 4])
            value = model.model_validate({'schema_version': version, 'overrides': {'floor_area': 0, 'total_area': None}, 'scenario_inputs': scenario})
            key = 'synthetic-v' + str(index)
            row = owner.create_owned(session, PRINCIPAL, key, value, base.model_copy(update={'building_key': key}))
            tokens.append(shares.rotate(session, row))
        session.commit()
    return tokens


@unittest.skipUnless(os.getenv('RUN_PERSONAL_RENOVATIONS_DB_TESTS') == '1', 'requires explicit owned 55433 fixtures')
class RenovationsStorageTests(unittest.TestCase):
    def test_all_archive_profiles_upgrade_retry_fingerprints_and_capabilities(self):
        for profile in PROFILES:
            with self.subTest(profile=profile), state_pair() as (target, restored):
                with operations.connected(target, database=target['source_database'], read_only=True) as source:
                    source_before = rows(source, 'SELECT to_jsonb(t) FROM public.source_probe t ORDER BY probe_id')
                upgrade = operations.command.upgrade
                with patch('src.state_storage.operations.command.upgrade', side_effect=lambda c, r: upgrade(c, profile)):
                    self.assertEqual(operations.apply(target)['version'], profile)
                tokens = seed(target, profile)
                with runtime_environment(target, operations.load_bundle(target)):
                    for version in (1, 2, 3, 4):
                        self.assertEqual(state_ready(version), version <= PROFILES.index(profile) + 1)
                with operations.connected(target, database=target['database'], read_only=True) as c:
                    before = recovery.data_fingerprint(c); audit_schema(c, profile); audit_acl(c, target)
                self.assertEqual(recovery.backup(target)['version'], profile)
                # Historical command destinations remain EXACT, not alias-to-head.
                if profile in (HISTORICAL_VERSION, PROPERTY_VERSION):
                    self.assertEqual(operations.upgrade_personal_v3(target)['version'], HVAC_VERSION)
                if profile != VERSION:
                    self.assertEqual(operations.upgrade_personal_v4(target)['version'], VERSION)
                for old_upgrade in (operations.upgrade_personal_v2, operations.upgrade_personal_v3):
                    with self.assertRaises(StorageError): old_upgrade(target)
                with operations.connected(target, database=target['database'], read_only=True) as c:
                    after = recovery.data_fingerprint(c)
                    self.assertEqual({k: v for k, v in before.items() if k != 'alembic_version'},
                                     {k: v for k, v in after.items() if k != 'alembic_version'})
                # First interruption: pg_restore committed, grants transaction has
                # committed, upgrade has NOT run. Writer remains disabled.
                original_migrate = recovery.migrate_personal_v4
                with patch('src.state_storage.recovery.migrate_personal_v4', side_effect=RuntimeError('test_before_upgrade')):
                    with self.assertRaisesRegex(RuntimeError, 'test_before_upgrade'): recovery.restore(restored)
                with operations.connected(restored, database=restored['database'], read_only=True) as c:
                    self.assertFalse(scalar(c, 'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s', (restored['writer'],)))
                    audit_schema(c, profile)
                def interrupted(t, bundle):
                    original_migrate(t, bundle)
                    raise RuntimeError('test_after_upgrade_commit')
                with patch('src.state_storage.recovery.migrate_personal_v4', side_effect=interrupted):
                    with self.assertRaisesRegex(RuntimeError, 'test_after_upgrade_commit'): recovery.restore(restored)
                with operations.connected(restored, database=restored['database'], read_only=True) as c:
                    audit_schema(c, VERSION)
                    self.assertFalse(scalar(c, 'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s', (restored['writer'],)))
                # Committed restore/upgrade retry must never replay pg_restore.
                original_client = recovery.client
                def no_replay(t, program, args, **kwargs):
                    if program == 'pg_restore' and '--list' not in args: raise AssertionError('archive replay')
                    return original_client(t, program, args, **kwargs)
                with patch('src.state_storage.recovery.client', side_effect=no_replay):
                    self.assertEqual(recovery.restore(restored)['status'], 'restore_verified')
                with operations.connected(restored, database=restored['database'], read_only=True) as c:
                    audit_schema(c, VERSION); audit_acl(c, restored)
                    self.assertTrue(recovery.restored_data_matches(c, read_json(Path(target['backup_dir'])/'manifest.json')))
                with runtime_environment(restored, operations.load_bundle(restored)):
                    self.assertTrue(state_ready(4))
                    with state_session(read_only=True) as s:
                        for index, token in enumerate(tokens, 1):
                            version = min(index, 4)
                            row = shares.lookup(s, token)
                            read = owner.detail(row, None, version=4)
                            shared = shared_projection(row, None, 'source_unavailable', 4)
                            self.assertEqual(read.document_schema_version, version)
                            self.assertEqual(shared.scenario_inputs, read.scenario_inputs)
                            if version == 4:
                                expected = all_renovations((230.5, 0, None, ...)[index - 4])
                                self.assertEqual(personal_document(owner.stored_document(row))['scenario_inputs']['renovations'], expected)
                                self.assertEqual(sparse(read.scenario_inputs)['renovations'], expected)
                    with state_session() as s:
                        row = shares.lookup(s, tokens[-1]); rotated = shares.rotate(s, row); s.commit()
                    with state_session() as s:
                        with self.assertRaises(shares.ShareNotFound): shares.lookup(s, tokens[-1])
                        row = shares.lookup(s, rotated); shares.revoke(s, row); s.commit()
                    with state_session() as s:
                        with self.assertRaises(shares.ShareNotFound): shares.lookup(s, rotated)
                        row = owner.load_owned(s, PRINCIPAL, 'synthetic-v' + str(len(tokens)), lock=True)
                        token = shares.rotate(s, row); s.commit()
                    with state_session() as s:
                        row = shares.lookup(s, token); s.delete(row); s.commit()
                    with state_session(read_only=True) as s:
                        with self.assertRaises(shares.ShareNotFound): shares.lookup(s, token)
                with operations.connected(target, database=target['source_database'], read_only=True) as source:
                    self.assertEqual(source_before, rows(source, 'SELECT to_jsonb(t) FROM public.source_probe t ORDER BY probe_id'))

    def test_backup_concurrent_mutation_has_one_complete_snapshot(self):
        with state_pair() as (target, restored):
            operations.apply(target); tokens = seed(target, VERSION)
            with operations.connected(target, database=target['database'], read_only=True) as c:
                before = recovery.data_fingerprint(c)
            original_client = recovery.client
            changed = False
            def concurrent_save(t, program, args, **kwargs):
                nonlocal changed
                if program == 'pg_dump' and '--version' not in args and not changed:
                    changed = True
                    with runtime_environment(target, operations.load_bundle(target)), state_session() as session:
                        row = shares.lookup(session, tokens[3]); owner.bump_revision(row)
                        row.scenario_inputs = dict(row.scenario_inputs, renovations=all_renovations(99))
                        session.commit()
                return original_client(t, program, args, **kwargs)
            with patch('src.state_storage.recovery.client', side_effect=concurrent_save): recovery.backup(target)
            self.assertTrue(changed)
            manifest = read_json(Path(target['backup_dir'])/'manifest.json')
            self.assertEqual(manifest['data'], before)
            with operations.connected(target, database=target['database'], read_only=True) as c:
                self.assertNotEqual(recovery.data_fingerprint(c), before)
            recovery.restore(restored)
            with operations.connected(restored, database=restored['database'], read_only=True) as c:
                self.assertEqual(recovery.data_fingerprint(c), before)

    def test_downgrade_waits_for_insert_then_refuses_v4(self):
        with state_pair() as (target, _):
            operations.apply(target)
            base = CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
            entered = threading.Event()
            def downgrade():
                with migrator_connection(target) as c:
                    c.exec_driver_sql("SET application_name='test-renovations-downgrade'")
                    entered.set()
                    with Operations.context(MigrationContext.configure(c)): definition().downgrade()
            with runtime_environment(target, operations.load_bundle(target)), state_session() as session:
                owner.create_owned(session, PRINCIPAL, base.building_key, PersonalBuildingPutV4(schema_version=4), base)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    task = pool.submit(downgrade)
                    try:
                        self.assertTrue(entered.wait(10))
                        deadline = time.monotonic() + 10
                        waiting = False
                        while time.monotonic() < deadline:
                            with operations.connected(target, read_only=True) as c:
                                waiting = scalar(c, "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name='test-renovations-downgrade' AND wait_event_type='Lock')")
                            if waiting: break
                            time.sleep(0.02)
                        self.assertTrue(waiting, 'actual exclusive-lock wait not observed')
                    finally:
                        session.commit()  # release insert; DDL must then see v4
                    with self.assertRaisesRegex(RuntimeError, 'downgrade refused'): task.result(timeout=15)
            with operations.connected(target, database=target['database'], read_only=True) as c:
                audit_schema(c, VERSION)
                self.assertEqual(scalar(c, 'SELECT schema_version FROM energymap_state.energymap_user_buildings'), 4)

    def test_wrong_ownership_and_restore_to_origin_refuse_without_mutation(self):
        from uuid import uuid4
        from src.state_storage.config import Target
        from tests.personal_state_fixture import cleanup_target
        with state_pair() as (target, _):
            operations.apply(target); seed(target, VERSION)
            with operations.connected(target, database=target['database'], read_only=True) as c:
                before = recovery.data_fingerprint(c)
            impostor = Target(dict(target.values, provisioning_id=str(uuid4())))
            with self.assertRaises(StorageError): cleanup_target(impostor)
            recovery.backup(target)
            origin = Target(dict(target.values, restore_from=target['backup_dir']))
            with self.assertRaises(StorageError): recovery.restore(origin)
            with operations.connected(target, database=target['database'], read_only=True) as c:
                self.assertEqual(recovery.data_fingerprint(c), before)
                audit_schema(c, VERSION); audit_acl(c, target)

    def test_fixture_finalizer_cleans_after_intentional_failure(self):
        target = None
        with self.assertRaisesRegex(RuntimeError, 'synthetic test failure'):
            with state_pair() as (target, _):
                operations.apply(target)
                seed(target, VERSION)
                raise RuntimeError('synthetic test failure')
        # The fixed service remains reachable; exact created roles/DB are gone.
        from tests.personal_state_fixture import fixture_template
        with operations.connected(fixture_template(), read_only=True) as c:
            self.assertFalse(scalar(c, 'SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=%s)', (target['database'],)))
            self.assertFalse(scalar(c, 'SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname IN (%s,%s))', (target['migrator'], target['writer'])))
