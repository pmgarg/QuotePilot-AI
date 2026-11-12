"""release review 1 fixes: immutable approval snapshot, AI consent, tighter users/sessions policies

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

USERS_SELECT = (
    "id = NULLIF(current_setting('app.current_user', true), '')::uuid "
    "OR email = NULLIF(current_setting('app.login_email', true), '')::citext "
    "OR id IN (SELECT m.user_id FROM memberships m WHERE m.organization_id = "
    "NULLIF(current_setting('app.current_org', true), '')::uuid)"
)
SESSIONS_SELECT = (
    "token_hash = NULLIF(current_setting('app.session_token_hash', true), '') "
    "OR user_id = NULLIF(current_setting('app.current_user', true), '')::uuid "
    "OR organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid"
)


def upgrade() -> None:
    # H-1: the approved document is frozen at approval time (header fields + validity) and never re-rendered.
    op.add_column("quote_versions", sa.Column("document_snapshot", postgresql.JSONB(), nullable=True))
    # X-05: hosted AI processing requires an explicit owner consent record per organisation.
    op.add_column("organizations", sa.Column("ai_processing_consented_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("organizations", sa.Column("ai_processing_consented_by", postgresql.UUID(as_uuid=True), nullable=True))

    # L-1: split the auth-table policies per command so writes are as narrow as reads.
    op.execute("DROP POLICY IF EXISTS users_scope ON users")
    op.execute(f"CREATE POLICY users_select ON users FOR SELECT USING ({USERS_SELECT})")
    op.execute(
        "CREATE POLICY users_insert ON users FOR INSERT WITH CHECK ("
        "NULLIF(current_setting('app.current_org', true), '') IS NOT NULL)"
    )
    op.execute(
        "CREATE POLICY users_update ON users FOR UPDATE USING ("
        "id = NULLIF(current_setting('app.current_user', true), '')::uuid) "
        "WITH CHECK (id = NULLIF(current_setting('app.current_user', true), '')::uuid)"
    )
    op.execute("DROP POLICY IF EXISTS sessions_scope ON sessions")
    op.execute(f"CREATE POLICY sessions_select ON sessions FOR SELECT USING ({SESSIONS_SELECT})")
    op.execute(
        "CREATE POLICY sessions_insert ON sessions FOR INSERT WITH CHECK ("
        "user_id = NULLIF(current_setting('app.current_user', true), '')::uuid "
        "AND organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY sessions_update ON sessions FOR UPDATE USING ({SESSIONS_SELECT}) "
        f"WITH CHECK ({SESSIONS_SELECT})"
    )


def downgrade() -> None:
    for pol in ("sessions_update", "sessions_insert", "sessions_select"):
        op.execute(f"DROP POLICY IF EXISTS {pol} ON sessions")
    for pol in ("users_update", "users_insert", "users_select"):
        op.execute(f"DROP POLICY IF EXISTS {pol} ON users")
    op.execute(
        "CREATE POLICY users_scope ON users USING (" + USERS_SELECT + ") "
        "WITH CHECK (NULLIF(current_setting('app.current_org', true), '') IS NOT NULL)"
    )
    op.execute(
        "CREATE POLICY sessions_scope ON sessions USING (" + SESSIONS_SELECT + ") "
        "WITH CHECK (user_id = NULLIF(current_setting('app.current_user', true), '')::uuid "
        "OR organization_id = NULLIF(current_setting('app.current_org', true), '')::uuid)"
    )
    op.drop_column("organizations", "ai_processing_consented_by")
    op.drop_column("organizations", "ai_processing_consented_at")
    op.drop_column("quote_versions", "document_snapshot")
