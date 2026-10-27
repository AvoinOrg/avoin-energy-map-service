"""Explicit, synthetic-only sandbox persistence probe; never a fixture suite.

Run create, read and cleanup in separate interpreters with a protected sandbox
descriptor. The private probe file is the sole cleanup authority. It must remain
available until backup/recovery verification has finished. No owner enumeration,
source connection, destructive DDL or private value output is permitted here.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
from uuid import UUID, uuid4

from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from src import building_shares, user_buildings
from src.building_contract import LastKnownBuildingSummaryV1
from src.db.state import check_state
from src.db.user_building_models import UserBuilding, UserBuildingShare
from src.state_storage.config import load_target
from src.state_storage.operations import load_bundle
from src.state_storage.private import private_file, read_json, require

ISSUER = 'https://energymap.invalid/f0032-synthetic'


def validate_probe(probe):
    require(set(probe) == {'issuer', 'subject', 'key', 'saved_id', 'token', 'tag'}, 'probe_scope')
    saved_id = str(UUID(probe['saved_id']))
    require(probe['saved_id'] == saved_id and probe['issuer'] == ISSUER
            and probe['subject'] == 'f0032:' + saved_id
            and probe['key'] == 'f0032-probe:' + saved_id
            and building_shares.TOKEN_PATTERN.fullmatch(probe['token'])
            and probe['tag'] == '"ub1:' + saved_id + ':1"', 'probe_scope')
    return probe


def scope(probe):
    validate_probe(probe)
    return (UserBuilding.saved_id == UUID(probe['saved_id']),
            UserBuilding.owner_issuer == ISSUER,
            UserBuilding.owner_subject == probe['subject'],
            UserBuilding.building_key == probe['key'])


def reserve_probe(path, probe):
    """Only one creator may retain the durable cleanup authority."""
    path = private_file(path, missing=True)
    content = json.dumps(validate_probe(probe), sort_keys=True) + '\n'
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def run(operation, target, probe_file):
    require(target['mode'] == 'sandbox', 'probe_target')
    path = private_file(probe_file, missing=operation == 'create')
    require(path == Path(target['private_dir']) / 'synthetic-probe.json', 'probe_scope')
    if operation == 'create':
        require(not path.exists(), 'probe_collision')
        saved_id = str(uuid4())
        probe = dict(issuer=ISSUER, subject='f0032:' + saved_id,
                     key='f0032-probe:' + saved_id, saved_id=saved_id,
                     token=secrets.token_urlsafe(32), tag='"ub1:' + saved_id + ':1"')
        # Persist exact cleanup identity before touching the database.
        reserve_probe(path, probe)
    else:
        probe = validate_probe(read_json(path))
    bundle = load_bundle(target)
    engine = create_engine('postgresql+psycopg2://', hide_parameters=True,
        connect_args=target.settings(target['writer'], bundle['writer_password'], target['database']).kwargs())
    try:
        with Session(engine) as session:
            check_state(session)
            if operation == 'create':
                now = datetime.now(timezone.utc)
                session.add(UserBuilding(saved_id=UUID(probe['saved_id']), owner_issuer=ISSUER,
                    owner_subject=probe['subject'], building_key=probe['key'], display_name='Synthetic probe',
                    schema_version=1, overrides={'construction_year': 2001}, scenario_inputs={},
                    last_known_summary=LastKnownBuildingSummaryV1(building_key=probe['key'],
                        source_availability={}, captured_at=now).model_dump(mode='json'),
                    revision=1, created_at=now, updated_at=now))
                session.flush()
                session.add(UserBuildingShare(saved_id=UUID(probe['saved_id']),
                    token_digest=building_shares.digest(probe['token'])))
                session.commit()
            elif operation == 'read':
                row = session.scalar(select(UserBuilding).where(*scope(probe)))
                require(row is not None and row.revision == 1 and row.display_name == 'Synthetic probe'
                        and user_buildings.etag(row) == probe['tag']
                        and row.overrides == {'construction_year': 2001}
                        and row.scenario_inputs == {}
                        and user_buildings.stored_summary(row).building_key
                        == probe['key'], 'probe_data')
                share = session.scalar(select(UserBuildingShare.saved_id).join(UserBuilding,
                    UserBuildingShare.saved_id == UserBuilding.saved_id).where(*scope(probe),
                    UserBuildingShare.token_digest == building_shares.digest(probe['token'])))
                require(share == row.saved_id, 'probe_share')
                # Allowed UPDATE is rolled back; forbidden UPDATE targets only
                # this exact synthetic UUID/owner/key and must be a rights error.
                with session.begin_nested() as savepoint:
                    row.display_name = 'Synthetic allowed update'
                    session.flush()
                    savepoint.rollback()
                try:
                    with session.begin_nested():
                        session.execute(text('UPDATE energymap_state.energymap_user_buildings '
                            'SET owner_subject=owner_subject WHERE saved_id=:id AND owner_issuer=:issuer '
                            'AND owner_subject=:subject AND building_key=:key'),
                            dict(id=probe['saved_id'], issuer=ISSUER, subject=probe['subject'], key=probe['key']))
                except DBAPIError as error:
                    require(getattr(error.orig, 'pgcode', None) == '42501', 'probe_privileges')
                else:
                    raise ValueError('probe_privileges')
                session.rollback()
            elif operation == 'cleanup':
                session.execute(delete(UserBuilding).where(*scope(probe)))
                session.commit()
                require(session.scalar(select(UserBuilding.saved_id).where(*scope(probe))) is None, 'probe_cleanup')
                require(session.scalar(select(UserBuildingShare.saved_id).where(
                    UserBuildingShare.saved_id == UUID(probe['saved_id']))) is None, 'probe_cleanup')
            else:
                raise ValueError('probe_operation')
    finally:
        engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('create', 'read', 'cleanup'))
    parser.add_argument('--config-file', required=True)
    parser.add_argument('--probe-file', required=True)
    parser.add_argument('--allow-sandbox-synthetic', action='store_true', required=True)
    args = parser.parse_args(argv)
    try:
        run(args.operation, load_target(args.config_file), args.probe_file)
    except Exception:
        print('synthetic_probe=false')
        return 1
    print('synthetic_probe=true')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
