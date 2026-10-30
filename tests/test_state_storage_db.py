"""Real F003.1 acceptance on doctor-owned 127.0.0.1:55433 resources only.

Runtime doctor provisions the instance/source/client lifecycle. This harness never
uses a sandbox descriptor, resets a source, or modifies the F001 fixture.
"""
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from psycopg2 import sql
import psycopg2
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from src.api.auth import AuthenticatedPrincipal
from src.db.state import StateUnavailable, check_state, state_ready
from src.db.state_settings import connection_settings
from src.db.user_building_models import UserBuilding, UserBuildingShare
from src import building_shares, user_buildings
from src.state_storage import recovery
from src.state_storage.catalog import (audit_schema, audit_acl, audit_source, normalized, rows, scalar, snapshot)
from src.state_storage.config import ROOT, Target, load_target
from src.state_storage.operations import (apply, connected, inspect, load_bundle,
    output_texts, runtime_probe, save_bundle, secret_safe_session, verify)
from src.state_storage.private import StorageError, read, read_json, write_json
from src.state_storage.recovery import backup, data_fingerprint, restore

CONFIG = 'ENERGY_MAP_STATE_TEST_CONFIG'
RESTORE = 'ENERGY_MAP_STATE_RESTORE_CONFIG'


def guarded():
    require_path = os.environ.get(CONFIG)
    if not require_path or not os.environ.get(RESTORE):
        raise RuntimeError('Explicit doctor descriptor required')
    target, recovery = load_target(require_path), load_target(os.environ[RESTORE])
    for t in (target,recovery):
        if (t['mode'] != 'disposable' or t['host'] != '127.0.0.1' or t['port'] != 55433
                or t['source_database'] != 'energymap_f0031_source_test'):
            raise RuntimeError('Refusing unguarded state tests')
    if target['database'] == recovery['database']:
        raise RuntimeError('Distinct recovery target required')
    return target,recovery


@contextmanager
def runtime_environment(target,bundle):
    _,content=output_texts(target,bundle)
    values=dict(line.split('=',1) for line in content.splitlines())
    with patch.dict(os.environ,values):
        yield


@contextmanager
def source_audit_transaction(target, connection):
    """Expose rollback-only source ACLs to the real provision/verify audit."""
    @contextmanager
    def connect_for_audit(t, **kwargs):
        if kwargs.get('database') == target['source_database']:
            yield connection
        else:
            with connected(t, **kwargs) as actual:
                yield actual
    with patch('src.state_storage.operations.connected', side_effect=connect_for_audit):
        yield


