"""POST /internal/wake-typing -- the typing-on-wake nudge from ingress.

When ingress buffers an update for a sleeping tenant it nudges this endpoint
(bot_id + chat_id only); control-api owns the bot token, so it is the one that
can fire Telegram sendChatAction("typing") and keep it alive for the ~15-20s
wake window. These tests pin:

  * auth: same internal bearer as every /internal route;
  * unknown bot_id -> 404 (same convention as by-bot lookup / heartbeat);
  * the happy path 202s and fires sendChatAction the configured number of
    times with action="typing" (TestClient runs BackgroundTasks inline, and
    conftest zeroes WAKE_TYPING_INTERVAL_SECONDS, so "the whole loop ran"
    is observable synchronously);
  * a Telegram-side failure never turns into a 5xx for ingress;
  * the repeat schedule (interval between sends, capped total) -- tested on
    the loop function directly with an injected sleep, so no real waiting.

chat_id handling: it is Telegram routing metadata (like bot.id), NOT message
content. It is never persisted -- this endpoint touches no table beyond the
bot-token lookup -- and never logged, so the privacy-schema whitelist is
untouched by this feature.
"""

from __future__ import annotations

import json

import httpx
import respx

from control_api.routers.internal import _typing_loop
from conftest import make_bot_token

CHAT_ID = 987654321


def bot_api_base(bot_id: int = 123456) -> str:
    return f"https://api.telegram.org/bot{make_bot_token(bot_id)}"


# ---------------------------------------------------------------------------
# Auth + unknown bot
# ---------------------------------------------------------------------------


def test_wake_typing_requires_bearer_token(client, seeded_bot):
    body = {"bot_id": seeded_bot["id"], "chat_id": CHAT_ID}
    assert client.post("/internal/wake-typing", json=body).status_code == 401
    assert (
        client.post(
            "/internal/wake-typing",
            json=body,
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )


def test_wake_typing_404s_for_unknown_bot(client, auth):
    r = client.post(
        "/internal/wake-typing",
        json={"bot_id": 999999, "chat_id": CHAT_ID},
        headers=auth,
    )
    assert r.status_code == 404


def test_wake_typing_rejects_extra_fields(client, auth, seeded_bot):
    """extra="forbid" -- the nudge channel must stay ids-only, same reasoning
    as HeartbeatRequest: no field means no place for content to ride along."""
    r = client.post(
        "/internal/wake-typing",
        json={"bot_id": seeded_bot["id"], "chat_id": CHAT_ID, "text": "sneaky"},
        headers=auth,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Happy path: 202 + repeated sendChatAction("typing")
# ---------------------------------------------------------------------------


@respx.mock
def test_wake_typing_202s_and_fires_typing_actions(client, auth, seeded_bot):
    route = respx.post(f"{bot_api_base()}/sendChatAction").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )

    r = client.post(
        "/internal/wake-typing",
        json={"bot_id": seeded_bot["id"], "chat_id": CHAT_ID},
        headers=auth,
    )

    assert r.status_code == 202
    assert r.json() == {"bot_id": seeded_bot["id"], "accepted": True}

    # conftest sets WAKE_TYPING_REPEATS=5 explicitly (the shipped default),
    # and TestClient runs the background loop before returning.
    assert route.call_count == 5
    for call in route.calls:
        sent = json.loads(call.request.read())
        assert sent == {"chat_id": CHAT_ID, "action": "typing"}


@respx.mock
def test_telegram_error_still_yields_202(client, auth, seeded_bot):
    """A dead chat / blocked bot is ingress's 202 all the same -- Telegram-side
    failures are logged, never surfaced as a 5xx to the nudge caller."""
    route = respx.post(f"{bot_api_base()}/sendChatAction").mock(
        return_value=httpx.Response(
            403, json={"ok": False, "description": "Forbidden: bot was blocked"}
        )
    )

    r = client.post(
        "/internal/wake-typing",
        json={"bot_id": seeded_bot["id"], "chat_id": CHAT_ID},
        headers=auth,
    )

    assert r.status_code == 202
    # The loop stops on the first failure (a chat that refused typing once
    # will refuse it four more times) rather than hammering Telegram.
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# The repeat schedule, driven directly with a fake sleep
# ---------------------------------------------------------------------------


@respx.mock
def test_typing_loop_schedule_is_interval_spaced_and_capped():
    token = make_bot_token(123456)
    route = respx.post(f"{bot_api_base()}/sendChatAction").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )

    sleeps: list[float] = []
    _typing_loop(token, CHAT_ID, repeats=5, interval_seconds=4.0, sleep=sleeps.append)

    # 5 sends total, spaced by 4s: sleep runs BETWEEN sends, so 4 sleeps.
    # Telegram's typing status lasts ~5s, so 4s spacing keeps it unbroken
    # across the ~16s the loop covers (+ the wake finishing the job).
    assert route.call_count == 5
    assert sleeps == [4.0, 4.0, 4.0, 4.0]


@respx.mock
def test_typing_loop_swallows_network_errors():
    token = make_bot_token(123456)
    route = respx.post(f"{bot_api_base()}/sendChatAction").mock(
        side_effect=httpx.ConnectError("telegram unreachable")
    )

    sleeps: list[float] = []
    # Must not raise -- this runs as a BackgroundTask after the 202 went out.
    _typing_loop(token, CHAT_ID, repeats=5, interval_seconds=4.0, sleep=sleeps.append)

    assert route.call_count == 1  # gave up after the first failure
    assert sleeps == []
