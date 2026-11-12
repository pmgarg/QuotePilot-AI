# Operations Runbook

## Local demo (Docker Compose)
```bash
cp .env.example .env            # placeholders are fine for local demo
docker compose --env-file .env -f infra/docker-compose.yml up --build   # --env-file: Compose otherwise reads infra/.env
# web: http://localhost:3000 (the API is not published; it is reached through the web proxy at /api/v1/*)
```
The `migrate` service runs `alembic upgrade head` and `python -m app.fixtures.seed` (synthetic data; credentials printed
to the container log only, never committed). Default demo password is read from `DEMO_PASSWORD` in `.env`.

## Local without Docker (as used in this repository's CI and evidence)
```bash
make db-up        # starts PostgreSQL 16 service if present, creates roles quotepilot_owner/quotepilot_app and DBs
make api-install && make web-install
make demo         # migrate + seed
make api-run      # uvicorn on :8000
make web-run      # next dev on :3000 (proxies /api → :8000)
```

## Production topology (documented, not provisioned)
Reverse proxy (Caddy/Nginx) with TLS + HSTS → web (Next standalone, `WEB_TRUST_FORWARDED_HEADERS=true` only here) → api (uvicorn workers ×2 per vCPU, `FORWARDED_ALLOW_IPS=<comma-separated IPs of EVERY proxy hop: TLS proxy and web container>` — uvicorn walks
`X-Forwarded-For` from the right and stops at the first untrusted hop, so a missing hop attributes all traffic to that hop; never publish the API port directly) → managed
PostgreSQL 16 (daily base backup + WAL/PITR, 30 d), S3-compatible private bucket (SSE, versioning, lifecycle),
Redis (Stage 2+), worker container (Stage 2+). Separate `staging` and `production` environments with distinct secrets.

### Environment variables (see `.env.example`)
`APP_ENV`, `SECRET_KEY`, `DATABASE_URL` (app role), `DATABASE_ADMIN_URL` (migrations), `STORAGE_BACKEND=local|s3`,
`STORAGE_DIR`, `S3_*`, `AI_PROVIDER=disabled|anthropic`, `ANTHROPIC_API_KEY` (server only), `CORS_ORIGINS`,
`COOKIE_SECURE`, limits. Web: `API_INTERNAL_URL`, `WEB_TRUST_FORWARDED_HEADERS` (set `true` only behind your own
reverse proxy that overwrites `X-Forwarded-For`; the API container runs uvicorn with `--proxy-headers`).

### Stage 2 worker
`cd apps/api && uv run python -m app.workers.runner --interval 30` (or `--once`). It needs the app DB role only.
`TOKEN_ENCRYPTION_KEY` (base64, 32 random bytes: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`)
is required before any secret-bearing integration (SMTP password, future OAuth tokens) can be stored. SMTP settings:
`MAIL_PROVIDER_DEFAULT=smtp`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `SMTP_FROM_EMAIL`.
Gmail / Microsoft Graph: register an OAuth app with the minimal scopes (`gmail.send`, `gmail.readonly` or
`Mail.Send`, `Mail.Read`), store client credentials only in the environment, and implement the adapter against the
`MailProvider` protocol in `app/services/comms/providers.py`; no real credentials are used in this repository.

### Migrations
`alembic upgrade head` as `DATABASE_ADMIN_URL` before deploying new API images; migrations are additive within a
release; destructive changes in a follow-up release after backfill. Rollback: `alembic downgrade -1` + previous image.

### Backups & restore rehearsal
Nightly `pg_dump -Fc` + object storage replication; quarterly restore into staging and run `make api-test` smoke subset.

### Monitoring
Structured JSON logs (request id, latency, status, org id — no bodies); `/api/v1/health/ready`; metrics via uvicorn
access logs → Prometheus exporter (optional). Alerts: 5xx rate, parse failure rate, DB connections, disk.

### Incident response
See PRIVACY_AND_RETENTION.md breach draft. Kill switches: set `AI_PROVIDER=disabled`, revoke sessions with
`DATABASE_ADMIN_URL=… uv run python ../../scripts/revoke_sessions.py --org <id> | --user-email <e> | --all`
(owner role, audited as `ops.sessions_revoked`), disable automation flags (Stage 2+).

### Secret rotation
`SECRET_KEY` (CSRF signing) rotation invalidates CSRF tokens only; DB credentials via managed secret store; LLM key by
env redeploy. Never commit `.env`.
