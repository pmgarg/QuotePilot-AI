SHELL := /bin/bash
API_DIR := apps/api
WEB_DIR := apps/web
UV := uv
export DATABASE_URL ?= postgresql+psycopg://quotepilot_app:quotepilot_app@localhost:5432/quotepilot
export DATABASE_ADMIN_URL ?= postgresql+psycopg://quotepilot_owner:quotepilot_owner@localhost:5432/quotepilot

.PHONY: help db-up api-install api-lint api-test api-test-unit api-test-security api-migrate api-run demo web-install web-check web-run e2e security-scan openapi fixtures compose-check

help:
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

db-up: ## Start local PostgreSQL 16 (service) and create roles/databases
	@(service postgresql start >/dev/null 2>&1 || pg_ctlcluster 16 main start >/dev/null 2>&1 || true)
	./scripts/db_setup.sh

api-install: ## Install API dependencies (uv)
	cd $(API_DIR) && $(UV) sync --all-extras

api-lint: ## ruff + mypy
	cd $(API_DIR) && $(UV) run ruff check app tests && $(UV) run ruff format --check app tests && $(UV) run mypy app

api-test: ## All API tests (unit + integration + security) against quotepilot_test
	cd $(API_DIR) && $(UV) run pytest -q

api-test-unit: ## Unit tests only (no database)
	cd $(API_DIR) && $(UV) run pytest -q tests/unit

api-test-security: ## Security-marked tests
	cd $(API_DIR) && $(UV) run pytest -q -m security

api-migrate: ## alembic upgrade head (owner role)
	cd $(API_DIR) && $(UV) run alembic upgrade head

worker-run: ## Run the Stage 2 follow-up worker (loop)
	cd $(API_DIR) && $(UV) run python -m app.workers.runner --interval 30

api-run: ## Run the API on :8000
	cd $(API_DIR) && $(UV) run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

demo: api-migrate ## Migrate + seed synthetic demo data (prints local-only credentials)
	cd $(API_DIR) && $(UV) run python -m app.fixtures.seed

web-install: ## Install web dependencies
	cd $(WEB_DIR) && npm ci

web-check: ## tsc + eslint + next build
	cd $(WEB_DIR) && npx tsc --noEmit && npx eslint . && npm run build

web-run: ## Run the web app on :3000 (proxies /api → API_INTERNAL_URL)
	cd $(WEB_DIR) && npm run dev

e2e: ## Playwright end-to-end tests (boots API + web against seeded test DB)
	cd tests/e2e && npm ci --silent && npx playwright test

openapi: ## Export OpenAPI schema to docs/openapi.json
	cd $(API_DIR) && $(UV) run python -c "import json; from app.main import app; print(json.dumps(app.openapi(), indent=2, sort_keys=True))" > ../../docs/openapi.json

fixtures: ## Regenerate synthetic test fixtures
	$(API_DIR)/.venv/bin/python scripts/make_fixtures.py

security-scan: ## Dependency, static and secret scans; writes docs/evidence/scan-<date>.md
	./scripts/security_scan.sh

compose-check: ## Render the compose topology as documented and check credential boundaries (needs docker CLI, no daemon)
	@test -f .env || cp .env.example .env
	docker compose --env-file .env -f infra/docker-compose.yml config > /tmp/quotepilot-compose.yml
	cd $(API_DIR) && $(UV) run python ../../scripts/check_compose.py /tmp/quotepilot-compose.yml
