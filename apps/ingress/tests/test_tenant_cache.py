"""Unit tests for the TTL cache in front of control-api's tenant lookup."""
from __future__ import annotations

import httpx
import pytest

from ingress.tenant_cache import TenantCache, TenantLookupError

TENANT_PAYLOAD = {
    "tenant_id": "tenant-abc",
    "status": "running",
    "internal_url": "http://tenant-abc.railway.internal:8080",
    "webhook_secret": "s3cr3t",
}


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_lookup_returns_tenant_info_on_200(settings, fake_clock):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/internal/tenants/by-bot/42"
        assert request.headers["Authorization"] == "Bearer test-internal-token"
        return httpx.Response(200, json=TENANT_PAYLOAD)

    cache = TenantCache(_client_with(handler), settings, clock=fake_clock)
    tenant = await cache.lookup(42)

    assert tenant is not None
    assert tenant.tenant_id == "tenant-abc"
    assert tenant.status == "running"
    assert tenant.internal_url == TENANT_PAYLOAD["internal_url"]
    assert tenant.webhook_secret == "s3cr3t"
    assert len(calls) == 1


async def test_lookup_returns_none_on_404(settings, fake_clock):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    cache = TenantCache(_client_with(handler), settings, clock=fake_clock)
    tenant = await cache.lookup(999)

    assert tenant is None


async def test_lookup_raises_on_unexpected_status(settings, fake_clock):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    cache = TenantCache(_client_with(handler), settings, clock=fake_clock)
    with pytest.raises(TenantLookupError):
        await cache.lookup(1)


async def test_lookup_raises_on_connection_error(settings, fake_clock):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    cache = TenantCache(_client_with(handler), settings, clock=fake_clock)
    with pytest.raises(TenantLookupError):
        await cache.lookup(1)


async def test_cache_hit_within_ttl_avoids_second_call(settings, fake_clock):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=TENANT_PAYLOAD)

    cache = TenantCache(_client_with(handler), settings, clock=fake_clock)

    await cache.lookup(42)
    fake_clock.advance(30)  # still within the 60s TTL
    await cache.lookup(42)

    assert call_count == 1


async def test_cache_expires_after_ttl(settings, fake_clock):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=TENANT_PAYLOAD)

    cache = TenantCache(_client_with(handler), settings, clock=fake_clock)

    await cache.lookup(42)
    fake_clock.advance(61)  # past the 60s TTL
    await cache.lookup(42)

    assert call_count == 2


async def test_negative_lookups_are_also_cached(settings, fake_clock):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(404)

    cache = TenantCache(_client_with(handler), settings, clock=fake_clock)

    await cache.lookup(999)
    await cache.lookup(999)

    assert call_count == 1
