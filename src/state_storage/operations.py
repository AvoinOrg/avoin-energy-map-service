"""Target-specific provisioning with one protected retry bundle."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
from urllib.parse import quote

from alembic import command
from alembic.config import Config
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import encrypt_password
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from src.db.state import check_state
from .admin_secret import admin_password
from .catalog import (audit_acl, audit_schema, audit_source, grant_state, harden,
                      role_audit, rows, scalar)
from .config import ROOT
from .source_entry import audit_policy, entry_probe
from .private import atomic_write, read, read_json, require, write_json


def connect(target, *, database=None, user=None, password=None, read_only=False):
    settings = target.settings(user or target['admin_user'], password if password is not None else
        admin_password(target), database or target['maintenance_database'])
    connection = psycopg2.connect(**settings.kwargs(read_only=read_only))
    try:
        connection.autocommit = True
        require(scalar(connection, 'SELECT current_database()') == (database or target['maintenance_database']), 'target_rejected')
        require(scalar(connection, 'SELECT current_user') == (user or target['admin_user']), 'target_rejected')
        if user is None:
            require(scalar(connection, 'SELECT system_identifier::text FROM pg_control_system()') == target['system_identifier'], 'target_rejected')
        if target['sslmode'] == 'verify-full':
            require(scalar(connection, 'SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()') is True, 'transport_failed')
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def connected(target, **kwargs):
    connection = connect(target, **kwargs)
    try:
        yield connection
    finally:
        connection.close()


def secret_safe_session(connection):
    # Session-local only. No credential DDL until this policy is verified.
    require(scalar(connection, "SELECT current_setting('shared_preload_libraries')") == '', 'unsafe_logging')
    for setting in ('session_preload_libraries', 'local_preload_libraries'):
        require(scalar(connection, 'SELECT current_setting(%s)', (setting,)) == '', 'unsafe_logging')
    for key, value in {'log_statement':'none', 'log_min_duration_statement':'-1',
                       'log_min_duration_sample':'-1', 'log_transaction_sample_rate':'0',
                       'log_min_error_statement':'panic',
                       'log_parameter_max_length':'0', 'log_parameter_max_length_on_error':'0'}.items():
        rows(connection, 'SELECT set_config(%s,%s,false)', (key,value))
        require(scalar(connection, 'SELECT current_setting(%s)', (key,)) == value, 'unsafe_logging')


def bundle_path(target):
    return Path(target['private_dir']) / 'bundle.json'


def passwords_from_note(target):
    content = read(target['note_file'])
    require(content.startswith('---\nsensitivity: secret\n---\n'), 'credential_mismatch')
    return json.loads(content.split('```json\n',1)[1].split('\n```',1)[0])


def load_bundle(target, *, prepare=False):
    path = bundle_path(target)
    if path.exists():
        bundle = read_json(path)
        require(bundle['binding'] == target.binding and bundle['marker'] == target.marker, 'credential_mismatch')
        if 'writer_password' not in bundle:
            require(bundle['phase'] == 'complete', 'credential_mismatch')
            note = passwords_from_note(target)
            for name in ('writer', 'migrator'):
                password = note[name + '_password']
                require(hashlib.sha256(password.encode()).hexdigest() == bundle[name + '_hash'], 'credential_mismatch')
                bundle[name + '_password'] = password
        return bundle
    require(prepare, 'unmanaged')
    require(not Path(target['note_file']).exists() and not Path(target['runtime_file']).exists(), 'output_collision')
    bundle = dict(binding=target.binding, marker=target.marker, phase='prepared',
                  writer_password=secrets.token_urlsafe(40), migrator_password=secrets.token_urlsafe(40))
    write_json(path, bundle)
    return bundle


def save_bundle(target, bundle, phase):
    bundle['phase'] = phase
    write_json(bundle_path(target), bundle)


def resource_state(connection, target, bundle):
    roles = {}
    for key in ('migrator', 'writer'):
        name = target[key]
        record = rows(connection, "SELECT rolcanlogin,shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s", (name,))
        if record:
            require(record[0][1] == target.marker and bundle['phase'] != 'prepared', 'resource_collision')
            role_audit(connection, name, login=record[0][0])
            roles[key] = record[0][0]
    require(not roles or len(roles) == 2, 'partial_incompatible')
    if roles and bundle['phase'] not in ('migrated','writer_enabled','audited','complete'):
        require(roles['writer'] is False, 'role_drift')
    if roles and bundle['phase'] in ('creating_roles','roles','creating_database'):
        require(roles['migrator'] is False, 'role_drift')
    db = rows(connection, """SELECT oid,pg_get_userbyid(datdba),shobj_description(oid,'pg_database'),
        pg_encoding_to_char(encoding),datcollate,datctype,datistemplate,datallowconn
        FROM pg_database WHERE datname=%s""", (target['database'],))
    if db:
        require(len(roles) == 2 and db[0][1:] == (target['migrator'],target.marker,'UTF8','C','C',False,True), 'resource_collision')
        require(bundle.get('database_oid') in (None,db[0][0]), 'resource_collision')
    return roles, db


def preflight(connection, target):
    require(scalar(connection, 'SELECT rolsuper OR (rolcreatedb AND rolcreaterole) FROM pg_roles WHERE rolname=current_user') is True, 'admin_rights')
    require(int(scalar(connection, 'SHOW server_version_num')) // 10000 == 17, 'server_version')


def credentials_match(connection, target, bundle):
    # Compare supplied passwords to saved SCRAM verifiers by authenticating when LOGIN.
    for name in ('migrator','writer'):
        can_login = scalar(connection, 'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s', (target[name],))
        if can_login:
            with connected(target, database=target['database'], user=target[name], password=bundle[name + '_password']):
                pass
        else:
            # NOLOGIN phase: retain prewritten bundle; do not reset any verifier.
            verifier = scalar(connection, 'SELECT rolpassword FROM pg_authid WHERE rolname=%s', (target[name],))
            require(hashlib.sha256(verifier.encode()).hexdigest() == bundle[name + '_verifier_hash'], 'credential_mismatch')


def runtime_probe(target, bundle):
    settings = target.settings(target['writer'], bundle['writer_password'], target['database'])
    engine = create_engine('postgresql+psycopg2://', connect_args=settings.kwargs(read_only=True),
                           poolclass=NullPool, hide_parameters=True)
    try:
        with Session(engine) as session:
            check_state(session)
    finally:
        engine.dispose()


def full_audit(target, bundle, *, writer_login=True):
    with connected(target, database=target['database'], read_only=True) as connection:
        audit_schema(connection)
        audit_acl(connection, target, writer_login=writer_login)
    with connected(target, database=target['source_database'], read_only=True) as connection:
        audit_source(connection, target)
        audit_policy(connection, target)
    if writer_login:
        runtime_probe(target, bundle)
        entry_probe(target, bundle)


def output_texts(target, bundle):
    note = {k:target[k] for k in ('host','port','database','migrator','writer','sslmode','sslrootcert','provisioning_id')}
    note.update({k:bundle[k] for k in ('writer_password','migrator_password')})
    note_text = '---\nsensitivity: secret\n---\n\n# Energy personal-state credentials\n\n```json\n' + json.dumps(note, indent=2,sort_keys=True) + '\n```\n'
    # Strong URL-safe generated passwords; still encode every URL component.
    host = target['host'] if ':' not in target['host'] else '[' + target['host'] + ']'
    dsn = 'postgresql+psycopg2://' + quote(target['writer'],safe='') + ':' + quote(bundle['writer_password'],safe='') + '@' + host + ':' + str(target['port']) + '/' + quote(target['database'],safe='')
    settings = {'ENERGY_MAP_STATE_ENABLED':'true','ENERGY_MAP_STATE_DATABASE_URL':dsn,
                'ENERGY_MAP_STATE_SSLMODE':target['sslmode'], 'ENERGY_MAP_STATE_SSLROOTCERT':target['sslrootcert']}
    if target['disposable_insecure']:
        settings['ENERGY_MAP_STATE_DISPOSABLE_INSECURE'] = 'true'
    for key in ('sslcert','sslkey'):
        if target.values.get(key):
            settings['ENERGY_MAP_STATE_' + key.upper()] = target[key]
    require(all(not any(c in v for c in '\r\n\x00') for v in settings.values()), 'configuration')
    return note_text, ''.join(k + '=' + v + '\n' for k,v in settings.items())


def finalize(target, bundle):
    note, runtime = output_texts(target, bundle)
    for path, content in ((target['note_file'],note),(target['runtime_file'],runtime)):
        if Path(path).exists():
            require(read(path) == content, 'output_collision')
        else:
            atomic_write(path, content)
    require(read(target['note_file']) == note and read(target['runtime_file']) == runtime, 'output_readback')
    receipt = {k:v for k,v in bundle.items() if not k.endswith('_password')}
    receipt['phase'] = 'complete'
    for name in ('writer','migrator'):
        receipt[name + '_hash'] = hashlib.sha256(bundle[name + '_password'].encode()).hexdigest()
    write_json(bundle_path(target), receipt)


def inspect(target):
    with connected(target, read_only=True) as connection:
        preflight(connection,target)
        exists = scalar(connection, 'SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=%s)', (target['database'],))
        role_exists=scalar(connection,'SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname IN (%s,%s))',(target['migrator'],target['writer']))
        if exists or role_exists:
            bundle = load_bundle(target)
            resource_state(connection,target,bundle)
    return {'status':'managed_present' if exists else 'target_absent'}


def verify(target):
    bundle = load_bundle(target)
    require(bundle['phase'] == 'complete', 'incomplete')
    with connected(target, read_only=True) as connection:
        preflight(connection,target)
        roles, db = resource_state(connection,target,bundle)
        require(db and roles == {'writer':True,'migrator':True}, 'incomplete')
        credentials_match(connection,target,bundle)
    full_audit(target,bundle)
    note,runtime = output_texts(target,bundle)
    require(read(target['note_file']) == note and read(target['runtime_file']) == runtime, 'output_readback')
    return {'status':'verified','version':'es0001_saved_share'}


def apply(target, *, restore_callback=None):
    with connected(target) as admin:
        preflight(admin,target)
        require(scalar(admin, 'SELECT pg_try_advisory_lock(193847251,1)') is True, 'provision_busy')
        # The session owns/relinquishes the lock even on failure; waits are bounded.
        # A lock loser must never create or replace the durable retry credentials.
        bundle = load_bundle(target, prepare=True)
        roles, db = resource_state(admin,target,bundle)
        if bundle['phase'] == 'complete':
            require(roles and db, 'incomplete')
            return verify(target)
        try:
            if not roles:
                require(bundle['phase'] in ('prepared','creating_roles'), 'partial_incompatible')
                secret_safe_session(admin)
                # Persist boundary intent before transaction: a post-commit retry can identify its roles.
                save_bundle(target,bundle,'creating_roles')
                admin.autocommit = False
                try:
                    for name in ('migrator','writer'):
                        verifier = encrypt_password(bundle[name + '_password'],target[name],admin,algorithm='scram-sha-256')
                        bundle[name + '_verifier_hash'] = hashlib.sha256(verifier.encode()).hexdigest()
                        # The hash is not recoverable from the bundle; the password is prewritten.
                        rows(admin, sql.SQL('CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}').format(sql.Identifier(target[name]),sql.Literal(verifier)))
                        rows(admin, sql.SQL('COMMENT ON ROLE {} IS {}').format(sql.Identifier(target[name]),sql.Literal(target.marker)))
                    # Journal is durable before commit; a rolled-back retry may create again.
                    save_bundle(target,bundle,'creating_roles')
                    admin.commit()
                except BaseException:
                    admin.rollback()
                    raise
                finally:
                    admin.autocommit = True
                save_bundle(target,bundle,'roles')
            if roles:
                credentials_match(admin,target,bundle)
            if not db:
                require(bundle['phase'] in ('roles','creating_roles','creating_database'), 'partial_incompatible')
                save_bundle(target,bundle,'creating_database')
                # CREATE DATABASE cannot be atomic with COMMENT. An unmarked database after a
                # process crash is deliberately blocked, never automatically adopted.
                rows(admin, sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8' LOCALE_PROVIDER libc LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(target['database']),sql.Identifier(target['migrator'])))
                rows(admin, sql.SQL('COMMENT ON DATABASE {} IS {}').format(sql.Identifier(target['database']),sql.Literal(target.marker)))
                bundle['database_oid'] = scalar(admin,'SELECT oid FROM pg_database WHERE datname=%s',(target['database'],))
                save_bundle(target,bundle,'database')
            if bundle['phase'] in ('roles','creating_roles','creating_database','database'):
                with connected(target,database=target['database']) as connection:
                    # Never bootstrap over a non-empty database on resume.
                    require(scalar(connection,"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'") == 0,'partial_incompatible')
                    harden(connection,target)
                audit_policy(admin, target)
                rows(admin,sql.SQL('ALTER ROLE {} LOGIN').format(sql.Identifier(target['migrator'])))
                save_bundle(target,bundle,'migrating')
            # Older interrupted restores could record migrated before rebuilding
            # grants. Resume only while the positively identified writer is disabled;
            # an enabled/completed target remains verification-only on a retry.
            if bundle['phase'] == 'migrating' or (bundle['phase'] == 'migrated' and
                    restore_callback is not None and not roles['writer']):
                if restore_callback is not None:
                    restore_callback(target,bundle)
                else:
                    _migrate_and_grant(target,bundle)
                save_bundle(target,bundle,'migrated')
            if bundle['phase'] == 'migrated':
                already_enabled = scalar(admin,'SELECT rolcanlogin FROM pg_roles WHERE rolname=%s',(target['writer'],))
                full_audit(target,bundle,writer_login=already_enabled)
                if not already_enabled:
                    rows(admin,sql.SQL('ALTER ROLE {} LOGIN').format(sql.Identifier(target['writer'])))
                save_bundle(target,bundle,'writer_enabled')
            if bundle['phase'] in ('writer_enabled','audited'):
                full_audit(target,bundle)
                save_bundle(target,bundle,'audited')
                finalize(target,bundle)
            else:
                require(False,'incomplete')
        except BaseException:
            # Includes post-LOGIN audit, output and interrupted resume failures.
            # Only positively marked incomplete roles can be quarantined; a
            # completed verification-only rerun never enters this block.
            if bundle['phase'] in ('migrated','writer_enabled','audited') and scalar(admin,
                    "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=%s AND shobj_description(oid,'pg_authid')=%s)",
                    (target['writer'],target.marker)):
                rows(admin,sql.SQL('ALTER ROLE {} NOLOGIN').format(sql.Identifier(target['writer'])))
                if bundle['phase'] in ('writer_enabled','audited'):
                    save_bundle(target,bundle,'migrated')
            raise
    return verify(target)


def _migrate_and_grant(target,bundle):
    settings = target.settings(target['migrator'],bundle['migrator_password'],target['database'])
    engine = create_engine('postgresql+psycopg2://',connect_args=settings.kwargs(),poolclass=NullPool,hide_parameters=True)
    try:
        with engine.begin() as connection:
            # A crash after migration commit but before journal update can resume only
            # by auditing the complete schema, never by stamping/adopting unmarked data.
            exists = connection.exec_driver_sql("SELECT to_regnamespace('energymap_state_meta') IS NOT NULL").scalar_one()
            if not exists:
                connection.exec_driver_sql('CREATE SCHEMA energymap_state_meta')
                config = Config()
                config.set_main_option('script_location',str(ROOT / 'state_migrations'))
                config.attributes['connection'] = connection
                command.upgrade(config,'head')
                grant_state(connection.connection.driver_connection,target)
            audit_schema(connection.connection.driver_connection)
    finally:
        engine.dispose()
