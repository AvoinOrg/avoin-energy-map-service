"""Prevent accidental broad/private-data access by the live-safe probe helper."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from sqlalchemy.exc import DBAPIError

from src.building_contract import LastKnownBuildingSummaryV1
from src.db.state import StateUnavailable
from src.state_storage.config import Target
from src.state_storage.private import StorageError
from src.state_storage.private import read_json
from tests.sandbox_state_probe import ISSUER, reserve_probe, run, validate_probe


class SandboxProbeScopeTests(unittest.TestCase):
    def test_read_revalidates_json_timestamp_and_refuses_invalid_summary(self):
        saved_id = '00000000-0000-4000-8000-000000000001'
        probe = dict(issuer=ISSUER, subject='f0032:' + saved_id,
                     key='f0032-probe:' + saved_id, saved_id=saved_id,
                     token='a' * 43, tag='"ub1:' + saved_id + ':1"')
        summary = LastKnownBuildingSummaryV1(building_key=probe['key'],
            source_availability={}, captured_at=datetime.now(timezone.utc)).model_dump(mode='json')
        self.assertIsInstance(summary['captured_at'], str)
        cases = (
            ('json_timestamp', summary, False),
            ('wrong_key', summary | {'building_key': 'another-building'}, True),
            ('invalid_timestamp', summary | {'captured_at': 'invalid'}, True),
            ('naive_timestamp', summary | {'captured_at': '2026-10-27T00:00:00'}, True),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'synthetic-probe.json'
            reserve_probe(path, probe)
            target = Target(dict(mode='sandbox', private_dir=folder,
                                 writer='test_writer', database='test_state'))
            for name, stored, refused in cases:
                with self.subTest(name=name):
                    row = SimpleNamespace(saved_id=UUID(saved_id), revision=1,
                        display_name='Synthetic probe', building_key=probe['key'],
                        overrides={'construction_year': 2001}, scenario_inputs={},
                        last_known_summary=stored)
                    session = MagicMock()
                    session.scalar.side_effect = [row, row.saved_id]
                    denied = Exception()
                    denied.pgcode = '42501'
                    session.execute.side_effect = DBAPIError(None, None, denied)
                    with patch('tests.sandbox_state_probe.load_bundle', return_value={'writer_password': 'synthetic'}), \
                            patch.object(Target, 'settings', return_value=SimpleNamespace(kwargs=lambda: {})), \
                            patch('tests.sandbox_state_probe.create_engine') as engine, \
                            patch('tests.sandbox_state_probe.Session') as sessions, \
                            patch('tests.sandbox_state_probe.check_state'):
                        sessions.return_value.__enter__.return_value = session
                        if refused:
                            with self.assertRaises(StateUnavailable):
                                run('read', target, path)
                            session.execute.assert_not_called()
                        else:
                            run('read', target, path)
                            session.flush.assert_called_once()
                            session.execute.assert_called_once()
                            session.rollback.assert_called_once()
                        session.commit.assert_not_called()
                        engine.return_value.dispose.assert_called_once()

    def test_competing_creators_cannot_replace_cleanup_authority(self):
        first = '00000000-0000-4000-8000-000000000001'
        second = '00000000-0000-4000-8000-000000000002'
        def probe(saved_id):
            return dict(issuer=ISSUER, subject='f0032:' + saved_id,
                        key='f0032-probe:' + saved_id, saved_id=saved_id,
                        token='a' * 43, tag='"ub1:' + saved_id + ':1"')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'probe.json'
            def reserve(saved_id):
                try:
                    reserve_probe(path, probe(saved_id))
                    return saved_id
                except FileExistsError:
                    return None
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(reserve, (first, second)))
            winners = [value for value in results if value]
            self.assertEqual(len(winners), 1)
            self.assertEqual(read_json(path), probe(winners[0]))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                reserve_probe(path, probe(first))
            self.assertEqual(path.read_bytes(), before)

    def test_non_sandbox_targets_fail_before_credentials_or_database_access(self):
        with patch('tests.sandbox_state_probe.load_bundle') as credentials, \
                patch('tests.sandbox_state_probe.create_engine') as database:
            for mode in ('disposable', 'production', ''):
                with self.assertRaises(StorageError):
                    run('cleanup', Target({'mode': mode}), '/unused')
            credentials.assert_not_called()
            database.assert_not_called()

    def test_real_owner_or_mismatched_cleanup_identity_is_refused(self):
        saved_id = '00000000-0000-4000-8000-000000000001'
        valid = dict(issuer=ISSUER, subject='f0032:' + saved_id,
                     key='f0032-probe:' + saved_id, saved_id=saved_id,
                     token='a' * 43, tag='"ub1:' + saved_id + ':1"')
        self.assertEqual(validate_probe(valid), valid)
        for field, value in (('issuer', 'https://auth.example.org'),
                             ('subject', 'ordinary-owner'), ('key', 'real-building'),
                             ('saved_id', '00000000-0000-4000-8000-000000000002'),
                             ('token', 'unsafe/token'), ('tag', 'unbound')):
            with self.subTest(field=field), self.assertRaises(StorageError):
                validate_probe(valid | {field: value})
        with self.assertRaises(StorageError):
            validate_probe(valid | {'owner_filter': '*'})


if __name__ == '__main__':
    unittest.main()
