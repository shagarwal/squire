"""
Integration tests for POST /telegram/{bot_id} -- the full request path
through TenantCache -> secret check -> forwarder -> buffer, wired together
exactly as ingress.app.create_app() assembles them in production, but with
control-api and the tenant's webhook both replaced by an httpx.MockTransport
so no real network traffic happens.

We drive the app via httpx.AsyncClient(transport=ASGITransport(...)) inside
async test functions (rather than fastapi.testclient.TestClient, which runs
requests on a separate thread/event loop) so that a test can freely mix
"send a webhook request" and "call app.state.buffer.retry_once() directly"
in the same event loop -- needed for the buffer-and-wake tests, which drive
retries deterministically via the fake clock instead of waiting on the real
background task's real-time sleep loop.
"""
from __future__ import annotations

import json

import httpx
import pytest

from ingress.app import create_app

TENANT_PAYLOAD = {
    "tenant_id": "tenant-abc",
    "status": "running",
    "internal_url": "http://tenant-abc.internal",
    "webhook_secret": "correct-secret",
}


def make_handler(*, control_status: int = 200, forward_status_seq=None, tenant_payload=None):
    """Build a MockTransport handler routing control-api vs. tenant-forward calls.

    forward_status_seq: iterable of ints/exceptions consumed one per forward
    call (lets a test simulate "fails twice, then succeeds").
    tenant_payload: override the control-api response body (defaults to
    TENANT_PAYLOAD) -- used e.g. to simulate control-api returning an empty
    webhook_secret.
    """
    forward_iter = iter(forward_status_seq) if forward_status_seq is not None else None
    payload = tenant_payload if tenant_payload is not None else TENANT_PAYLOAD
    calls = {"control": [], "forward": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/internal/tenants/by-bot/"):
            calls["control"].append(request)
            if control_status == 404:
                return httpx.Response(404)
            return httpx.Response(control_status, json=payload)

        if request.url.path == "/webhook/telegram":
            calls["forward"].append(request)
            outcome = next(forward_iter) if forward_iter is not None else 200
            if isinstance(outcome, Exception):
                raise outcome
            return httpx.Response(outcome)

        raise AssertionError(f"unexpected request to {request.url}")

    return handler, calls


def make_app(settings, fake_clock, handler):
    """Build a create_app() instance wired to the given mock handler."""
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return create_app(settings=settings, client=mock_client, clock=fake_clock)


def webhook_client(app) -> httpx.AsyncClient:
    """An httpx client that talks to `app` in-process (no real sockets, no lifespan)."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_happy_path_forwards_and_returns_200(settings, fake_clock):
    handler, calls = make_handler(forward_status_seq=[200])
    app = make_app(settings, fake_clock, handler)

    body = {"update_id": 1, "message": {"text": "hello from a real user"}}
    async with webhook_client(app) as client:
        resp = await client.post(
            "/telegram/42",
            json=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
        )

    assert resp.status_code == 200
    assert len(calls["forward"]) == 1
    # Body forwarded verbatim.
    assert json.loads(calls["forward"][0].content) == body


async def test_missing_secret_header_returns_403(settings, fake_clock):
    handler, calls = make_handler(forward_status_seq=[200])
    app = make_app(settings, fake_clock, handler)

    async with webhook_client(app) as client:
        resp = await client.post("/telegram/42", json={"update_id": 1})

    assert resp.status_code == 403
    assert len(calls["forward"]) == 0  # never even attempted a forward


async def test_wrong_secret_header_returns_403(settings, fake_clock):
    handler, calls = make_handler(forward_status_seq=[200])
    app = make_app(settings, fake_clock, handler)

    async with webhook_client(app) as client:
        resp = await client.post(
            "/telegram/42",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        )

    assert resp.status_code == 403
    assert len(calls["forward"]) == 0


async def test_empty_configured_secret_never_authenticates_returns_403(settings, fake_clock):
    """Regression test: an empty tenant.webhook_secret must not be an auth bypass.

    hmac.compare_digest("", "") is True -- so if control-api ever returns an
    empty webhook_secret (bad data / provisioning bug), a request with *no*
    X-Telegram-Bot-Api-Secret-Token header at all (which also comes through
    as "") would otherwise pass the secret check. app.py explicitly guards
    against this with `if not tenant.webhook_secret or not hmac.compare...`.
    """
    empty_secret_payload = dict(TENANT_PAYLOAD, webhook_secret="")
    handler, calls = make_handler(forward_status_seq=[200], tenant_payload=empty_secret_payload)
    app = make_app(settings, fake_clock, handler)

    async with webhook_client(app) as client:
        # No secret header at all -- would compare "" == "" if we only did
        # hmac.compare_digest without the emptiness guard.
        resp = await client.post("/telegram/42", json={"update_id": 1})

    assert resp.status_code == 403
    assert len(calls["forward"]) == 0


async def test_unknown_bot_returns_200_and_swallows(settings, fake_clock):
    handler, calls = make_handler(control_status=404)
    app = make_app(settings, fake_clock, handler)

    async with webhook_client(app) as client:
        resp = await client.post(
            "/telegram/999999",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "anything"},
        )

    # Swallowed: 200 even though the bot is unknown, per contract (don't let
    # Telegram retry-storm us over a bad/foreign bot_id).
    assert resp.status_code == 200
    assert len(calls["forward"]) == 0


async def test_sleeping_tenant_forward_failure_returns_200_and_buffers(settings, fake_clock):
    # Forward raises a connection error -- simulates a sleeping/restarting tenant.
    handler, calls = make_handler(forward_status_seq=[httpx.ConnectError("refused")])
    app = make_app(settings, fake_clock, handler)

    async with webhook_client(app) as client:
        resp = await client.post(
            "/telegram/42",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
        )

    # Telegram still gets a 200 -- we don't want it retry-storming us.
    assert resp.status_code == 200
    assert app.state.buffer.queue_depth("tenant-abc") == 1


async def test_buffered_update_is_redelivered_once_tenant_wakes(settings, fake_clock):
    # First attempt (inline, at request time) fails; the retry worker's
    # first attempt also fails; the second retry succeeds.
    handler, calls = make_handler(
        forward_status_seq=[httpx.ConnectError("refused"), httpx.ConnectError("refused"), 200]
    )
    app = make_app(settings, fake_clock, handler)

    async with webhook_client(app) as client:
        resp = await client.post(
            "/telegram/42",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
        )
    assert resp.status_code == 200
    assert app.state.buffer.queue_depth("tenant-abc") == 1

    # Drive the retry worker directly (deterministic, no real sleeping) --
    # same retry_once() the background task calls on its interval.
    fake_clock.advance(2.0)  # past initial_delay
    await app.state.buffer.retry_once()
    assert app.state.buffer.queue_depth("tenant-abc") == 1  # 2nd attempt still failing

    fake_clock.advance(4.0)  # delay doubled to 4.0
    await app.state.buffer.retry_once()
    assert app.state.buffer.queue_depth("tenant-abc") == 0  # delivered

    assert len(calls["forward"]) == 3


async def test_queue_overflow_drops_oldest(settings, fake_clock):
    # settings fixture: queue_max_size=3. Send 5 updates that all fail to
    # forward -- the buffer should cap at 3 and evict oldest first.
    handler, calls = make_handler(forward_status_seq=[httpx.ConnectError("refused")] * 5)
    app = make_app(settings, fake_clock, handler)

    async with webhook_client(app) as client:
        for i in range(5):
            resp = await client.post(
                "/telegram/42",
                json={"update_id": i},
                headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
            )
            assert resp.status_code == 200

    assert app.state.buffer.queue_depth("tenant-abc") == 3
