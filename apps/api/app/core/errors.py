"""Application error hierarchy mapped to HTTP responses without leaking internals."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 400
    code = "error"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        super().__init__(detail or self.code)
        self.detail = detail or self.code
        self.extra = extra


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class BlockedError(AppError):
    status_code = 409
    code = "blocked"


class StateError(AppError):
    status_code = 409
    code = "invalid_state"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_error"


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"


class UnsupportedMedia(AppError):
    status_code = 415
    code = "unsupported_media_type"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class LimitExceeded(AppError):
    """Plan entitlement or runtime budget exhausted (S4-F04 / S4-A06); enforced server-side."""

    status_code = 429
    code = "limit_exceeded"


class KillSwitchEngaged(AppError):
    status_code = 503
    code = "kill_switch_engaged"
