"""Follow-up worker: `python -m app.workers.runner [--once] [--interval 30]`. Uses the app DB role (RLS enforced)."""

from __future__ import annotations

import argparse
import sys
import time

from app.core.logging import configure_logging
from app.db.session import get_sessionmaker
from app.services.intelligence.retention import enqueue_due_purges
from app.services.jobs.queue import enqueue_due_schedules, run_once


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=30.0)
    args = ap.parse_args()
    configure_logging()
    while True:
        with get_sessionmaker()() as db:
            enqueue_due_schedules(db)
            enqueue_due_purges(db)
            n = run_once(db)
        if args.once:
            print(f"processed {n} job(s)")
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
