"""CI check for the Compose topology (NEW-16 / R3-3): render it the way the README tells users to run it and assert
that the api and web services never receive owner-role credentials. Run from the repository root:
    cp .env.example .env && docker compose --env-file .env -f infra/docker-compose.yml config > /tmp/c.yml
    python scripts/check_compose.py /tmp/c.yml
"""

from __future__ import annotations

import sys

import yaml

FORBIDDEN_KEYS = {"QP_OWNER_PASSWORD", "POSTGRES_PASSWORD"}


def main(path: str) -> int:
    rendered = yaml.safe_load(open(path, encoding="utf-8"))  # noqa: SIM115 - short script
    problems: list[str] = []
    for name in ("api", "web", "worker"):
        svc = rendered.get("services", {}).get(name)
        if svc is None:
            continue
        env = svc.get("environment") or {}
        for key, value in env.items():
            if key in FORBIDDEN_KEYS or "quotepilot_owner" in str(value):
                problems.append(f"{name}: {key} carries owner-role credentials")
        if env.get("DATABASE_ADMIN_URL"):
            problems.append(f"{name}: DATABASE_ADMIN_URL must be empty")
        if "env_file" in svc:
            problems.append(f"{name}: env_file would inject every key of .env")
    for p in problems:
        print("FAIL", p)
    print("compose check:", "FAIL" if problems else "OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
