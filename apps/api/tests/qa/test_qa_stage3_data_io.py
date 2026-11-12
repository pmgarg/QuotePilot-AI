"""QA (independent) — S3-F05 CSV import/export: S3-A02 (cost/margin hidden from Sales/Read-only in exports),
S3-A06 (unauthorized export → 403 *and* an audit row), S3-A07 (partial-import recovery incl. the failed-rows CSV
round trip and idempotency keys) and formula-injection neutralisation (CLAUDE.md export rule)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from tests.conftest import Tenant, login_as
from tests.qa.test_qa_stage3_helpers import (
    approved_quote,
    audit_rows,
    connector,
    csv_bytes,
    import_csv,
    parse_csv,
    sync,
)

pytestmark = pytest.mark.integration

DANGEROUS = ['=HYPERLINK("http://evil.example")', "+1+1", "-2+3", "@SUM(A1:A9)", "\tTabbed", "\rCarriage"]


# ------------------------------------------------------------ S3-A06 + S3-A02
def test_unauthorized_export_is_403_and_audited_with_actor_and_entity(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    for role in ("sales", "readonly"):
        ac = login_as(app, tenant_a.users[role])
        for entity in ("customers", "orders", "products"):
            r = ac.get(f"/data/export/{entity}")
            assert r.status_code == 403 and r.json()["error"] == "forbidden" and r.json()["action"] == "export_data"
            assert "text/csv" not in r.headers.get("content-type", "")
        denied = [a for a in audit_rows(tenant_a.org_id, "export.denied") if a["actor"] == ac.user_id]
        assert sorted(a["summary"]["entity"] for a in denied) == ["customers", "orders", "products"], role
        assert all(a["summary"]["role"] == role for a in denied)
    assert len(audit_rows(tenant_a.org_id, "export.denied")) == 6
    # unknown entity is a 404 for everyone (no oracle, no audit noise)
    assert login_as(app, tenant_a.users["owner"]).get("/data/export/users").status_code == 404
    assert len(audit_rows(tenant_a.org_id, "export.")) == 6
    # allowed roles: audited with include_costs flag
    for role in ("owner", "admin", "approver"):
        ac = login_as(app, tenant_a.users[role])
        r = ac.get("/data/export/customers")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv"), role
        assert r.headers["cache-control"] == "no-store" and "attachment" in r.headers["content-disposition"]
    allowed = [a for a in audit_rows(tenant_a.org_id, "export.customers")]
    assert len(allowed) == 3 and all(a["summary"]["include_costs"] is True for a in allowed)


def test_cost_columns_only_for_view_costs_roles_and_margin_arithmetic(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    sales, approver, q = approved_quote(app, tenant_a)
    sales.post(f"/quotes/{q['id']}/orders", json={})
    owner = login_as(app, tenant_a.users["owner"])
    header, rows = parse_csv(owner.get("/data/export/orders").content)
    assert header[-2:] == ["cost_price", "margin_pct"]
    bolt = next(r for r in rows if r["sku"] == "SKU-1001")
    # (0.85 − 0.52) / 0.85 = 38.8235 % → 38.82 (2 dp), copied cost 0.52
    assert Decimal(bolt["cost_price"]) == Decimal("0.52") and bolt["margin_pct"] == "38.82"
    assert Decimal(bolt["line_total"]) == Decimal("446.25") and Decimal(bolt["unit_price"]) == Decimal("0.85")
    header_p, prod = parse_csv(owner.get("/data/export/products").content)
    assert header_p[-1] == "cost_price" and next(p for p in prod if p["sku"] == "SKU-1001")["cost_price"] == "0.520000"
    header_c, _ = parse_csv(owner.get("/data/export/customers").content)
    assert not any("cost" in h or "margin" in h for h in header_c)
    # approver has view_costs; nobody without it can obtain a CSV at all (S3-A02 enforced by the gate)
    assert "cost_price" in parse_csv(approver.get("/data/export/products").content)[0]
    for role in ("sales", "readonly"):
        ac = login_as(app, tenant_a.users[role])
        assert all(ac.get(f"/data/export/{e}").status_code == 403 for e in ("orders", "products"))
        # and the JSON views they *can* reach carry no cost either
        assert (
            all("cost_price" not in p for p in ac.get("/products").json())
            if ac.get("/products").status_code == 200
            else True
        )


# ---------------------------------------------------------- formula safety
def test_exports_neutralise_formula_prefixes_in_every_user_controlled_cell(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    for i, name in enumerate(DANGEROUS):
        r = admin.post("/customers", json={"name": name, "reference": f"D-{i}", "notes": "=cmd|' /C calc'!A0"})
        assert r.status_code == 201, r.text
    raw = admin.get("/data/export/customers").content
    _, rows = parse_csv(raw)
    cells = {r["name"]: r for r in rows}
    for name in DANGEROUS:
        assert name not in cells, f"{name!r} exported verbatim"
        assert "'" + name in cells, f"{name!r} not neutralised with a leading apostrophe"
        assert cells["'" + name]["notes"] == "'=cmd|' /C calc'!A0"
    text = raw.decode("utf-8-sig")
    assert text.startswith('"id"') and "\r\n" in text  # QUOTE_ALL, CRLF
    # product description changed through the conflict queue is neutralised in the products export
    conn = connector(admin, ["read:products"])
    run = sync(admin, conn["id"], "products", "p1", [{"sku": "SKU-1002", "description": "=EVIL()"}]).json()
    assert run["conflicts"] == 1
    c = admin.get("/sync-conflicts").json()[0]
    assert admin.post(f"/sync-conflicts/{c['id']}/resolve", json={"resolution": "apply_remote"}).status_code == 200
    prod = admin.get("/data/export/products").content.decode("utf-8-sig")
    assert '"\'=EVIL()"' in prod and '"=EVIL()"' not in prod
    # the failed-rows CSV is an export too
    bad = csv_bytes(["name", "reference", "default_currency"], [['=HYPERLINK("x")', "Z-1", "ZZZ"]])
    run = import_csv(admin, "customers", "fr-1", bad).json()
    assert run["rejected"] == 1
    failed = admin.get(f"/data/runs/{run['id']}/failed.csv").content.decode("utf-8-sig")
    assert "\"'=HYPERLINK" in failed and '"=HYPERLINK' not in failed


# -------------------------------------------------------------- S3-A07
def test_partial_import_then_fix_and_reupload_the_failed_rows_file(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    before = {c["name"] for c in admin.get("/customers").json()}
    content = csv_bytes(
        ["Name", "Reference", "Email", "Default_Currency", "notes"],  # header case-insensitive
        [
            ["Good Co", "GC-1", "buyer@good.example", "aed", "ok"],
            ["", "NON-1", "", "", "name missing"],
            ["Bad Mail", "BM-1", "not-an-email", "USD", ""],
            ["Dup Ref", "GC-1", "", "", "duplicate inside the same file"],
            ["Bad Currency", "BC-1", "", "ZZZ", ""],
            ["Fine Ltd", "", "", "", "no reference is fine"],
        ],
    )
    r = import_csv(admin, "customers", "imp-1", content)
    assert r.status_code == 201, r.text
    run = r.json()
    assert (run["received"], run["applied"], run["rejected"], run["status"]) == (6, 2, 4, "partial")
    assert {f["row"]: f["errors"] for f in run["report"]["failed_rows"]} == {
        3: ["name is required"],
        4: ["invalid email"],
        5: ["reference GC-1 already exists"],
        6: ["unsupported currency ZZZ"],
    }
    after = {c["name"]: c for c in admin.get("/customers").json()}
    assert set(after) - before == {"Good Co", "Fine Ltd"}
    assert after["Good Co"]["default_currency"] == "AED" and after["Good Co"]["reference"] == "GC-1"
    # failed-rows CSV: original header + _row/_errors, one line per failed row, values preserved
    failed = admin.get(f"/data/runs/{run['id']}/failed.csv")
    assert failed.status_code == 200 and failed.headers["content-type"].startswith("text/csv")
    header, rows = parse_csv(failed.content)
    assert header == ["name", "reference", "email", "default_currency", "notes", "_row", "_errors"]
    assert [r["_row"] for r in rows] == ["3", "4", "5", "6"]
    assert rows[1]["name"] == "Bad Mail" and rows[1]["_errors"] == "invalid email"
    # fix in place and re-upload the *downloaded file* (extra _row/_errors columns must be tolerated)
    rows[0]["name"] = "No Name Co"
    rows[1]["email"] = "buyer@badmail.example"
    rows[2]["reference"] = "DR-1"
    rows[3]["default_currency"] = "EUR"
    fixed = csv_bytes(header, [[r[h] for h in header] for r in rows])
    r2 = import_csv(admin, "customers", "imp-2", fixed)
    assert r2.status_code == 201, r2.text
    assert (r2.json()["applied"], r2.json()["rejected"], r2.json()["status"]) == (4, 0, "completed")
    names = {c["name"] for c in admin.get("/customers").json()}
    assert {"No Name Co", "Bad Mail", "Dup Ref", "Bad Currency"} <= names
    assert len(admin.get("/data/runs").json()) == 2
    acts = audit_rows(tenant_a.org_id, "import.customers")
    assert [a["summary"]["applied"] for a in acts] == [2, 4] and all(a["actor"] == admin.user_id for a in acts)


def test_import_idempotency_key_semantics(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    first = csv_bytes(["name", "reference"], [["Alpha One", "A-1"]])
    second = csv_bytes(["name", "reference"], [["Alpha Two", "A-2"]])
    run = import_csv(admin, "customers", "same-key", first).json()
    assert run["applied"] == 1
    replay = import_csv(admin, "customers", "same-key", second)
    assert replay.status_code == 201 and replay.json()["id"] == run["id"]  # replay returns the original outcome
    names = {c["name"] for c in admin.get("/customers").json()}
    assert "Alpha One" in names and "Alpha Two" not in names  # nothing applied on replay
    fresh = import_csv(admin, "customers", "other-key", second).json()
    assert fresh["id"] != run["id"] and fresh["applied"] == 1
    # the key is mandatory and bounded
    r = admin.post("/data/import/customers", files={"file": ("c.csv", first, "text/csv")}, data={})
    assert r.status_code == 422
    assert import_csv(admin, "customers", "k" * 161, first).status_code == 422
    # rejected-before-run uploads (bad header, empty, wrong encoding) create no run and leave the key reusable
    assert import_csv(admin, "customers", "reuse", b"foo,bar\r\n1,2\r\n").status_code == 422
    assert import_csv(admin, "customers", "reuse", b"").status_code == 422
    assert import_csv(admin, "customers", "reuse", b"\xff\xfe\x00name").status_code == 422
    assert import_csv(admin, "customers", "reuse", first).status_code == 201
    assert len(admin.get("/data/runs").json()) == 3


def test_import_size_and_role_limits(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    for role in ("sales", "approver", "readonly"):
        ac = login_as(app, tenant_a.users[role])
        assert import_csv(ac, "customers", f"r-{role}", b"name\r\nX\r\n").status_code == 403, role
        assert ac.get("/data/runs").status_code == 403
    big = b"name\r\n" + b"A" * (5 * 1024 * 1024)
    assert import_csv(admin, "customers", "big", big).status_code == 413
    many = b"name\r\n" + b"x\r\n" * 10_001
    assert import_csv(admin, "customers", "many", many).status_code == 413
    assert (
        import_csv(admin, "products", "p", b"name\r\nX\r\n").status_code == 404
    )  # products go through catalog versions
    assert import_csv(admin, "invoices", "i", b"name\r\nX\r\n").status_code == 404
    assert admin.get("/data/runs").json() == []
    assert admin.get(f"/data/runs/{uuid.uuid4()}/failed.csv").status_code == 404


def test_orders_import_creates_proposed_orders_with_exact_totals_and_per_row_errors(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    admin = login_as(app, tenant_a.users["admin"])
    owner = login_as(app, tenant_a.users["owner"])
    sales = login_as(app, tenant_a.users["sales"])
    content = csv_bytes(
        [
            "order_ref",
            "customer_reference",
            "sku",
            "description",
            "quantity",
            "unit",
            "unit_price",
            "currency",
            "tax_rate_pct",
        ],
        [
            ["PO-1", "CUST-1", "SKU-1001", "Bolt", "100", "EA", "0.85", "AED", "5"],
            ["PO-1", "CUST-1", "SKU-1003", "Nut", "100", "EA", "0.25", "AED", "5"],
            ["PO-2", "", "SKU-1001", "Bolt", "3", "EA", "0.333", "AED", ""],  # org default tax 5 %
            ["PO-3", "", "SKU-1001", "Bolt", "1", "EA", "0.85", "AED", "101"],
            ["PO-4", "", "", "", "1", "EA", "0.85", "AED", ""],
            ["PO-5", "", "SKU-1001", "Bolt", "1", "EA", "0.85", "USD", ""],
            ["PO-5", "", "SKU-1003", "Nut", "1", "EA", "0.25", "AED", ""],  # mixed currencies in one order
            ["PO-6", "GHOST", "SKU-1001", "Bolt", "1", "EA", "0.85", "AED", ""],
            ["", "", "SKU-1001", "Bolt", "1", "EA", "0.85", "AED", ""],
        ],
    )
    r = import_csv(admin, "orders", "po-1", content)
    assert r.status_code == 201, r.text
    run = r.json()
    assert (run["received"], run["applied"], run["rejected"], run["status"]) == (9, 3, 6, "partial")
    errors = {f["row"]: f["errors"] for f in run["report"]["failed_rows"]}
    assert errors[5] == ["tax_rate_pct must be between 0 and 100"]
    assert errors[6] == ["sku or description required"]
    assert errors[8] == ["all lines of an order must share one currency"] and errors[7] == ["order rejected"]
    assert errors[9] == ["unknown customer_reference GHOST"] and errors[10] == ["order_ref is required"]
    orders = {o["external_ref"] if "external_ref" in o else None: o for o in []}
    listed = owner.get("/orders").json()
    assert len(listed) == 2 and all(o["status"] == "proposed" and o["source"] == "import" for o in listed)
    details = {d["external_ref"]: d for d in (owner.get(f"/orders/{o['id']}").json() for o in listed)}
    po1, po2 = details["PO-1"], details["PO-2"]
    # 85.00 + 4.25 + 25.00 + 1.25 = 115.50 ; 3 × 0.333 = 0.999 → 1.00 net, tax 0.05 → 1.05
    assert (po1["subtotal"], po1["tax_total"], po1["grand_total"]) == ("110", "5.5", "115.5")
    assert po1["customer_id"] == str(tenant_a.customer_id) and [ln["line_no"] for ln in po1["lines"]] == [1, 2]
    assert (po2["subtotal"], po2["tax_total"], po2["grand_total"]) == ("1", "0.05", "1.05")
    assert po2["customer_id"] is None and po2["lines"][0]["tax_rate_pct"] == "5"
    # imported orders still pass the gate: importer cannot confirm, owner can; sales sees no cost columns
    assert admin.post(f"/orders/{po1['id']}/confirm", json={}).status_code == 403
    assert owner.post(f"/orders/{po1['id']}/confirm", json={}).status_code == 200
    assert all("cost_price" not in ln for ln in sales.get(f"/orders/{po1['id']}").json()["lines"])
    # same order_ref in a later file is rejected, not duplicated
    again = import_csv(
        admin,
        "orders",
        "po-2",
        csv_bytes(["order_ref", "sku", "quantity", "unit_price"], [["PO-1", "SKU-1001", "1", "1"]]),
    ).json()
    assert again["applied"] == 0 and again["report"]["failed_rows"][0]["errors"] == ["order_ref PO-1 already imported"]
    assert len(owner.get("/orders").json()) == 2 and orders == {}


def test_oversized_field_values_are_rejected_per_row_not_as_a_crash(app, tenant_a: Tenant) -> None:  # type: ignore[no-untyped-def]
    """S3-A07: every row is validated independently; a value the schema cannot hold must land in the failed
    rows report (fix-and-re-upload) and must not abort the import of the other rows."""
    admin = login_as(app, tenant_a.users["admin"])
    content = csv_bytes(
        ["order_ref", "sku", "quantity", "unit", "unit_price"],
        [
            ["PO-OK", "SKU-1001", "1", "EA", "0.85"],
            ["PO-UNIT", "SKU-1001", "1", "CARTONS_OF_TWELVE_PIECES", "0.85"],  # unit column holds 16 chars
            ["PO-" + "R" * 120, "SKU-1001", "1", "EA", "0.85"],  # external_ref column holds 100 chars
        ],
    )
    r = import_csv(admin, "orders", "big-fields", content)
    assert r.status_code == 201, f"import must not fail as a whole: HTTP {r.status_code} {r.text[:200]}"
    run = r.json()
    assert run["applied"] == 1 and run["rejected"] == 2, run
    assert sorted(f["row"] for f in run["report"]["failed_rows"]) == [3, 4]
    assert [o["status"] for o in admin.get("/orders").json()] == ["proposed"]
