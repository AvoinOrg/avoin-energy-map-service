"""Owned temporary state databases on the EXISTING guarded 55433 fixture service.

No containers/services, shared source data, historical fixtures or live controls
are created/reset. Reuse private bootstrap references; never copy their contents
into Git or output. Exact marker/endpoint checks precede every cleanup mutation.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from uuid import uuid4

from psycopg2 import sql
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from src.db.state import _engine
from src.state_storage.catalog import rows, scalar
from src.state_storage.config import ROOT, load_target
from src.state_storage.operations import connected, load_bundle
from src.state_storage.private import directory, require, write_json

BOOTSTRAP = ROOT / '.codex-orch/runtime/f003-state-private/target.json'


def fixture_template():
    # Fixed private reference, no ambient URL or caller-chosen server.
    template = load_target(BOOTSTRAP)
    require((template['mode'], template['host'], template['port'], template['source_database'],
        template['admin_user'], template['source_reader']) == ('disposable', '127.0.0.1', 55433,
        'energymap_f0031_source_test', 'energymap_f0031_admin_test', 'energymap_f0031_reader_test'), 'fixture_identity')
    require(template.values.get('source_entry', {'mode': 'acl'}) == {'mode': 'acl'}, 'fixture_identity')
    # connected pins real database/user/system_identifier/TLS; no guessing/adoption.
    with connected(template, read_only=True) as connection:
        require(scalar(connection, 'SHOW server_version_num').startswith('17'), 'fixture_identity')
    return template


def cleanup_target(target):
    folder = directory(target['private_dir'])
    require((target['mode'], target['host'], target['port'], target['source_database']) ==
        ('disposable', '127.0.0.1', 55433, 'energymap_f0031_source_test'), 'fixture_cleanup_identity')
    require(folder.parent.parent == BOOTSTRAP.parent and folder.parent.name.startswith('test-personal-') and
        folder.name in ('state', 'restore'), 'fixture_cleanup_identity')
    match = re.fullmatch(r'(energymap_f0031_p4_[a-f0-9]{12}_' + folder.name + r')_test', target['database'])
    require(match is not None and target['migrator'] == match[1] + '_m_test' and
        target['writer'] == match[1] + '_w_test', 'fixture_cleanup_identity')
    with connected(target) as connection:
        db = rows(connection, 'SELECT pg_get_userbyid(datdba),shobj_description(oid,\'pg_database\') FROM pg_database WHERE datname=%s', (target['database'],))
        roles = {name: rows(connection, "SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s", (name,))
                 for name in (target['migrator'], target['writer'])}
        require(not db or db == [(target['migrator'], target.marker)], 'fixture_cleanup_identity')
        require(all(not values or values == [(target.marker,)] for values in roles.values()), 'fixture_cleanup_identity')
        if db:
            # Never terminate/adopt unknown sessions or FORCE away uncertain work.
            # Client/pool closure can precede server-side disconnect visibility.
            # Wait briefly without terminating/adopting any remaining sessions.
            deadline = time.monotonic() + 3
            while scalar(connection, 'SELECT count(*) FROM pg_stat_activity WHERE datname=%s', (target['database'],)):
                require(time.monotonic() < deadline, 'fixture_cleanup_busy')
                time.sleep(0.02)
            rows(connection, sql.SQL('DROP DATABASE {}').format(sql.Identifier(target['database'])))
        for name, values in roles.items():
            if values:
                rows(connection, sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))


@contextmanager
def state_pair():
    """Register exact-resource finalization BEFORE provision (success or failure).

    Each pair is NEW and owned by this invocation. Unknown cleanup outcomes retain
    private descriptors/retry data and raise; no broad sweep or forced downgrade.
    """
    template = fixture_template()
    base = Path(tempfile.mkdtemp(prefix='test-personal-', dir=BOOTSTRAP.parent))
    os.chmod(base, 0o700)
    targets = []
    try:
        label = uuid4().hex[:12]
        for kind in ('state', 'restore'):
            folder = base / kind
            folder.mkdir(mode=0o700)
            backups = folder / 'backup'; backups.mkdir(mode=0o700)
            values = dict(template.values)
            for k in ('source_entry', 'restore_from'):
                values.pop(k, None)
            prefix = f'energymap_f0031_p4_{label}_{kind}'
            values.update(database=prefix + '_test', migrator=prefix + '_m_test', writer=prefix + '_w_test',
                provisioning_id=str(uuid4()), private_dir=str(folder), note_file=str(folder/'credentials.md'),
                runtime_file=str(folder/'runtime.env'), backup_dir=str(backups))
            if kind == 'restore': values['restore_from'] = targets[0]['backup_dir']
            path = folder / 'target.json'; write_json(path, values)
            target = load_target(path)
            with connected(target, read_only=True) as connection:
                require(not rows(connection, 'SELECT datname FROM pg_database WHERE datname=%s', (target['database'],)), 'fixture_collision')
                require(not rows(connection, 'SELECT rolname FROM pg_roles WHERE rolname IN (%s,%s)', (target['migrator'], target['writer'])), 'fixture_collision')
            targets.append(target)
        yield tuple(targets)
    finally:
        # Owned state_engine pools can outlive an environment override; close just
        # pools matching these invocation-owned database names before exact DDL.
        # Other application services/processes are never touched.
        for target in reversed(targets):
            if (Path(target['private_dir'])/'bundle.json').exists():
                bundle = load_bundle(target)
                settings = target.settings(target['writer'], bundle['writer_password'], target['database'])
                _engine(settings).dispose()
            cleanup_target(target)
        shutil.rmtree(base)


@contextmanager
def migrator_connection(target):
    bundle = load_bundle(target)
    settings = target.settings(target['migrator'], bundle['migrator_password'], target['database'])
    engine = create_engine('postgresql+psycopg2://', connect_args=settings.kwargs(), poolclass=NullPool, hide_parameters=True)
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()
