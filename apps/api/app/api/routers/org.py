from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DB, PrincipalDep
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.security import hash_password
from app.models.org import Membership, User
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
    for k, v in data.items():
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
        summary={"fields": sorted(data.keys())},
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
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name.strip(),
        )
        db.add(user)
        db.flush()
    existing = db.scalar(
        select(Membership).where(Membership.organization_id == principal.org_id, Membership.user_id == user.id)
    )
    if existing is not None:
        raise ConflictError("User is already a member")
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
    membership = db.scalar(
        select(Membership).where(Membership.id == member_id, Membership.organization_id == principal.org_id)
    )
    if membership is None:
        raise NotFoundError("Member not found")
    owners = db.scalars(
        select(Membership).where(Membership.organization_id == principal.org_id, Membership.role == "owner")
    ).all()
    changes: dict[str, object] = {}
    if payload.role is not None and payload.role != membership.role:
        if payload.role == "owner" and principal.role != "owner":
            raise ValidationFailed("Only an owner can grant the owner role", field="role")
        if membership.role == "owner" and len(owners) <= 1:
            raise ConflictError("Cannot change the role of the last owner")
        if membership.role == "owner" and principal.role != "owner":
            raise ValidationFailed("Only an owner can change another owner's role", field="role")
        membership.role = payload.role
        changes["role"] = payload.role
        revoke_user_sessions(db, membership.user_id)
    if payload.is_active is not None:
        user = db.get(User, membership.user_id)
        assert user is not None
        if not payload.is_active and membership.role == "owner" and len(owners) <= 1:
            raise ConflictError("Cannot deactivate the last owner")
        if user.id == principal.user.id and not payload.is_active:
            raise ConflictError("You cannot deactivate yourself")
        user.is_active = payload.is_active
        changes["is_active"] = payload.is_active
        if not payload.is_active:
            revoke_user_sessions(db, user.id)
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
