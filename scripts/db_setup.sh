#!/usr/bin/env bash
# Creates PostgreSQL roles and databases for local development and tests.
# Idempotent. Requires a running PostgreSQL 16 reachable by the `postgres` superuser via peer auth or PG* env vars.
set -euo pipefail
OWNER_PW="${QP_OWNER_PASSWORD:-quotepilot_owner}"
APP_PW="${QP_APP_PASSWORD:-quotepilot_app}"
PSQL=(psql -v ON_ERROR_STOP=1 -X -q)
if command -v sudo >/dev/null 2>&1 && id postgres >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
  PSQL=(sudo -u postgres psql -v ON_ERROR_STOP=1 -X -q)
fi
"${PSQL[@]}" <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quotepilot_owner') THEN
    CREATE ROLE quotepilot_owner LOGIN PASSWORD '${OWNER_PW}' NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quotepilot_app') THEN
    CREATE ROLE quotepilot_app LOGIN PASSWORD '${APP_PW}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END \$\$;
SQL
for db in quotepilot quotepilot_test; do
  if ! "${PSQL[@]}" -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
    "${PSQL[@]}" -c "CREATE DATABASE ${db} OWNER quotepilot_owner"
  fi
  "${PSQL[@]}" -d "${db}" -c "GRANT USAGE ON SCHEMA public TO quotepilot_app; ALTER DEFAULT PRIVILEGES FOR ROLE quotepilot_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quotepilot_app; ALTER DEFAULT PRIVILEGES FOR ROLE quotepilot_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO quotepilot_app; ALTER ROLE quotepilot_app SET statement_timeout = '15s';"
done
echo "roles and databases ready: quotepilot, quotepilot_test"
