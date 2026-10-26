"""Explicit state transport. No ambient libpq connection fallback."""
from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import stat

from sqlalchemy.engine import make_url

# Analytical settings remain valid; PGOPTIONS is explicitly replaced for state.
ALLOWED_PG_ENV = {'PGUSER', 'PGPASSWORD', 'PGHOST', 'PGPORT', 'PGDATABASE', 'PGOPTIONS'}
OPTIONS = ('-c default_transaction_read_only=off -c statement_timeout=10000 '
           '-c lock_timeout=2000 -c search_path=pg_catalog,energymap_state')


def check_ambient(environ):
    if any(k.startswith('PG') and v and k not in ALLOWED_PG_ENV for k, v in environ.items()):
        raise ValueError('state configuration rejected')


def tls_file(value, *, secret=False):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('state configuration rejected')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('state configuration rejected')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or
            info.st_mode & 0o022 or (secret and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600))):
        raise ValueError('state configuration rejected')
    return str(path)


@dataclass(frozen=True, repr=False)
class StateSettings:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    sslmode: str = 'verify-full'
    sslrootcert: str = ''
    sslcert: str = ''
    sslkey: str = ''

    def kwargs(self, *, read_only=False):
        return dict(host=self.host, port=self.port, dbname=self.database,
                    user=self.user, password=self.password, connect_timeout=3,
                    options=OPTIONS.replace('read_only=off', 'read_only=on') if read_only else OPTIONS,
                    sslmode=self.sslmode, sslrootcert=self.sslrootcert,
                    sslcert=self.sslcert, sslkey=self.sslkey, passfile='/dev/null',
                    application_name='energymap-state')


def connection_settings(*, host, port, database, user, password, sslmode,
                        sslrootcert='', sslcert='', sslkey='', disposable_insecure=False):
    check_ambient(os.environ)
    if (not all(isinstance(v, str) and v and not any(c in v for c in '\x00\r\n')
                for v in (host, database, user, password)) or
            any(c in host for c in ',/ ') or type(port) is not int or not 1 <= port <= 65535):
        raise ValueError('state configuration rejected')
    if sslmode == 'disable':
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback or disposable_insecure is not True or any((sslrootcert, sslcert, sslkey)):
            raise ValueError('state configuration rejected')
    elif sslmode == 'verify-full':
        sslrootcert = tls_file(sslrootcert)
        if bool(sslcert) != bool(sslkey):
            raise ValueError('state configuration rejected')
        if sslcert:
            sslcert, sslkey = tls_file(sslcert), tls_file(sslkey, secret=True)
    else:
        raise ValueError('state configuration rejected')
    return StateSettings(host, port, database, user, password, sslmode, sslrootcert, sslcert, sslkey)


def runtime_settings(environ):
    url = make_url(environ['ENERGY_MAP_STATE_DATABASE_URL'])
    if url.drivername not in ('postgresql', 'postgresql+psycopg2') or url.query:
        raise ValueError('state configuration rejected')
    return connection_settings(host=url.host, port=url.port or 5432,
        database=url.database, user=url.username, password=url.password,
        sslmode=environ.get('ENERGY_MAP_STATE_SSLMODE', 'verify-full'),
        sslrootcert=environ.get('ENERGY_MAP_STATE_SSLROOTCERT', ''),
        sslcert=environ.get('ENERGY_MAP_STATE_SSLCERT', ''),
        sslkey=environ.get('ENERGY_MAP_STATE_SSLKEY', ''),
        disposable_insecure=environ.get('ENERGY_MAP_STATE_DISPOSABLE_INSECURE') == 'true')
