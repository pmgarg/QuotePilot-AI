"""index for address-level suppression lookups (lower(email) within a tenant)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-10 09:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_contacts_org_lower_email ON contacts (organization_id, lower(email))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_contacts_org_lower_email")
