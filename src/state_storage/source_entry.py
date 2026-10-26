"""Pinned HBA evidence and fresh entry checks; no source ACL changes."""
from pathlib import Path
import time
import re

import psycopg2

from .catalog import rows, scalar
from .hba_file import digest, policy
from .private import StorageError, read, read_json, require, write_json


def prefix(target):
    return policy(target['database'], target['migrator'], target['writer'], target['provisioning_id'])


def contents(connection):
    path = scalar(connection, 'SHOW hba_file')
    return path, bytes(scalar(connection, 'SELECT pg_read_binary_file(%s)', (path,)))


def audit_policy(connection, target):
    entry = target.values.get('source_entry', {'mode':'acl'})
    if entry['mode'] == 'acl':
        return
    path, data = contents(connection)
    block = prefix(target)
    offset = data.find(block)
    require(offset >= 0 and data.count(block) == 1 and
            digest(data[offset + len(block):]) == entry['baseline_sha256'], 'entry_policy')
    first_line = data[:offset].count(b'\n') + 2
    require(scalar(connection, 'SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL') == 0, 'entry_policy')
    expected = [
        ('local',['all'],None,'reject'),
        ('local',['replication'],None,'reject'),
        ('hostssl',[target['database']],'all','scram-sha-256'),
        ('host',['all'],'all','reject'),
        ('host',['replication'],'all','reject'),
    ]
    actual = rows(connection, '''SELECT type,database,address,auth_method,user_name,options,file_name
        FROM pg_hba_file_rules WHERE file_name=%s AND line_number >= %s
        ORDER BY rule_number LIMIT 5''', (path, first_line))
    # Later independently confined account pairs may prepend their own rules.
    # No preceding rule may match either of OUR roles, directly or by a broad
    # token/regex/group/file expansion. Memberships are independently forbidden.
    preceding = rows(connection, '''SELECT user_name FROM pg_hba_file_rules
        WHERE rule_number < (SELECT rule_number FROM pg_hba_file_rules
            WHERE file_name=%s AND line_number=%s)''', (path, first_line))
    require(all(users and all(re.fullmatch('[a-z][a-z0-9_]{0,62}', u)
            and u not in ('all',target['writer'],target['migrator']) for u in users)
            for (users,) in preceding), 'entry_policy')
    require(len(actual) == 5, 'entry_policy')
    for row, wanted in zip(actual, expected):
        require(row[:4] == wanted and row[4] == [target['migrator'],target['writer']]
                and not row[5] and row[6] == path, 'entry_policy')


def reader_probe(target):
    from .operations import connected
    password = read(target['source_entry']['reader_password_file']).strip()
    with connected(target, database=target['source_database'], user=target['source_reader'],
                   password=password, read_only=True) as connection:
        require(scalar(connection, 'SELECT 1') == 1, 'source_reader')


def entry_probe(target, bundle):
    """Successful password/TLS authentication to state precedes every refusal.

    libpq connection exceptions do not expose a structured SQLSTATE. Accept only
    the specific server FATAL entry errors, never arbitrary operational errors.
    Raw error text is consumed privately and is never reflected.
    """
    from .operations import connected
    mode = target.values.get('source_entry', {'mode':'acl'})['mode']
    for role in ('migrator','writer'):
        with connected(target, database=target['database'], user=target[role],
                       password=bundle[role+'_password'], read_only=True):
            pass
        try:
            with connected(target, database=target['source_database'], user=target[role],
                           password=bundle[role+'_password'], read_only=True):
                pass
        except psycopg2.OperationalError as error:
            text = str(error)
            # Require explicit server rejection, bound to this user/database.
            if mode == 'hba':
                denied = ('pg_hba.conf rejects connection for host ' in text
                    and ('user "' + target[role] + '"') in text
                    and ('database "' + target['source_database'] + '"') in text)
            else:
                denied = ('permission denied for database "' + target['source_database'] + '"') in text
            require(denied and 'FATAL:' in text, 'source_entry_unproven')
        else:
            raise StorageError('source_separation')
    if mode == 'hba':
        reader_probe(target)


def prepare(target):
    from .operations import connected, preflight
    require(target.values.get('source_entry',{}).get('mode') == 'hba', 'configuration')
    with connected(target, read_only=True) as connection:
        preflight(connection, target)
        require(not scalar(connection, 'SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname IN (%s,%s))',
            (target['migrator'],target['writer'])), 'resource_collision')
        require(not scalar(connection, 'SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=%s)',
            (target['database'],)), 'resource_collision')
        path, data = contents(connection)
        require(digest(data) == target['source_entry']['baseline_sha256'], 'entry_policy')
        require(scalar(connection, 'SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL') == 0, 'entry_policy')
        # Exact role tokens already present are a policy/name collision.
        require(not scalar(connection, '''SELECT EXISTS(SELECT 1 FROM pg_hba_file_rules
            WHERE user_name && ARRAY[%s,%s])''', (target['migrator'],target['writer'])), 'resource_collision')
    request = {k:target[k] for k in ('mode','database','migrator','writer','provisioning_id')}
    request.update(hba_file=path, baseline_sha256=target['source_entry']['baseline_sha256'])
    dest = Path(target['private_dir'])/'entry-request.json'
    reader_probe(target)
    if dest.exists():
        require(read_json(dest) == request, 'output_collision')
    else:
        write_json(dest, request)
    return {'status':'entry_request_prepared'}


def reload_policy(target):
    from .operations import connected, preflight
    require(target.values.get('source_entry',{}).get('mode') == 'hba', 'configuration')
    with connected(target) as connection:
        preflight(connection, target)
        _, data = contents(connection)
        if digest(data) == target['source_entry']['baseline_sha256']:
            # Rollback may remove confinement only while both new accounts are
            # disabled (or absent). Never alter a pre-existing unmarked role.
            actual = rows(connection, '''SELECT rolcanlogin,shobj_description(oid,'pg_authid')
                FROM pg_roles WHERE rolname IN (%s,%s)''', (target['migrator'],target['writer']))
            require(all(not login and marker == target.marker for login,marker in actual), 'entry_rollback')
            require(scalar(connection, 'SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL') == 0, 'entry_policy')
        else:
            audit_policy(connection, target)
        previous = scalar(connection, 'SELECT pg_conf_load_time()')
        require(scalar(connection, 'SELECT pg_reload_conf()') is True, 'entry_reload')
        deadline = time.monotonic() + 3
        while scalar(connection, 'SELECT pg_conf_load_time()') <= previous:
            require(time.monotonic() < deadline, 'entry_reload')
            time.sleep(0.05)
    reader_probe(target)
    return {'status':'entry_reload_requested'}
