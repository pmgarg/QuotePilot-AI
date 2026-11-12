"""Security confirmation of fix round 11 (matching worker, rate limits). Narrow checks, synthetic data only."""

from __future__ import annotations

import json
import os
import sys
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.services import matching
from tests.conftest import API, Tenant, login_as
from tests.integration.test_round11_fixes import _adversarial_lines, _bulk_products

integration = pytest.mark.integration


def _pgrep() -> str:
    return subprocess.run(["pgrep", "-f", "app.services.match_worker"], capture_output=True, text=True).stdout.strip()


def test_worker_output_parser_rejects_hostile_lines() -> None:
    p = matching._parse_worker_lines
    good = b'{"i": 0, "hits": [[91.0, 1]]}\n'
    assert p(good, 2, 3) == {0: [(91.0, 1)]}
    assert p(b'{"i": 0, "hits": [[91.0, 3]]}\n', 2, 3) == {}  # index out of range
    assert p(b'{"i": 0, "hits": [[91.0, -1]]}\n', 2, 3) == {}
    assert p(b'{"i": 0, "hits": [["x", 1]]}\n', 2, 3) == {}  # non numeric score
    assert p(good + good, 2, 3) == {0: [(91.0, 1)]}  # duplicate line index dropped
    assert p(b'{"i": 1, "hits": []}\n', 2, 3) == {}  # skipped index
    assert p(b"\xff\xfe\x00garbage\n" + good, 2, 3) == {}
    assert p(good + b'{"i": 1, "hits": [[5', 2, 3) == {0: [(91.0, 1)]}  # torn tail


@pytest.mark.xfail(strict=True, reason="SEC8-1 (Low, defence in depth): RecursionError from deeply nested worker output is not caught")
def test_sec8_1_deeply_nested_worker_output_does_not_raise() -> None:
    assert matching._parse_worker_lines(b"[" * 100000 + b"\n", 2, 3) == {}


