"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import LoginRequired
from app.config import get_settings
from app.logging_config import configure_logging
from app.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.monitoring import setup_monitoring
from app.routers import (
    admin,
    billing,
    dashboard,
    health,
    intake,
    onboarding,
    order_form,
    signup,
    webhook_meta,
)

log = logging.getLogger("cod_confirm")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log.info(
        "app starting",
        extra={
            "env": settings.app_env,
            "version": settings.app_version,
            "dry_run": settings.dry_run,
        },
    )
    if settings.scheduler_enabled and settings.app_env != "test":
        from app.services.scheduler import start_scheduler

        start_scheduler()
    yield
    from app.services.scheduler import stop_scheduler

    stop_scheduler()
    log.info("app stopping")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    setup_monitoring(settings)

    app = FastAPI(
        title="COD Confirm",
        version=settings.app_version,
        lifespan=lifespan,
        # No public API docs in production.
        docs_url=None if settings.app_env == "prod" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.app_env == "prod" else "/openapi.json",
    )
    app.include_router(health.router)
    app.include_router(intake.router)
    app.include_router(order_form.router)
    app.include_router(webhook_meta.router)
    app.include_router(dashboard.router)
    app.include_router(admin.router)
    app.include_router(billing.router)
    app.include_router(signup.router)
    app.include_router(onboarding.router)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key.get_secret_value(),
        session_cookie="cc_session",
        max_age=7 * 24 * 3600,
        same_site="lax",
        https_only=settings.app_env == "prod",
    )

    hosts = [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()] or ["*"]
    if "*" not in hosts:
        hosts += ["127.0.0.1", "localhost"]  # container health checks
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.app_env == "prod")
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(_request: Request, _exc: LoginRequired) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> RedirectResponse:
        return RedirectResponse("/static/favicon.svg", status_code=308)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/dashboard", status_code=307)

    return app


app = create_app()
