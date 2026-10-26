"""Meaningful target/secret and operational failure boundary checks."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.state_storage.__main__ import main
from src.state_storage.config import identifier, load_target
from src.state_storage.private import StorageError, atomic_write, private_file, read_json
from src.state_storage.operations import apply, finalize, load_bundle, output_texts


class StateStorageTests(unittest.TestCase):
    def test_identifiers_are_bounded_plain_ascii(self):
        for value in ('', 'x'*64,'Name','a;drop','a"b','a-b','á','1x',None):
            with self.assertRaises(StorageError):identifier(value)
        self.assertEqual(identifier('energymap_f0031_state_test'),'energymap_f0031_state_test')

    def test_busy_apply_does_not_prepare_or_read_retry_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            from src.state_storage.config import Target
            target = Target({'private_dir': folder})
            with patch('src.state_storage.operations.connected'), \
                 patch('src.state_storage.operations.preflight'), \
                 patch('src.state_storage.operations.scalar', return_value=False), \
                 patch('src.state_storage.operations.load_bundle') as bundle, \
                 patch('src.state_storage.operations.secrets.token_urlsafe') as generate, \
                 patch('src.state_storage.operations.write_json') as persist:
                with self.assertRaises(StorageError) as failure:
                    apply(target)
                self.assertEqual(failure.exception.category, 'provision_busy')
                bundle.assert_not_called()
                generate.assert_not_called()
                persist.assert_not_called()
            self.assertFalse(list(Path(folder).iterdir()))

    def test_unsafe_transaction_sampling_stops_before_credential_ddl(self):
        from src.state_storage.config import Target
        for denied in (False, True):
            with self.subTest(set_config_denied=denied), tempfile.TemporaryDirectory() as folder:
                target = Target(dict(private_dir=folder, note_file=folder+'/note.md',
                    runtime_file=folder+'/writer.env', provisioning_id=str(uuid4())))
                settings = {'log_transaction_sample_rate': '1'}

                def query(connection, statement, parameters=()):
                    if statement == 'SELECT pg_try_advisory_lock(193847251,1)':
                        return True
                    if statement == "SELECT current_setting('shared_preload_libraries')":
                        return ''
                    return settings.get(parameters[0], '')

                def set_config(connection, statement, parameters):
                    self.assertEqual(statement, 'SELECT set_config(%s,%s,false)')
                    key, value = parameters
                    if key == 'log_transaction_sample_rate':
                        self.assertEqual(value, '0')
                        if denied:
                            raise StorageError('unsafe_logging')
                        # Simulate a setting that failed to take effect.
                    else:
                        settings[key] = value

                with patch('src.state_storage.operations.connected'), \
                     patch('src.state_storage.operations.preflight'), \
                     patch('src.state_storage.operations.resource_state', return_value=({}, [])), \
                     patch('src.state_storage.operations.scalar', side_effect=query), \
                     patch('src.state_storage.operations.rows', side_effect=set_config), \
                     patch('src.state_storage.operations.encrypt_password') as encrypt:
                    with self.assertRaises(StorageError) as failure:
                        apply(target)
                    self.assertEqual(failure.exception.category, 'unsafe_logging')
                    encrypt.assert_not_called()
                self.assertEqual(load_bundle(target)['phase'], 'prepared')

    def test_private_paths_and_atomic_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'private.json'
            atomic_write(path,'first');self.assertEqual(path.stat().st_mode & 0o777,0o600)
            atomic_write(path,'second');self.assertEqual(path.read_text(),'second')
            path.chmod(0o644)
            with self.assertRaises(StorageError):atomic_write(path,'third')
            self.assertEqual(path.read_text(),'second')
            link=Path(folder)/'link';link.symlink_to(path)
            with self.assertRaises(StorageError):private_file(link)
            with self.assertRaises(StorageError):private_file(Path(folder)/'..'/'other')

    def test_every_cli_exception_is_sanitized(self):
        canary='secret-CANARY:@/encoded%40'
        for error in (RuntimeError(canary),ValueError(canary),OSError(canary)):
            output=io.StringIO()
            with patch('src.state_storage.__main__.load_target',side_effect=error),redirect_stdout(output):
                result=main(['apply','--config-file','/private/descriptor.json'])
            self.assertEqual(result,1);self.assertNotIn(canary,output.getvalue())
            self.assertEqual(json.loads(output.getvalue()),{'status':'failed','category':'operation_failed'})

    def test_output_finalization_reuses_pending_credentials(self):
        from src.state_storage.config import Target
        with tempfile.TemporaryDirectory() as folder:
            target=Target(dict(private_dir=folder,note_file=folder+'/note.md',runtime_file=folder+'/writer.env',
                host='127.0.0.1',port=55433,database='state',migrator='migrator',writer='writer',
                sslmode='disable',sslrootcert='',disposable_insecure=True,provisioning_id=str(uuid4())))
            bundle=load_bundle(target,prepare=True)
            initial=dict(bundle)
            real=atomic_write
            def interrupted(path,content):
                if str(path)==target['runtime_file']:raise OSError('synthetic secret')
                real(path,content)
            with patch('src.state_storage.operations.atomic_write',side_effect=interrupted),self.assertRaises(OSError):finalize(target,bundle)
            restored=load_bundle(target)
            self.assertEqual(restored['writer_password'],initial['writer_password'])
            self.assertNotEqual(read_json(Path(folder)/'bundle.json')['phase'],'complete')
            finalize(target,restored)
            receipt=read_json(Path(folder)/'bundle.json')
            self.assertEqual(receipt['phase'],'complete');self.assertNotIn('writer_password',receipt)
            reopened=load_bundle(target)
            self.assertEqual(reopened['writer_password'],initial['writer_password'])
            self.assertNotIn(initial['migrator_password'],Path(target['runtime_file']).read_text())

    def test_descriptor_unknown_keys_and_ambient_targets_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'descriptor.json'
            atomic_write(path,json.dumps({'PGDATABASE':'source','unknown':'canary'}))
            with self.assertRaises(StorageError):load_target(str(path))

    def test_closed_descriptor_guards_before_connection(self):
        from src.state_storage.config import ROOT
        base=str(ROOT/'.codex-orch/runtime/f003-state-private')
        values=dict(mode='disposable',host='127.0.0.1',port=55433,
            maintenance_database='postgres',admin_user='postgres',admin_password_file=base+'/admin-password',
            system_identifier='1234567890123456789',source_database='energymap_f0031_source_test',
            source_reader='energymap_f0031_reader_test',database='energymap_f0031_state_test',
            migrator='energymap_f0031_migrator_test',writer='energymap_f0031_writer_test',
            provisioning_id=str(uuid4()),private_dir=base+'/target-private',note_file=base+'/target-private/note.md',
            runtime_file=base+'/target-private/writer.env',sslmode='disable',sslrootcert='',
            disposable_insecure=True,client_bin_dir='/usr/bin')
        with patch('src.state_storage.config.private_file',side_effect=lambda p,**k:Path(p)),\
             patch('src.state_storage.config.directory',side_effect=lambda p:Path(p)),\
             patch('src.state_storage.config.admin_password',return_value='synthetic'),\
             patch.dict(os.environ,{},clear=True):
            with patch('src.state_storage.config.read_json',return_value=values):
                self.assertEqual(load_target('/private/target.json')['database'],values['database'])
            for change in ({'database':'sandbox_energymap'},{'database':'production'},
                    {'mode':'production'},{'mode':'sandbox'}, {'database':values['source_database']},
                    {'writer':values['source_reader']},{'maintenance_database':'template1'},
                    {'host':'localhost'},{'port':5432},{'database':'x;DROP'},{'new_option':'unsafe'},
                    {'source_entry':{'mode':'unsafe'}},{'source_entry':{'mode':'acl','bypass':True}},
                    {'source_entry':{'mode':'hba','baseline_sha256':'bad','reader_password_file':'/private/reader'}}):
                with patch('src.state_storage.config.read_json',return_value=values|change),self.assertRaises((StorageError,ValueError)):
                    load_target('/private/target.json')

    def test_pending_output_collision_is_not_overwritten(self):
        from src.state_storage.config import Target
        with tempfile.TemporaryDirectory() as folder:
            target=Target(dict(private_dir=folder,note_file=folder+'/note.md',runtime_file=folder+'/writer.env',
                host='127.0.0.1',port=55433,database='state',migrator='migrator',writer='writer',
                sslmode='disable',sslrootcert='',disposable_insecure=True,provisioning_id=str(uuid4())))
            bundle=load_bundle(target,prepare=True)
            atomic_write(target['note_file'],'unrelated protected note')
            with self.assertRaises(StorageError):finalize(target,bundle)
            self.assertEqual(Path(target['note_file']).read_text(),'unrelated protected note')

    def test_client_secret_uses_only_private_passfile_and_errors_are_fixed(self):
        from src.state_storage.config import Target
        from src.state_storage.recovery import client
        import subprocess
        canary='distinct-synthetic-client-secret'
        with tempfile.TemporaryDirectory() as folder:
            executable=Path(folder)/'pg_dump';executable.write_text('fixture');executable.chmod(0o700)
            target=Target(dict(private_dir=folder,client_bin_dir=folder,host='127.0.0.1',port=55433,
                sslmode='disable',sslrootcert=''))
            observed=[]
            def process(argv,**kwargs):
                observed.append(canary not in repr(argv) and canary not in repr(kwargs['env']))
                path=Path(kwargs['env']['PGPASSFILE'])
                observed.append(path.stat().st_mode & 0o777 == 0o600 and canary in path.read_text())
                return subprocess.CompletedProcess(argv,1,canary.encode(),canary.encode())
            with patch('src.state_storage.recovery.subprocess.run',side_effect=process),self.assertRaises(StorageError) as failure:
                client(target,'pg_dump',['--version'],user='writer',password=canary,database='fixture')
            self.assertEqual(str(failure.exception),'client_failed');self.assertTrue(all(observed))
            self.assertFalse(list(Path(folder).glob('.pass-*')))

    def test_live_admin_selector_is_structural_and_memory_only(self):
        from src.state_storage.admin_secret import sandbox_admin_password
        content='''- Sandbox PostGIS
    - Admin GIS access
    - PostgreSQL administration
        - Database: postgres
        - Owner user: dedicated_admin
        - Owner password: correct-synthetic-canary
- Application
    - Owner user: application_owner
    - Owner password: wrong-synthetic-canary
'''
        self.assertEqual(sandbox_admin_password(content,'dedicated_admin'),'correct-synthetic-canary')
        with self.assertRaises(StorageError):sandbox_admin_password(content,'application_owner')
        with self.assertRaises(StorageError):sandbox_admin_password(content+content,'dedicated_admin')
