"""Explicit protected descriptor and finite lower-only resource budgets."""
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from .validation import ExportError, strict_json, text_ok

PRIVATE_REL = '.codex-orch/runtime/f005-canonical-source-export-private'
MAXIMA = {'manifest': 65536, 'record': 8192, 'rows': 25000000, 'total_bytes': 80 * 1024**3,
          'buffer_bytes': 16 * 1024**2, 'overall_seconds': 28800, 'statement_ms': 60000,
          'lock_ms': 1000, 'connect_seconds': 10, 'disk_bytes': 1024**4, 'scratch_bytes': 1024**4,
          'reserve_bytes': 1024**4}
DEFAULTS = dict(MAXIMA, rows=1000000, total_bytes=4*1024**3, buffer_bytes=1024**2,
                overall_seconds=900, statement_ms=30000, connect_seconds=5,
                disk_bytes=16*1024**3, scratch_bytes=8*1024**3, reserve_bytes=2*1024**3)

# Admission is qualified per exact fixture-proven patch, never per major version.
PUBLIC_TEMP_VERSIONS = frozenset({170002, 170005})
FIXTURE_DATABASES = frozenset({'energymap_f005_canonical_export_test',
                               'energymap_f006_canonical_export_pg170002_test'})


@dataclass(frozen=True)
class DatabaseTempPolicy:
    mode: str = 'deny'
    database_name: str | None = None
    database_oid: int | None = None
    reader_name: str | None = None
    reader_oid: int | None = None
    server_version_num: int | None = None
    evidence_reference: str | None = None


def temp_policy(value, connection):
    if not isinstance(value, dict):
        raise ExportError('temp_policy_invalid')
    if value == {'mode': 'deny'}:
        return DatabaseTempPolicy()
    fields = set(DatabaseTempPolicy.__dataclass_fields__)
    if set(value) != fields or value['mode'] != 'allow-inherited-public-temp':
        raise ExportError('temp_policy_invalid')
    for field in ('database_oid', 'reader_oid', 'server_version_num'):
        if type(value[field]) is not int or not 0 < value[field] <= 4294967295:
            raise ExportError('temp_policy_invalid')
    for field, bound in (('database_name', 'dbname'), ('reader_name', 'user')):
        if not text_ok(value[field], 63, True) or value[field] != connection[bound]:
            raise ExportError('temp_policy_identity')
    # A reference identifier, never a URL/DSN, free-form note or secret payload.
    evidence = value['evidence_reference']
    if (not isinstance(evidence, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}', evidence)
        or '://' in evidence):
        raise ExportError('temp_policy_invalid')
    if value['server_version_num'] not in PUBLIC_TEMP_VERSIONS:
        raise ExportError('temp_policy_version')
    return DatabaseTempPolicy(**value)


def safe_path(path):
    p = Path(os.path.abspath(path))
    for q in (p, *p.parents):
        if q.is_symlink():
            raise ExportError('unsafe_path')
    return p


def private_dir(path):
    p = safe_path(path)
    s = p.stat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700:
        raise ExportError('unsafe_directory')
    return p


def private_read(path, limit=65536):
    p = safe_path(path)
    private_dir(p.parent)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1 or s.st_size > limit:
            raise ExportError('unsafe_file')
        data = os.read(fd, limit+1)
        if len(data) > limit:
            raise ExportError('resource_limit')
        return data
    finally:
        os.close(fd)


def protected_root(path):
    p = private_dir(path)
    # Default boundary must be effective Git ignore, not merely an assumed rule.
    repo = subprocess.check_output(['git','-C',str(p),'rev-parse','--show-toplevel'],stderr=subprocess.DEVNULL,timeout=5).decode().strip()
    boundary = Path(repo) / PRIVATE_REL
    if not p.is_relative_to(boundary):
        raise ExportError('output_boundary')
    for parent in (p,*p.parents):
        private_dir(parent)
        if parent == boundary:
            break
    r = subprocess.run(['git', '-C', str(p), 'check-ignore', '-q', str(p / 'probe')],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    if r.returncode:
        raise ExportError('output_not_ignored')
    return p


@dataclass(frozen=True)
class Config:
    connection: dict
    root: Path
    publisher: str
    license_references: list
    fixture: dict | None
    limits: dict
    database_temp_policy: DatabaseTempPolicy = DatabaseTempPolicy()


def load_config(path):
    c = strict_json(private_read(path))
    fields = {'version', 'connection', 'output_root', 'publisher_role', 'license_references', 'fixture', 'limits'}
    if (not isinstance(c, dict) or type(c.get('version')) is not int or c['version'] not in (1, 2)
        or set(c) != fields | ({'database_temp_policy'} if c['version'] == 2 else set())):
        raise ExportError('config_invalid')
    conn = c['connection']
    required = {'host', 'port', 'dbname', 'user', 'password', 'sslmode'}
    if not isinstance(conn, dict) or not required <= set(conn) or set(conn) - required - {'sslrootcert'}:
        raise ExportError('connection_invalid')
    if any(not isinstance(conn[k], str) or not conn[k] or '\0' in conn[k] for k in required - {'port'}):
        raise ExportError('connection_invalid')
    if type(conn['port']) is not int or not 1 <= conn['port'] <= 65535 or ',' in conn['host'] or '/' in conn['host']:
        raise ExportError('connection_invalid')
    fixture = c['fixture']
    if fixture is not None:
        if (not isinstance(fixture, dict) or set(fixture) != {'marker', 'database_oid', 'server_version_num'}
            or not isinstance(fixture['marker'], str) or fixture['marker'] not in FIXTURE_DATABASES
            or conn['dbname'] != fixture['marker'] or conn['host'] not in ('127.0.0.1','localhost')
            or type(fixture['database_oid']) is not int or type(fixture['server_version_num']) is not int):
            raise ExportError('fixture_invalid')
    if conn['sslmode'] != 'verify-full':
        if not (fixture and conn['sslmode'] == 'disable'):
            raise ExportError('tls_required')
    elif not isinstance(conn.get('sslrootcert'), str) or not Path(conn['sslrootcert']).is_file():
        raise ExportError('tls_root_required')
    if any(k.startswith('PG') for k in os.environ):
        raise ExportError('ambient_pg_environment')
    policy = temp_policy(c['database_temp_policy'], conn) if c['version'] == 2 else DatabaseTempPolicy()
    if policy.mode != 'deny' and conn['sslmode'] != 'verify-full':
        raise ExportError('tls_required')
    if not text_ok(c['publisher_role'], 128, True) or not isinstance(c['license_references'], list) or not 1 <= len(c['license_references']) <= 8 or any(not text_ok(v, 256, True) for v in c['license_references']):
        raise ExportError('publisher_evidence_required')
    cap = DEFAULTS.copy()
    if not isinstance(c['limits'], dict):
        raise ExportError('limits_invalid')
    for k, v in c['limits'].items():
        if k not in MAXIMA or type(v) is not int or not 1 <= v <= MAXIMA[k]:
            raise ExportError('limits_invalid')
        cap[k] = v
    # Worst case DB wire/Python strings + projected object/schema/serialized row
    # at fetch size one. SQLite cache is separately bounded local audit memory.
    if cap['buffer_bytes'] < 256*1024 or cap['scratch_bytes'] > cap['disk_bytes'] or cap['record']*16 > cap['buffer_bytes']:
        raise ExportError('limits_invalid')
    return Config(dict(conn), protected_root(c['output_root']), c['publisher_role'], c['license_references'], fixture, cap, policy)


def generation_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', value):
        raise ExportError('generation_invalid')
    return value
