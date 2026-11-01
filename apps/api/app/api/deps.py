"""FastAPI dependencies: DB session, authentication, tenant binding, RBAC and CSRF."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import constant_time_equals, csrf_token_for
from app.db.session import get_db, set_tenant
from app.models.org import Membership, Organization, User, UserSession
from app.services import auth as auth_service
from app.services.authz import Permission, require_permission

DB = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@dataclass
class Principal:
    user: User
    membership: Membership
    organization: Organization
    session: UserSession
    ip: str | None

    @property
    def org_id(self) -> uuid.UUID:
        return self.organization.id

    @property
    def role(self) -> str:
        return self.membership.role

    def require(self, permission: Permission) -> None:
        require_permission(self.role, permission)


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def current_session(request: Request, db: DB, settings: SettingsDep) -> UserSession:
    token = request.cookies.get(settings.session_cookie_name)
    session = auth_service.resolve_session(db, token)
    if session is None:
        raise UnauthorizedError("Not authenticated")
    return session


def csrf_protect(request: Request, settings: SettingsDep, session: UserSession = Depends(current_session)) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    header = request.headers.get("x-csrf-token", "")
    cookie = request.cookies.get(settings.csrf_cookie_name, "")
    expected = csrf_token_for(session.csrf_secret, settings.secret_key)
    if (
        not header
        or not cookie
        or not constant_time_equals(header, cookie)
        or not constant_time_equals(header, expected)
    ):
        raise ForbiddenError("CSRF token missing or invalid", action="csrf")


def current_principal(
    request: Request,
    db: DB,
    session: UserSession = Depends(current_session),
    _csrf: None = Depends(csrf_protect),
) -> Principal:
    set_tenant(db, session.organization_id, session.user_id)
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == session.user_id,
            Membership.organization_id == session.organization_id,
        )
    )
    user = db.get(User, session.user_id)
    organization = db.get(Organization, session.organization_id)
    if membership is None or user is None or organization is None or not user.is_active:
        raise UnauthorizedError("Session is no longer valid")
    return Principal(
        user=user,
        membership=membership,
        organization=organization,
        session=session,
        ip=client_ip(request),
    )


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require(permission: Permission):  # type: ignore[no-untyped-def]
    def _dep(principal: PrincipalDep) -> Principal:
        principal.require(permission)
        return principal

    return Depends(_dep)
