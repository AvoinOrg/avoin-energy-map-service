"""Add energy certificate data.

Revision ID: 9b2f4a7e6c3d
Revises: 13da3291148a
Create Date: 2026-05-10 18:25:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '9b2f4a7e6c3d'
down_revision = '13da3291148a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply schema changes."""
    op.create_table('energy_certificates',
    sa.Column('certificate_id', sa.String(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=True),
    sa.Column('signed_at', sa.TIMESTAMP(timezone=False), nullable=True),
    sa.Column('valid_until', sa.TIMESTAMP(timezone=False), nullable=True),
    sa.Column('language', sa.String(), nullable=True),
    sa.Column('drafting_stage', sa.String(), nullable=True),
    sa.Column('inspection_date', sa.Date(), nullable=True),
    sa.Column('name_fi', sa.String(), nullable=True),
    sa.Column('name_sv', sa.String(), nullable=True),
    sa.Column('completion_year', sa.Integer(), nullable=True),
    sa.Column('street_address_fi', sa.String(), nullable=True),
    sa.Column('street_address_sv', sa.String(), nullable=True),
    sa.Column('postal_code', sa.String(), nullable=True),
    sa.Column('building_identifier', sa.String(), nullable=True),
    sa.Column('use_purpose', sa.String(), nullable=True),
    sa.Column('sub_use_purpose_fi', sa.String(), nullable=True),
    sa.Column('e_value', sa.Float(), nullable=True),
    sa.Column('e_class', sa.String(), nullable=True),
    sa.Column('e_class_limit_2018', sa.Float(), nullable=True),
    sa.Column('e_class_purpose_label_fi', sa.String(), nullable=True),
    sa.Column('recommendations_fi', sa.Text(), nullable=True),
    sa.Column('recommendations_sv', sa.Text(), nullable=True),
    sa.Column('heated_net_area', sa.Float(), nullable=True),
    sa.Column('ventilation_type_id', sa.String(), nullable=True),
    sa.Column('ventilation_description_fi', sa.Text(), nullable=True),
    sa.Column('ventilation_description_sv', sa.Text(), nullable=True),
    sa.Column('heating_form_1_id', sa.String(), nullable=True),
    sa.Column('heating_form_2_id', sa.String(), nullable=True),
    sa.Column('heating_form_1_description_fi', sa.Text(), nullable=True),
    sa.Column('heating_form_1_description_sv', sa.Text(), nullable=True),
    sa.Column('heating_form_2_description_fi', sa.Text(), nullable=True),
    sa.Column('heating_form_2_description_sv', sa.Text(), nullable=True),
    sa.Column('heat_distribution_id', sa.String(), nullable=True),
    sa.Column('heat_distribution_description_fi', sa.Text(), nullable=True),
    sa.Column('heat_distribution_description_sv', sa.Text(), nullable=True),
    sa.Column('source_row', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.PrimaryKeyConstraint('certificate_id'),
    if_not_exists=True
    )
    op.create_index('idx_energy_certificates_building_identifier', 'energy_certificates', ['building_identifier'], unique=False, if_not_exists=True)
    op.create_index('idx_energy_certificates_signed_at', 'energy_certificates', ['signed_at'], unique=False, if_not_exists=True)


def downgrade() -> None:
    """Revert schema changes."""
    op.drop_index('idx_energy_certificates_signed_at', table_name='energy_certificates')
    op.drop_index('idx_energy_certificates_building_identifier', table_name='energy_certificates')
    op.drop_table('energy_certificates')
