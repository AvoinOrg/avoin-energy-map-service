"""Bounded sandbox startup adapter, stdlib only; never starts/reloads PostgreSQL.

Install frozen copies of this file and hba_file.py in the EXISTING persistent
/scripts/custom/energy-state bind. The doctor installs the reviewed modifier
suffix with an exclusive backup, current-image/metadata guards and readback.
The modifier appends an explicit checked guard AFTER completed HBA generation.
No application imports, credentials, scheduler or generic startup framework.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

if __package__:
    from . import hba_file
else:
    hba_file = None  # Loaded from the pinned protected file in __main__.

IMAGE = 'sha256:0fea25c4d3ffb65a8cd7148af702544d835baa3c208a7fe215f8c6f65f48a9f9'
DIRECTORY = Path('/scripts/custom/energy-state')
REQUEST = DIRECTORY / 'request.json'
SETUP = Path('/scripts/setup-pg_hba.sh')
MODIFIER = Path('/scripts/custom/modify-setup-pga_hba.sh')
HBA = '/etc/postgresql/17/main/pg_hba.conf'
CONFIGURATION = '/scripts/setup-conf.sh'
CONFIG_INSERTION = b'max_connections = 1000\n'
IDENTITY = (101, 103)
CALLER_IDENTITY = (0, 0)
MODIFIER_HASH = 'a8507057b062f7748aa1ea194b838189d1d6cf47bf8243e638ca496b9cbcfec6'
SETUP_HASH = 'dd0780176abcfd2f0e3fc2bf33282da43795789fc0b7d914f668dc31841944d4'
PRISTINE_SETUP_HASH = '52e14f403ff8cbcb700d5429e1f8f2f38101aa17cd7178eca000ceb58906d6b3'
HBA_HELPER_HASH = 'bf5a43cdf0e74eed333ff5939448c439be0ee2e813a1b17cce73354b59db04c9'

# exit is deliberate: these files are SOURCED, and a nonzero return/subprocess
# inside a function/conditional does not necessarily stop the enclosing shell.
NORMALIZE_CALL = b'''
# Energy state: canonicalize ONLY the pinned prior additions injection
if ! /usr/bin/python3 -I /scripts/custom/energy-state/sandbox_startup.py normalize; then
    exit 1
fi
'''
PREPARE_CALL = b'''
# Energy state: checked completed-HBA adapter preparation
if ! /usr/bin/python3 -I /scripts/custom/energy-state/sandbox_startup.py prepare; then
    exit 1
fi
'''
GUARD_CALL = b'''
# Energy state: completed generation OR initialized return, before ANY listener
if ! /usr/bin/python3 -I /scripts/custom/energy-state/sandbox_startup.py guard; then
    exit 1
fi
'''

CHAIN = {
    '/scripts/custom/docker-entrypoint.sh': 'd05ec5f29393a2e3fac2e4bd92c3f48a12fdcbbbb85e9e1d329174e1cda99c46',
    '/scripts/custom/modify-setup-conf.sh': '371167ac98962506cbde473bbd3fd38dfdb99c74d2ff7f3d53195cb437875852',
    '/scripts/custom/pg_hba-additions.sh': 'ed4604d160e5007adcebe2a234077f001b3fd91421fc9dbef9971ec0c9e333ab',
    '/scripts/docker-entrypoint.sh': 'ed9502a3bd4261c6ffd4b24c528c524697e786509249f911b77128475587fd02',
    '/scripts/env-data.sh': '3a2bb830a3872a2ac261f7f42aaac09c1c209dd52b43c74ba19e308cfa168871',
    '/scripts/setup-database.sh': 'bacbf2b5f5eb4af54378047187ce9b2be374fb6dd41ff0f7357b8600c2c0fbcf',
    '/scripts/setup-conf.sh': 'd8142f48fb4a5ecb3196fa33079f71b667b7e966766af21b11fe4102e6d66c13',
    '/scripts/setup-user.sh': '13c846c49dd6c8a83001d91ee7aec125606bddf03aa18baffe677a80ab9b5df0',
    '/scripts/setup-ssl.sh': '226c37771a93f6a268ce8141c0ca143798d57052aaad76608058b89c3ce0e0fd',
    '/scripts/setup-replication.sh': '0f9811f7e8888889bebf6ae5f3e9382924a3776fa154d13de1ef8ec14a51274c',
}
NATIVE_SCRIPTS = frozenset({str(SETUP)} | {
    path for path in CHAIN if Path(path).parent == Path('/scripts')
})

# -I intentionally ignores PYTHONPATH, cwd and user site. The standalone loader
# below explicitly loads only the pinned adjacent file, after integrity checks.


def require(condition):
    if not condition:
        raise ValueError('startup_refused')


def script(path):
    p = Path(path)
    require(p.is_absolute() and '..' not in p.parts)
    require(not any(x.is_symlink() for x in (p, *p.parents)))
    info = p.stat()
    native = str(p) in NATIVE_SCRIPTS
    owners = (IDENTITY, CALLER_IDENTITY) if native else (IDENTITY,)
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            (info.st_uid, info.st_gid) in owners and stat.S_IMODE(info.st_mode) == 0o755)
    parent = p.parent.stat()
    if native:
        require(stat.S_ISDIR(parent.st_mode) and
                (parent.st_uid, parent.st_gid) in owners and
                stat.S_IMODE(parent.st_mode) == 0o755)
    else:
        require(parent.st_uid in (0, IDENTITY[0]) and not parent.st_mode & 0o022)
    return p


def modifier_bytes(original):
    require(hba_file.digest(original) == MODIFIER_HASH)
    first, rest = original.split(b'\n', 1)
    require(first.startswith(b'#!'))
    return first + b'\n' + NORMALIZE_CALL + rest + PREPARE_CALL


def original_modifier(current, backup):
    """Doctor's exact reverse rollback, AFTER marker-checked role quarantine."""
    require(current == modifier_bytes(backup))
    return backup


