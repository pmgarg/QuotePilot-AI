"""Single source of truth for the role → permission matrix (see docs/USER_JOURNEYS.md)."""

from __future__ import annotations

from enum import StrEnum

from app.core.errors import ForbiddenError


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    SALES = "sales"
    APPROVER = "approver"
    READONLY = "readonly"


class Permission(StrEnum):
    MANAGE_ORG = "manage_org"
    MANAGE_MEMBERS = "manage_members"
    IMPORT_CATALOG = "import_catalog"
    EDIT_QUOTES = "edit_quotes"  # create/edit RFQs, quotes, customers
    SUBMIT_QUOTE = "submit_quote"
    APPROVE_QUOTE = "approve_quote"
    OVERRIDE_PRICING = "override_pricing"
    WITHDRAW_QUOTE = "withdraw_quote"
    EXPORT = "export"
    VIEW_COSTS = "view_costs"
    VIEW = "view"
    # Stage 3
    APPROVE_ORDER = "approve_order"  # confirm/cancel orders, decide repeat-order proposals
    MANAGE_CONNECTORS = "manage_connectors"  # connectors, sync, conflict resolution, FX table
    IMPORT_DATA = "import_data"  # CSV imports (customers/orders)
    EXPORT_DATA = "export_data"  # bulk CSV exports of customer/order/product data (PII + commercial data)
    MANAGE_RETENTION = "manage_retention"  # approve/reject deletion requests
    # Stage 4
    USE_ASSISTANT = "use_assistant"  # bounded assistant; tools are further gated per tool permission + plan scope
    MANAGE_WORKFLOWS = "manage_workflows"  # create/validate/activate workflows, run them live
    KILL_SWITCH = "kill_switch"  # engage/release the tenant kill switch
    MANAGE_BILLING = "manage_billing"  # change plan (test-mode adapter)
    VIEW_OPS = "view_ops"  # usage/ops metrics, support export


_MATRIX: dict[Permission, frozenset[Role]] = {
    Permission.MANAGE_ORG: frozenset({Role.OWNER, Role.ADMIN}),
    Permission.MANAGE_MEMBERS: frozenset({Role.OWNER, Role.ADMIN}),
    Permission.IMPORT_CATALOG: frozenset({Role.OWNER, Role.ADMIN}),
    Permission.EDIT_QUOTES: frozenset({Role.OWNER, Role.ADMIN, Role.SALES, Role.APPROVER}),
    Permission.SUBMIT_QUOTE: frozenset({Role.OWNER, Role.ADMIN, Role.SALES, Role.APPROVER}),
    Permission.APPROVE_QUOTE: frozenset({Role.OWNER, Role.ADMIN, Role.APPROVER}),
    Permission.OVERRIDE_PRICING: frozenset({Role.OWNER, Role.ADMIN}),
    Permission.WITHDRAW_QUOTE: frozenset({Role.OWNER, Role.ADMIN, Role.APPROVER}),
    Permission.EXPORT: frozenset(Role),
    Permission.VIEW_COSTS: frozenset({Role.OWNER, Role.ADMIN, Role.APPROVER}),
    Permission.VIEW: frozenset(Role),
    Permission.APPROVE_ORDER: frozenset({Role.OWNER, Role.ADMIN, Role.APPROVER}),
    Permission.MANAGE_CONNECTORS: frozenset({Role.OWNER, Role.ADMIN}),
    Permission.IMPORT_DATA: frozenset({Role.OWNER, Role.ADMIN}),
    Permission.EXPORT_DATA: frozenset({Role.OWNER, Role.ADMIN, Role.APPROVER}),
    Permission.MANAGE_RETENTION: frozenset({Role.OWNER, Role.ADMIN}),
    Permission.USE_ASSISTANT: frozenset(Role),
    Permission.MANAGE_WORKFLOWS: frozenset({Role.OWNER, Role.ADMIN}),
    Permission.KILL_SWITCH: frozenset({Role.OWNER, Role.ADMIN}),
    Permission.MANAGE_BILLING: frozenset({Role.OWNER}),
    Permission.VIEW_OPS: frozenset({Role.OWNER, Role.ADMIN}),
}


def has_permission(role: str, permission: Permission) -> bool:
    try:
        return Role(role) in _MATRIX[permission]
    except ValueError:
        return False


def require_permission(role: str, permission: Permission) -> None:
    if not has_permission(role, permission):
        raise ForbiddenError(f"Role '{role}' may not perform '{permission.value}'", action=permission.value)
