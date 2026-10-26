"""Post-commit recovery regression on two pristine doctor-owned targets only."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from psycopg2 import sql

from src.db.state import state_ready
from src.state_storage import recovery
from src.state_storage.catalog import audit_acl, rows, scalar
from src.state_storage.config import load_target
from src.state_storage.operations import connected, inspect, load_bundle, output_texts, save_bundle, verify
from src.state_storage.private import StorageError, read_json


RETRY = 'ENERGY_MAP_STATE_RESTORE_RETRY_CONFIG'
MIGRATED = 'ENERGY_MAP_STATE_RESTORE_MIGRATED_CONFIG'


@unittest.skipUnless(os.getenv(RETRY), 'requires pristine doctor-owned restore descriptors')
class InterruptedRestoreDatabaseTests(unittest.TestCase):
    def resume_interrupted_restore(self, config, phase):
        target = load_target(os.environ[config])
        self.assertTrue(target['mode'] == 'disposable' and
                        target['host'] == '127.0.0.1' and target['port'] == 55433)
        self.assertEqual(inspect(target)['status'], 'target_absent')
        manifest = read_json(Path(target['restore_from']) / 'manifest.json')
        real_client = recovery.client

        def interrupt_after_commit(t, program, args, **kwargs):
            result = real_client(t, program, args, **kwargs)
            if program == 'pg_restore' and any(arg.startswith('--dbname=') for arg in args):
                raise RuntimeError('post_restore_commit')
            return result

        with patch('src.state_storage.recovery.client', side_effect=interrupt_after_commit):
            with self.assertRaisesRegex(RuntimeError, '^post_restore_commit$'):
                recovery.restore(target)
        pending = load_bundle(target)
        self.assertEqual(pending['phase'], 'migrating')
        if phase == 'migrated':
            # Reproduce the journal left by the former schema-present return.
            save_bundle(target, pending, 'migrated')
        with connected(target, database=target['database']) as connection:
            self.assertTrue(recovery.data_fingerprint(connection) == manifest['data'])
            self.assertFalse(scalar(connection, 'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s', (target['writer'],)))
            audit_acl(connection, target, writer_login=False, allow_missing_state_grants=True)
            connection.autocommit = False
            for statement in (
                sql.SQL('GRANT UPDATE ON energymap_state.energymap_user_buildings TO {}').format(sql.Identifier(target['writer'])),
                sql.SQL('GRANT SELECT ON energymap_state.energymap_user_buildings TO {} WITH GRANT OPTION').format(sql.Identifier(target['writer'])),
                sql.SQL('GRANT USAGE ON SCHEMA energymap_state TO PUBLIC'),
            ):
                rows(connection, statement)
                with self.assertRaises(StorageError):
                    audit_acl(connection, target, writer_login=False, allow_missing_state_grants=True)
                connection.rollback()

        real_grant = recovery.grant_state

        def interrupt_grants(connection, t):
            real_grant(connection, t)
            raise RuntimeError('before_grant_commit')

        # Even an interruption after all GRANT statements rolls back the whole
        # reconstruction and must not advance the journal or enable the writer.
        with patch('src.state_storage.recovery.grant_state', side_effect=interrupt_grants):
            with self.assertRaisesRegex(RuntimeError, '^before_grant_commit$'):
                recovery.restore(target)
        self.assertEqual(load_bundle(target)['phase'], phase)
        with connected(target, database=target['database'], read_only=True) as connection:
            self.assertFalse(scalar(connection, 'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s', (target['writer'],)))
            self.assertFalse(scalar(connection, "SELECT has_schema_privilege(%s,'energymap_state','USAGE')", (target['writer'],)))
            self.assertTrue(recovery.data_fingerprint(connection) == manifest['data'])

        def no_restore_replay(t, program, args, **kwargs):
            self.assertFalse(program == 'pg_restore' and any(arg.startswith('--dbname=') for arg in args))
            return real_client(t, program, args, **kwargs)

        with patch('src.state_storage.recovery.client', side_effect=no_restore_replay):
            self.assertEqual(recovery.restore(target)['status'], 'restore_verified')
        completed = load_bundle(target)
        self.assertEqual(completed['phase'], 'complete')
        self.assertTrue(all(completed[key] == value for key, value in pending.items() if key != 'phase'))
        self.assertEqual(verify(target)['status'], 'verified')
        _, runtime = output_texts(target, completed)
        with patch.dict(os.environ, dict(line.split('=', 1) for line in runtime.splitlines())):
            self.assertTrue(state_ready())
        with connected(target, database=target['database'], read_only=True) as connection:
            self.assertTrue(recovery.data_fingerprint(connection) == manifest['data'])
        with patch('src.state_storage.recovery.grant_state', side_effect=AssertionError('completed_target_repair')):
            self.assertEqual(recovery.restore(target)['status'], 'restore_verified')

    def test_resume_from_migrating_after_restore_commit(self):
        self.resume_interrupted_restore(RETRY, 'migrating')

    def test_resume_from_retained_legacy_migrated_phase(self):
        self.resume_interrupted_restore(MIGRATED, 'migrated')
