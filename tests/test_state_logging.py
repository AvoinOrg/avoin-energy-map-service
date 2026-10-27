"""Credential encryption/DDL must stay behind the fixed preload/logging guard."""
import unittest
from unittest.mock import patch

from src.state_storage.config import Target
from src.state_storage.logging_policy import CRON_FILES
from src.state_storage.operations import apply, secret_safe_session
from src.state_storage.private import StorageError


class StateLoggingTests(unittest.TestCase):
    def policy(self, *, shared='', changed=None, denied=None, ineffective=None):
        self.values = {
            'shared_preload_libraries': shared,
            'session_preload_libraries': '', 'local_preload_libraries': '',
            'server_version_num': '170002', 'dynamic_library_path': '$libdir',
            'PKGLIBDIR': '/usr/lib/postgresql/17/lib', 'pending_restart': False,
            'mappings': '7f-8f r-xp 00000000 08:01 123 /usr/lib/postgresql/17/lib/pg_cron.so\n',
            'unchanged_since_start': True,
            **CRON_FILES, **(changed or {}),
        }
        self.sets = []

        def scalar(connection, statement, parameters=()):
            if statement == 'SELECT pg_try_advisory_lock(193847251,1)':
                return True
            if statement == "SELECT current_setting('shared_preload_libraries')":
                return self.values['shared_preload_libraries']
            if statement == "SELECT current_setting('server_version_num')":
                return self.values['server_version_num']
            if statement == "SELECT current_setting('dynamic_library_path')":
                return self.values['dynamic_library_path']
            if statement == "SELECT setting FROM pg_config WHERE name='PKGLIBDIR'":
                return self.values['PKGLIBDIR']
            if statement == "SELECT pending_restart FROM pg_settings WHERE name='shared_preload_libraries'":
                return self.values['pending_restart']
            if statement.startswith('SELECT pg_catalog.pg_read_file('):
                return self.values['mappings']
            if statement.startswith('SELECT s.modification'):
                return self.values['unchanged_since_start']
            return self.values[parameters[0]]

        def rows(connection, statement, parameters):
            self.assertEqual(statement, 'SELECT set_config(%s,%s,false)')
            key, value = parameters
            self.sets.append(key)
            if key == denied:
                raise RuntimeError('synthetic diagnostic must not escape')
            if key != ineffective:
                self.values[key] = value

        return patch('src.state_storage.operations.scalar', side_effect=scalar), \
            patch('src.state_storage.operations.rows', side_effect=rows)

    def test_empty_and_exact_verified_scheduler_suppress_every_native_channel(self):
        for shared in ('', 'pg_cron'):
            with self.subTest(shared=shared):
                scalar, rows = self.policy(shared=shared)
                with scalar, rows:
                    secret_safe_session(object())
                self.assertEqual(set(self.sets), {'log_statement', 'log_duration',
                    'log_min_duration_statement', 'log_min_duration_sample',
                    'log_transaction_sample_rate', 'log_min_error_statement',
                    'log_parameter_max_length', 'log_parameter_max_length_on_error'})
                self.assertEqual(self.values['log_duration'], 'off')
                self.assertEqual(self.values['log_transaction_sample_rate'], '0')
                self.assertFalse(any(key.startswith('cron.') for key in self.sets))

    def assert_apply_refuses_before_encrypt_or_ddl(self, **policy):
        scalar, rows = self.policy(**policy)
        with scalar, rows, \
                patch('src.state_storage.operations.connected'), \
                patch('src.state_storage.operations.preflight'), \
                patch('src.state_storage.operations.load_bundle', return_value={'phase': 'prepared'}), \
                patch('src.state_storage.operations.resource_state', return_value=({}, False)), \
                patch('src.state_storage.operations.save_bundle') as journal, \
                patch('src.state_storage.operations.encrypt_password') as encrypt:
            with self.assertRaises(StorageError) as result:
                apply(Target({}))
            self.assertEqual(str(result.exception), 'unsafe_logging')
            encrypt.assert_not_called()
            journal.assert_not_called()
            # Our rows stub refuses any statement except set_config. This also
            # asserts that neither CREATE ROLE nor scheduler SQL was executed.

    def test_unknown_additional_or_path_aliased_preloads_refuse(self):
        for value in ('pgaudit', 'pg_cron,pgaudit', 'pg_cron,pg_stat_statements',
                      'pg_cron,pg_cron', '"pg_cron"', ' pg_cron',
                      '$libdir/pg_cron', '/usr/lib/postgresql/17/lib/pg_cron.so'):
            with self.subTest(shared=value):
                self.assert_apply_refuses_before_encrypt_or_ddl(shared=value)

    def test_unverified_version_bytes_or_loader_paths_refuse(self):
        for changed in ({'server_version_num': '170005'}, {'PKGLIBDIR': '/other/lib'},
                        {'dynamic_library_path': '/other:$libdir'}, {'pending_restart': True},
                        {'mappings': ''}, {'unchanged_since_start': False},
                        {'mappings': '7f-8f r-xp 0 08:01 123 /other/pg_cron.so\n'},
                        {'mappings': '7f-8f r-xp 0 08:01 123 /usr/lib/postgresql/17/lib/pg_cron.so (deleted)\n'},
                        *({path: '0'*64} for path in CRON_FILES),
                        {'session_preload_libraries': 'pg_cron'},
                        {'local_preload_libraries': 'pgaudit'}):
            with self.subTest(changed=changed):
                self.assert_apply_refuses_before_encrypt_or_ddl(shared='pg_cron', changed=changed)

    def test_each_denied_or_ineffective_logging_control_refuses(self):
        for name in ('log_statement', 'log_duration', 'log_min_duration_statement',
                     'log_min_duration_sample', 'log_transaction_sample_rate',
                     'log_min_error_statement', 'log_parameter_max_length',
                     'log_parameter_max_length_on_error'):
            for mode in ('denied', 'ineffective'):
                with self.subTest(setting=name, mode=mode):
                    self.assert_apply_refuses_before_encrypt_or_ddl(
                        changed={name: 'unsafe'}, **{mode: name})

    def test_provenance_read_failure_is_sanitized(self):
        with patch('src.state_storage.operations.scalar', side_effect=RuntimeError('secret diagnostic')):
            with self.assertRaises(StorageError) as result:
                secret_safe_session(object())
        self.assertEqual(str(result.exception), 'unsafe_logging')
