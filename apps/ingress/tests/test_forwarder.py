"""Unit tests for ingress.forwarder (the tenant-forward HTTP call)."""
from __future__ import annotations

import httpx

from ingress.forwarder import forward_update


async def test_forward_success_on_2xx():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://tenant-1.internal/webhook/telegram"
        assert request.content == b'{"update_id": 1}'
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok = await forward_update(
        client,
        "http://tenant-1.internal",
        b'{"update_id": 1}',
        connect_timeout=5.0,
        total_timeout=10.0,
    )
    assert ok is True


async def test_forward_failure_on_non_2xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok = await forward_update(
        client, "http://tenant-1.internal", b"{}", connect_timeout=5.0, total_timeout=10.0
    )
    assert ok is False


async def test_forward_failure_on_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok = await forward_update(
        client, "http://tenant-1.internal", b"{}", connect_timeout=5.0, total_timeout=10.0
    )
    assert ok is False


async def test_forward_failure_on_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok = await forward_update(
        client, "http://tenant-1.internal", b"{}", connect_timeout=5.0, total_timeout=10.0
    )
    assert ok is False


async def test_forward_strips_trailing_slash_from_internal_url():
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await forward_update(
        client, "http://tenant-1.internal/", b"{}", connect_timeout=5.0, total_timeout=10.0
    )
    assert seen_urls == ["http://tenant-1.internal/webhook/telegram"]


async def test_forward_stamps_per_bot_secret_header():
    """The tenant must be able to tell an ingress forward from a raw POST.

    This is a security control, not plumbing: the tenant image binds its owner
    on first contact, so an unauthenticated forgery to a not-yet-bound tenant
    would be permanent account takeover. See forwarder.forward_update.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ok = await forward_update(
        client, "http://tenant-1.internal", b"{}",
        connect_timeout=5.0, total_timeout=10.0,
        webhook_secret="per-bot-secret-value",
    )
    assert ok is True
    assert seen.get("x-telegram-bot-api-secret-token") == "per-bot-secret-value"
    assert seen.get("content-type") == "application/json"


async def test_forward_omits_header_when_no_secret_configured():
    """An empty secret must not become a literal empty header.

    A tenant treating "present but empty" as authenticated would reintroduce
    exactly the bypass app.py guards against on the inbound side.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await forward_update(
        client, "http://tenant-1.internal", b"{}",
        connect_timeout=5.0, total_timeout=10.0, webhook_secret="",
    )
    assert "x-telegram-bot-api-secret-token" not in seen
