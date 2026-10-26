"""PUBLIC-CONNECT acceptance through the fixed doctor-owned HBA bridge only.

The bridge owns fixture HBA/source ACL lifecycle and refuses any caller-selected
server or resource. No sandbox path or ad-hoc destructive SQL is accepted here.
"""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import psycopg2

from src.state_storage.catalog import audit_source, scalar
from src.state_storage.config import ROOT, load_target
from src.state_storage.operations import apply, connected, load_bundle, verify
from src.state_storage.private import StorageError, read
from src.state_storage.recovery import backup, data_fingerprint, restore
from src.state_storage.source_entry import entry_probe, prepare, reload_policy
from tests import test_state_storage_db as storage_tests
from tests.test_state_storage_db import runtime_environment
from src.db.state import state_engine, state_ready

CONFIG = 'ENERGY_MAP_STATE_ENTRY_CONFIG'
RESTORE = 'ENERGY_MAP_STATE_ENTRY_RESTORE_CONFIG'
BRIDGE = 'ENERGY_MAP_STATE_ENTRY_BRIDGE'


@unittest.skipUnless(os.getenv(CONFIG), 'requires exact doctor-owned HBA descriptor')
class StateEntryDatabaseTests(unittest.TestCase):
    @classmethod
    def bridge(cls, operation):
        path = Path(os.environ[BRIDGE])
        if not path.is_absolute() or not path.is_relative_to(ROOT/'.codex-orch/runtime') or path.is_symlink():
            raise RuntimeError('Guarded doctor bridge required')
        result = subprocess.run(['/usr/bin/python3',str(path),operation],
            env={'PATH':'/usr/bin:/bin'},stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45)
        if result.returncode:
            raise RuntimeError('Doctor entry bridge failed')

    @classmethod
    def setUpClass(cls):
        cls.target=load_target(os.environ[CONFIG])
        cls.recovery=load_target(os.environ[RESTORE])
        for t in (cls.target,cls.recovery):
            if t['mode']!='disposable' or t['host']!='127.0.0.1' or t['port']!=55433 or t['source_entry']['mode']!='hba':
                raise RuntimeError('Refusing unguarded entry tests')
        cls.original_env=os.environ.copy()

    @classmethod
    def tearDownClass(cls):
        # Fixed bridge guards/checks rollback; never change any source data.
        cls.bridge('cleanup')

    def test_01_prepare_install_reload_preserves_reader_and_enables_public_fixture(self):
        self.assertEqual(prepare(self.target)['status'],'entry_request_prepared')
        # Real absence of the reviewed policy must stop before either new
        # account can log in, even though the pinned instance is reachable.
        with self.assertRaises(StorageError) as failure:apply(self.target)
        self.assertEqual(failure.exception.category,'entry_policy')
        with connected(self.target) as connection:
            for role in ('writer','migrator'):
                self.assertFalse(scalar(connection,'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s',(self.target[role],)))
        self.assertFalse(Path(self.target['note_file']).exists())
        self.bridge('install-target')
        self.assertEqual(reload_policy(self.target)['status'],'entry_reload_requested')
        self.bridge('public-connect-on')

    def test_02_failed_post_login_proof_quarantines_and_safe_retry_preserves_credentials(self):
        t=self.target
        with patch('src.state_storage.operations.entry_probe',side_effect=StorageError('source_entry_unproven')):
            with self.assertRaises(StorageError) as failure:apply(t)
        self.assertEqual(failure.exception.category,'source_entry_unproven')
        pending=load_bundle(t)
        self.assertEqual(pending['phase'],'migrated')
        self.assertFalse(Path(t['note_file']).exists())
        self.assertFalse(Path(t['runtime_file']).exists())
        with connected(t) as connection:
            self.assertFalse(scalar(connection,'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s',(t['writer'],)))
        self.assertEqual(apply(t)['status'],'verified')
        completed=load_bundle(t)
        for role in ('writer','migrator'):
            self.assertTrue(pending[role+'_password']==completed[role+'_password'])
        with runtime_environment(t,completed):
            try:self.assertTrue(state_ready())
            finally:state_engine().dispose()

    def test_03_real_public_connect_is_safe_only_with_specific_entry_refusal(self):
        t=self.target;b=load_bundle(t)
        with connected(t,database=t['source_database'],read_only=True) as connection:
            for role in ('writer','migrator'):
                self.assertTrue(scalar(connection,"SELECT has_database_privilege(%s,current_database(),'CONNECT')",(t[role],)))
            audit_source(connection,t)
        entry_probe(t,b)
        # Same transport and database with the wrong password cannot establish
        # the prerequisite successful state login, even though HBA source reject
        # would happen before password checking. Do not accept that as proof.
        bad=b|{'writer_password':'deliberately-invalid-fixture-password'}
        with self.assertRaises(psycopg2.OperationalError):entry_probe(t,bad)
        self.assertEqual(verify(t)['status'],'verified')

    def test_04_actual_saved_share_operations_with_confined_writer(self):
        storage_tests.StateStorageDatabaseTests.test_07_owner_service_identity_json_etag_and_digest_preservation(self)

    def test_05_full_backup_distinct_confined_restore_and_second_process(self):
        t=self.target
        self.assertEqual(backup(t)['status'],'backup_verified')
        # This fixed doctor action updates only the still-absent restore
        # descriptor's original-HBA digest after target policy installation.
        self.bridge('prepare-restore')
        r=load_target(os.environ[RESTORE]);self.__class__.recovery=r
        self.assertEqual(prepare(r)['status'],'entry_request_prepared')
        self.bridge('install-restore')
        self.assertEqual(reload_policy(r)['status'],'entry_reload_requested')
        self.assertEqual(restore(r)['status'],'restore_verified')
        self.assertEqual(verify(t)['status'],'verified')
        self.assertEqual(verify(r)['status'],'verified')
        with connected(t,database=t['database'],read_only=True) as a,connected(r,database=r['database'],read_only=True) as b:
            self.assertTrue(data_fingerprint(a)==data_fingerprint(b))
        result=subprocess.run([sys.executable,'-m','tests.state_recovery_probe',os.environ[RESTORE],
            str(Path(t['private_dir'])/'recovery-probe.json')],env={**os.environ,'PYTHONPATH':str(ROOT)+':'+str(ROOT/'src')},
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        self.assertTrue(result.returncode==0)
        self.assertEqual(result.stdout,b'recovery_operations=true\n')

    def test_06_confined_rerun_and_archive_keep_source_reader_healthy(self):
        self.assertEqual(apply(self.target)['status'],'verified')
        entry_probe(self.target,load_bundle(self.target))
        entry_probe(self.recovery,load_bundle(self.recovery))
        for t in (self.target,self.recovery):
            for path in (t['note_file'],t['runtime_file']):
                self.assertEqual(Path(path).stat().st_mode & 0o777,0o600)


if __name__=='__main__':unittest.main()
