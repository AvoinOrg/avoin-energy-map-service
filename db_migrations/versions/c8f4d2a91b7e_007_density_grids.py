"""Add energymap density grid tables.

Revision ID: c8f4d2a91b7e
Revises: 9b2f4a7e6c3d
Create Date: 2026-05-18 16:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
import geoalchemy2


# revision identifiers, used by Alembic.
revision = 'c8f4d2a91b7e'
down_revision = '9b2f4a7e6c3d'
branch_labels = None
depends_on = None


DENSITY_TABLES = (
    'energymap_density_grid_z5_8',
    'energymap_density_grid_z9_12',
)


def create_density_table(table_name: str) -> None:
    op.create_table(
        table_name,
        sa.Column('cell_id', sa.String(), nullable=False),
        sa.Column('cell_size_m', sa.Integer(), nullable=False),
        sa.Column('min_zoom', sa.Integer(), nullable=False),
        sa.Column('max_zoom', sa.Integer(), nullable=False),
        sa.Column('cell_x', sa.BigInteger(), nullable=False),
        sa.Column('cell_y', sa.BigInteger(), nullable=False),
        sa.Column('building_count', sa.Integer(), nullable=False),
        sa.Column('certified_building_count', sa.Integer(), nullable=False),
        sa.Column('certificate_coverage', sa.Float(), nullable=True),
        sa.Column('class_a_count', sa.Integer(), nullable=False),
        sa.Column('class_b_count', sa.Integer(), nullable=False),
        sa.Column('class_c_count', sa.Integer(), nullable=False),
        sa.Column('class_d_count', sa.Integer(), nullable=False),
        sa.Column('class_e_count', sa.Integer(), nullable=False),
        sa.Column('class_f_count', sa.Integer(), nullable=False),
        sa.Column('class_g_count', sa.Integer(), nullable=False),
        sa.Column('class_unknown_count', sa.Integer(), nullable=False),
        sa.Column('dominant_energy_certificate_class', sa.String(), nullable=True),
        sa.Column('dominant_energy_certificate_class_count', sa.Integer(), nullable=False),
        sa.Column(
            'geom',
            geoalchemy2.types.Geometry(
                geometry_type='POLYGON',
                srid=3857,
                dimension=2,
                from_text='ST_GeomFromEWKT',
                name='geometry',
            ),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint('cell_id'),
        if_not_exists=True,
    )
    op.create_index(
        f'idx_{table_name}_geom',
        table_name,
        ['geom'],
        unique=False,
        postgresql_using='gist',
        if_not_exists=True,
    )


def grant_readonly_if_present(table_name: str) -> None:
    op.execute(
        sa.text(f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sandbox_energymap_readonly') THEN
                    EXECUTE 'GRANT SELECT ON TABLE public.{table_name} TO sandbox_energymap_readonly';
                END IF;
            END
            $$;
        """)
    )


def upgrade() -> None:
    """Apply schema changes."""
    for table_name in DENSITY_TABLES:
        create_density_table(table_name)
        grant_readonly_if_present(table_name)


def downgrade() -> None:
    """Revert schema changes."""
    for table_name in reversed(DENSITY_TABLES):
        op.drop_index(f'idx_{table_name}_geom', table_name=table_name, postgresql_using='gist')
        op.drop_table(table_name)
