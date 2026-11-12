#!/bin/bash
# Creates the owner (migrations) and app (runtime, RLS-enforced) roles plus the database on first start.
set -e
OWNER_PW="${QP_OWNER_PASSWORD:-quotepilot_owner}"
APP_PW="${QP_APP_PASSWORD:-quotepilot_app}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<SQL
CREATE ROLE quotepilot_owner LOGIN PASSWORD '${OWNER_PW}' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE quotepilot_app LOGIN PASSWORD '${APP_PW}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE DATABASE quotepilot OWNER quotepilot_owner;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d quotepilot <<SQL
GRANT USAGE ON SCHEMA public TO quotepilot_app;
ALTER DEFAULT PRIVILEGES FOR ROLE quotepilot_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quotepilot_app;
ALTER DEFAULT PRIVILEGES FOR ROLE quotepilot_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO quotepilot_app;
ALTER ROLE quotepilot_app SET statement_timeout = '15s';
SQL
