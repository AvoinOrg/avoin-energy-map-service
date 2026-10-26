"""Small protected-file primitives. All error messages are fixed categories."""
import json
import os
from pathlib import Path
import stat
import tempfile


class StorageError(Exception):
    def __init__(self, category='operation_failed'):
        self.category = category
        super().__init__(category)


def require(condition, category='incompatible'):
    if not condition:
        raise StorageError(category)


def safe_path(value):
    require(isinstance(value, str), 'private_path')
    path = Path(value)
    require(path.is_absolute() and '..' not in path.parts, 'private_path')
    for part in (path, *path.parents):
        require(not part.is_symlink(), 'private_path')
    return path


def directory(value):
    path = safe_path(str(value))
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and
            stat.S_IMODE(info.st_mode) == 0o700, 'private_path')
    return path


def private_file(value, *, missing=False):
    path = safe_path(str(value))
    if missing and not path.exists():
        info = path.parent.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and
                not info.st_mode & 0o022, 'private_path')
        return path
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
            stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, 'private_path')
    return path


def read(value):
    path = private_file(value)
    with open(path, encoding='utf8') as handle:
        return handle.read()


def read_json(value):
    return json.loads(read(value))


def atomic_write(value, content):
    path = private_file(value, missing=True)
    fd, temporary = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Recheck before replacement: never replace a symlink or public file.
        private_file(path, missing=True)
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(value, content):
    atomic_write(value, json.dumps(content, sort_keys=True, indent=2) + '\n')
