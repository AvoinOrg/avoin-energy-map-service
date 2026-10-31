"""Allow personal formats 1/2/3, changing only the private-state constraint."""
from alembic import op
import sqlalchemy as sa

revision = 'f6a213bde901'
down_revision = 'e5f102acd890'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('user_buildings_version', 'energymap_user_buildings', schema='energymap_state', type_='check')
    op.create_check_constraint('user_buildings_version', 'energymap_user_buildings',
        'schema_version = 1 OR schema_version = 2 OR schema_version = 3', schema='energymap_state')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM energymap_state.energymap_user_buildings WHERE schema_version NOT IN (1,2))')).scalar_one():
        raise RuntimeError('Personal v3 data present; downgrade refused')
    op.drop_constraint('user_buildings_version', 'energymap_user_buildings', schema='energymap_state', type_='check')
    op.create_check_constraint('user_buildings_version', 'energymap_user_buildings',
        'schema_version = 1 OR schema_version = 2', schema='energymap_state')
