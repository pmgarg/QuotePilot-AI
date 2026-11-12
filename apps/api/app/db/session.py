"""Engine/session management and the per-transaction tenant context used by RLS policies."""

from __future__ import annotations

import uuid
from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            future=True,
        )
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def reset_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def set_tenant(db: Session, organization_id: uuid.UUID | None, user_id: uuid.UUID | None) -> None:
    """Bind the current transaction to a tenant and actor. `set_config(..., true)` is transaction-local,
    so the setting disappears at COMMIT/ROLLBACK and can never leak to another request on the pooled
    connection."""
    db.execute(
        text("SELECT set_config('app.current_org', :org, true), set_config('app.current_user', :usr, true)"),
        {
            "org": str(organization_id) if organization_id else "",
            "usr": str(user_id) if user_id else "",
        },
    )


def set_auth_scope(db: Session, *, login_email: str | None = None, session_token_hash: str | None = None) -> None:
    """Transaction-local settings consumed by the RLS policies on users/sessions during authentication."""
    db.execute(
        text("SELECT set_config('app.login_email', :email, true), set_config('app.session_token_hash', :tok, true)"),
        {"email": login_email or "", "tok": session_token_hash or ""},
    )


def get_db() -> Generator[Session, None, None]:
    db = get_sessionmaker()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
