"""Stage 3 hardening: idempotency keys scoped per (kind, connector, entity) with a request hash (SEC3-06);
one *active* connector per kind instead of one ever (SEC3-07).

Revision ID: 0007b
Revises: 0007
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007b"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sync_runs", sa.Column("scope_key", sa.String(length=120), server_default="", nullable=False))
    op.add_column("sync_runs", sa.Column("request_hash", sa.String(length=64), nullable=True))
    # the owner role is subject to FORCE RLS: relax it for the backfill only (same pattern as 0005b)
    op.execute("ALTER TABLE sync_runs NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE sync_runs SET scope_key = kind || ':' || COALESCE(connection_id::text, '-') || ':' || entity "
        "WHERE scope_key = ''"
    )
    op.execute("ALTER TABLE sync_runs FORCE ROW LEVEL SECURITY")
    op.drop_constraint("uq_sync_run_idem", "sync_runs", type_="unique")
    op.create_unique_constraint(
        "uq_sync_run_scope_idem", "sync_runs", ["organization_id", "scope_key", "idempotency_key"]
    )
    op.drop_constraint("uq_connector_org_kind", "connector_connections", type_="unique")
    op.create_index(
        "uq_connector_org_kind_active",
        "connector_connections",
        ["organization_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_connector_org_kind_active", table_name="connector_connections")
    op.create_unique_constraint("uq_connector_org_kind", "connector_connections", ["organization_id", "kind"])
    op.drop_constraint("uq_sync_run_scope_idem", "sync_runs", type_="unique")
    op.create_unique_constraint("uq_sync_run_idem", "sync_runs", ["organization_id", "idempotency_key"])
    op.drop_column("sync_runs", "request_hash")
    op.drop_column("sync_runs", "scope_key")
