"""Server-local, compare-and-swap HBA prefix installer; no service lifecycle.

Transfer this reviewed standalone file and its protected request over the existing
admin channel. Run as the HBA file owner. SQL parser/reload/probes stay in the
pinned database client. Only the two new accounts can match the prefix.
"""
import argparse
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import UUID


def digest(value):
    return hashlib.sha256(value).hexdigest()


def policy(database, migrator, writer, provisioning_id):
    for name in (database, migrator, writer):
        if not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9_]{0,62}', name):
            raise ValueError('entry_policy')
    if str(UUID(provisioning_id)) != provisioning_id:
        raise ValueError('entry_policy')
    users = '"' + migrator + '","' + writer + '"'
    return (f'# Energy state entry {provisioning_id}\n'
        f'local all {users} reject\n'
        f'local replication {users} reject\n'
        f'hostssl "{database}" {users} all scram-sha-256\n'
        f'host all {users} all reject\n'
        f'host replication {users} all reject\n'
        f'# End Energy state entry {provisioning_id}\n').encode('ascii')


def protected(path, missing=False):
    p = Path(path)
    if not p.is_absolute() or '..' in p.parts or any(x.is_symlink() for x in (p, *p.parents)):
        raise ValueError('entry_path')
    if missing and not p.exists():
        info = p.parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('entry_path')
        return p
    s = p.stat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1:
        raise ValueError('entry_path')
    return p


def active_file(path):
    # Active PostgreSQL configuration may be readable by service tooling. This
    # exception never applies to requests, locks, backups or credentials.
    p = Path(path)
    if not p.is_absolute() or '..' in p.parts or any(x.is_symlink() for x in (p,*p.parents)):
        raise ValueError('entry_path')
    info = p.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) not in (0o600,0o640,0o644)):
        raise ValueError('entry_path')
    parent = p.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise ValueError('entry_path')
    return p


def replace(path, content, previous):
    info = active_file(path).stat()
    fd, temporary = tempfile.mkstemp(prefix='.energy-hba-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        current = active_file(path).stat()
        if ((current.st_dev,current.st_ino,current.st_uid,current.st_gid,current.st_mode) !=
                (info.st_dev,info.st_ino,info.st_uid,info.st_gid,info.st_mode) or path.read_bytes() != previous):
            raise ValueError('entry_drift')
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate(request):
    if set(request) != {'mode','database','migrator','writer','provisioning_id','hba_file','baseline_sha256'}:
        raise ValueError('entry_request')
    names = tuple(request[k] for k in ('database','migrator','writer'))
    live = ('energymap_personal_state','energymap_state_migrator','energymap_state_writer')
    if request['mode'] == 'sandbox':
        if names != live:
            raise ValueError('entry_target')
    elif request['mode'] != 'disposable' or not all(re.fullmatch('energymap_f0031_[a-z0-9_]+_test', n) for n in names):
        raise ValueError('entry_target')
    return policy(*names, request['provisioning_id'])


def _change(request, *, rollback=False):
    prefix = validate(request)
    path = active_file(request['hba_file'])
    backup = protected(str(path) + '.energy-' + request['provisioning_id'] + '.backup', missing=True)
    current = path.read_bytes()
    if backup.exists():
        original = protected(backup).read_bytes()
    else:
        if rollback or digest(current) != request['baseline_sha256']:
            raise ValueError('entry_drift')
        original = current
        # Exclusive durable backup before replacing any server file.
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(backup.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if digest(original) != request['baseline_sha256'] or current not in (original, prefix + original):
        raise ValueError('entry_drift')
    desired = original if rollback else prefix + original
    if current != desired:
        replace(path, desired, current)
    if path.read_bytes() != desired:
        raise ValueError('entry_readback')


def change(request, *, rollback=False):
    # One stable inode serializes cooperating policies, including distinct
    # recovery account pairs. Uncoordinated external editors remain unsupported.
    validate(request)
    path = active_file(request['hba_file'])
    lock = protected(str(path) + '.energy.lock', missing=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        protected(lock)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _change(request, rollback=rollback)
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Confined new-account HBA prefix; never reloads/restarts')
    parser.add_argument('operation', choices=('install','rollback'))
    parser.add_argument('--request-file', required=True)
    args = parser.parse_args(argv)
    try:
        request = json.loads(protected(args.request_file).read_text())
        change(request, rollback=args.operation == 'rollback')
    except Exception:
        print('{"status":"failed","category":"entry_file_failed"}')
        return 1
    print('{"status":"entry_file_verified"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