def test_worker_launch_is_argument_safe_and_failure_leaves_lines_unmatched(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        seen.update(cmd=cmd, kw=kw)
        return subprocess.CompletedProcess(cmd, -9, stdout=b"", stderr=b"")

    monkeypatch.setattr(matching.subprocess, "run", fake_run)
    monkeypatch.setenv("SECRET_KEY", "must-not-leak-into-worker-env-0123456789abcdef")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    descs = [f"d{i}" for i in range(60_000)]
    assert matching.fuzzy_batch(descs, ["q1", "q2"]) == {}  # killed worker: nothing answered
    assert seen["cmd"][1:] == ["-I", "-m", "app.services.match_worker"]
    assert not seen["kw"].get("shell")
    assert set(seen["kw"]["env"]) <= {"PATH", "PYTHONPATH", "LANG", "VIRTUAL_ENV"}
    payload = json.loads(seen["kw"]["input"])
    assert set(payload) == {"descriptions", "queries", "limit", "cutoff", "cpu_seconds", "memory_bytes"}
    assert seen["kw"]["timeout"] <= get_settings().match_timeout_seconds


def test_worker_rejects_bad_stdin_and_real_timeout_kills_child() -> None:
    r = subprocess.run(
        [sys.executable, "-I", "-m", "app.services.match_worker"],
        input=b"not json",
        capture_output=True,
        env={"PYTHONPATH": os.getcwd(), "PATH": os.environ["PATH"]},
        timeout=20,
    )
    assert r.returncode == 2 and r.stdout == b""
    assert _pgrep() == ""


def test_production_defaults_intact() -> None:
    f = Settings.model_fields
    assert f["rfq_parse_rate_limit_attempts"].default == 30
    assert f["catalog_import_rate_limit_attempts"].default == 10
    assert f["expensive_rate_limit_window_seconds"].default == 300
    assert f["match_timeout_seconds"].default == 8.0


@integration
def test_item1_worst_case_end_to_end_and_add_item_cost(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    _bulk_products(tenant_a, 30_000, 300)
    sales = login_as(app, tenant_a.users["sales"])
    probe = TestClient(app, base_url="http://testserver")
    lat: list[float] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            t0 = time.perf_counter()
            probe.get(f"{API}/health")
            lat.append(time.perf_counter() - t0)
            time.sleep(0.01)

    th = threading.Thread(target=poll)
    th.start()
    time.sleep(0.3)
    before = len(lat)
    t0 = time.perf_counter()
    r = sales.post("/rfqs", json={"text": _adversarial_lines(450), "customer_id": str(tenant_a.customer_id)})
    elapsed = time.perf_counter() - t0
    stop.set()
    th.join()
    assert r.status_code == 201
    items = r.json()["items"]
    skipped = sum("match_skipped_budget" in i["flags"] for i in items)
    during = lat[before:]
    survivors = _pgrep()
    detail = subprocess.run(["ps", "-o", "pid,ppid,stat,etimes,args", "-p", ",".join(survivors.split()) or "1"], capture_output=True, text=True).stdout
    time.sleep(1.5)
    later = _pgrep()
    # route NOT limited and not worker-bounded: manual add of an item = full catalog load + in-process match
    times = []
    rfq_id = r.json()["id"]
    for _ in range(3):
        t1 = time.perf_counter()
        a = sales.post(f"/rfqs/{rfq_id}/items", json={"description": "hex bolt size 77 steel zinc plated " * 5, "quantity": "1"})
        times.append(time.perf_counter() - t1)
        assert a.status_code in (200, 201), a.text
    with open("/tmp/claude-0/-home-user-QuotePilot-AI/98b61cc2-31d0-51f6-a519-b934aa445354/scratchpad/item1.json", "w") as fh:
        json.dump(
            {
                "elapsed": elapsed, "lines": len(items), "matched": len(items) - skipped, "skipped": skipped,
                "probes": len(during), "max": max(during), "mean": sum(during) / len(during),
                "survivors": survivors, "detail": detail, "later": later, "add_item_seconds": times,
            },
            fh,
        )
    assert later == ""


@integration
def test_add_item_is_not_rate_limited(app, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "rfq_parse_rate_limit_attempts", 2)
    sales = login_as(app, tenant_a.users["sales"])
    r = sales.post("/rfqs", json={"text": "5 pcs SKU-1001", "customer_id": str(tenant_a.customer_id)})
    rfq_id = r.json()["id"]
    codes = [
        sales.post(f"/rfqs/{rfq_id}/items", json={"description": "hex bolt", "quantity": "1"}).status_code
        for _ in range(6)
    ]
    assert 429 not in codes, codes


@integration
def test_worker_input_contains_only_callers_catalog(app, tenant_a: Tenant, tenant_b: Tenant, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    _bulk_products(tenant_a, 3_000, 100)
    _bulk_products(tenant_b, 3_000, 100)
    from sqlalchemy import create_engine, text
    import os

    eng = create_engine(os.environ["DATABASE_ADMIN_URL"], isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text("UPDATE products SET description='tenantbmarker zzz' WHERE organization_id=:o AND sku='BULK-5'"), {"o": str(tenant_b.org_id)})
    eng.dispose()
    sent: list[list[str]] = []
    orig = matching.fuzzy_batch

    def spy(d, q):  # type: ignore[no-untyped-def]
        sent.append(list(d))
        return orig(d, q)

    monkeypatch.setattr(matching, "fuzzy_batch", spy)
    sales = login_as(app, tenant_a.users["sales"])
    r = sales.post("/rfqs", json={"text": "\n".join(f"hex bolt size {i} steel" for i in range(30)), "customer_id": str(tenant_a.customer_id)})
    assert r.status_code == 201
    assert sent and all("tenantbmarker" not in x for batch in sent for x in batch)
    assert not any(k for k in vars(matching) if k.lower().startswith("_cache"))
