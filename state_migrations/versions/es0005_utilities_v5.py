"""State-only v5 successor; share the combined private-state definition."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

revision = 'es0005_utilities_v5'
down_revision = 'es0004_renovations_v4'
branch_labels = None
depends_on = None


def definition():
    path = Path(__file__).resolve().parents[2] / 'db_migrations/versions/b825df0a431c_015_personal_utilities_v5.py'
    spec = spec_from_file_location('personal_utilities_v5_definition', path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade():
    definition().upgrade()


def downgrade():
    definition().downgrade()
