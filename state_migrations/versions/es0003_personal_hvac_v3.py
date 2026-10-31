"""State-only v3 successor; same private-state definition as combined lineage."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

revision = 'es0003_personal_hvac_v3'
down_revision = 'es0002_personal_v2'
branch_labels = None
depends_on = None


def definition():
    path = Path(__file__).resolve().parents[2] / 'db_migrations/versions/f6a213bde901_013_personal_hvac_v3.py'
    spec = spec_from_file_location('personal_hvac_v3_definition', path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade():
    definition().upgrade()


def downgrade():
    definition().downgrade()