def private_control(path, *, missing=False):
    """Root may read ONLY these controls with the exact service owner/mode.

    This does not relax hba_file.protected(): HBA mutation uses that unchanged
    helper only AFTER permanent descent to the HBA owner.
    """
    p = Path(path)
    require(p.parent == DIRECTORY and not any(x.is_symlink() for x in (p, *p.parents)))
    if missing and not p.exists():
        return p
    info = p.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            (info.st_uid, info.st_gid) == IDENTITY and stat.S_IMODE(info.st_mode) == 0o600)
    return p


def load_request():
    require((os.getuid(), os.getgid()) == CALLER_IDENTITY and
            (os.geteuid(), os.getegid()) == CALLER_IDENTITY)
    info = DIRECTORY.stat()
    require(not DIRECTORY.is_symlink() and stat.S_ISDIR(info.st_mode) and
            (info.st_uid, info.st_gid) == IDENTITY and stat.S_IMODE(info.st_mode) == 0o700)
    request = json.loads(private_control(REQUEST).read_text())
    # Reuse EXACT active helper validation/policy, with no disposable-mode CLI.
    hba_file.validate(request)
    require(request['mode'] == 'sandbox' and request['hba_file'] == HBA)
    require(isinstance(request['baseline_sha256'], str) and
            len(request['baseline_sha256']) == 64 and
            all(c in '0123456789abcdef' for c in request['baseline_sha256']))
    return request


def canonical_configuration(current):
    """Compare old modifier replays without changing their existing behavior.

    Only one contiguous run of its exact insertion is reduced, in memory. The
    caller then hashes the ENTIRE result against the frozen one-insertion file,
    binding the insertion's location, surrounding bytes and every other setting.
    """
    lines = current.splitlines(keepends=True)
    positions = [i for i, line in enumerate(lines) if line == CONFIG_INSERTION]
    require(positions and positions == list(range(positions[0], positions[-1] + 1)))
    return b''.join(lines[:positions[0] + 1] + lines[positions[-1] + 1:])


def check_chain():
    for path, expected in CHAIN.items():
        current = script(path).read_bytes()
        if path == CONFIGURATION:
            current = canonical_configuration(current)
        require(hba_file.digest(current) == expected)
    modifier = script(MODIFIER).read_bytes()
    require(modifier.endswith(PREPARE_CALL))
    first, rest = modifier[:-len(PREPARE_CALL)].split(b'\n', 1)
    require(rest.startswith(NORMALIZE_CALL))
    original = first + b'\n' + rest[len(NORMALIZE_CALL):]
    require(hba_file.digest(original) == MODIFIER_HASH)


