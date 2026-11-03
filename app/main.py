"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.logging_config import configure_logging
from app.routers import health, intake, order_form

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
    yield
    log.info("app stopping")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

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
    return app


app = create_app()
