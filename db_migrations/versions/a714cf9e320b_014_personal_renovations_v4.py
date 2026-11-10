"""Admit closed personal format v4 without converting any saved document."""
from alembic import op
import sqlalchemy as sa

revision = 'a714cf9e320b'
down_revision = 'f6a213bde901'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('user_buildings_version', 'energymap_user_buildings', schema='energymap_state', type_='check')
    op.create_check_constraint('user_buildings_version', 'energymap_user_buildings',
        'schema_version = 1 OR schema_version = 2 OR schema_version = 3 OR schema_version = 4', schema='energymap_state')


def downgrade():
    # Serialize the refusal check with inserts/promotions, including empty v4.
    op.execute('LOCK TABLE energymap_state.energymap_user_buildings IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM energymap_state.energymap_user_buildings WHERE schema_version NOT IN (1,2,3))')).scalar_one():
        raise RuntimeError('Personal v4 data present; downgrade refused')
    op.drop_constraint('user_buildings_version', 'energymap_user_buildings', schema='energymap_state', type_='check')
    op.create_check_constraint('user_buildings_version', 'energymap_user_buildings',
        'schema_version = 1 OR schema_version = 2 OR schema_version = 3', schema='energymap_state')