def prepare(*, normalize=False):
    load_request()
    check_chain()
    backup = private_control(DIRECTORY / 'setup-original.backup', missing=True)
    if backup.exists():
        require(hba_file.digest(backup.read_bytes()) == SETUP_HASH)
    # Serialize cooperating startup invocations, including durable backup intent.
    lock = private_control(DIRECTORY / 'startup.lock', missing=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    else:
        os.fchown(fd, *IDENTITY)
        os.fchmod(fd, 0o600)
    try:
        private_control(lock)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        normalize_setup() if normalize else prepare_setup()
    finally:
        os.close(fd)


def prepare_setup():
    # The existing modifier must have finished its own additions injection first.
    path = script(SETUP)
    current = path.read_bytes()
    original = original_setup(current)
    save_setup_backup(original)
    expected = managed_setup(original)
    if current != expected:
        replace_script(path, expected, current)
    require(path.read_bytes() == expected)


def save_setup_backup(original):
    backup = private_control(DIRECTORY / 'setup-original.backup', missing=True)
    if not backup.exists():
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            os.fchown(stream.fileno(), *IDENTITY)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        parent = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    require(private_control(backup).read_bytes() == original)


def normalize_setup():
    """Avoid the real old modifier's repeated sed injection on one overlay.

    Only three frozen states are accepted. Remove ONLY the known single prior
    injection/own guard; the unchanged sourced old body then reinserts it once.
    Unknown/duplicate edits are never normalized or adopted.
    """
    path = script(SETUP)
    current = path.read_bytes()
    if hba_file.digest(current) == PRISTINE_SETUP_HASH:
        return
    canonical = original_setup(current)
    lines = canonical.splitlines(keepends=True)
    additions = [i for i, line in enumerate(lines) if b'/scripts/custom/pg_hba-additions.sh' in line]
    require(len(additions) == 1)
    pristine = b''.join(line for i, line in enumerate(lines) if i != additions[0])
    require(hba_file.digest(pristine) == PRISTINE_SETUP_HASH)
    save_setup_backup(canonical)
    replace_script(path, pristine, current)
    require(path.read_bytes() == pristine)


def original_setup(current):
    """Exact ephemeral-file rollback bytes, only after account quarantine."""
    if hba_file.digest(current) == SETUP_HASH:
        return current
    require(current.endswith(GUARD_CALL))
    guarded = current[:-len(GUARD_CALL)]
    require(guarded.count(GUARD_CALL) == 1)
    original = guarded.replace(GUARD_CALL, b'', 1)
    require(hba_file.digest(original) == SETUP_HASH)
    require(current == managed_setup(original))
    return original


def managed_setup(original):
    """Guard both frozen paths: initialized early return and final generation."""
    require(hba_file.digest(original) == SETUP_HASH)
    lines = original.splitlines(keepends=True)
    returns = [i for i, line in enumerate(lines) if line.strip() == b'return 0']
    # The pinned native setup has one initialized-path return, at line 7.
    # The whole-file pin fixes its branch/context; no blanket return rewriting.
    require(len(returns) == 1)
    lines.insert(returns[0], GUARD_CALL)
    return b''.join(lines) + GUARD_CALL


def replace_script(path, desired, previous):
    """Atomic byte/metadata-preserving CAS for the single ephemeral setup file."""
    import tempfile
    info = script(path).stat()
    fd, temporary = tempfile.mkstemp(prefix='.energy-setup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
        now = script(path).stat()
        require((now.st_dev, now.st_ino, now.st_uid, now.st_gid, now.st_mode) ==
                (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode))
        require(path.read_bytes() == previous)
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def guard():
    request = load_request()
    check_chain()
    setup = script(SETUP).read_bytes()
    require(setup == managed_setup(original_setup(setup)))
    prepare_hba_owner(request)
    drop_to_hba_owner()
    hba_file.change(request)
    # Exact byte readback + prior reviewed baseline/parser proof: no listener,
    # reload or credentials are used to establish the prefix at startup.
    prefix = hba_file.validate(request)
    current = hba_file.active_file(HBA).read_bytes()
    require(current.startswith(prefix) and
            hba_file.digest(current[len(prefix):]) == request['baseline_sha256'])


def prepare_hba_owner(request):
    """Move only the native root-generated HBA ownership repair before guard.

    Existing service-owned files are left untouched. On initialization the
    native HBA writer leaves root:root/0644 in the service-owned directory;
    database setup normally repairs that ownership later. No listener may run
    before our owner-only helper, so perform that exact transition here instead.
    """
    path = Path(HBA)
    require(not any(p.is_symlink() for p in (path, *path.parents)))
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            stat.S_IMODE(info.st_mode) == 0o644)
    if (info.st_uid, info.st_gid) == IDENTITY:
        return
    require((os.getuid(), os.getgid()) == CALLER_IDENTITY == (0, 0) and
            (info.st_uid, info.st_gid) == (0, 0))
    parent = path.parent.stat()
    require(stat.S_ISDIR(parent.st_mode) and
            (parent.st_uid, parent.st_gid) == IDENTITY and
            stat.S_IMODE(parent.st_mode) == 0o755)
    def identity(value):
        # Reading can update atime; all identity/content-change fields stay pinned.
        return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
                value.st_uid, value.st_gid, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        original = stream.read()
        require(hba_file.digest(original) == request['baseline_sha256'])
        opened = os.fstat(stream.fileno())
        require(identity(opened) == identity(info) == identity(path.lstat()))
        os.fchown(stream.fileno(), *IDENTITY)
        os.fsync(stream.fileno())
        after = os.fstat(stream.fileno())
        require((after.st_uid, after.st_gid) == IDENTITY and
                (after.st_dev, after.st_ino, after.st_mode, after.st_nlink) ==
                (info.st_dev, info.st_ino, info.st_mode, info.st_nlink) and
                identity(path.lstat()) == identity(after))
        stream.seek(0)
        require(stream.read() == original)


def drop_to_hba_owner():
    # Only this short-lived child changes identity. The original enclosing
    # startup shell and every existing startup/client path keep their behavior.
    require((os.getuid(), os.getgid()) == CALLER_IDENTITY)
    os.setgroups([])
    os.setresgid(IDENTITY[1], IDENTITY[1], IDENTITY[1])
    os.setresuid(IDENTITY[0], IDENTITY[0], IDENTITY[0])
    require(os.getresuid() == (IDENTITY[0],) * 3 and os.getresgid() == (IDENTITY[1],) * 3
            and os.getgroups() == [])


def main(argv=None):
    parser = argparse.ArgumentParser(description='Fixed sandbox completed-HBA guard; no lifecycle')
    parser.add_argument('operation', choices=('normalize', 'prepare', 'guard'))
    args = parser.parse_args(argv)
    try:
        if args.operation == 'guard':
            guard()
        else:
            prepare(normalize=args.operation == 'normalize')
    except Exception:
        print('{"status":"failed","category":"startup_refused"}')
        return 1
    print('{"status":"startup_verified"}')
    return 0


if __name__ == '__main__':
    # Python -I omits the script directory. Load one fixed protected adjacent
    # helper without changing sys.path; no ambient module/plugin can interpose.
    from types import ModuleType
    try:
        require((os.getuid(), os.getgid()) == CALLER_IDENTITY and
                (os.geteuid(), os.getegid()) == CALLER_IDENTITY)
        path = DIRECTORY / 'hba_file.py'
        require(not any(p.is_symlink() for p in (path, *path.parents)))
        parent = DIRECTORY.stat()
        require(stat.S_ISDIR(parent.st_mode) and (parent.st_uid, parent.st_gid) == IDENTITY and
                stat.S_IMODE(parent.st_mode) == 0o700)
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                (info.st_uid, info.st_gid) == IDENTITY and stat.S_IMODE(info.st_mode) == 0o600)
        source = path.read_bytes()
        require(hashlib.sha256(source).hexdigest() == HBA_HELPER_HASH)
        # Execute exactly the bytes just verified, never a cached .pyc or a
        # second file read after the hash check.
        hba_file = ModuleType('energy_frozen_hba')
        exec(compile(source, str(path), 'exec'), hba_file.__dict__)
    except Exception:
        print('{"status":"failed","category":"startup_refused"}')
        raise SystemExit(1)
    raise SystemExit(main())
