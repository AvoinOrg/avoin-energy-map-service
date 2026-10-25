"""Stable area views for the latest active internal release."""
from alembic import op
from src.area_contract import STABLE_AREA_VIEWS

revision = "9b812a37ef01"
down_revision = "3e719a2c804f"
branch_labels = None
depends_on = None


def upgrade():
    for level, name in STABLE_AREA_VIEWS.items():
        op.execute(f"""
            CREATE VIEW {name} AS
            SELECT a.area_code::bigint AS fid,a.level,a.area_code,
              a.level||':'||a.area_code AS area_id,a.name_fi,a.name_sv,a.name_en,
              s.building_count,s.valid_official_class_count,s.certificate_coverage,
              a.display_geom AS geom
            FROM energymap_area_stats s JOIN energymap_area_releases r USING(release_id)
            JOIN energymap_area_boundaries a ON a.boundary_set_id=s.boundary_set_id
              AND a.level=s.level AND a.area_code=s.area_code
            WHERE r.active AND r.validated AND a.level='{level}'
        """)
        op.execute(f"""DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='sandbox_energymap_readonly') THEN
                GRANT SELECT ON {name} TO sandbox_energymap_readonly;
            END IF;
        END $$""")


def downgrade():
    for name in STABLE_AREA_VIEWS.values():
        op.execute(f"DROP VIEW {name}")
