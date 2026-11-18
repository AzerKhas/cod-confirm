"""Jinja2 environment shared by HTML routes (autoescape always on) + UI helpers."""

from __future__ import annotations

import zlib
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.icons import icon

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_version() -> str:
    """Cache-busting token for /static files (changes when a file changes)."""
    crc = 0
    for f in sorted(STATIC_DIR.glob("*")):
        if f.is_file():
            crc = zlib.crc32(f.read_bytes(), crc)
    return f"{crc:08x}"


def local_dt(value: datetime | None, tz: str = "Africa/Tunis", fmt: str = "%d/%m %H:%M") -> str:
    if value is None:
        return ""
    return value.astimezone(ZoneInfo(tz or "Africa/Tunis")).strftime(fmt)


def ago(value: datetime | None) -> str:
    """'à l'instant', 'il y a 5 min', 'il y a 3 h', 'il y a 2 j'."""
    if value is None:
        return ""
    seconds = int((datetime.now(UTC) - value).total_seconds())
    if seconds < 60:
        return "à l'instant"
    if seconds < 3600:
        return f"il y a {seconds // 60} min"
    if seconds < 86400:
        return f"il y a {seconds // 3600} h"
    return f"il y a {seconds // 86400} j"


def initials(name: str | None) -> str:
    parts = [p for p in (name or "").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def avatar_class(seed: object) -> str:
    return f"c{zlib.crc32(str(seed).encode()) % 6}"


_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(default=True, default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.globals.update(icon=icon, asset_v=_asset_version())
_env.filters.update(local_dt=local_dt, ago=ago, initials=initials, avatar_class=avatar_class)
templates = Jinja2Templates(env=_env)
