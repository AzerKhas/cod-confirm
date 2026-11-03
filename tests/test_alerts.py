from __future__ import annotations

import json

import httpx
import respx

from app.config import Settings
from app.models import Shop
from app.services.alerts import notify_owner


def _shop(chat: str | None = "12345") -> Shop:
    return Shop(id=1, name="B", slug="b", api_key_hash="h", api_key_prefix="p",
                owner_telegram_chat_id=chat)  # fmt: skip


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


@respx.mock
def test_telegram_sent() -> None:
    route = respx.post("https://api.telegram.org/botTOKEN/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    assert notify_owner(
        _shop(), "hello", settings=_settings(dry_run=False, telegram_bot_token="TOKEN")
    )
    assert json.loads(route.calls.last.request.content) == {"chat_id": "12345", "text": "hello"}


@respx.mock
def test_telegram_failure_returns_false() -> None:
    respx.post(url__regex=r".*telegram.*").mock(return_value=httpx.Response(500))
    assert not notify_owner(_shop(), "x", settings=_settings(dry_run=False, telegram_bot_token="T"))


def test_not_configured_or_dry_run() -> None:
    assert (
        notify_owner(_shop(None), "x", settings=_settings(dry_run=False, telegram_bot_token="T"))
        is False
    )
    assert notify_owner(_shop(), "x", settings=_settings(dry_run=True)) is True
