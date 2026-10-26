"""New-account policy scope, byte preservation, and fail-closed proof taxonomy."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg2

from src.state_storage.config import Target
from src.state_storage.hba_file import change, digest, main, policy
from src.state_storage.private import StorageError
from src.state_storage.source_entry import entry_probe


class StateEntryTests(unittest.TestCase):
    def test_prefix_preserves_all_existing_bytes_and_guarded_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'pg_hba.conf'
            original = b'# retained\r\nlocal all all peer\ninclude other.conf\n'
            path.write_bytes(original); path.chmod(0o600)
            request = dict(mode='disposable', database='energymap_f0031_hba_test',
                migrator='energymap_f0031_hba_m_test', writer='energymap_f0031_hba_w_test',
                provisioning_id=str(uuid4()), hba_file=str(path), baseline_sha256=digest(original))
            change(request)
            expected = policy(request['database'],request['migrator'],request['writer'],request['provisioning_id']) + original
            self.assertEqual(path.read_bytes(),expected)
            change(request); self.assertEqual(path.read_bytes(),expected)
            backup = Path(str(path)+'.energy-'+request['provisioning_id']+'.backup')
            self.assertEqual(backup.read_bytes(),original)
            self.assertEqual(backup.stat().st_mode & 0o777,0o600)
            path.write_bytes(expected+b'# concurrent change\n')
            with self.assertRaises(ValueError):change(request,rollback=True)
            self.assertTrue(path.read_bytes().endswith(b'# concurrent change\n'))
            path.write_bytes(expected)
            change(request,rollback=True); change(request,rollback=True)
            self.assertEqual(path.read_bytes(),original)
            request['writer']='existing_account'
            with self.assertRaises(ValueError):change(request)

    def test_unprotected_or_symlink_hba_and_bad_baseline_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'hba';path.write_bytes(b'original');path.chmod(0o646)
            request=dict(mode='sandbox',database='energymap_personal_state',migrator='energymap_state_migrator',
                writer='energymap_state_writer',provisioning_id=str(uuid4()),hba_file=str(path),baseline_sha256=digest(b'original'))
            for permissions in (0o646,0o660,0o664,0o666,0o755):
                path.chmod(permissions)
                with self.assertRaises(ValueError):change(request)
            path.chmod(0o600);request['baseline_sha256']='0'*64
            with self.assertRaises(ValueError):change(request)
            link=Path(folder)/'link';link.symlink_to(path);request['hba_file']=str(link)
            with self.assertRaises(ValueError):change(request)
            self.assertEqual(path.read_bytes(),b'original')
            self.assertFalse(list(Path(folder).glob('*.backup')))

    def test_active_config_mode_owner_group_preserved_while_controls_stay_private(self):
        for permissions in (0o600,0o640,0o644):
            with self.subTest(mode=permissions), tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'hba';path.write_bytes(b'#original\n');path.chmod(permissions)
                before=path.stat()
                request=dict(mode='sandbox',database='energymap_personal_state',migrator='energymap_state_migrator',
                    writer='energymap_state_writer',provisioning_id=str(uuid4()),hba_file=str(path),baseline_sha256=digest(path.read_bytes()))
                change(request)
                for operation in (False,True):
                    change(request,rollback=operation)
                    now=path.stat()
                    self.assertEqual((now.st_uid,now.st_gid,now.st_mode & 0o777),(before.st_uid,before.st_gid,permissions))
                    backup=Path(str(path)+'.energy-'+request['provisioning_id']+'.backup')
                    self.assertEqual(backup.stat().st_mode & 0o777,0o600)
                self.assertEqual(path.read_bytes(),b'#original\n')
                request_file=Path(folder)/'request.json';request_file.write_text(json.dumps(request));request_file.chmod(0o644)
                with patch('builtins.print'):
                    self.assertEqual(main(['install','--request-file',str(request_file)]),1)
                backup.chmod(0o644)
                with self.assertRaises(ValueError):change(request)

    def test_competing_hba_install_fails_bounded_without_file_change(self):
        import fcntl
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'hba';path.write_bytes(b'original');path.chmod(0o600)
            request=dict(mode='sandbox',database='energymap_personal_state',migrator='energymap_state_migrator',
                writer='energymap_state_writer',provisioning_id=str(uuid4()),hba_file=str(path),baseline_sha256=digest(b'original'))
            lock=Path(str(path)+'.energy.lock');lock.touch(mode=0o600)
            with lock.open('rb') as held:
                fcntl.flock(held.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):change(request)
            self.assertEqual(path.read_bytes(),b'original')
            self.assertFalse(list(Path(folder).glob('*.backup')))

    def test_policy_audit_rejects_broad_precedence_and_baseline_drift(self):
        from src.state_storage.source_entry import audit_policy
        original=b'host all all all scram-sha-256\n'
        t=Target(dict(database='state',migrator='new_owner',writer='new_writer',provisioning_id=str(uuid4()),
                      source_entry={'mode':'hba','baseline_sha256':digest(original)}))
        block=policy('state','new_owner','new_writer',t['provisioning_id'])
        parsed=[('local',['all'],None,'reject'),('local',['replication'],None,'reject'),
                ('hostssl',['state'],'all','scram-sha-256'),('host',['all'],'all','reject'),
                ('host',['replication'],'all','reject')]
        parsed=[row+(['new_owner','new_writer'],None,'/fixture/hba') for row in parsed]
        for preceding, accepted in (([],True),([(['other_new_owner','other_new_writer'],)],True),
                ([(['all'],)],False),([(['+other_group'],)],False),([(['new_writer'],)],False),
                ([(['/new_.*'],)],False)):
            with patch('src.state_storage.source_entry.contents',return_value=('/fixture/hba',block+original)), \
                 patch('src.state_storage.source_entry.scalar',return_value=0), \
                 patch('src.state_storage.source_entry.rows',side_effect=[parsed,preceding]):
                if accepted:audit_policy(object(),t)
                else:
                    with self.assertRaises(StorageError):audit_policy(object(),t)
        with patch('src.state_storage.source_entry.contents',return_value=('/fixture/hba',block+original+b'#drift')):
            with self.assertRaises(StorageError):audit_policy(object(),t)

    def test_only_explicit_entry_refusal_after_successful_state_login_is_proof(self):
        t=Target(dict(database='state',source_database='source',writer='new_writer',migrator='new_owner',
                      source_entry={'mode':'hba'}))
        bundle={'writer_password':'writer-canary','migrator_password':'owner-canary'}
        attempts=[]
        @contextmanager
        def good(target,**kwargs):
            attempts.append((kwargs['user'],kwargs['database']))
            if kwargs['database']=='source':
                raise psycopg2.OperationalError('FATAL:  pg_hba.conf rejects connection for host "fixture", user "'+kwargs['user']+'", database "source", SSL encryption')
            yield object()
        with patch('src.state_storage.operations.connected',side_effect=good),patch('src.state_storage.source_entry.reader_probe') as reader:
            entry_probe(t,bundle)
            reader.assert_called_once_with(t)
        self.assertEqual(attempts,[('new_owner','state'),('new_owner','source'),('new_writer','state'),('new_writer','source')])
        for text in ('password authentication failed','certificate verify failed','connection refused',
                     'database "source" does not exist','no pg_hba.conf entry for host',
                     'pg_hba.conf rejects connection for host "fixture", user "other", database "source"'):
            @contextmanager
            def bad(target,**kwargs):
                if kwargs['database']=='source':raise psycopg2.OperationalError(text+' secret-canary')
                yield object()
            with patch('src.state_storage.operations.connected',side_effect=bad):
                with self.assertRaises(StorageError) as caught:entry_probe(t,bundle)
                self.assertEqual(str(caught.exception),'source_entry_unproven')
        @contextmanager
        def accessible(*args,**kwargs):yield object()
        with patch('src.state_storage.operations.connected',side_effect=accessible):
            with self.assertRaises(StorageError) as caught:entry_probe(t,bundle)
            self.assertEqual(str(caught.exception),'source_separation')


if __name__=='__main__':unittest.main()
