"""Durable state; deliberately independent of every refreshable source table."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, LargeBinary, Text, TIMESTAMP, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, column_property

from .models import Base


class UserBuilding(Base):
    __tablename__ = 'energymap_user_buildings'
    __table_args__ = (
        CheckConstraint('schema_version = 1', name='user_buildings_version'),
        CheckConstraint('revision > 0', name='user_buildings_revision'),
        CheckConstraint("octet_length(building_key) BETWEEN 1 AND 256", name='user_buildings_key'),
        CheckConstraint("length(owner_issuer) > 0 AND length(owner_subject) > 0", name='user_buildings_owner'),
        CheckConstraint("display_name IS NULL OR length(display_name) BETWEEN 1 AND 120", name='user_buildings_name'),
        CheckConstraint("jsonb_typeof(overrides) = 'object' AND jsonb_typeof(scenario_inputs) = 'object' AND jsonb_typeof(last_known_summary) = 'object'", name='user_buildings_objects'),
        {'schema': 'energymap_state'},
    )
    saved_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_issuer: Mapped[str] = mapped_column(Text)
    owner_subject: Mapped[str] = mapped_column(Text)
    building_key: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[int] = mapped_column(BigInteger)
    overrides: Mapped[dict] = mapped_column(JSONB)
    scenario_inputs: Mapped[dict] = mapped_column(JSONB)
    last_known_summary: Mapped[dict] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    # Database identity remains the UUID PK. Include trusted ownership in ORM
    # UPDATE/DELETE predicates too, after the owner-scoped row lock.
    __mapper_args__ = {'primary_key': (saved_id, owner_issuer, owner_subject, building_key)}


# Provider subjects may exceed PostgreSQL's B-tree tuple limit. Hashes bound
# indexes; every lookup ALSO compares exact identity. A unique-conflict handler
# verifies exact identity before translating it (a collision fails closed).
Index('uq_user_buildings_owner_key', func.md5(UserBuilding.owner_issuer),
      func.md5(UserBuilding.owner_subject), UserBuilding.building_key, unique=True)
Index('ix_user_buildings_owner_updated', func.md5(UserBuilding.owner_issuer),
      func.md5(UserBuilding.owner_subject), UserBuilding.updated_at.desc(), UserBuilding.saved_id.desc())


class UserBuildingShare(Base):
    __tablename__ = 'energymap_user_building_shares'
    __table_args__ = (
        UniqueConstraint('token_digest', name='uq_building_shares_digest'),
        CheckConstraint('octet_length(token_digest) = 32', name='building_shares_digest_length'),
        {'schema': 'energymap_state'},
    )
    saved_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey(
        'energymap_state.energymap_user_buildings.saved_id', name='fk_building_shares_saved', ondelete='CASCADE'), primary_key=True)
    token_digest: Mapped[bytes] = mapped_column(LargeBinary)


# Included in the same statement as the saved revision; never loads a raw digest.
UserBuilding.share_enabled = column_property(select(UserBuildingShare.saved_id).where(
    UserBuildingShare.saved_id == UserBuilding.saved_id).correlate_except(UserBuildingShare).exists())
