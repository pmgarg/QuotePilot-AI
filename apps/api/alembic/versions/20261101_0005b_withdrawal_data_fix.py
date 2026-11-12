"""L2-5 / D2-3 / NEW-14: withdraw legacy approved versions whose decision trail says `withdrawn`.

Revision ID: 0005b
Revises: 0005
Create Date: 2026-11-01

`quotes` / `quote_versions` / `approval_decisions` are FORCE ROW LEVEL SECURITY, so the owner role that runs
migrations sees no rows unless the policy is relaxed for the duration of the fix (D2-3). The predicate keys on
the decision trail — a `withdrawn` decision recorded for a version withdraws that version and every earlier
approved version of the same quote — never on `quotes.withdrawn_reason`, which `new_version` clears (the exact
legacy shape of N2-2). A version approved *after* the withdrawal (higher version_no) is left alone, so the fix
is idempotent against round-4 data (NEW-14 b).
"""

from __future__ import annotations

from alembic import op

revision = "0005b"
down_revision = "0005"
branch_labels = None
depends_on = None

TABLES = ("quotes", "quote_versions", "approval_decisions")

DATA_FIX = """
UPDATE quote_versions v
   SET status = 'withdrawn'
  FROM (
        SELECT v2.quote_id, MAX(v2.version_no) AS last_withdrawn_no
          FROM approval_decisions d
          JOIN quote_versions v2 ON v2.id = d.quote_version_id
         WHERE d.decision = 'withdrawn'
         GROUP BY v2.quote_id
       ) w
 WHERE v.quote_id = w.quote_id
   AND v.version_no <= w.last_withdrawn_no
   AND v.status = 'approved'
"""


def upgrade() -> None:
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
    try:
        op.execute(DATA_FIX)
    finally:
        for t in TABLES:
            op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    pass  # data fix only; the per-version statuses are correct either way
