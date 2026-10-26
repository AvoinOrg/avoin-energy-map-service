"""Complete private PostgreSQL 17 dumps and fresh-target restores only."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .catalog import (DATA, META, SAVED, SHARES, VERSION, audit_acl, audit_schema, grant_state,
                      rows, scalar)
from .operations import apply, connected, load_bundle, verify
from .private import (directory, private_file, read_json, safe_path,
                      require, write_json)

POLICY = {'contract':1, 'version':VERSION, 'database':'CONNECT', 'schema':'USAGE',
          'saved':['SELECT','INSERT','DELETE'], 'share':['SELECT','INSERT','DELETE'],
          'mutable':['display_name','schema_version','overrides','scenario_inputs',
                     'last_known_summary','revision','updated_at'],
          'public':[], 'default_public_function_execute':False, 'default_public_type_usage':False,
          'roles':'LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; no memberships'}


def data_fingerprint(connection):
    rows(connection,"SET TIME ZONE 'UTC'")
    result = {}
    for schema,table in ((DATA,SAVED),(DATA,SHARES),(META,'alembic_version')):
        # Full rows including exact identities, all JSON, timestamps and bytea.
        values = rows(connection, 'SELECT to_jsonb(t) FROM ' + schema + '.' + table + ' t ORDER BY 1::text')
        encoded = sorted(json.dumps(row[0],sort_keys=True,separators=(',',':')).encode() for row in values)
        digest = hashlib.sha256()
        for value in encoded:
            digest.update(len(value).to_bytes(8,'big')); digest.update(value)
        result[table] = {'count':len(values),'sha256':digest.hexdigest()}
    return result


def file_hash(path):
    digest = hashlib.sha256()
    with open(private_file(path),'rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def client(target, program, args, *, user, password, database):
    executable = safe_path(str(Path(target['client_bin_dir']) / program))
    require(executable.is_file() and os.access(executable,os.X_OK), 'client_missing')
    info=executable.stat()
    require(info.st_uid in (0,os.getuid()) and not info.st_mode & 0o022,'client_missing')
    fd, passpath = tempfile.mkstemp(prefix='.pass-',dir=directory(target['private_dir']))
    try:
        def escape(value):
            return str(value).replace('\\','\\\\').replace(':','\\:')
        with os.fdopen(fd,'w') as handle:
            os.fchmod(handle.fileno(),0o600)
            handle.write(':'.join(escape(v) for v in (target['host'],target['port'],database,user,password))+'\n')
        env = {'PATH':'/usr/bin:/bin','HOME':target['private_dir'], 'LC_ALL':'C',
               'PGHOST':target['host'],'PGPORT':str(target['port']),'PGDATABASE':database,
               'PGUSER':user,'PGPASSFILE':passpath,'PGCONNECT_TIMEOUT':'3',
               'PGSSLMODE':target['sslmode'],'PGSSLROOTCERT':target['sslrootcert'],
               'PGOPTIONS':('-c default_transaction_read_only=on ' if program=='pg_dump' else '-c default_transaction_read_only=off ')+'-c statement_timeout=120000 -c lock_timeout=2000 -c search_path=pg_catalog'}
        for key in ('sslcert','sslkey'):
            if target.values.get(key):
                env['PG' + key.upper()] = target[key]
        result = subprocess.run([str(executable),*args],env=env,stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180,check=False)
        require(result.returncode == 0,'client_failed')
        return result.stdout
    finally:
        os.unlink(passpath)


def client_version(target):
    value = client(target,'pg_dump',['--version'],user=target['migrator'],password='unused',database=target['database'])
    require(re.search(rb'PostgreSQL\) 17\.',value) is not None,'client_version')
    return '17'


def inventory(target,archive,bundle, *, source_owner):
    listing = client(target,'pg_restore',['--list',str(archive)],user=target['migrator'],password=bundle['migrator_password'],database=target['database']).decode()
    public = False
    count = 0
    allowed = {
        ('SCHEMA','-','energymap_state'), ('SCHEMA','-','energymap_state_meta'),
        ('SCHEMA','-','public'),
        ('TABLE',DATA,SAVED),('TABLE',DATA,SHARES),('TABLE',META,'alembic_version'),
        ('TABLE DATA',DATA,SAVED),('TABLE DATA',DATA,SHARES),('TABLE DATA',META,'alembic_version'),
        ('CONSTRAINT',DATA,SAVED+' energymap_user_buildings_pkey'),
        ('CONSTRAINT',DATA,SHARES+' energymap_user_building_shares_pkey'),
        ('CONSTRAINT',DATA,SHARES+' uq_building_shares_digest'),
        ('CONSTRAINT',META,'alembic_version alembic_version_pkc'),
        ('FK CONSTRAINT',DATA,SHARES+' fk_building_shares_saved'),
        ('INDEX',DATA,'uq_user_buildings_owner_key'),('INDEX',DATA,'ix_user_buildings_owner_updated'),
        ('ACL','-','SCHEMA '+DATA),('ACL','-','SCHEMA '+META),('ACL','-','SCHEMA public'),
        ('ACL',DATA,'TABLE '+SAVED),('ACL',DATA,'TABLE '+SHARES),('ACL',META,'TABLE alembic_version'),
        *{('ACL',DATA,'COLUMN '+SAVED+'.'+col) for col in POLICY['mutable']},
        ('DEFAULT ACL','-','DEFAULT PRIVILEGES FOR FUNCTIONS'),('DEFAULT ACL','-','DEFAULT PRIVILEGES FOR TYPES'),
        ('COMMENT','-','SCHEMA public'),
    }
    for line in listing.splitlines():
        if not line or line.startswith(';'):
            continue
        match = re.fullmatch(r'\d+; \d+ \d+ (SCHEMA|TABLE DATA|TABLE|FK CONSTRAINT|CONSTRAINT|INDEX|ACL|DEFAULT ACL|COMMENT) (\S+) (.+) (\S+)',line)
        require(match is not None,'archive_inventory')
        kind,schema,name,owner = match.groups()
        require((kind,schema,name) in allowed and owner in (source_owner,'pg_database_owner','-'),'archive_inventory')
        public |= (kind,schema,name) == ('SCHEMA','-','public')
        count += 1
    require(count >= 14,'archive_inventory')
    return public


def backup(target):
    verify(target)
    bundle = load_bundle(target)
    destination = directory(target['backup_dir'])
    require(not any(destination.iterdir()),'backup_collision')
    client_version(target)
    archive = destination / 'state.dump'
    fd,temporary = tempfile.mkstemp(prefix='.dump-',dir=destination)
    os.close(fd)
    try:
        # One read-only repeatable-read snapshot binds dump and full-row manifest.
        with connected(target,database=target['database'],user=target['migrator'],password=bundle['migrator_password']) as connection:
            connection.autocommit=False
            rows(connection,'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            rows(connection,"SET TIME ZONE 'UTC'")
            snapshot_id = scalar(connection,'SELECT pg_export_snapshot()')
            fingerprint = data_fingerprint(connection)
            client(target,'pg_dump',['--no-password','--format=custom','--no-publications','--no-subscriptions',
                   '--snapshot='+snapshot_id,'--file='+temporary],user=target['migrator'],password=bundle['migrator_password'],database=target['database'])
            connection.rollback()
        inventory(target,temporary,bundle,source_owner=target['migrator'])
        os.chmod(temporary,0o600)
        with open(temporary,'rb') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary,archive)
        manifest = dict(policy=POLICY, archive_sha256=file_hash(archive), data=fingerprint,
                        source_owner=target['migrator'],source_writer=target['writer'],
                        source_provisioning_id=target['provisioning_id'],source_database=target['database'],
                        postgresql_major=17,client_major=17)
        write_json(destination / 'manifest.json',manifest)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return {'status':'backup_verified','version':VERSION}


def restore(target):
    origin = directory(target['restore_from'])
    archive = private_file(origin / 'state.dump')
    manifest = read_json(origin / 'manifest.json')
    require(manifest['policy'] == POLICY and manifest['archive_sha256'] == file_hash(archive) and
            manifest['postgresql_major'] == manifest['client_major'] == 17,'archive_integrity')
    require(target['database'] != manifest['source_database'] and target['provisioning_id'] != manifest['source_provisioning_id'] and
            not {target['migrator'],target['writer']} & {manifest['source_owner'],manifest['source_writer']},'restore_target')
    client_version(target)
    # Pre-validate before provision creates anything. Values are only used for pg_restore listing.
    public = inventory(target,archive,{'migrator_password':'unused'},source_owner=manifest['source_owner'])
    def recover(t,bundle):
        with connected(t,database=t['database'],user=t['migrator'],password=bundle['migrator_password']) as connection:
            restored = scalar(connection,"SELECT to_regnamespace('energymap_state') IS NOT NULL")
            # Public may already be absent after an interrupted attempt: its
            # baseline removal commits before the separate restore transaction.
            # A present schema must still pass both checks before removal.
            if not restored and public:
                public_exists = scalar(connection, "SELECT to_regnamespace('public') IS NOT NULL")
                if public_exists:
                    require(scalar(connection,"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'") == 0,'restore_target')
                    require(scalar(connection,"SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname='public'") == 'pg_database_owner','restore_target')
                    rows(connection,'DROP SCHEMA public')
        if not restored:
            client(t,'pg_restore',['--no-password','--no-owner','--no-privileges','--single-transaction','--exit-on-error',
                   '--dbname='+t['database'],str(archive)],user=t['migrator'],password=bundle['migrator_password'],database=t['database'])
        with connected(t,database=t['database'],user=t['migrator'],password=bundle['migrator_password']) as connection:
            connection.autocommit=False
            audit_schema(connection)
            require(data_fingerprint(connection) == manifest['data'],'restore_data')
            # PostgreSQL 17 --no-owner can omit the baseline public schema even
            # when it has an archive entry. Rebuild it in the grant transaction
            # after verifying the restored rows, including on a post-commit retry.
            if public and not scalar(connection, "SELECT to_regnamespace('public') IS NOT NULL"):
                rows(connection, 'CREATE SCHEMA public AUTHORIZATION pg_database_owner')
                rows(connection, 'REVOKE ALL ON SCHEMA public FROM PUBLIC')
            # pg_restore commits separately from grant reconstruction. An
            # interrupted retry must complete that transaction, never skip it or
            # conceal unsafe ACLs by revoking them. Only missing contract grants
            # are permitted on this managed, disabled, archive-identical target.
            audit_acl(connection,t,writer_login=False,allow_missing_state_grants=True)
            grant_state(connection,t)
            audit_acl(connection,t,writer_login=False)
            connection.commit()
    result = apply(target,restore_callback=recover)
    with connected(target,database=target['database'],read_only=True) as connection:
        require(data_fingerprint(connection) == manifest['data'],'restore_data')
    return result | {'status':'restore_verified'}
