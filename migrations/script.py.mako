"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Must stay backward compatible with the previous image (expand now, contract in a later
release) so rolling back the app never needs a downgrade. See DEPLOY.md.
"""

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else 'pass'}


def downgrade() -> None:
    ${downgrades if downgrades else 'pass'}
