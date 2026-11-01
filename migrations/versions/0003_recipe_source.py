"""recipe source + labels: the URL of the real recipe a dinner was adapted from, and the
agent's cuisine / main-protein labels that keep one plan from repeating itself

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Expand only: the default keeps the previous app version's INSERTs working.
    op.execute("ALTER TABLE recipes ADD COLUMN source TEXT NOT NULL DEFAULT '', "
               "ADD COLUMN cuisine TEXT NOT NULL DEFAULT '', "
               "ADD COLUMN main_protein TEXT NOT NULL DEFAULT ''")


def downgrade() -> None:
    op.execute("ALTER TABLE recipes DROP COLUMN source, DROP COLUMN cuisine, "
               "DROP COLUMN main_protein")
