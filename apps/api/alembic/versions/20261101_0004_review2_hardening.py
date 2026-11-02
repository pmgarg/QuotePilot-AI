"""review 2 hardening: column-level write grants on users/sessions for the runtime role (NEW-5)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

GRANT_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quotepilot_app') THEN "
    "REVOKE UPDATE, DELETE ON sessions FROM quotepilot_app; "
    "GRANT UPDATE (last_seen_at, revoked_at) ON sessions TO quotepilot_app; "
    "REVOKE UPDATE, DELETE ON users FROM quotepilot_app; "
    "GRANT UPDATE (full_name, password_hash, updated_at) ON users TO quotepilot_app; "
    "END IF; END $$;"
)
REVERT_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quotepilot_app') THEN "
    "GRANT UPDATE, DELETE ON sessions TO quotepilot_app; "
    "GRANT UPDATE, DELETE ON users TO quotepilot_app; "
    "END IF; END $$;"
)


def upgrade() -> None:
    # The app may only touch session liveness columns and a user's own profile/password; identity columns
    # (user_id, organization_id, token_hash, email, is_active) are immutable for the runtime role.
    op.execute(GRANT_SQL)


def downgrade() -> None:
    op.execute(REVERT_SQL)
