# Security confirmation, Stage 1 fix round 11 (2026-11-12)
Scope: `git diff f8ef581..23c1eb3` only, local app, DB `quotepilot_sec`, no product code changed, nothing committed, no scan re-run.
Own file added: `apps/api/tests/security/test_confirm_round11.py` (8 tests). No `.env` was created.

## Checklist
| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | SEC7-1 / SEC6-2 availability | MITIGATED (not closed) | 450 lines x 30,000 products x 300 chars: 11.4-11.6 s (was 389.9 s); health probe served throughout, max 0.37-0.42 s, mean 0.009 s. Residual below |
| 2 | Worker hardening | MITIGATED | Launch, limits, stdin, kill and failure handling confirmed; one Low gap (SEC8-1) |
| 3 | Tenant isolation of the new path | CLOSED | Descriptions come from the caller's catalog version only; no cross-request state |
| 4 | SEC7-7 rate limits | MITIGATED | Limiter correct on the 3 routes; `POST /rfqs/{id}/items` is expensive and unlimited (SEC8-2) |
| 5 | Parser SEC7-2/3/5/6 | CLOSED | `test_audit_followups8.py` + `followups7.py`: 76 passed, 2 xfailed |
| 6 | Seed SEC7-4 | CLOSED | Only `DEMO_PASSWORD` is read; invalid-value and undecodable-file tests pass |
| 7 | Declared test/gitleaks edits | CLOSED | Edits match the declaration; see item 7 |

## Item 1: measured (3 runs, `test_item1_worst_case_end_to_end_and_add_item_cost`)
30,000 synthetic products x 300 chars bulk-inserted as owner; one `POST /rfqs` with 450 adversarial ~190-char lines (seed 7), sales user,
default `match_timeout_seconds` 8; second thread polling `GET /health` every 10 ms.
| Run | Request s | Lines matched / flagged `match_skipped_budget` | Probes | Probe max / mean |
|---|---|---|---|---|
| 1 | 11.37 | 50 / 400 | 596 | 0.392 s / 0.009 s |
| 2 | 11.52 | 52 / 398 | 580 | 0.405 s / 0.010 s |
| 3 | 11.58 | 47 / 403 | 592 | 0.424 s / 0.009 s |
`pgrep -f app.services.match_worker`: empty after the request in 3 of 4 runs, and empty 1.5 s later in all runs. In the very first run
(before I added the 1.5 s wait) the immediate `pgrep` listed two PIDs (3021, 3030) that were gone by the next command; I could not
reproduce it and could not capture their command lines. Reported as unexplained, not as a leak.
Precise residual:
- Jobs up to 50,000 comparisons run in the API process holding the GIL (about 0.14 s for one line against 30,000 products;
  the implementer reports up to ~1 s per job).
- The 8 s deadline is wall clock for the worker only; the request also spends about 3.5 s loading products and storing items.
  The DB transaction stays open while waiting for the worker (up to 8 s) and for a slot (up to another 8 s).
- At most 2 matchers per process; a third big parse waits up to 8 s for a slot, then its lines are all flagged. The limit is per process.
- About 89% of lines of this adversarial request are left flagged for a person (nothing is guessed): a functional degradation, not a defect in safety.
- Manual add-item against the same catalog still costs 1.4-2.1 s per call in-process (SEC8-2).

## Item 2: worker
- Launch (`matching.py:109-123`): `[sys.executable, "-I", "-m", "app.services.match_worker"]`, no shell, env limited to PATH, PYTHONPATH, LANG, VIRTUAL_ENV.
  Test with `SECRET_KEY` and `DATABASE_URL` set in the parent: env keys are a subset of those four; payload is stdin JSON with 6 fixed keys.
- Limits (`match_worker.py:20`): RLIMIT_AS = parser memory limit, RLIMIT_CPU = timeout+2 s, NOFILE 64; skipped silently where refused (platform caveat).
- Output parsing: out-of-range or negative index, non-numeric score, duplicate or skipped query index, garbage bytes and a torn last line all
  give a valid prefix or nothing (test passes). A killed worker (returncode -9, no stdout) gives `{}`, so the lines are flagged.
  A non-zero exit keeps the complete lines written before it; that prefix came from the scorer, it is not a guess. Timeout: `subprocess.run(timeout=)` kills the child; no survivor found.
- Bad stdin: exit 2, empty stdout.
- Gap SEC8-1: `json.loads` of a 100,000-deep `[` line raises RecursionError, which `_parse_worker_lines` does not catch (only ValueError/KeyError/TypeError).
  The output comes from our own worker, so it is not attacker reachable today.

## Item 3: tenant isolation
Test `test_worker_input_contains_only_callers_catalog`: two tenants with 3,000 bulk products each, one of tenant B's marked; a spy on `fuzzy_batch` shows
no tenant B description in tenant A's batches. Products are loaded with `Catalog version` filter under tenant session (RLS). `CatalogIndex` state is per instance;
`matching.py` has no module-level cache (only the matcher semaphore).

