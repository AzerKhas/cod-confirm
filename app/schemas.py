"""Pydantic v2 schemas (API input/output)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    env: str
    db: Literal["ok", "error"]
    dry_run: bool
