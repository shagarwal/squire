"""
In-memory TTL cache in front of control-api's tenant-by-bot lookup.

`POST /telegram/<bot_id>` is on the hot path of every inbound Telegram
message, and the interface contract requires us to resolve bot_id -> tenant
via a control-api round trip. We cache both hits and misses for a short TTL
(default 60s, see Settings.cache_ttl_seconds) so a burst of messages from
the same chat doesn't turn into a burst of control-api calls.

This cache is per-process / in-memory only -- fine for a single ingress
instance in alpha; if ingress is ever horizontally scaled, each replica
just has its own (bounded-staleness) view, which is an acceptable
trade-off given the TTL is already short.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

import httpx

from .config import Settings


@dataclass(frozen=True)
class TenantInfo:
    tenant_id: str
    # provisioning | running | sleeping | stopped | deleted -- intentionally
    # *unused* by ingress in v0. We forward regardless of status; a
    # "stopped"/"deleted" tenant's forward call just fails like any other
    # unreachable tenant and falls into the normal buffer-and-wake /
    # eventual-give-up path. Explicit status-aware short-circuiting (e.g.
    # don't even try forwarding to a "deleted" tenant, or surface a
    # different Telegram-facing behavior per status) is deferred until
    # control-api/ops actually need ingress to act on this field.
    status: str
    internal_url: str
    webhook_secret: str


class TenantLookupError(Exception):
    """control-api was unreachable or returned something other than 200/404."""


class TenantCache:
    """Per-process cache of bot_id -> TenantInfo | None (None = confirmed unknown)."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock
        # bot_id -> (expires_at, TenantInfo | None). OrderedDict so we can
        # cheaply evict the oldest-inserted entry once we hit the size cap
        # (see _store) -- bot_id comes straight off a public URL path
        # segment, so an attacker hammering random/junk bot_ids must not be
        # able to grow this dict without bound.
        self._entries: "OrderedDict[int, Tuple[float, Optional[TenantInfo]]]" = OrderedDict()
        # Single shared lock guarding the miss path (see lookup()). One
        # lock for the whole cache rather than one per bot_id: per-bot_id
        # locks would need their own unbounded-growth guard for the exact
        # same attacker-controlled-bot_id reason as _entries above, and a
        # miss is rare once the cache is warm, so serializing all misses
        # behind a single lock is a fine trade-off for v0.
        self._lock = asyncio.Lock()

    async def lookup(self, bot_id: int) -> Optional[TenantInfo]:
        """Return TenantInfo for bot_id, or None if control-api says 404.

        Raises TenantLookupError if control-api is unreachable / errors --
        callers decide how to fail safe (see ingress.app).
        """
        now = self._clock()
        cached = self._entries.get(bot_id)
        if cached is not None and cached[0] > now:
            return cached[1]

        # Single-flight: collapse concurrent cache misses into one upstream
        # control-api call. Without this, a cold cache (e.g. right after an
        # ingress restart) hit by a burst of messages would fire one
        # control-api request per concurrent request instead of one total.
        async with self._lock:
            # Double-check after acquiring the lock: another task may have
            # already populated this entry (or evicted/refreshed a
            # different one) while we were waiting.
            now = self._clock()
            cached = self._entries.get(bot_id)
            if cached is not None and cached[0] > now:
                return cached[1]

            info = await self._fetch(bot_id)
            self._store(bot_id, now + self._settings.cache_ttl_seconds, info)
            return info

    def _store(self, bot_id: int, expires_at: float, info: Optional[TenantInfo]) -> None:
        is_new_key = bot_id not in self._entries
        self._entries[bot_id] = (expires_at, info)
        if is_new_key and len(self._entries) > self._settings.tenant_cache_max_entries:
            # Drop the oldest-inserted entry. Simple O(1) eviction -- we
            # don't bother preferring "expired" entries specifically since
            # this cache's TTL is short (default 60s) and the cap is meant
            # as a blunt memory-growth guard against junk bot_ids, not a
            # precision cache-hit optimizer.
            self._entries.popitem(last=False)

    async def _fetch(self, bot_id: int) -> Optional[TenantInfo]:
        url = f"{self._settings.control_api_url}/internal/tenants/by-bot/{bot_id}"
        headers = {"Authorization": f"Bearer {self._settings.internal_api_token}"}
        try:
            resp = await self._client.get(url, headers=headers, timeout=5.0)
        except httpx.HTTPError as exc:
            raise TenantLookupError(f"control-api unreachable for bot_id={bot_id}") from exc

        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise TenantLookupError(f"control-api returned {resp.status_code} for bot_id={bot_id}")

        data = resp.json()
        return TenantInfo(
            tenant_id=data["tenant_id"],
            status=data["status"],
            internal_url=data["internal_url"],
            webhook_secret=data["webhook_secret"],
        )
