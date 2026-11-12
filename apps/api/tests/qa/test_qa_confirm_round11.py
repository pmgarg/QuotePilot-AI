"""QA confirmation of fix round 11 (narrow): large request path, parser through a catalog import, seed precedence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.services.money import parse_decimal
from tests.conftest import API_DIR, login_as, make_tenant, upload
from tests.integration.test_round11_fixes import MAPPING, _bulk_products
from tests.qa.test_qa_reverify_review7 import PW1, ROOT, _login_ok, _RootEnv, _seed

integration = pytest.mark.integration


def _worker_pids() -> list[str]:
    out = subprocess.run(["pgrep", "-f", "match_worker"], capture_output=True, text=True).stdout.split()
    return [p for p in out if p != str(os.getpid())]


def _lines() -> list[str]:
    ls = []
    for i in range(1, 60):
        ls.append(f"{i} pcs hex bolt size {i * 37} steel zinc")
    ls[10] = "3 pcs SKU-1001"
    ls[40] = "9 pcs BULK-77"
    return ls[:58] + ["2 pcs unobtainium widget", "4 pcs flange nut m10"]


def _post(c, cid, lines):
    r = c.post("/rfqs", json={"text": "\n".join(lines), "customer_id": cid})
    assert r.status_code == 201, r.text
    return r.json()["items"]


@integration
def test_large_request_matches_like_small_requests_and_flags_skipped_lines(app, tenant_a, monkeypatch) -> None:
    from app.core.config import get_settings

    _bulk_products(tenant_a, 5000, 60)
    sales = login_as(app, tenant_a.users["sales"])
    cid = str(tenant_a.customer_id)
    lines = _lines()
    assert len(lines) == 60
    big = _post(sales, cid, lines)  # 5,000+ products x 60 lines > 50,000 -> worker path
    assert len(big) == 60
    small: list[dict] = []
    for i in range(0, 60, 8):  # 8 lines x ~5,000 products < 50,000 -> in-process path
        small += _post(sales, cid, lines[i : i + 8])
    assert len(small) == 60
    flagged = [i for i in big if "match_skipped_budget" in i["flags"]]
    for b, s in zip(big, small, strict=True):
        if "match_skipped_budget" in b["flags"]:
            continue
        bc = [(c["sku"], c["score"], c["rank"]) for c in b["candidates"]]
        sc = [(c["sku"], c["score"], c["rank"]) for c in s["candidates"]]
        assert (b["match_status"], bc) == (s["match_status"], sc), (b["raw_text"], bc, sc)
        assert b["candidates"] == s["candidates"], (b["raw_text"], b["candidates"], s["candidates"])
        diffs = [
            (k, x[k], y[k]) for x, y in zip(b["candidates"], s["candidates"], strict=True) for k in x if x[k] != y[k]
        ]
        assert not diffs, (b["raw_text"], diffs[:3])
    assert big[10]["match_status"] == "exact" and big[40]["match_status"] == "exact"
    assert not flagged, f"{len(flagged)} flagged at the default deadline"
    assert _worker_pids() == []
    # tiny deadline: some lines are skipped and carry the flag, exact SKUs still match
    monkeypatch.setattr(get_settings(), "match_timeout_seconds", 0.05)
    cut = _post(sales, cid, lines)
    skipped = [i for i in cut if "match_skipped_budget" in i["flags"]]
    assert skipped, "no line was skipped with a 0.05 s deadline"
    assert all(i["match_status"] == "unmatched" and not i["candidates"] for i in skipped)
    assert cut[10]["match_status"] == "exact" and cut[40]["match_status"] == "exact"
    assert _worker_pids() == []


CELLS = {
    "1234,567": D("1234.567"), "1234,567,890": None, "0 500": None, "05 050": None, ". 5": None, "..-5": None,
    ".5": D("0.5"), "$.50": D("0.50"), "Rs. 500": D("500"), "1,234": D("1234"), "1,234.56": D("1234.56"),
    "1.234,56": D("1234.56"), "1 234,56": D("1234.56"), "0,500": D("0.500"), "12,5": D("12.5"),
}


def test_parser_cells_direct() -> None:
    for cell, expected in CELLS.items():
        try:
            got = parse_decimal(cell)
        except Exception:
            got = None
        assert got == expected, (cell, got, expected)


@integration
def test_parser_cells_through_catalog_import(app) -> None:
    t = make_tenant("QA11 Parse", with_catalog=False)
    admin = login_as(app, t.users["admin"])
    rows = [f"P{i},Item {i},EA,AED,\"{c}\",1,1" for i, c in enumerate(CELLS)]
    content = b"SKU,Description,Unit,Currency,List Price,Cost,Stock\n" + ("\n".join(rows) + "\n").encode()
    r = upload(admin, "/catalogs/import", "c.csv", content, {"mapping": MAPPING, "catalog_name": "M"})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    cat = admin.get(f"/catalogs/versions/{body['id']}/products?page_size=100")
    text = json.dumps(body)
    stored = {}
    if cat is not None and cat.status_code == 200:
        data = cat.json()
        data = data.get("items", data) if isinstance(data, dict) else data
        stored = {p["sku"]: D(str(p["list_price"])) for p in data}
    rejected = [c for c, e in CELLS.items() if e is None]
    ok = [(f"P{i}", e) for i, (c, e) in enumerate(CELLS.items()) if e is not None]
    assert stored, f"could not read stored products; response: {text[:600]}"
    for sku, e in ok:
        assert stored.get(sku) == e, (sku, stored.get(sku), e)
    for i, c in enumerate(CELLS):
        if c in rejected:
            assert f"P{i}" not in stored, (c, stored.get(f"P{i}"))


PW_ENV, PW_CWD = "Qa11-env-9xQ!", "Qa11-cwd-7zT!"


def _no_leak(r) -> None:
    assert not any(p in r.stdout + r.stderr for p in (PW_ENV, PW_CWD, PW1)) and "Traceback" not in r.stderr


@integration
def test_seed_empty_env_falls_to_cwd_env_over_root_env(app) -> None:
    with tempfile.TemporaryDirectory() as d, _RootEnv(f"DEMO_PASSWORD={PW1}\n"):
        (Path(d) / ".env").write_text(f"DEMO_PASSWORD={PW_CWD}\n")
        r = _seed({"DEMO_PASSWORD": ""}, cwd=Path(d), pythonpath=True)
    assert r.returncode == 0, r.stderr[-300:]
    assert _login_ok(app, PW_CWD) and not _login_ok(app, PW1)
    _no_leak(r)


@integration
def test_seed_env_beats_files(app) -> None:
    with tempfile.TemporaryDirectory() as d, _RootEnv(f"DEMO_PASSWORD={PW1}\n"):
        (Path(d) / ".env").write_text(f"DEMO_PASSWORD={PW_CWD}\n")
        r = _seed({"DEMO_PASSWORD": PW_ENV}, cwd=Path(d), pythonpath=True)
    assert r.returncode == 0 and _login_ok(app, PW_ENV) and not _login_ok(app, PW_CWD)
    _no_leak(r)


@integration
def test_seed_empty_env_and_no_cwd_file_uses_root_env(app) -> None:
    with tempfile.TemporaryDirectory() as d, _RootEnv(f"DEMO_PASSWORD={PW1}\n"):
        r = _seed({"DEMO_PASSWORD": ""}, cwd=Path(d), pythonpath=True)
    assert r.returncode == 0 and _login_ok(app, PW1)
    _no_leak(r)


@integration
def test_seed_undecodable_root_env_is_absent_exit_2_no_traceback(app) -> None:
    with tempfile.TemporaryDirectory() as d, _RootEnv("x"):
        (ROOT / ".env").write_bytes(b"DEMO_PASSWORD=\xff\xfe\x80bad\n")
        r = _seed({}, cwd=Path(d), pythonpath=True)
    assert r.returncode == 2
    _no_leak(r)
    assert not (ROOT / ".env").exists()
