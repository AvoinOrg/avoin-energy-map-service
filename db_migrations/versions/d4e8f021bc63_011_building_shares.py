"""Revocable digest-only capabilities against durable saved records.

Revision ID: d4e8f021bc63
Revises: c7d3e921ab40
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd4e8f021bc63'
down_revision = 'c7d3e921ab40'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('energymap_user_building_shares',
        sa.Column('saved_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('token_digest', sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(['saved_id'], ['energymap_state.energymap_user_buildings.saved_id'],
                                name='fk_building_shares_saved', ondelete='CASCADE'),
        sa.UniqueConstraint('token_digest', name='uq_building_shares_digest'),
        sa.CheckConstraint('octet_length(token_digest) = 32', name='building_shares_digest_length'),
        schema='energymap_state')
    op.execute('REVOKE ALL ON energymap_state.energymap_user_building_shares FROM PUBLIC')


def downgrade():
    # DESTRUCTIVE: back up complete private state first. Owner/source rows survive.
    op.drop_table('energymap_user_building_shares', schema='energymap_state')
