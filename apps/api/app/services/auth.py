"""Login, logout and session resolution. Sessions are opaque tokens stored hashed; revocable server-side."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import RateLimited, UnauthorizedError
from app.core.security import generate_token, hash_password, hash_token, verify_password
from app.db.base import utcnow
from app.models.org import Membership, Organization, User, UserSession
from app.services import audit


class LoginRateLimiter:
    """Sliding-window limiter keyed by (ip, email). In-process; see KNOWN_LIMITATIONS for multi-replica."""

    def __init__(self, attempts: int, window_seconds: int) -> None:
        self.attempts = attempts
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.attempts:
                raise RateLimited("Too many login attempts; try again later")

    def hit(self, key: str) -> None:
        with self._lock:
            self._hits.setdefault(key, deque()).append(time.monotonic())

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_DUMMY_HASH = hash_password("timing-equalisation-dummy-password")
_settings = get_settings()
login_limiter = LoginRateLimiter(_settings.login_rate_limit_attempts, _settings.login_rate_limit_window_seconds)
# per-account budget regardless of source address (brute force across IPs); 5× the per-ip budget
account_limiter = LoginRateLimiter(_settings.login_rate_limit_attempts * 5, _settings.login_rate_limit_window_seconds)


class LoginResult:
    def __init__(
        self,
        user: User,
        membership: Membership,
        organization: Organization,
        session: UserSession,
        token: str,
    ):
        self.user = user
        self.membership = membership
        self.organization = organization
        self.session = session
        self.token = token


def login(db: Session, *, email: str, password: str, ip: str | None, user_agent: str | None) -> LoginResult:
    settings = get_settings()
    normalized = email.strip().lower()
    key = f"{ip or '-'}|{normalized}"
    account_key = f"account|{normalized}"
    login_limiter.check(key)
    account_limiter.check(account_key)
    from app.db.session import set_auth_scope, set_tenant

    set_auth_scope(db, login_email=normalized)
    user = db.scalar(select(User).where(User.email == normalized))
    if user is None:
        verify_password(password, _DUMMY_HASH)  # equalise timing for unknown accounts (ASVS V2.2)
    ok = user is not None and user.is_active and verify_password(password, user.password_hash)
    if not ok:
        login_limiter.hit(key)
        account_limiter.hit(account_key)
        audit.record(
            db,
            organization_id=None,
            actor_user_id=None,
            action="auth.login_failed",
            ip=ip,
            summary={"email_domain": email.split("@")[-1][:100]},
        )
        db.commit()
        raise UnauthorizedError("Invalid email or password")
    if user is None:  # pragma: no cover - guarded by `ok`
        raise UnauthorizedError("Invalid email or password")
    set_tenant(db, None, user.id)  # memberships are RLS-visible to the user before an org is chosen
    membership = db.scalar(
        select(Membership)
        .where(Membership.user_id == user.id, Membership.is_active.is_(True))
        .order_by(Membership.created_at)
        .limit(1)
    )
    if membership is None:
        raise UnauthorizedError("Invalid email or password")
    # the organization row is only visible to the session once the tenant context is set
    set_tenant(db, membership.organization_id, user.id)
    organization = db.get(Organization, membership.organization_id)
    if organization is None:
        raise UnauthorizedError("Invalid email or password")
    token = generate_token(32)
    now = utcnow()
    session = UserSession(
        user_id=user.id,
        organization_id=membership.organization_id,
        token_hash=hash_token(token),
        csrf_secret=generate_token(32),
        created_at=now,
        expires_at=now + timedelta(seconds=settings.session_absolute_seconds),
        last_seen_at=now,
        ip=ip,
        user_agent=(user_agent or "")[:300] or None,
    )
    db.add(session)
    db.flush()
    audit.record(
        db,
        organization_id=membership.organization_id,
        actor_user_id=user.id,
        action="auth.login",
        entity_type="session",
        entity_id=session.id,
        ip=ip,
    )
    return LoginResult(user, membership, organization, session, token)


def resolve_session(db: Session, token: str | None) -> UserSession | None:
    if not token:
        return None
    settings = get_settings()
    from app.db.session import set_auth_scope

    token_hash = hash_token(token)
    set_auth_scope(db, session_token_hash=token_hash)
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))
    if session is None or session.revoked_at is not None:
        return None
    now = utcnow()
    if session.expires_at <= now:
        return None
    if (now - session.last_seen_at) > timedelta(seconds=settings.session_idle_seconds):
        return None
    if (now - session.last_seen_at) > timedelta(seconds=60):
        session.last_seen_at = now
    return session


def logout(db: Session, session: UserSession, ip: str | None) -> None:
    session.revoked_at = utcnow()
    audit.record(
        db,
        organization_id=session.organization_id,
        actor_user_id=session.user_id,
        action="auth.logout",
        entity_type="session",
        entity_id=session.id,
        ip=ip,
    )


def revoke_user_sessions(db: Session, user_id: uuid.UUID, organization_id: uuid.UUID | None = None) -> int:
    """Revoke a user's sessions; scoped to one organisation when given (membership-level actions)."""
    stmt = select(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    if organization_id is not None:
        stmt = stmt.where(UserSession.organization_id == organization_id)
    rows = db.scalars(stmt).all()
    for s in rows:
        s.revoked_at = utcnow()
    return len(rows)