## Item 4: rate limits
- Key `"{action}|{user_id}"` from the session principal; file, text and re-parse share the key `rfq_parse` (30 per 300 s); catalog import 10 per 300 s.
  Sliding window, refused call not appended (`ratelimit.py:33-38`); 429 with `Retry-After` header (`main.py:88`). Re-parse counts only after the 404 tenant check.
  The implementer's integration tests were not re-run by me (code reading only for the counting behaviour); `RFQ_PARSE_...`/`CATALOG_IMPORT_...` are raised only through
  `tests/conftest.py` environment defaults; `Settings.model_fields` defaults are 30 / 10 / 300 / 8.0 (test passes).
- Not limited and expensive: `POST /rfqs/{id}/items` (loads the whole catalog version, in-process fuzzy match; 6 calls with the limit set to 2: no 429; 1.4-2.1 s each on 30,000 products),
  quote generation and exports (declared by the implementer; not measured by me). Limits are per process (several replicas multiply them).

## Item 7: edits
`git diff f8ef581..23c1eb3 -- apps/api/tests/security`: 4 xfail decorators removed (SEC7-2, 3, 5, 4), `strict=True` to `strict=False` on the SEC7-1 test, and two assertions
changed (SEC7-6: `. 5`/`..-5` now None; SEC7-4: undecodable root `.env` gives exit 2, no traceback, no password in output). No other assertion touched.
`.gitleaks.toml`: `tiny-secret-visible` and `Root-Env-Pass-Marker-77` are synthetic markers of that test; they are unanchored literal regexes, so they only
hide a finding whose match contains that exact text. Pre-existing and broader: the path allowlist `docs/evidence/.*` and `tests/fixtures/.*` hides any secret placed there.

## Runs
Environment: `APP_ENV=test`, `DATABASE_URL_TEST`/`DATABASE_ADMIN_URL_TEST` on `quotepilot_sec`; from `apps/api`, `.venv/bin/pytest -q -p no:cacheprovider`.
| Command | Exit | Result |
|---|---|---|
| `tests/security/test_audit_followups8.py tests/security/test_audit_followups7.py -rfEx` | 0 | 76 passed, 2 xfailed (SEC7-1, SEC6-2 per-call latency, accepted) in 63 s |
| `tests/security/test_confirm_round11.py -rfEx` (final) | 0 | 7 passed, 1 xfailed (SEC8-1) in 24 s |
| same file, first runs | 1 | parser test failed on RecursionError (became SEC8-1); `-I` test setup error of mine (fixed); one pgrep observation above |
| `-k item1` twice | 0 / 0 | 1 passed each, numbers above |

## New findings
- SEC8-2, Medium (backlog), inside the diff in the sense that the limiter set was chosen there: `POST /rfqs/{id}/items` has no per-user limit and no worker offload.
  Repro: create an RFQ on a tenant with 30,000 x 300-char products, post items in a loop: each call 1.4-2.1 s, 0 of 6 refused at a limit of 2.
  A single member can keep several calls running; the in-process match holds the GIL (~0.14 s per call) and the catalog load is repeated each time. Fix: apply `limit_rfq_parse`
  (or a separate item limit) and route single-line matching through the same bounded path; cache nothing across tenants.
- SEC8-1, Low (backlog), inside the diff (`matching.py:73`): catch `RecursionError` (or `Exception`) in `_parse_worker_lines`. Not attacker reachable today. Test marked strict xfail.

## Still open for Stage 1 (carried from reverify7, updated)
| ID | Severity | State |
|---|---|---|
| SEC7-1 / SEC6-2 | High to Medium (availability) | MITIGATED: 11.5 s and a responsive API instead of 390 s; residual as listed (in-process <= 50,000 comparisons, 8 s deadline, 2 matchers per process, DB transaction held). I do not consider it release-blocking if the residual is accepted in KNOWN_LIMITATIONS |
| SEC8-2 | Medium | new: add-item route unlimited and in-process |
| SEC7-7 | Medium (design), residual | limiter in place on 3 routes; quote generation/exports unlimited; per-process counters |
| Post-sandbox parsing without a wall-clock timeout (design) | Medium | partly reduced (matching has a deadline; text parsing and cell parsing still do not) |
| SEC8-1 | Low | new |
| SEC7-4 | Low | CLOSED (seed in the working directory still builds `Settings`, as the implementer declares) |
| SEC7-2, SEC7-3, SEC7-5, SEC7-6 | n/a | CLOSED |
| SEC6-6, SEC5-4 | Info | unchanged (collapsed raw_text; mixed digit scripts, `5%` accepted) |
No Critical or High finding that I consider release-blocking is open after this round, subject to the residual in item 1.

## Observations
- RLIMIT application is silent on failure; on a platform that refuses it the worker runs unconstrained apart from the deadline.
- `apps/api/tests/qa/test_qa_confirm_round11.py` is untracked and not mine.

## Not checked
Rate-limit tests of the implementer were not re-run; counting behaviour was read, not re-measured. Parallel load from several users; the 2-slot semaphore under
contention; quote and export route costs; `update_item` matching cost; real PDF/XLSX files; browser bundle, logs and secrets scans; anything outside the diff.
