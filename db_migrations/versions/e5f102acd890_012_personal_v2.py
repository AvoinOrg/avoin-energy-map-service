"""Allow closed personal document formats 1 and 2; state tables only."""
from alembic import op
import sqlalchemy as sa

revision = 'e5f102acd890'
down_revision = 'd4e8f021bc63'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('user_buildings_version', 'energymap_user_buildings', schema='energymap_state', type_='check')
    op.create_check_constraint('user_buildings_version', 'energymap_user_buildings',
        'schema_version = 1 OR schema_version = 2', schema='energymap_state')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM energymap_state.energymap_user_buildings WHERE schema_version <> 1)')).scalar_one():
        raise RuntimeError('Personal v2 data present; downgrade refused')
    op.drop_constraint('user_buildings_version', 'energymap_user_buildings', schema='energymap_state', type_='check')
    op.create_check_constraint('user_buildings_version', 'energymap_user_buildings',
        'schema_version = 1', schema='energymap_state')
