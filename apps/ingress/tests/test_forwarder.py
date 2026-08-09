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
