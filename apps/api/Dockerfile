FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN addgroup --system app && adduser --system --ingroup app --home /app app
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project || uv sync --no-dev --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
RUN mkdir -p /data/storage && chown -R app:app /app /data
USER app
ENV PATH="/app/.venv/bin:$PATH" STORAGE_DIR=/data/storage
EXPOSE 8000
# Forwarded headers are trusted from 127.0.0.1 only (uvicorn default). Behind a reverse proxy set
# FORWARDED_ALLOW_IPS to EVERY proxy hop IP (comma separated) — see docs/OPERATIONS_RUNBOOK.md.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-server-header"]
