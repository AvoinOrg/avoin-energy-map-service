"""Add personal v2 using the identical state-only legacy migration."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

revision = 'es0002_personal_v2'
down_revision = 'es0001_saved_share'
branch_labels = None
depends_on = None


def definition():
    path = Path(__file__).resolve().parents[2] / 'db_migrations/versions/e5f102acd890_012_personal_v2.py'
    spec = spec_from_file_location('personal_v2_state_definition', path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade():
    definition().upgrade()


def downgrade():
    definition().downgrade()
