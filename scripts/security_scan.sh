#!/usr/bin/env bash
# Runs every locally available scanner and records real output + exit codes under docs/evidence/.
# Reports tool unavailability explicitly instead of assuming a clean result.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/docs/evidence/scan-$(date -u +%Y%m%d-%H%M%S).md"
mkdir -p "$ROOT/docs/evidence"
{
  echo "# Security scan evidence ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
  echo
  echo "Commit: $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo n/a)"
  echo
} > "$OUT"

run() { # name, dir, command...
  local name="$1" dir="$2"; shift 2
  echo "## $name" >> "$OUT"
  echo '```' >> "$OUT"
  echo "\$ $*" >> "$OUT"
  if ! command -v "$1" >/dev/null 2>&1 && [ ! -x "$dir/$1" ] && [ ! -x "$1" ]; then
    echo "TOOL NOT AVAILABLE: $1" >> "$OUT"; echo '```' >> "$OUT"; echo >> "$OUT"; return
  fi
  (cd "$dir" && "$@") >> "$OUT" 2>&1
  echo "exit code: $?" >> "$OUT"
  echo '```' >> "$OUT"; echo >> "$OUT"
}

run "pip-audit (API dependencies)" "$ROOT/apps/api" .venv/bin/pip-audit --skip-editable
run "bandit (API static analysis)" "$ROOT/apps/api" .venv/bin/bandit -r app -q -f txt -ll
run "ruff (project config includes flake8-bandit S* rules; S101 assert excluded by policy)" "$ROOT/apps/api" .venv/bin/ruff check app --output-format concise
run "npm audit (web, production deps)" "$ROOT/apps/web" npm audit --omit=dev
run "npm audit (web, all deps)" "$ROOT/apps/web" npm audit
run "gitleaks (secrets)" "$ROOT" gitleaks detect --source . --no-banner --redact --config .gitleaks.toml
run "fallback secret pattern grep (used when gitleaks is unavailable)" "$ROOT" bash -c 'grep -rInE "(sk-ant-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----|ghp_[A-Za-z0-9]{36})" --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=.next --exclude-dir=.git --exclude-dir=evidence --exclude-dir=tests --exclude-dir=fixtures . && echo "POSSIBLE SECRET FOUND" || echo "no secret patterns found"' 
# semgrep registry rulesets require telemetry/login; it runs in CI (.github/workflows/security.yml) via semgrep-action.
run "semgrep (CI-only: registry rulesets need metrics/login; local run attempted if a binary is on PATH)" "$ROOT" semgrep scan --config p/python --config p/typescript --config p/secrets --error --quiet --exclude node_modules --exclude .next --exclude .venv apps
run "web bundle secret grep" "$ROOT/apps/web" bash -c 'if [ -d .next ]; then grep -rIlE "sk-ant-|AKIA[0-9A-Z]{16}|ANTHROPIC_API_KEY=" .next/static 2>/dev/null && echo "POSSIBLE SECRET IN BUNDLE" || echo "no secret patterns in .next/static"; else echo "no build present (run make web-check first)"; fi'
echo "evidence written to $OUT"
