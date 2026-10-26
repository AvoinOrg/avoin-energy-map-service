"""Closed explicit target descriptors, never ambient application configuration."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import UUID

from src.db.state_settings import connection_settings
from .admin_secret import ADMIN_NOTE, admin_password
from .private import directory, private_file, read_json, require, safe_path

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = {'mode', 'host', 'port', 'maintenance_database', 'admin_user',
    'admin_password_file', 'system_identifier', 'source_database', 'source_reader',
    'database', 'migrator', 'writer', 'provisioning_id', 'private_dir', 'note_file',
    'runtime_file', 'sslmode', 'sslrootcert', 'disposable_insecure', 'client_bin_dir'}
OPTIONAL = {'sslcert', 'sslkey', 'backup_dir', 'restore_from', 'source_entry'}
LIVE = ('energymap_personal_state', 'energymap_state_migrator', 'energymap_state_writer')


def identifier(value):
    require(isinstance(value, str) and re.fullmatch('[a-z][a-z0-9_]{0,62}', value) is not None, 'configuration')
    return value


@dataclass(repr=False)
class Target:
    values: dict = field(repr=False)

    def __getitem__(self, key):
        return self.values[key]

    @property
    def marker(self):
        return 'energymap-state:' + self['provisioning_id']

    @property
    def binding(self):
        return hashlib.sha256(json.dumps({k: v for k, v in self.values.items()
            if k not in {'backup_dir', 'restore_from', 'client_bin_dir'}}, sort_keys=True).encode()).hexdigest()

    def settings(self, user, password, database):
        return connection_settings(host=self['host'], port=self['port'], database=database,
            user=user, password=password, sslmode=self['sslmode'], sslrootcert=self['sslrootcert'],
            sslcert=self.values.get('sslcert', ''), sslkey=self.values.get('sslkey', ''),
            disposable_insecure=self['disposable_insecure'])


def load_target(path):
    private_file(path)
    directory(Path(path).parent)
    values = read_json(path)
    require(type(values) is dict and REQUIRED <= values.keys() <= REQUIRED | OPTIONAL, 'configuration')
    t = Target(values)
    require(t['mode'] in ('sandbox', 'disposable') and type(t['disposable_insecure']) is bool, 'configuration')
    for k in ('database', 'migrator', 'writer', 'maintenance_database', 'admin_user', 'source_database', 'source_reader'):
        identifier(t[k])
    require(str(UUID(t['provisioning_id'])) == t['provisioning_id'], 'configuration')
    require(isinstance(t['system_identifier'], str) and re.fullmatch('[0-9]{10,25}', t['system_identifier']), 'configuration')
    require(t['maintenance_database'] == 'postgres', 'target_rejected')
    require(len({t[k] for k in ('migrator', 'writer', 'admin_user', 'source_reader')}) == 4, 'target_rejected')
    require(t['database'] not in (t['source_database'], 'postgres', 'template0', 'template1', 'sandbox_energymap'), 'target_rejected')
    names = (t['database'], t['migrator'], t['writer'])
    if t['mode'] == 'sandbox':
        require(names == LIVE and not t['disposable_insecure'] and t['source_database']=='sandbox_energymap', 'target_rejected')
        require(t['admin_password_file']==ADMIN_NOTE,'admin_reference')
        require(t['note_file'] == '/home/xbexbex/syncthing/work/avoin/projects/energymap/reference/personal-state-sandbox-credentials.md', 'target_rejected')
        require(t['runtime_file'] == str(ROOT / '.codex-orch/runtime/f003-sandbox-state/energy-state.env'), 'target_rejected')
    else:
        require(t['host'] == '127.0.0.1' and t['port'] == 55433, 'target_rejected')
        require(all(re.fullmatch('energymap_f0031_[a-z0-9_]+_test', n) for n in names), 'target_rejected')
        require(t['source_database'] == 'energymap_f0031_source_test', 'target_rejected')
    entry = values.get('source_entry', {'mode': 'acl'})
    require(type(entry) is dict and entry.get('mode') in ('acl', 'hba'), 'configuration')
    if entry['mode'] == 'acl':
        require(set(entry) == {'mode'}, 'configuration')
    else:
        require(set(entry) == {'mode', 'baseline_sha256', 'reader_password_file'}, 'configuration')
        require(isinstance(entry['baseline_sha256'], str) and re.fullmatch('[a-f0-9]{64}', entry['baseline_sha256']), 'configuration')
        private_file(entry['reader_password_file'])
        require(t['sslmode'] == 'verify-full', 'configuration')
    base = directory(t['private_dir'])
    allowed = ROOT / '.codex-orch/runtime' / ('f003-state-private' if t['mode'] == 'disposable' else 'f003-sandbox-state')
    require(base.is_relative_to(allowed), 'private_path')
    private_file(t['admin_password_file'])
    for k in ('note_file', 'runtime_file'):
        p = private_file(t[k], missing=True)
        if t['mode'] == 'disposable':
            require(p.is_relative_to(allowed), 'private_path')
    directory(Path(t['runtime_file']).parent)
    for k in ('backup_dir', 'restore_from'):
        if k in values:
            directory(values[k])
            require(Path(values[k]).is_relative_to(allowed), 'private_path')
    client_dir = safe_path(t['client_bin_dir'])
    require(client_dir.is_dir(), 'configuration')
    client_info=client_dir.stat()
    require(client_info.st_uid in (0,os.getuid()) and not client_info.st_mode & 0o022,'private_path')
    # Validate full transport before network/mutation, including ambient libpq.
    t.settings(t['admin_user'], admin_password(t), t['maintenance_database'])
    return t
