"""Auto-generated Alembic revision.

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""

from alembic import op
import sqlalchemy as sa
import geoalchemy2


# revision identifiers, used by Alembic.
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    """Apply schema changes."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Revert schema changes."""
    ${downgrades if downgrades else "pass"}
