"""FastAPI application factory with request ids, security headers and uniform error handling."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routers import auth, catalogs, customers, files, followup, health, org, quotes, rfqs
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, log_event, request_id_var

logger = logging.getLogger("quotepilot.api")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-_.]{8,64}$")
API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="QuotePilot AI API",
        version="0.1.0",
        docs_url=f"{API_PREFIX}/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json" if not settings.is_production else None,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        started = time.perf_counter()
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        request.state.request_id = rid
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith(API_PREFIX):
            response.headers.setdefault("Cache-Control", "no-store")
            if not request.url.path.startswith(f"{API_PREFIX}/docs"):
                response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if settings.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        log_event(
            logger,
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            request_id=rid,
            ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        body: dict[str, object] = {
            "error": exc.code,
            "detail": exc.detail,
            "request_id": request_id_var.get(),
        }
        body.update(exc.extra)
        headers = {"Retry-After": str(exc.extra["retry_after"])} if "retry_after" in exc.extra else None
        return JSONResponse(status_code=exc.status_code, content=body, headers=headers)

    from app.services.comms.providers import ProviderError

    @app.exception_handler(ProviderError)
    async def provider_error_handler(request: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse(
            status_code=503 if exc.retryable else 400,
            content={
                "error": "provider_unavailable" if exc.retryable else "provider_rejected",
                "detail": str(exc),
                "request_id": request_id_var.get(),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": [str(x) for x in e.get("loc", [])], "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "detail": errors,
                "request_id": request_id_var.get(),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": "http_error",
                "detail": exc.detail,
                "request_id": request_id_var.get(),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # runs in Starlette's outermost ServerErrorMiddleware, after the request context var was reset
        rid = getattr(request.state, "request_id", None) or request_id_var.get()
        token = request_id_var.set(rid)
        try:
            logger.exception("unhandled error")
        finally:
            request_id_var.reset(token)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": "Unexpected error", "request_id": rid},
            headers={"X-Request-ID": rid, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )

    for r in (
        health.router,
        auth.router,
        org.router,
        customers.router,
        catalogs.router,
        rfqs.router,
        quotes.router,
        files.router,
        followup.router,
    ):
        app.include_router(r, prefix=API_PREFIX)
    return app


app = create_app()