@unittest.skipUnless(os.getenv(CONFIG), 'requires exact doctor-owned F003.1 descriptor')
class StateStorageDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.target,cls.recovery=guarded()
        cls.original_env=os.environ.copy()

    def test_00_transaction_sampling_is_disabled_session_locally(self):
        with connected(self.target) as baseline:
            original = scalar(baseline, 'SHOW log_transaction_sample_rate')
            with connected(self.target) as connection:
                rows(connection, 'SET log_transaction_sample_rate = 1')
                self.assertEqual(scalar(connection, 'SHOW log_transaction_sample_rate'), '1')
                secret_safe_session(connection)
                self.assertEqual(scalar(connection, 'SHOW log_transaction_sample_rate'), '0')
            self.assertEqual(scalar(baseline, 'SHOW log_transaction_sample_rate'), original)

    def test_01_fresh_apply_exact_schema_narrow_ready_and_safe_rerun(self):
        t=self.target
        before=inspect(t)
        self.assertIn(before['status'],('target_absent','managed_present'))
        if before['status']=='target_absent':
            # Overlap two real sessions deterministically at the durable precommit
            # boundary. The loser cannot touch the active retry authority. Then
            # interrupt immediately after role commit, before its phase write.
            contended = False
            def interrupt_roles(target, bundle, phase):
                nonlocal contended
                if phase == 'roles':
                    raise RuntimeError('injected post-role-commit interruption')
                save_bundle(target, bundle, phase)
                if phase == 'creating_roles' and 'writer_verifier_hash' in bundle:
                    retained = read(Path(t['private_dir'])/'bundle.json')
                    with patch('src.state_storage.operations.load_bundle') as loser_load, \
                         patch('src.state_storage.operations.write_json') as loser_write, \
                         patch('src.state_storage.operations.encrypt_password') as loser_encrypt:
                        with self.assertRaises(StorageError) as failure:
                            apply(t)
                        self.assertEqual(failure.exception.category, 'provision_busy')
                        loser_load.assert_not_called()
                        loser_write.assert_not_called()
                        loser_encrypt.assert_not_called()
                    self.assertTrue(read(Path(t['private_dir'])/'bundle.json') == retained)
                    contended = True
            with patch('src.state_storage.operations.save_bundle', side_effect=interrupt_roles), self.assertRaises(RuntimeError):
                apply(t)
            self.assertTrue(contended)
            committed = load_bundle(t)
            self.assertEqual(committed['phase'], 'creating_roles')
            with connected(t) as connection:
                for name in ('writer', 'migrator'):
                    self.assertFalse(scalar(connection,'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s',(t[name],)))
                self.assertFalse(scalar(connection,'SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=%s)',(t['database'],)))
            with patch('src.state_storage.operations._migrate_and_grant',side_effect=RuntimeError('injected boundary')),self.assertRaises(RuntimeError):
                apply(t)
            pending=load_bundle(t)
            self.assertEqual(pending['phase'],'migrating')
            for name in ('writer', 'migrator'):
                self.assertTrue(pending[name+'_password'] == committed[name+'_password'])
                self.assertTrue(pending[name+'_verifier_hash'] == committed[name+'_verifier_hash'])
            with connected(t) as connection:
                self.assertFalse(scalar(connection,'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s',(t['writer'],)))
            # The source audit must stop a real apply before writer LOGIN. Keep
            # this deliberately unsafe source grant in one rollback-only session.
            with connected(t, database=t['source_database']) as source:
                original_acl = scalar(source, 'SELECT datacl::text FROM pg_database WHERE datname=current_database()')
                source.autocommit = False
                try:
                    rows(source, sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                        sql.Identifier(t['source_database']), sql.Identifier(t['writer'])))
                    with source_audit_transaction(t, source), self.assertRaises(StorageError) as failure:
                        apply(t)
                    self.assertEqual(failure.exception.category, 'source_separation')
                    self.assertEqual(load_bundle(t)['phase'], 'migrated')
                    self.assertFalse(scalar(source, 'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s', (t['writer'],)))
                    self.assertFalse(Path(t['note_file']).exists())
                    self.assertFalse(Path(t['runtime_file']).exists())
                finally:
                    source.rollback()
                self.assertEqual(original_acl, scalar(source, 'SELECT datacl::text FROM pg_database WHERE datname=current_database()'))
            from src.state_storage.private import atomic_write
            def fail_second_output(path,content):
                if str(path)==t['runtime_file']:raise OSError('injected finalization boundary')
                atomic_write(path,content)
            with patch('src.state_storage.operations.atomic_write',side_effect=fail_second_output),self.assertRaises(OSError):
                apply(t)
            resumed=load_bundle(t)
            self.assertEqual(resumed['phase'],'migrated')
            self.assertTrue(resumed['writer_password']==pending['writer_password'])
            self.assertFalse(Path(t['runtime_file']).exists())
        self.assertEqual(apply(t)['status'],'verified')
        bundle=load_bundle(t)
        with runtime_environment(t,bundle):self.assertTrue(state_ready())
        note_before=read(t['note_file']);runtime_before=read(t['runtime_file'])
        with connected(t,database=t['database'],read_only=True) as connection:
            fingerprint=data_fingerprint(connection)
            self.assertIsNone(scalar(connection,"SELECT to_regclass('public.alembic_version')"))
            self.assertEqual(scalar(connection,'SELECT count(*) FROM pg_extension'),1)
        self.assertEqual(apply(t)['status'],'verified')
        self.assertTrue(note_before==read(t['note_file']) and runtime_before==read(t['runtime_file']))
        with connected(t,database=t['database'],read_only=True) as connection:
            self.assertTrue(fingerprint==data_fingerprint(connection))

    def test_02_legacy_frozen_upgrade_equivalence_and_transaction_rollback(self):
        # Separate temporary schema layout in a rollback-only NEW target transaction.
        t=self.target;bundle=load_bundle(t)
        settings=t.settings(t['migrator'],bundle['migrator_password'],t['database'])
        engine=create_engine('postgresql+psycopg2://',connect_args=settings.kwargs(),poolclass=NullPool,hide_parameters=True)
        try:
            with engine.connect() as connection:
                trans=connection.begin()
                original=snapshot(connection.connection.driver_connection)
                connection.exec_driver_sql('ALTER SCHEMA energymap_state RENAME TO retained_state')
                context=MigrationContext.configure(connection)
                with Operations.context(context):
                    for filename in ('c7d3e921ab40_010_owner_saved_buildings.py','d4e8f021bc63_011_building_shares.py','e5f102acd890_012_personal_v2.py'):
                        spec=importlib.util.spec_from_file_location('legacy_'+filename[:12],ROOT/'db_migrations/versions'/filename)
                        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.upgrade()
                self.assertTrue(normalized(original)==normalized(snapshot(connection.connection.driver_connection)))
                # Force rollback of real DDL after both upgrades, including FK/indexes.
                trans.rollback()
            with connected(t,database=t['database'],read_only=True) as connection:
                audit_schema(connection)
                self.assertIsNone(scalar(connection,"SELECT to_regnamespace('retained_state')"))
        finally:engine.dispose()

    def test_03_real_privilege_denials_and_wrong_runtime_owner(self):
        t=self.target;bundle=load_bundle(t)
        forbidden=[
            'CREATE SCHEMA prohibited', 'CREATE TEMP TABLE prohibited(x int)',
            'CREATE TABLE energymap_state.prohibited(x int)',
            'TRUNCATE energymap_state.energymap_user_buildings',
            'UPDATE energymap_state.energymap_user_buildings SET owner_subject=owner_subject',
            'UPDATE energymap_state.energymap_user_buildings SET saved_id=saved_id',
            'UPDATE energymap_state.energymap_user_building_shares SET token_digest=token_digest',
            'SELECT * FROM energymap_state_meta.alembic_version',
            'SET ROLE '+t['migrator'],
        ]
        with connected(t,database=t['database'],user=t['writer'],password=bundle['writer_password']) as connection:
            connection.autocommit=False
            for statement in forbidden:
                with self.subTest(operation=statement.split()[0]):
                    denied=False
                    try:rows(connection,statement)
                    except psycopg2.Error as error:denied=error.pgcode=='42501'
                    finally:connection.rollback()
                    self.assertTrue(denied)
        with connected(t,database=t['database'],user=t['writer'],password=bundle['writer_password']) as connection:
            connection.autocommit=False
            before_acl=scalar(connection,"SELECT relacl::text FROM pg_class WHERE oid='energymap_state.energymap_user_buildings'::regclass")
            # PostgreSQL can warn and grant nothing instead of throwing 42501.
            rows(connection,'GRANT SELECT ON energymap_state.energymap_user_buildings TO PUBLIC')
            self.assertEqual(before_acl,scalar(connection,"SELECT relacl::text FROM pg_class WHERE oid='energymap_state.energymap_user_buildings'::regclass"))
            connection.rollback()
        for role in ('migrator',):
            settings=t.settings(t[role],bundle[role+'_password'],t['database'])
            engine=create_engine('postgresql+psycopg2://',connect_args=settings.kwargs(),hide_parameters=True)
            try:
                with Session(engine) as session,self.assertRaises(StateUnavailable):check_state(session)
            finally:engine.dispose()
        with connected(t,database=t['database'],read_only=True) as connection:
            self.assertFalse(scalar(connection,'SELECT has_database_privilege(%s,current_database(),\'CONNECT\')',(t['source_reader'],)))

    def test_04_exact_catalog_and_grant_drift_rejected_without_repair(self):
        t=self.target;bundle=load_bundle(t)
        # Rollback-only corruption never affects another test/project.
        with connected(t,database=t['database']) as connection:
            connection.autocommit=False
            corruptions=[
                'GRANT UPDATE ON energymap_state.energymap_user_buildings TO '+t['writer'],
                'GRANT SELECT ON energymap_state.energymap_user_buildings TO '+t['writer']+' WITH GRANT OPTION',
                'ALTER TABLE energymap_state.energymap_user_buildings ADD COLUMN unexpected text',
                'DROP INDEX energymap_state.ix_user_buildings_owner_updated',
                "UPDATE energymap_state_meta.alembic_version SET version_num='unknown'",
                'GRANT TEMP ON DATABASE '+t['database']+' TO '+t['writer'],
                'GRANT USAGE ON SCHEMA energymap_state_meta TO '+t['writer'],
            ]
            for statement in corruptions:
                rows(connection,statement)
                rejected=False
                try:audit_schema(connection);audit_acl(connection,t)
                except StorageError:rejected=True
                finally:connection.rollback()
                self.assertTrue(rejected)
        # Actual readiness must also reject concrete overgrants, not merely the
        # administrative verifier. SET LOCAL ROLE uses the same rolled-back DDL.
        settings=t.settings(t['admin_user'],read(t['admin_password_file']).strip(),t['database'])
        engine=create_engine('postgresql+psycopg2://',connect_args=settings.kwargs(),hide_parameters=True,poolclass=NullPool)
        try:
            for statement in corruptions[:2]:
                with engine.connect() as connection:
                    transaction=connection.begin()
                    connection.exec_driver_sql(statement)
                    connection.exec_driver_sql('SET LOCAL ROLE '+t['writer'])
                    with Session(bind=connection) as session,self.assertRaises(StateUnavailable):check_state(session)
                    transaction.rollback()
        finally:engine.dispose()
        self.assertEqual(verify(t)['status'],'verified')

    def test_05_wrong_identity_names_and_lock_fail_before_mutation(self):
        t=self.target
        wrong=Target(t.values|{'system_identifier':'9999999999999999999'})
        with self.assertRaises(StorageError):inspect(wrong)
        # An unrelated role collision is checked before any role/database DDL.
        with tempfile.TemporaryDirectory(dir=t['private_dir']) as work:
            wrong=Target(t.values|{'private_dir':work,'note_file':work+'/note.md',
                'runtime_file':work+'/writer.env','database':self.recovery['database'],
                'migrator':t['source_reader'],'writer':self.recovery['writer'],'provisioning_id':str(uuid4())})
            with connected(t) as connection:
                count=scalar(connection,'SELECT count(*) FROM pg_database')
                with self.assertRaises(StorageError):apply(wrong)
                self.assertEqual(count,scalar(connection,'SELECT count(*) FROM pg_database'))
        with connected(t) as blocker:
            scalar(blocker,'SELECT pg_advisory_lock(193847251,1)')
            with self.assertRaises(StorageError) as failure:apply(t)
            self.assertEqual(failure.exception.category,'provision_busy')

    def test_06_real_tls_chain_hostname_and_ambient_failure(self):
        t=self.target;bundle=load_bundle(t)
        require_tls=t['sslmode']=='verify-full';self.assertTrue(require_tls)
        with connected(t,database=t['database'],user=t['writer'],password=bundle['writer_password']) as connection:
            self.assertTrue(scalar(connection,'SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()'))
            self.assertEqual(scalar(connection,'SHOW statement_timeout'),'10s')
            self.assertEqual(scalar(connection,'SHOW lock_timeout'),'2s')
            self.assertEqual(scalar(connection,'SHOW default_transaction_read_only'),'off')
        root=Path(t['sslrootcert'])
        with tempfile.NamedTemporaryFile(dir=t['private_dir']) as invalid:
            os.chmod(invalid.name,0o600);invalid.write(b'not a root certificate');invalid.flush()
            bad=Target(t.values|{'sslrootcert':invalid.name})
            with self.assertRaises(Exception):
                with connected(bad):pass
        # Driver-only negative check pins the reachable address while presenting a
        # mismatching hostname; a connection refusal cannot pass this assertion.
        kwargs=t.settings(t['writer'],bundle['writer_password'],t['database']).kwargs()
        kwargs.update(host='mismatch.invalid',hostaddr='127.0.0.1')
        mismatch=False
        try:
            connection=psycopg2.connect(**kwargs);connection.close()
        except psycopg2.Error as error:
            mismatch='does not match host name' in str(error)
        self.assertTrue(mismatch)
        with runtime_environment(t,bundle),patch.dict(os.environ,{'PGSERVICE':'poison'}):self.assertFalse(state_ready())

    def test_07_owner_service_identity_json_etag_and_digest_preservation(self):
        t=self.target;bundle=load_bundle(t)
        if (Path(t['private_dir'])/'recovery-probe.json').exists():
            # Repeatable harness: preserve the exact backed-up source rows.
            self.assertEqual(verify(t)['status'],'verified')
            return
        principal=AuthenticatedPrincipal('https://issuer.example.invalid','same-subject')
        settings=t.settings(t['writer'],bundle['writer_password'],t['database'])
        engine=create_engine('postgresql+psycopg2://',connect_args=settings.kwargs(),hide_parameters=True)
        try:
            with Session(engine) as session:
                from src.api.buildings import public_example
                from src.building_contract import CanonicalBuildingV1, PersonalBuildingPutV1
                base=CanonicalBuildingV1.model_validate_json(json.dumps(public_example()))
                key='f0031-opaque/key/'+str(uuid4())
                base.building_key=key
                document=PersonalBuildingPutV1.model_validate_json(json.dumps({'display_name':'Private <name>',
                    'overrides':{'floor_area':0,'total_area':None},'scenario_inputs':{}}))
                row=user_buildings.create_owned(session,principal,key,document,base)
                self.assertIsNotNone(user_buildings.load_owned(session,principal,key))
                self.assertIsNone(user_buildings.load_owned(session,AuthenticatedPrincipal('https://other.example.invalid',principal.subject),key,required=False))
                # Actual delivered lifecycle primitives with the narrow writer.
                initial_tag=user_buildings.etag(row)
                from starlette.datastructures import Headers
                user_buildings.preconditions(Headers({'If-Match':initial_tag}),row)
                token=building_shares.rotate(session,row)
                self.assertEqual(building_shares.lookup(session,token).saved_id,row.saved_id)
                building_shares.revoke(session,row)
                with self.assertRaises(building_shares.ShareNotFound):building_shares.lookup(session,token)
                token=building_shares.rotate(session,row)
                for _ in range(3):user_buildings.bump_revision(row)
                self.assertEqual(row.revision,7)
                self.assertEqual(user_buildings.stored_summary(row).building_key,key)
                self.assertTrue(session.scalars(user_buildings.page_query(principal,10,None)).first() is not None)
                session.commit()
                self.assertEqual(row.overrides['floor_area'],0);self.assertIsNone(row.overrides['total_area'])
                payload={'saved_id':str(row.saved_id),'key':row.building_key,'issuer':principal.issuer,'subject':principal.subject,'tag':user_buildings.etag(row),'token':token}
                write_json(Path(t['private_dir'])/'recovery-probe.json',payload)
            with connected(t,database=t['database'],read_only=True) as connection:before=data_fingerprint(connection)
            self.assertEqual(apply(t)['status'],'verified')
            with connected(t,database=t['database'],read_only=True) as connection:self.assertTrue(before==data_fingerprint(connection))
        finally:engine.dispose()

    def test_08_source_database_separation_and_public_independence(self):
        t=self.target;bundle=load_bundle(t)
        with connected(t,database=t['source_database'],read_only=True) as connection:
            before=rows(connection,'SELECT to_jsonb(t) FROM public.source_probe t')
        with connected(t,database=t['source_database'],read_only=True) as connection:
            source_connect=scalar(connection,"SELECT has_database_privilege(%s,current_database(),'CONNECT')",(t['writer'],))
            audit_source(connection, t)
        self.assertFalse(source_connect)
        with self.assertRaises(psycopg2.Error):
            with connected(t,database=t['source_database'],user=t['writer'],password=bundle['writer_password']):pass
        with connected(t,database=t['source_database'],read_only=True) as connection:
            self.assertTrue(before==rows(connection,'SELECT to_jsonb(t) FROM public.source_probe t'))
        # Existing public API health never probes invalid state/auth configuration.
        from fastapi.testclient import TestClient
        from src.api.main import app
        with patch.dict(os.environ,{'ENERGY_MAP_STATE_DATABASE_URL':'invalid','ENERGY_MAP_ZITADEL_ISSUER':'invalid'}),TestClient(app) as client:
            self.assertEqual(client.get('/healthz').status_code,200)

    def test_08a_direct_and_public_source_connect_rejected_despite_table_denial(self):
        t = self.target
        with connected(t, database=t['source_database']) as connection:
            def source_snapshot():
                return (
                    scalar(connection, 'SELECT datacl::text FROM pg_database WHERE datname=current_database()'),
                    rows(connection, 'SELECT to_jsonb(t) FROM public.source_probe t'),
                    rows(connection, 'SELECT oid,lomowner,lomacl::text FROM pg_largeobject_metadata ORDER BY oid'),
                    rows(connection, 'SELECT loid,pageno,data FROM pg_largeobject ORDER BY loid,pageno'),
                )
            before = source_snapshot()
            connection.autocommit = False
            for label, grantee in (('direct', sql.Identifier(t['writer'])), ('public', sql.SQL('PUBLIC'))):
                with self.subTest(grant=label):
                    try:
                        rows(connection, sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                            sql.Identifier(t['source_database']), grantee))
                        self.assertTrue(scalar(connection,
                            "SELECT has_database_privilege(%s,current_database(),'CONNECT')", (t['writer'],)))
                        self.assertFalse(scalar(connection,
                            "SELECT has_table_privilege(%s,'public.source_probe','INSERT,UPDATE,DELETE')", (t['writer'],)))
                        # Reproduce the missed write using the exact writer role,
                        # without committing a source grant or a large object.
                        rows(connection, sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(t['writer'])))
                        oid = scalar(connection, 'SELECT pg_catalog.lo_create(0)')
                        self.assertTrue(scalar(connection,
                            'SELECT lomowner=current_user::regrole FROM pg_largeobject_metadata WHERE oid=%s', (oid,)))
                        rows(connection, 'RESET ROLE')
                        with self.assertRaises(StorageError) as failure:
                            audit_source(connection, t)
                        self.assertEqual(failure.exception.category, 'source_separation')
                        # Real completed-target paths cannot report readiness;
                        # only source-session borrowing makes uncommitted ACLs visible.
                        with source_audit_transaction(t, connection):
                            for operation in (verify, apply):
                                with self.assertRaises(StorageError) as failure:
                                    operation(t)
                                self.assertEqual(failure.exception.category, 'source_separation')
                    finally:
                        connection.rollback()
                    self.assertTrue(before == source_snapshot())
                    audit_source(connection, t)
                    connection.rollback()
        self.assertEqual(verify(t)['status'], 'verified')

    def test_09_public_schema_restore_resumes_before_client_commit(self):
        t, target = self.target, self.recovery
        self.assertTrue(target['restore_from'] == t['backup_dir'])
        self.assertEqual(inspect(target)['status'], 'target_absent')
        # A migrator-owned empty public schema is a supported archive variant.
        # Restore source ownership even if backup fails; never edit its rows.
        with connected(t, database=t['database']) as connection:
            self.assertEqual(scalar(connection, "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname='public'"), 'pg_database_owner')
            rows(connection, sql.SQL('ALTER SCHEMA public OWNER TO {}').format(sql.Identifier(t['migrator'])))
            try:
                self.assertEqual(backup(t)['status'], 'backup_verified')
            finally:
                rows(connection, 'ALTER SCHEMA public OWNER TO pg_database_owner')
        self.assertEqual(verify(t)['status'], 'verified')
        manifest = read_json(Path(t['backup_dir']) / 'manifest.json')
        real_client = recovery.client

        def interrupt_before_restore(t, program, args, **kwargs):
            if program == 'pg_restore' and any(arg.startswith('--dbname=') for arg in args):
                raise StorageError('injected_before_restore')
            return real_client(t, program, args, **kwargs)

        with patch('src.state_storage.recovery.client', side_effect=interrupt_before_restore):
            with self.assertRaises(StorageError) as failure:
                restore(target)
        self.assertEqual(failure.exception.category, 'injected_before_restore')
        pending = load_bundle(target)
        self.assertEqual(pending['phase'], 'migrating')
        with connected(target, database=target['database'], read_only=True) as connection:
            self.assertIsNone(scalar(connection, "SELECT to_regnamespace('public')"))
            self.assertIsNone(scalar(connection, "SELECT to_regnamespace('energymap_state')"))
            self.assertFalse(scalar(connection, 'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s', (target['writer'],)))
        self.assertFalse(Path(target['note_file']).exists())
        self.assertFalse(Path(target['runtime_file']).exists())

        self.assertEqual(restore(target)['status'], 'restore_verified')
        completed = load_bundle(target)
        self.assertEqual(completed['phase'], 'complete')
        for name in ('writer', 'migrator'):
            self.assertTrue(completed[name + '_password'] == pending[name + '_password'])
            self.assertTrue(completed[name + '_verifier_hash'] == pending[name + '_verifier_hash'])
        self.assertEqual(verify(target)['status'], 'verified')
        with runtime_environment(target, completed):
            self.assertTrue(state_ready())
        with connected(target, database=target['database'], read_only=True) as connection:
            self.assertTrue(data_fingerprint(connection) == manifest['data'])

    def test_10_complete_private_backup_restore_and_second_interpreter(self):
        t=self.target;r=self.recovery
        # Reuse the public-schema archive and completed retry target above.
        backup_path=Path(t['backup_dir'])
        if not (backup_path/'manifest.json').exists():
            self.assertEqual(backup(t)['status'],'backup_verified')
        self.assertEqual(restore(r)['status'],'restore_verified')
        self.assertEqual(restore(r)['status'],'restore_verified')
        for path in (backup_path/'state.dump',backup_path/'manifest.json'):
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
        with connected(t,database=t['database'],read_only=True) as source,connected(r,database=r['database'],read_only=True) as recovered:
            self.assertTrue(data_fingerprint(source)==data_fingerprint(recovered))
        env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
        env['PYTHONPATH']=str(ROOT)+':'+str(ROOT/'src')
        # Private probe path only in argv. Output is boolean success, never a token/digest.
        process=subprocess.run([sys.executable,'-m','tests.state_recovery_probe',os.environ[RESTORE],str(Path(t['private_dir'])/'recovery-probe.json')],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        self.assertTrue(process.returncode==0)
        self.assertEqual(process.stdout,b'recovery_operations=true\n')

    def test_11_archive_integrity_and_restore_target_rejection(self):
        t=self.target;r=self.recovery
        # A source/current-target restore must be rejected before mutation.
        wrong=Target(t.values|{'restore_from':t['backup_dir']})
        with self.assertRaises(StorageError):restore(wrong)
        path=Path(t['backup_dir'])/'state.dump'
        with path.open('ab') as handle:handle.write(b'corruption')
        try:
            with self.assertRaises(StorageError):restore(r)
        finally:
            with path.open('r+b') as handle:handle.truncate(path.stat().st_size-len(b'corruption'))
