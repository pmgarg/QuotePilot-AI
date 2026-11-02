"""membership activation + RLS on users/sessions (security audit SEC-01/SEC-02)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Per-tenant activation: deactivating a member must never touch another tenant's access to the same account.
    op.add_column("memberships", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))

    # users: visible only as (a) the acting user, (b) the account being logged in (app.login_email),
    # (c) members of the current organisation. Inserts require an authenticated tenant context.
    # ENABLE (not FORCE): the runtime role quotepilot_app is always subject to these policies; the owner role is
    # reserved for migrations and operator maintenance scripts (e.g. session revocation) and bypasses them.
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY users_scope ON users USING ("
        "id = NULLIF(current_setting('app.current_user', true), '')::uuid "
        "OR email = NULLIF(current_setting('app.login_email', true), '')::citext "
        "OR id IN (SELECT m.user_id FROM memberships m WHERE m.organization_id = "
        "NULLIF(current_setting('app.current_org', true), '')::uuid)) "
        "WITH CHECK (NULLIF(current_setting('app.current_org', true), '') IS NOT NULL)"
    )
    # sessions: visible only by the presented token hash (resolution), the acting user, or the current org
    # (administrative revocation on role change / deactivation).
    op.execute("ALTER TABLE sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sessions NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY sessions_scope ON sessions USING ("
        "token_hash = NULLIF(current_setting('app.session_token_hash', true), '') "
        "OR user_id = NULLIF(current_setting('app.current_user', true), '')::uuid "
        "OR organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid) "
        "WITH CHECK (user_id = NULLIF(current_setting('app.current_user', true), '')::uuid "
        "OR organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS sessions_scope ON sessions")
    op.execute("ALTER TABLE sessions DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS users_scope ON users")
    op.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY")
    op.drop_column("memberships", "is_active")
