"""Explicit transport, poisoned environment and public import boundary checks."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.db.state import StateUnavailable, state_engine
from src.db.state_settings import connection_settings, runtime_settings


class StateTransportTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True); self.env.start(); self.addCleanup(self.env.stop)
        self.args = dict(host='127.0.0.1',port=55433,database='test',user='writer',password='canary')

    def test_literal_loopback_requires_explicit_disposable_opt_in(self):
        for host in ('localhost','db.example.invalid','127.0.0.1.evil'):
            with self.assertRaises(ValueError):
                connection_settings(**(self.args | {'host':host}),sslmode='disable',disposable_insecure=True)
        with self.assertRaises(ValueError): connection_settings(**self.args,sslmode='disable')
        settings = connection_settings(**self.args,sslmode='disable',disposable_insecure=True)
        kwargs = settings.kwargs()
        self.assertIn('default_transaction_read_only=off',kwargs['options'])
        self.assertIn('statement_timeout=10000',kwargs['options'])
        self.assertIn('lock_timeout=2000',kwargs['options'])
        self.assertEqual(kwargs['connect_timeout'],3)
        self.assertEqual(kwargs['passfile'],'/dev/null')
        self.assertNotIn('canary',repr(settings))

    def test_tls_requires_root_and_verified_mode(self):
        for mode in ('require','prefer','allow','verify-ca',''):
            with self.assertRaises(ValueError): connection_settings(**self.args,sslmode=mode)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'roots.pem'; root.write_text('public roots'); root.chmod(0o600)
            settings = connection_settings(**self.args,sslmode='verify-full',sslrootcert=str(root))
            self.assertEqual(settings.sslmode,'verify-full')
            with self.assertRaises(ValueError): connection_settings(**self.args,sslmode='verify-full',sslrootcert=str(root),sslcert=str(root))
            link = Path(folder)/'link';link.symlink_to(root)
            with self.assertRaises(ValueError): connection_settings(**self.args,sslmode='verify-full',sslrootcert=str(link))
            root.chmod(0o666)
            with self.assertRaises(ValueError): connection_settings(**self.args,sslmode='verify-full',sslrootcert=str(root))

    def test_poisoned_libpq_cannot_redirect_or_weaken_tls(self):
        for key in ('PGSERVICE','PGSERVICEFILE','PGHOSTADDR','PGSSLMODE','PGSSLROOTCERT','PGPASSFILE',
                    'PGTARGETSESSIONATTRS','PGCHANNELBINDING','PGSSLCERT','PGSSLKEY','PGREQUIRESSL','PGLOADBALANCEHOSTS'):
            with patch.dict(os.environ,{key:'poison'}),self.assertRaises(ValueError):
                connection_settings(**self.args,sslmode='disable',disposable_insecure=True)
        with patch.dict(os.environ,{'PGOPTIONS':'-c default_transaction_read_only=on','PGUSER':'analytical','PGPASSWORD':'reader'}):
            settings=connection_settings(**self.args,sslmode='disable',disposable_insecure=True)
            self.assertEqual(settings.user,'writer')
            self.assertIn('read_only=off',settings.kwargs()['options'])
            self.assertIn('read_only=on',os.environ['PGOPTIONS'])

    def test_lazy_disabled_and_sanitized_malformed_config(self):
        for values in ({},{'ENERGY_MAP_STATE_ENABLED':'false','PGSERVICE':'poison'},
                       {'ENERGY_MAP_STATE_ENABLED':'true','ENERGY_MAP_STATE_DATABASE_URL':'postgresql://writer:canary@127.0.0.1/db?sslmode=disable'},
                       {'ENERGY_MAP_STATE_ENABLED':'true','ENERGY_MAP_STATE_DATABASE_URL':'postgresql://writer:canary@127.0.0.1/db'}):
            with patch.dict(os.environ,values,clear=True),patch('src.db.state.create_engine') as factory:
                with self.assertRaises(StateUnavailable) as failure: state_engine()
                self.assertEqual(str(failure.exception),''); factory.assert_not_called()

    def test_engine_transport_cache_identity_changes(self):
        from src.db.state import _engine
        _engine.cache_clear()
        with patch('src.db.state.create_engine') as factory:
            first=connection_settings(**self.args,sslmode='disable',disposable_insecure=True)
            _engine(first);_engine(first)
            other=connection_settings(**(self.args|{'password':'other'}),sslmode='disable',disposable_insecure=True)
            _engine(other)
            self.assertEqual(factory.call_count,2)
            self.assertEqual(factory.call_args.kwargs['pool_size'],5)
            self.assertEqual(factory.call_args.kwargs['max_overflow'],0)
            self.assertEqual(factory.call_args.kwargs['pool_timeout'],3)
        _engine.cache_clear()
