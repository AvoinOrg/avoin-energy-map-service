"""Capability transactions. No authentication, source lookup, cache or commit here."""
import hashlib
import re
import secrets

from sqlalchemy import delete, select

from src import user_buildings as owner
from src.db.user_building_models import UserBuilding, UserBuildingShare

TOKEN_PATTERN = re.compile(r'[A-Za-z0-9_-]{43}', re.ASCII)
RESOLVER_PREFIX = '/energy-data/shared-buildings/'


class ShareNotFound(Exception):
    pass


def digest(token):
    return hashlib.sha256(token.encode('ascii')).digest()


def rotate(session, row):
    # Deleting and inserting needs no runtime UPDATE privilege on capabilities.
    # A collision/failure rolls back the caller's entire transaction, including
    # this delete and revision bump; no raw token has left the handler yet.
    token = secrets.token_urlsafe(32)
    session.execute(delete(UserBuildingShare).where(UserBuildingShare.saved_id == row.saved_id))
    session.add(UserBuildingShare(saved_id=row.saved_id, token_digest=digest(token)))
    owner.bump_revision(row)
    session.flush()
    return token


def revoke(session, row):
    session.execute(delete(UserBuildingShare).where(UserBuildingShare.saved_id == row.saved_id))
    owner.bump_revision(row)
    session.flush()


def lookup(session, token):
    if not TOKEN_PATTERN.fullmatch(token):
        raise ShareNotFound
    row = session.execute(select(UserBuilding).join(UserBuildingShare,
        UserBuildingShare.saved_id == UserBuilding.saved_id).where(
        UserBuildingShare.token_digest == digest(token)).execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None:
        raise ShareNotFound
    return row
