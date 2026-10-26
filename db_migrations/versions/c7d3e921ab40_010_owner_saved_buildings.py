"""Durable owner saved buildings only.

Revision ID: c7d3e921ab40
Revises: 9b812a37ef01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c7d3e921ab40'
down_revision = '9b812a37ef01'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE SCHEMA energymap_state')
    op.execute('REVOKE ALL ON SCHEMA energymap_state FROM PUBLIC')
    op.create_table('energymap_user_buildings',
        sa.Column('saved_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('owner_issuer', sa.Text(), nullable=False),
        sa.Column('owner_subject', sa.Text(), nullable=False),
        sa.Column('building_key', sa.Text(), nullable=False),
        sa.Column('display_name', sa.Text()),
        sa.Column('schema_version', sa.BigInteger(), nullable=False),
        sa.Column('overrides', postgresql.JSONB(), nullable=False),
        sa.Column('scenario_inputs', postgresql.JSONB(), nullable=False),
        sa.Column('last_known_summary', postgresql.JSONB(), nullable=False),
        sa.Column('revision', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint('schema_version = 1', name='user_buildings_version'),
        sa.CheckConstraint('revision > 0', name='user_buildings_revision'),
        sa.CheckConstraint('octet_length(building_key) BETWEEN 1 AND 256', name='user_buildings_key'),
        sa.CheckConstraint('length(owner_issuer) > 0 AND length(owner_subject) > 0', name='user_buildings_owner'),
        sa.CheckConstraint('display_name IS NULL OR length(display_name) BETWEEN 1 AND 120', name='user_buildings_name'),
        sa.CheckConstraint("jsonb_typeof(overrides) = 'object' AND jsonb_typeof(scenario_inputs) = 'object' AND jsonb_typeof(last_known_summary) = 'object'", name='user_buildings_objects'),
        schema='energymap_state')
    op.execute('CREATE UNIQUE INDEX uq_user_buildings_owner_key ON energymap_state.energymap_user_buildings (md5(owner_issuer), md5(owner_subject), building_key)')
    op.execute('CREATE INDEX ix_user_buildings_owner_updated ON energymap_state.energymap_user_buildings (md5(owner_issuer), md5(owner_subject), updated_at DESC, saved_id DESC)')


def downgrade():
    # DESTRUCTIVE: backup all personal rows first. No source table is touched.
    op.drop_table('energymap_user_buildings', schema='energymap_state')
    # No CASCADE: a later sharing migration must be downgraded first.
    op.execute('DROP SCHEMA energymap_state')
