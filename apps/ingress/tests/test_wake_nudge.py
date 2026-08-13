"""
Typing-on-wake nudge tests.

When a forward fails and the update is buffered (sleeping tenant), ingress
must fire-and-forget a POST to control-api's /internal/wake-typing so the
user sees "typing..." during the ~15s wake instead of a silent chat. These
tests pin the whole contract from ingress's side:

  * a buffered arrival produces exactly one nudge, carrying the right
    bot_id/chat_id and the internal bearer token;
  * a nudge failure can never affect the buffer path (the update is still
    queued, Telegram still gets its 200);
  * a successful forward never nudges;
  * at most one nudge per (bot_id, chat_id) per wake episode -- where an
    episode is "the tenant's buffer went from empty to non-empty" -- but a
    NEW episode (buffer drained, then a later failure) nudges again;
  * a body with no parseable chat id is buffered normally and simply not
    nudged (the nudge is best-effort UX, never a delivery precondition);
  * the log schema is unchanged: nudge-path log lines carry only the fixed
    structural fields -- no chat id, no body -- same guarantee as
    test_no_body_logging.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from ingress.app import create_app
from ingress.wake_nudge import extract_chat_id

TENANT_PAYLOAD = {
    "tenant_id": "tenant-abc",
    "status": "sleeping",
    "internal_url": "http://tenant-abc.internal",
    "webhook_secret": "correct-secret",
}

CHAT_ID = 555001
UPDATE = {
    "update_id": 1,
    "message": {"text": "wake up please", "chat": {"id": CHAT_ID, "type": "private"}},
}

# Must stay in lockstep with ingress.logging.log_event -- the typing-on-wake
# feature must NOT grow this schema (no chat_id, no body fields, ever).
ALLOWED_LOG_FIELDS = {
    "timestamp", "event", "bot_id", "tenant_id", "status_code", "latency_ms", "queue_depth",
}


def make_handler(*, forward_status_seq, nudge_status_seq=None):
    """MockTransport handler routing control-api lookup / tenant forward /
    wake-typing nudge calls, recording each.

    nudge_status_seq: per-nudge outcomes (int status or Exception); defaults
    to always-202.
    """
    forward_iter = iter(forward_status_seq)
    nudge_iter = iter(nudge_status_seq) if nudge_status_seq is not None else None
    calls = {"control": [], "forward": [], "nudge": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/internal/tenants/by-bot/"):
            calls["control"].append(request)
            return httpx.Response(200, json=TENANT_PAYLOAD)
        if request.url.path == "/internal/wake-typing":
            calls["nudge"].append(request)
            outcome = next(nudge_iter) if nudge_iter is not None else 202
            if isinstance(outcome, Exception):
                raise outcome
            return httpx.Response(outcome)
        if request.url.path == "/webhook/telegram":
            calls["forward"].append(request)
            outcome = next(forward_iter)
            if isinstance(outcome, Exception):
                raise outcome
            return httpx.Response(outcome)
        raise AssertionError(f"unexpected request to {request.url}")

    return handler, calls


def make_app(settings, fake_clock, handler):
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return create_app(settings=settings, client=mock_client, clock=fake_clock)


def webhook_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def post_update(client, body=None, bot_id: int = 42):
    return await client.post(
        f"/telegram/{bot_id}",
        content=json.dumps(body if body is not None else UPDATE).encode()
        if not isinstance(body, bytes)
        else body,
        headers={
            "X-Telegram-Bot-Api-Secret-Token": "correct-secret",
            "Content-Type": "application/json",
        },
    )


# ---------------------------------------------------------------------------
# The nudge fires, correctly addressed, without touching the buffer path
# ---------------------------------------------------------------------------


async def test_buffered_arrival_fires_one_nudge_with_bot_and_chat_id(settings, fake_clock):
    handler, calls = make_handler(forward_status_seq=[httpx.ConnectError("refused")])
    app = make_app(settings, fake_clock, handler)

    async with webhook_client(app) as client:
        resp = await post_update(client)
        await app.state.nudger.wait_idle()

    assert resp.status_code == 200
    assert app.state.buffer.queue_depth("tenant-abc") == 1

    assert len(calls["nudge"]) == 1
    nudge = calls["nudge"][0]
    assert json.loads(nudge.content) == {"bot_id": 42, "chat_id": CHAT_ID}
    # Same internal bearer token as the by-bot lookup.
    assert nudge.headers["Authorization"] == "Bearer test-internal-token"
    # Aimed at control-api, not the tenant.
    assert nudge.url.host == httpx.URL(settings.control_api_url).host


async def test_nudge_failure_never_affects_buffering(settings, fake_clock):
    for nudge_outcome in (httpx.ConnectError("nudge refused"), 500):
        handler, calls = make_handler(
            forward_status_seq=[httpx.ConnectError("refused")],
            nudge_status_seq=[nudge_outcome],
        )
        app = make_app(settings, fake_clock, handler)

        async with webhook_client(app) as client:
            resp = await post_update(client)
            await app.state.nudger.wait_idle()

        # Telegram still 200'd, the update is still queued for redelivery.
        assert resp.status_code == 200
        assert app.state.buffer.queue_depth("tenant-abc") == 1
        assert len(calls["nudge"]) == 1


async def test_successful_forward_never_nudges(settings, fake_clock):
    handler, calls = make_handler(forward_status_seq=[200])
    app = make_app(settings, fake_clock, handler)

    async with webhook_client(app) as client:
        resp = await post_update(client)
        await app.state.nudger.wait_idle()

    assert resp.status_code == 200
    assert len(calls["nudge"]) == 0


# ---------------------------------------------------------------------------
# One nudge per (bot_id, chat_id) per wake episode
# ---------------------------------------------------------------------------


async def test_second_buffered_update_in_same_episode_is_suppressed(settings, fake_clock):
    handler, calls = make_handler(
        forward_status_seq=[httpx.ConnectError("refused")] * 2
    )
    app = make_app(settings, fake_clock, handler)

    async with webhook_client(app) as client:
        await post_update(client)
        await post_update(client)
        await app.state.nudger.wait_idle()

    assert app.state.buffer.queue_depth("tenant-abc") == 2
    assert len(calls["nudge"]) == 1  # the second arrival did NOT re-nudge


async def test_distinct_chats_in_same_episode_each_get_one_nudge(settings, fake_clock):
    handler, calls = make_handler(
        forward_status_seq=[httpx.ConnectError("refused")] * 3
    )
    app = make_app(settings, fake_clock, handler)

    other_chat = dict(UPDATE, message={"text": "hi", "chat": {"id": 777002, "type": "private"}})
    async with webhook_client(app) as client:
        await post_update(client)
        await post_update(client, body=other_chat)
        await post_update(client)  # same chat as the first -- suppressed
        await app.state.nudger.wait_idle()

    assert len(calls["nudge"]) == 2
    chat_ids = {json.loads(n.content)["chat_id"] for n in calls["nudge"]}
    assert chat_ids == {CHAT_ID, 777002}


async def test_new_episode_after_drain_nudges_again(settings, fake_clock):
    # 1st forward fails (episode 1) -> retry succeeds (buffer drains) ->
    # a later forward fails again (episode 2): both episodes must nudge.
    handler, calls = make_handler(
        forward_status_seq=[
            httpx.ConnectError("refused"),  # inline attempt, episode 1
            200,                            # retry worker delivers; buffer empty
            httpx.ConnectError("refused"),  # inline attempt, episode 2
        ]
    )
    app = make_app(settings, fake_clock, handler)

    async with webhook_client(app) as client:
        await post_update(client)
        fake_clock.advance(2.0)
        await app.state.buffer.retry_once()
        assert app.state.buffer.queue_depth("tenant-abc") == 0

        await post_update(client)
        await app.state.nudger.wait_idle()

    assert len(calls["nudge"]) == 2


# ---------------------------------------------------------------------------
# Bodies without a chat id: buffer normally, just don't nudge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"this is not json at all",
        {"update_id": 1},  # no message/chat anywhere
        {"update_id": 1, "message": {"chat": {"id": "not-an-int"}}},
    ],
)
async def test_chatless_or_garbage_body_buffers_without_nudging(settings, fake_clock, body):
    handler, calls = make_handler(forward_status_seq=[httpx.ConnectError("refused")])
    app = make_app(settings, fake_clock, handler)

    async with webhook_client(app) as client:
        resp = await post_update(client, body=body)
        await app.state.nudger.wait_idle()

    assert resp.status_code == 200
    assert app.state.buffer.queue_depth("tenant-abc") == 1  # buffering unaffected
    assert len(calls["nudge"]) == 0


# ---------------------------------------------------------------------------
# chat-id extraction (pure function, no app)
# ---------------------------------------------------------------------------


def test_extract_chat_id_from_message():
    assert extract_chat_id(json.dumps(UPDATE).encode()) == CHAT_ID


def test_extract_chat_id_from_edited_message():
    body = {"update_id": 2, "edited_message": {"chat": {"id": 123}}}
    assert extract_chat_id(json.dumps(body).encode()) == 123


def test_extract_chat_id_from_callback_query():
    body = {"update_id": 3, "callback_query": {"message": {"chat": {"id": 456}}}}
    assert extract_chat_id(json.dumps(body).encode()) == 456


@pytest.mark.parametrize(
    "body",
    [
        b"junk",
        b"[]",  # JSON but not an object
        json.dumps({"update_id": 1}).encode(),
        json.dumps({"message": "not-a-dict"}).encode(),
        json.dumps({"message": {"chat": "not-a-dict"}}).encode(),
        json.dumps({"message": {"chat": {"id": "str"}}}).encode(),
        json.dumps({"message": {"chat": {"id": True}}}).encode(),  # bool is not a chat id
        json.dumps({"callback_query": {"message": None}}).encode(),
    ],
)
def test_extract_chat_id_returns_none_for_unusable_bodies(body):
    assert extract_chat_id(body) is None


# ---------------------------------------------------------------------------
# Log schema pinned: the nudge path adds events, never fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("nudge_outcome", [202, httpx.ConnectError("nudge refused")])
async def test_nudge_path_logs_only_the_fixed_schema(settings, fake_clock, caplog, nudge_outcome):
    caplog.set_level(logging.INFO, logger="ingress")
    handler, calls = make_handler(
        forward_status_seq=[httpx.ConnectError("refused")],
        nudge_status_seq=[nudge_outcome],
    )
    app = make_app(settings, fake_clock, handler)

    marker_update = {
        "update_id": 9,
        "message": {"text": "SECRET_NUDGE_MARKER_XYZ", "chat": {"id": CHAT_ID}},
    }
    async with webhook_client(app) as client:
        await post_update(client, body=marker_update)
        await app.state.nudger.wait_idle()

    assert caplog.records
    for record in caplog.records:
        message = record.getMessage()
        assert "SECRET_NUDGE_MARKER_XYZ" not in message
        assert "chat" not in message  # no chat_id key sneaking into the schema
        parsed = json.loads(message)
        assert set(parsed.keys()) == ALLOWED_LOG_FIELDS
