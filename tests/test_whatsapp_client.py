from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.config import Settings
from app.services.whatsapp import ReplyButton, WhatsAppClient, WhatsAppError

PNID = "1234567890"
URL = f"https://graph.facebook.com/v21.0/{PNID}/messages"
TOKEN = "EAAG-secret-token-1234"
OK = {"messaging_product": "whatsapp", "messages": [{"id": "wamid.ABC"}]}


def make_client(sleeps: list[float] | None = None, **kw: object) -> WhatsAppClient:
    settings = Settings(dry_run=False, _env_file=None)  # type: ignore[arg-type]
    return WhatsAppClient(
        PNID,
        TOKEN,
        settings=settings,
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
        **kw,  # type: ignore[arg-type]
    )


@respx.mock
def test_send_template_payload() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=OK))
    res = make_client().send_template(
        "+21698123456",
        "cod_order_confirmation_tn",
        "fr",
        ["Ahmed", "59"],
        ["CONFIRM:1", "CANCEL:1"],
    )
    assert res.message_id == "wamid.ABC"
    assert res.dry_run is False

    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(req.content)
    assert body["to"] == "21698123456"
    assert body["type"] == "template"
    tpl = body["template"]
    assert tpl["name"] == "cod_order_confirmation_tn"
    assert tpl["language"] == {"code": "fr"}
    assert tpl["components"][0] == {
        "type": "body",
        "parameters": [{"type": "text", "text": "Ahmed"}, {"type": "text", "text": "59"}],
    }
    assert tpl["components"][2]["index"] == "1"
    assert tpl["components"][2]["parameters"] == [{"type": "payload", "payload": "CANCEL:1"}]


@respx.mock
def test_send_text_and_buttons() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=OK))
    c = make_client()
    c.send_text("+21698123456", "Ya3tik essa7a")
    assert json.loads(route.calls.last.request.content)["text"]["body"] == "Ya3tik essa7a"

    c.send_buttons("+21698123456", "Tconfirmi ?", [ReplyButton("CONFIRM:1", "✅ Ey")])
    inter = json.loads(route.calls.last.request.content)["interactive"]
    assert inter["action"]["buttons"][0] == {
        "type": "reply",
        "reply": {"id": "CONFIRM:1", "title": "✅ Ey"},
    }


def test_button_limits() -> None:
    with pytest.raises(ValueError):
        ReplyButton("x", "a" * 21)
    with pytest.raises(ValueError):
        make_client().send_buttons(
            "+21698123456", "x", [ReplyButton(str(i), "ok") for i in range(4)]
        )


@respx.mock
def test_mark_as_read() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"success": True}))
    make_client().mark_as_read("wamid.IN")
    assert json.loads(route.calls.last.request.content) == {
        "messaging_product": "whatsapp", "status": "read", "message_id": "wamid.IN"
    }  # fmt: skip


@respx.mock
def test_retries_on_429_then_succeeds() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, json={"error": {"code": 130429, "message": "rate limit"}}),
            httpx.Response(503),
            httpx.Response(200, json=OK),
        ]
    )
    sleeps: list[float] = []
    assert make_client(sleeps).send_text("+21698123456", "hi").message_id == "wamid.ABC"
    assert route.call_count == 3
    assert sleeps == [1, 2]


@respx.mock
def test_network_error_retried_then_raised() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("boom"))
    sleeps: list[float] = []
    with pytest.raises(WhatsAppError) as ei:
        make_client(sleeps).send_text("+21698123456", "hi")
    assert ei.value.retryable
    assert len(sleeps) == 2  # 3 attempts


@respx.mock
def test_permanent_error_not_retried_and_no_secrets_in_message() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            400, json={"error": {"code": 131047, "message": "Re-engagement message"}}
        )
    )
    with pytest.raises(WhatsAppError) as ei:
        make_client().send_text("+21698123456", "hi")
    assert route.call_count == 1
    assert ei.value.window_closed
    assert TOKEN not in str(ei.value)
    assert "98123456" not in str(ei.value)


@respx.mock
def test_dry_run_sends_nothing() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=OK))
    c = WhatsAppClient(None, None, settings=Settings(dry_run=True, _env_file=None))  # type: ignore[arg-type]
    res = c.send_template("+21698123456", "x", "fr", ["a"])
    assert res.dry_run and res.message_id.startswith("dryrun.")
    c.mark_as_read("wamid.X")
    assert route.call_count == 0


def test_real_mode_requires_credentials() -> None:
    with pytest.raises(WhatsAppError):
        WhatsAppClient(None, None, settings=Settings(dry_run=False, _env_file=None))  # type: ignore[arg-type]
