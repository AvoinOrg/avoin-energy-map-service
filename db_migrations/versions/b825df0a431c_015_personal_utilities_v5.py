"""Admit closed personal format v5 without converting any saved document."""
from alembic import op
import sqlalchemy as sa

revision = 'b825df0a431c'
down_revision = 'a714cf9e320b'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('user_buildings_version', 'energymap_user_buildings', schema='energymap_state', type_='check')
    op.create_check_constraint('user_buildings_version', 'energymap_user_buildings',
        'schema_version = 1 OR schema_version = 2 OR schema_version = 3 OR schema_version = 4 OR schema_version = 5', schema='energymap_state')


def downgrade():
    # Serialize the refusal check with inserts/promotions, including empty v5.
    op.execute('LOCK TABLE energymap_state.energymap_user_buildings IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM energymap_state.energymap_user_buildings WHERE schema_version NOT IN (1,2,3,4))')).scalar_one():
        raise RuntimeError('Personal v5 data present; downgrade refused')
    op.drop_constraint('user_buildings_version', 'energymap_user_buildings', schema='energymap_state', type_='check')
    op.create_check_constraint('user_buildings_version', 'energymap_user_buildings',
        'schema_version = 1 OR schema_version = 2 OR schema_version = 3 OR schema_version = 4', schema='energymap_state')
