"""Stage 4 fix round 3 (after release review 1): S4-RR1-4, S4-RR1-5, S4-RR1-7. Implementing side's regression tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import get_sessionmaker, set_tenant
from app.services.platform import assistant
from app.services.platform.assistant import AnthropicPlanner
from app.services.platform.workflows import explanation_without_user_text
from tests.conftest import AuthedClient, Tenant, login_as

pytestmark = pytest.mark.integration


class _Raising:
    name = "anthropic"

    def plan(self, text: str, tools: list[Any]) -> Any:
        raise TimeoutError("provider timed out: secret-canary-123")


def _fake_hosted(raw: str) -> AnthropicPlanner:
    p = AnthropicPlanner.__new__(AnthropicPlanner)
    messages = SimpleNamespace(
        create=lambda **_: SimpleNamespace(
            content=[SimpleNamespace(text=raw)], usage=SimpleNamespace(input_tokens=1, output_tokens=1)
        )
    )
    p._p = SimpleNamespace(_client=SimpleNamespace(messages=messages), _model="fake")  # type: ignore[attr-defined]
    return p


def test_provider_failure_is_a_reply_not_a_500_and_is_recorded(
    app: Any, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    login_as(app, tenant_a.users["sales"]).get("/platform/plan")
    monkeypatch.setattr(assistant, "planner_for", lambda org: _Raising())
    c = AuthedClient(
        TestClient(app, base_url="http://testserver", raise_server_exceptions=False), tenant_a.users["sales"]
    )
    sid = c.post("/assistant/sessions", json={}).json()["id"]
    r = c.post(f"/assistant/sessions/{sid}/messages", json={"text": "hello"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert "temporarily unavailable" in body["content"] and "secret-canary-123" not in r.text
    assert body["proposed_actions"] == []
    with get_sessionmaker()() as db:
        set_tenant(db, tenant_a.org_id, None)
        outcomes = [o for (o,) in db.execute(text("SELECT outcome FROM ai_usage ORDER BY created_at"))]
        invocations = db.scalar(text("SELECT count(*) FROM tool_invocations"))
        db.rollback()
    assert outcomes == ["error"] and invocations == 0


@pytest.mark.parametrize("args", [[], 0, "", "x=1", [1, 2]])
def test_hosted_planner_passes_wrong_typed_args_on_for_rejection(args: Any) -> None:
    raw = json.dumps({"reply": "r", "actions": [{"tool": "search_quotes", "args": args}]})
    plan = _fake_hosted(raw).plan("hello", [])
    assert [a.args for a in plan.actions] == [args]  # never coerced to {}


def test_hosted_planner_treats_missing_or_null_args_as_empty() -> None:
    raw = json.dumps({"reply": "r", "actions": [{"tool": "search_quotes"}, {"tool": "kpis_summary", "args": None}]})
    assert [a.args for a in _fake_hosted(raw).plan("hello", []).actions] == [{}, {}]


@pytest.mark.parametrize(
    ("explanation", "exported"),
    [
        ("step 'order' rejected: ACME asked us to stop", "step 'order' rejected"),
        ("step 'send' failed: NotFoundError: Quote for ACME Trading LLC not found", "step 'send' failed"),
        (
            "step 'send' failed (StateError: draft for jane@acme.example); compensated: order cancelled",
            "step 'send' failed",
        ),
        ("kill switch engaged: customer ACME complained", "kill switch engaged"),
        ("kill switch engaged during the run", "kill switch engaged"),
        ("step budget 8 exhausted", "step budget 8 exhausted"),
        ("something new that quotes ACME", "other"),
        (None, ""),
        ("", ""),
    ],
)
def test_support_export_explanations_are_allowlisted(explanation: str | None, exported: str) -> None:
    assert explanation_without_user_text(explanation) == exported
