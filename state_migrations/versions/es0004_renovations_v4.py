"""State-only v4 successor; share the combined private-state definition."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

revision = 'es0004_renovations_v4'
down_revision = 'es0003_personal_hvac_v3'
branch_labels = None
depends_on = None


def definition():
    path = Path(__file__).resolve().parents[2] / 'db_migrations/versions/a714cf9e320b_014_personal_renovations_v4.py'
    spec = spec_from_file_location('personal_renovations_v4_definition', path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade():
    definition().upgrade()


def downgrade():
    definition().downgrade()
