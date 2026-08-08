"""Unit tests for the TTL cache in front of control-api's tenant lookup."""
from __future__ import annotations

import asyncio
import dataclasses

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


async def test_single_flight_collapses_concurrent_misses_for_same_bot_id(settings, fake_clock):
    """N concurrent first-hits for the same bot_id must fire only 1 upstream call.

    Without the shared lock in TenantCache.lookup(), every one of these
    concurrent coroutines would see a cache miss and race off to
    control-api independently -- a stampede against control-api on a cold
    cache (e.g. right after an ingress restart). The handler sleeps briefly
    so the concurrent lookups actually overlap while the first fetch is
    in flight, rather than happening to run sequentially by accident.
    """
    call_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)  # simulate real network latency
        return httpx.Response(200, json=TENANT_PAYLOAD)

    cache = TenantCache(_client_with(handler), settings, clock=fake_clock)

    results = await asyncio.gather(*(cache.lookup(42) for _ in range(5)))

    assert call_count == 1
    assert all(r is not None and r.tenant_id == "tenant-abc" for r in results)


async def test_cache_evicts_oldest_entry_when_over_max_entries(settings, fake_clock):
    """bot_id is attacker-controlled (public URL path segment) -- the cache
    must not grow without bound. tenant_cache_max_entries caps it; once
    full, the oldest-inserted entry is evicted first.
    """
    capped_settings = dataclasses.replace(settings, tenant_cache_max_entries=3)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bot_id = int(request.url.path.rsplit("/", 1)[-1])
        calls.append(bot_id)
        payload = dict(TENANT_PAYLOAD, tenant_id=f"tenant-{bot_id}")
        return httpx.Response(200, json=payload)

    cache = TenantCache(_client_with(handler), capped_settings, clock=fake_clock)

    # Fill the cache to its cap, in insertion order 1, 2, 3.
    await cache.lookup(1)
    await cache.lookup(2)
    await cache.lookup(3)
    assert calls == [1, 2, 3]

    # A 4th distinct bot_id goes over the cap -- must evict bot_id=1 (the
    # oldest-inserted entry), not 2 or 3. Cache is now {2, 3, 4}.
    await cache.lookup(4)
    assert calls == [1, 2, 3, 4]

    # bot_id=2 and 3 survived the eviction -- no new upstream calls for them.
    await cache.lookup(2)
    await cache.lookup(3)
    assert calls == [1, 2, 3, 4]

    # bot_id=1 was evicted, so looking it up again is a fresh miss (which
    # in turn evicts 2, now the oldest-inserted of {2, 3, 4} -- that
    # cascading behaviour is expected for a cache genuinely at capacity,
    # not asserted further here).
    await cache.lookup(1)
    assert calls == [1, 2, 3, 4, 1]
