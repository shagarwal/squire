"""
Tests the structural privacy guarantee directly: drive real request paths
(happy-path forward, buffered/failed forward, unknown bot, secret mismatch)
with a distinctive, easily-greppable "message body" and assert that string
never appears in any emitted log record -- and that every log record is a
JSON object containing only the fixed, safe field set from
ingress.logging.log_event.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from ingress.app import create_app

SECRET_MESSAGE_MARKER = "USER_TYPED_THIS_SUPER_SECRET_MESSAGE_CONTENT_XYZ123"

TENANT_PAYLOAD = {
    "tenant_id": "tenant-abc",
    "status": "running",
    "internal_url": "http://tenant-abc.internal",
    "webhook_secret": "correct-secret",
}

ALLOWED_LOG_FIELDS = {"timestamp", "event", "bot_id", "tenant_id", "status_code", "latency_ms", "queue_depth"}


def _handler(forward_status_seq):
    forward_iter = iter(forward_status_seq)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/internal/tenants/by-bot/"):
            return httpx.Response(200, json=TENANT_PAYLOAD)
        if request.url.path == "/webhook/telegram":
            outcome = next(forward_iter)
            if isinstance(outcome, Exception):
                raise outcome
            return httpx.Response(outcome)
        raise AssertionError(f"unexpected request to {request.url}")

    return handler


def _assert_no_body_leak(caplog):
    assert caplog.records, "expected at least one log record"
    for record in caplog.records:
        message = record.getMessage()
        # The literal secret string must never appear anywhere in a log line.
        assert SECRET_MESSAGE_MARKER not in message
        # Nor may the per-bot webhook secret. Ingress now re-stamps it on every
        # forward (it is what lets a tenant tell a real forward from a forged
        # POST, and therefore what protects first-contact owner binding), so it
        # passes through more code paths than before -- including the retry
        # buffer. A credential that gates account binding must never reach the
        # log aggregator.
        assert "correct-secret" not in message
        # Every log line from this service must be the structured JSON
        # object log_event() produces -- with *only* the allowed fields.
        # If some other code path ever calls logging directly (bypassing
        # log_event), this parse/keys check is what would catch it.
        parsed = json.loads(message)
        assert set(parsed.keys()) == ALLOWED_LOG_FIELDS


@pytest.mark.parametrize("forward_ok", [True, False])
async def test_forward_path_never_logs_body(settings, fake_clock, caplog, forward_ok):
    caplog.set_level(logging.INFO, logger="ingress")

    outcome = 200 if forward_ok else httpx.ConnectError("refused")
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler([outcome])))
    app = create_app(settings=settings, client=mock_client, clock=fake_clock)

    body = {
        "update_id": 1,
        "message": {"text": SECRET_MESSAGE_MARKER, "chat": {"id": 555, "username": "realuser"}},
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/telegram/42",
            json=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": "correct-secret"},
        )

    assert resp.status_code == 200
    _assert_no_body_leak(caplog)


async def test_secret_mismatch_path_never_logs_body(settings, fake_clock, caplog):
    caplog.set_level(logging.INFO, logger="ingress")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler([200])))
    app = create_app(settings=settings, client=mock_client, clock=fake_clock)

    body = {"update_id": 1, "message": {"text": SECRET_MESSAGE_MARKER}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/telegram/42", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}
        )

    assert resp.status_code == 403
    _assert_no_body_leak(caplog)


async def test_unknown_bot_path_never_logs_body(settings, fake_clock, caplog):
    caplog.set_level(logging.INFO, logger="ingress")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(settings=settings, client=mock_client, clock=fake_clock)

    body = {"update_id": 1, "message": {"text": SECRET_MESSAGE_MARKER}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/telegram/999", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "whatever"}
        )

    assert resp.status_code == 200
    _assert_no_body_leak(caplog)
