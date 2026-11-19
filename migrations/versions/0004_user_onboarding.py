"""user onboarding: the guided setup's state and resume point, per account

Revision ID: 0004
Revises: 0003

Expand-only: a new table. The row is created at sign-up only, so existing and claimed
accounts never see onboarding.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE user_onboarding (
    user_id       BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    started_at    TIMESTAMPTZ NOT NULL,
    completed_at  TIMESTAMPTZ,
    skipped_at    TIMESTAMPTZ,
    dismissed_at  TIMESTAMPTZ,
    step          TEXT NOT NULL,
    goals         TEXT NOT NULL DEFAULT '[]',
    conditions    TEXT NOT NULL DEFAULT '[]',
    cooking_style TEXT NOT NULL DEFAULT ''
)
""")


def downgrade() -> None:
    op.execute("DROP TABLE user_onboarding")
