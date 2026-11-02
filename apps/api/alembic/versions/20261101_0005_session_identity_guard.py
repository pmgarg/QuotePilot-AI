"""review 2: session/user identity guard trigger (L2-4) and data fix for pre-existing withdrawn approvals (L2-5)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01
"""
from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Defence in depth beyond column grants: identity columns of sessions and users can never change via UPDATE.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qp_guard_identity_columns() RETURNS trigger AS $$
        BEGIN
          IF TG_TABLE_NAME = 'sessions' THEN
            IF NEW.user_id IS DISTINCT FROM OLD.user_id
               OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
               OR NEW.token_hash IS DISTINCT FROM OLD.token_hash
               OR NEW.csrf_secret IS DISTINCT FROM OLD.csrf_secret THEN
              RAISE EXCEPTION 'session identity columns are immutable';
            END IF;
          ELSIF TG_TABLE_NAME = 'users' THEN
            IF NEW.id IS DISTINCT FROM OLD.id OR NEW.email IS DISTINCT FROM OLD.email THEN
              RAISE EXCEPTION 'user identity columns are immutable';
            END IF;
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_sessions_identity ON sessions")
    op.execute("CREATE TRIGGER trg_sessions_identity BEFORE UPDATE ON sessions FOR EACH ROW EXECUTE FUNCTION qp_guard_identity_columns()")
    op.execute("DROP TRIGGER IF EXISTS trg_users_identity ON users")
    op.execute("CREATE TRIGGER trg_users_identity BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION qp_guard_identity_columns()")
    # L2-5 data fix moved to 0005b: under FORCE ROW LEVEL SECURITY the owner role saw no rows here (D2-3)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_sessions_identity ON sessions")
    op.execute("DROP TRIGGER IF EXISTS trg_users_identity ON users")
    op.execute("DROP FUNCTION IF EXISTS qp_guard_identity_columns()")
