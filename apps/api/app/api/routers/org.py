from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DB, PrincipalDep
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.security import hash_password
from app.models.org import Membership, Organization, User
from app.schemas.common import (
    MemberCreate,
    MemberOut,
    MemberUpdate,
    OrganizationPrivileged,
    OrganizationPublic,
    OrganizationUpdate,
)
from app.services import audit
from app.services.auth import revoke_user_sessions
from app.services.authz import Permission, has_permission
from app.services.money import is_supported_currency

router = APIRouter(prefix="/org", tags=["organization"])


def _jsonable(v: object) -> object:
    return v if isinstance(v, str | int | float | bool) or v is None else str(v)


@router.get("", response_model=OrganizationPrivileged | OrganizationPublic)
def get_org(principal: PrincipalDep) -> OrganizationPrivileged | OrganizationPublic:
    if has_permission(principal.role, Permission.VIEW_COSTS):
        return OrganizationPrivileged.model_validate(principal.organization)
    return OrganizationPublic.model_validate(principal.organization)


@router.patch("", response_model=OrganizationPrivileged)
def update_org(payload: OrganizationUpdate, principal: PrincipalDep, db: DB) -> OrganizationPrivileged:
    principal.require(Permission.MANAGE_ORG)
    org = principal.organization
    data = payload.model_dump(exclude_unset=True)
    if "default_currency" in data and data["default_currency"] is not None:
        data["default_currency"] = data["default_currency"].upper()
        if not is_supported_currency(data["default_currency"]):
            raise ValidationFailed("Unsupported currency", field="default_currency")
    if "allow_self_approval" in data and data["allow_self_approval"] and principal.role != "owner":
        raise ValidationFailed("Only an owner can enable self-approval", field="allow_self_approval")
    consent = data.pop("ai_processing_consent", None)
    if consent is not None:
        if principal.role != "owner":
            raise ValidationFailed("Only an owner can change AI processing consent", field="ai_processing_consent")
        from app.db.base import utcnow

        org.ai_processing_consented_at = utcnow() if consent else None
        org.ai_processing_consented_by = principal.user.id if consent else None
        data["ai_processing_consent"] = consent
    before = {k: _jsonable(getattr(org, k)) for k in data if hasattr(org, k)}
    for k, v in data.items():
        if hasattr(org, k):
            setattr(org, k, v)
    db.flush()
    audit.record(
        db,
        organization_id=org.id,
        actor_user_id=principal.user.id,
        action="org.update",
        entity_type="organization",
        entity_id=org.id,
        ip=principal.ip,
        summary={"fields": sorted(data.keys()), "before": before, "after": {k: _jsonable(v) for k, v in data.items()}},
    )
    db.refresh(org)
    return OrganizationPrivileged.model_validate(org)


@router.get("/members", response_model=list[MemberOut])
def list_members(principal: PrincipalDep, db: DB) -> list[MemberOut]:
    principal.require(Permission.MANAGE_MEMBERS)
    rows = db.scalars(
        select(Membership).where(Membership.organization_id == principal.org_id).order_by(Membership.created_at)
    ).all()
    return [MemberOut.model_validate(m) for m in rows]


@router.post("/members", response_model=MemberOut, status_code=201)
def create_member(payload: MemberCreate, principal: PrincipalDep, db: DB) -> MemberOut:
    principal.require(Permission.MANAGE_MEMBERS)
    if payload.role == "owner" and principal.role != "owner":
        raise ValidationFailed("Only an owner can create another owner", field="role")
    email = payload.email.strip().lower()
    if "@" not in email:
        raise ValidationFailed("Invalid email", field="email")
    # SEC-01: an admin may only create NEW local accounts. Attaching an existing account (possibly another
    # tenant's user) would reuse that user's credentials and leak its profile; the response is deliberately
    # the same whether the address belongs to this organisation or to anyone else (no enumeration oracle).
    from app.db.session import set_auth_scope

    set_auth_scope(db, login_email=email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise ConflictError("This email address cannot be used for a new member")
    user = User(email=email, password_hash=hash_password(payload.password), full_name=payload.full_name.strip())
    db.add(user)
    db.flush()
    membership = Membership(organization_id=principal.org_id, user_id=user.id, role=payload.role)
    db.add(membership)
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="member.create",
        entity_type="membership",
        entity_id=membership.id,
        ip=principal.ip,
        summary={"role": payload.role},
    )
    db.refresh(membership)
    return MemberOut.model_validate(membership)


@router.patch("/members/{member_id}", response_model=MemberOut)
def update_member(member_id: uuid.UUID, payload: MemberUpdate, principal: PrincipalDep, db: DB) -> MemberOut:
    principal.require(Permission.MANAGE_MEMBERS)
    # L-3: serialise concurrent member changes within the organisation (last-owner guard is read-then-write)
    db.execute(select(Organization.id).where(Organization.id == principal.org_id).with_for_update())
    membership = db.scalar(
        select(Membership).where(Membership.id == member_id, Membership.organization_id == principal.org_id)
    )
    if membership is None:
        raise NotFoundError("Member not found")
    owners = db.scalars(  # only ACTIVE owner memberships count for the last-owner guards
        select(Membership).where(
            Membership.organization_id == principal.org_id, Membership.role == "owner", Membership.is_active.is_(True)
        )
    ).all()
    changes: dict[str, object] = {}
    if payload.role is not None and payload.role != membership.role:
        if payload.role == "owner" and principal.role != "owner":
            raise ValidationFailed("Only an owner can grant the owner role", field="role")
        if membership.role == "owner" and membership.is_active and len(owners) <= 1:
            raise ConflictError("Cannot change the role of the last owner")
        if membership.role == "owner" and principal.role != "owner":
            raise ValidationFailed("Only an owner can change another owner's role", field="role")
        membership.role = payload.role
        changes["role"] = payload.role
        revoke_user_sessions(db, membership.user_id, principal.org_id)
    if payload.is_active is not None and payload.is_active != membership.is_active:
        # activation is per membership (tenant-scoped); the global user row is never touched here
        if not payload.is_active and membership.role == "owner" and len(owners) <= 1:
            raise ConflictError("Cannot deactivate the last owner")
        if not payload.is_active and membership.role == "owner" and principal.role != "owner":
            raise ValidationFailed("Only an owner can deactivate another owner", field="is_active")
        if membership.user_id == principal.user.id and not payload.is_active:
            raise ConflictError("You cannot deactivate yourself")
        membership.is_active = payload.is_active
        changes["is_active"] = payload.is_active
        if not payload.is_active:
            revoke_user_sessions(db, membership.user_id, principal.org_id)
    db.flush()
    audit.record(
        db,
        organization_id=principal.org_id,
        actor_user_id=principal.user.id,
        action="member.update",
        entity_type="membership",
        entity_id=membership.id,
        ip=principal.ip,
        summary=changes,
    )
    db.refresh(membership)
    return MemberOut.model_validate(membership)
