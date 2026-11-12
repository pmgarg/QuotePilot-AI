"""Stage 1 fix round 9 — integration regressions (seed output, pasted RFQ text timing)."""

from __future__ import annotations

import time

import pytest

from app.db.session import get_sessionmaker
from app.fixtures import seed as seed_module
from tests.conftest import Tenant, login_as

pytestmark = pytest.mark.integration


def test_seed_output_lists_accounts_but_never_the_password(app, capsys) -> None:  # type: ignore[no-untyped-def]
    password = "Local-Only-Seed-Secret-2026!"
    with get_sessionmaker()() as db:
        created = seed_module.seed(db, password, verbose=True)
    out = capsys.readouterr()
    assert created and "owner@northwind.example" in out.out
    assert password not in out.out and password not in out.err


def test_sec5_5_pasted_text_with_long_whitespace_runs_is_answered_quickly(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales = login_as(app, tenant_a.users["sales"])
    text = "\n".join("abc" + " " * 20_000 + "x" for _ in range(5)) + "\n10 pcs Hex bolt SKU-1001\n"
    started = time.perf_counter()
    r = sales.post("/rfqs", json={"text": text, "customer_id": str(tenant_a.customer_id)})
    elapsed = time.perf_counter() - started
    assert r.status_code in (201, 413, 422), r.text[:200]
    assert elapsed < 2.0, elapsed
    if r.status_code == 201:
        quantities = [it["quantity"] for it in r.json()["items"]]
        assert "10" in quantities or "10.000" in quantities, quantities
