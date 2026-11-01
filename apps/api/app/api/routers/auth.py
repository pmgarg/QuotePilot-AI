from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import DB, PrincipalDep, SettingsDep, client_ip, csrf_protect, current_session
from app.core.config import Settings
from app.core.errors import ForbiddenError
from app.core.security import constant_time_equals, csrf_token_for, generate_token
from app.db.session import set_tenant
from app.models.org import UserSession
from app.schemas.common import LoginRequest, OrganizationPublic, SessionInfo, UserOut
from app.services import auth as auth_service
from app.services.authz import Permission, has_permission

router = APIRouter(prefix="/auth", tags=["auth"])

PRE_LOGIN_CSRF_COOKIE = "qp_csrf_prelogin"


def _set_cookie(
    response: Response,
    settings: Settings,
    name: str,
    value: str,
    *,
    http_only: bool,
    max_age: int | None,
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=http_only,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


def _permissions(role: str) -> list[str]:
    return [p.value for p in Permission if has_permission(role, p)]


@router.get("/csrf")
def csrf(response: Response, settings: SettingsDep) -> dict[str, str]:
    """Issue a pre-login CSRF token (double-submit). The login endpoint requires it."""
    token = generate_token(24)
    _set_cookie(response, settings, PRE_LOGIN_CSRF_COOKIE, token, http_only=False, max_age=3600)
    return {"csrf_token": token}


@router.post("/login", response_model=SessionInfo)
def login(payload: LoginRequest, request: Request, response: Response, db: DB, settings: SettingsDep) -> SessionInfo:
    header = request.headers.get("x-csrf-token", "")
    cookie = request.cookies.get(PRE_LOGIN_CSRF_COOKIE, "")
    if not header or not cookie or not constant_time_equals(header, cookie):
        raise ForbiddenError("CSRF token missing or invalid", action="csrf")
    result = auth_service.login(
        db,
        email=payload.email,
        password=payload.password,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    csrf_token = csrf_token_for(result.session.csrf_secret, settings.secret_key)
    _set_cookie(
        response,
        settings,
        settings.session_cookie_name,
        result.token,
        http_only=True,
        max_age=settings.session_absolute_seconds,
    )
    _set_cookie(
        response,
        settings,
        settings.csrf_cookie_name,
        csrf_token,
        http_only=False,
        max_age=settings.session_absolute_seconds,
    )
    response.delete_cookie(PRE_LOGIN_CSRF_COOKIE, path="/")
    return SessionInfo(
        user=UserOut.model_validate(result.user),
        organization=OrganizationPublic.model_validate(result.organization),
        role=result.membership.role,
        csrf_token=csrf_token,
        permissions=_permissions(result.membership.role),
    )


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: DB,
    settings: SettingsDep,
    session: UserSession = Depends(current_session),
    _csrf: None = Depends(csrf_protect),
) -> dict[str, str]:
    set_tenant(db, session.organization_id, session.user_id)  # audit row must carry the tenant under RLS
    auth_service.logout(db, session, client_ip(request))
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    return {"status": "logged_out"}


@router.get("/me", response_model=SessionInfo)
def me(principal: PrincipalDep, settings: SettingsDep) -> SessionInfo:
    return SessionInfo(
        user=UserOut.model_validate(principal.user),
        organization=OrganizationPublic.model_validate(principal.organization),
        role=principal.role,
        csrf_token=csrf_token_for(principal.session.csrf_secret, settings.secret_key),
        permissions=_permissions(principal.role),
    )
