"""Independent bootstrap reusing frozen c7d3e921ab40 + d4e8f021bc63 definitions."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

revision = 'es0001_saved_share'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    directory = Path(__file__).resolve().parents[2] / 'db_migrations' / 'versions'
    for name in ('c7d3e921ab40_010_owner_saved_buildings.py', 'd4e8f021bc63_011_building_shares.py'):
        spec = spec_from_file_location('frozen_' + name[:12], directory / name)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        module.upgrade()


def downgrade():
    raise RuntimeError('Destructive state downgrade refused; restore to a new managed target')
