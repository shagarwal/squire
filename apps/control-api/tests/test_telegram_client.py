"""Telegram Bot API client tests -- all HTTP mocked."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from control_api.clients.telegram import (
    TelegramClient,
    TelegramError,
    bot_id_from_token,
)

TOKEN = "123456:AA-secret"
BASE = f"https://api.telegram.org/bot{TOKEN}"


def test_bot_id_is_numeric_prefix_of_token():
    # This is the contract ingress relies on: `123456:ABC...` -> 123456
    assert bot_id_from_token("123456:ABCdef") == 123456
    assert bot_id_from_token("  7890123:xyz ") == 7890123


@pytest.mark.parametrize("bad", ["", "nocolon", "abc:def", ":123"])
def test_bot_id_rejects_malformed_tokens(bad):
    with pytest.raises(ValueError):
        bot_id_from_token(bad)


@respx.mock
def test_get_me_returns_result():
    respx.post(f"{BASE}/getMe").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"id": 123456, "username": "squire_bot"}}
        )
    )
    me = TelegramClient().get_me(TOKEN)
    assert me["username"] == "squire_bot"


@respx.mock
def test_get_me_raises_on_unauthorized_token():
    respx.post(f"{BASE}/getMe").mock(
        return_value=httpx.Response(
            401, json={"ok": False, "description": "Unauthorized"}
        )
    )
    with pytest.raises(TelegramError, match="Unauthorized"):
        TelegramClient().get_me(TOKEN)


@respx.mock
def test_set_webhook_payload():
    route = respx.post(f"{BASE}/setWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    ok = TelegramClient().set_webhook(
        TOKEN,
        url="https://ingress.squire.test/telegram/123456",
        secret_token="whsec-abc",
    )
    assert ok is True

    sent = json.loads(route.calls.last.request.read())
    assert sent["url"] == "https://ingress.squire.test/telegram/123456"
    assert sent["secret_token"] == "whsec-abc"
    # Stale updates from a previously-assigned tenant must never leak into a new one.
    assert sent["drop_pending_updates"] is True


@respx.mock
def test_delete_webhook():
    respx.post(f"{BASE}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    assert TelegramClient().delete_webhook(TOKEN) is True
