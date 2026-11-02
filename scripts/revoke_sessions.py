#!/usr/bin/env python3
"""Operator kill switch: revoke active sessions for one organisation, one user, or everyone.

Runs with the OWNER role (DATABASE_ADMIN_URL), which bypasses the (ENABLE-only) RLS on `sessions`.
Usage (from apps/api):  uv run python ../../scripts/revoke_sessions.py --org <uuid> | --user-email <email> | --all
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy import create_engine, text


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--org", help="organisation id")
    g.add_argument("--user-email", help="user e-mail (all organisations)")
    g.add_argument("--all", action="store_true", help="revoke every active session (incident response)")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_ADMIN_URL")
    if not url:
        print("DATABASE_ADMIN_URL is required (owner role)", file=sys.stderr)
        return 2
    if not args.yes:
        answer = input("Revoke sessions now? type 'revoke' to continue: ")
        if answer.strip() != "revoke":
            return 1
    eng = create_engine(url, future=True)
    with eng.begin() as conn:
        if args.org:  # audit_events FORCEs RLS even for the owner role: bind the tenant for this transaction
            conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": args.org})
        if args.org:
            res = conn.execute(
                text("UPDATE sessions SET revoked_at = now() WHERE revoked_at IS NULL AND organization_id = :o"),
                {"o": args.org},
            )
            scope = "org"
        elif args.user_email:
            res = conn.execute(
                text(
                    "UPDATE sessions SET revoked_at = now() WHERE revoked_at IS NULL "
                    "AND user_id = (SELECT id FROM users WHERE email = :e)"
                ),
                {"e": args.user_email.strip().lower()},
            )
            scope = "user"
        else:
            res = conn.execute(text("UPDATE sessions SET revoked_at = now() WHERE revoked_at IS NULL"))
            scope = "all"
        # NEW-15: audit rows are tenant-scoped (RLS hides organization_id IS NULL from every product role), so a
        # user- or platform-wide revocation writes one row per affected organisation; the platform-scope total
        # is also logged to stdout for the operator's incident record.
        if args.org:
            affected = [(args.org, res.rowcount)]
        else:
            affected = [
                (str(o), int(n))
                for o, n in conn.execute(
                    text(
                        "SELECT organization_id, count(*) FROM sessions "
                        "WHERE revoked_at = now() GROUP BY organization_id"
                    )
                )
            ]
        for org_id, n in affected:
            conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": org_id})
            conn.execute(
                text(
                    "INSERT INTO audit_events (organization_id, actor_user_id, action, entity_type, summary) "
                    "VALUES (:o, NULL, 'ops.sessions_revoked', 'session', CAST(:s AS jsonb))"
                ),
                {"o": org_id, "s": json.dumps({"scope": scope, "count": n, "total": res.rowcount})},
            )
    print(f"revoked {res.rowcount} session(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
