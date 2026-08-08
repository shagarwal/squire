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

import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

import httpx

from .config import Settings


@dataclass(frozen=True)
class TenantInfo:
    tenant_id: str
    status: str  # provisioning | running | sleeping | stopped | deleted
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
        # bot_id -> (expires_at, TenantInfo | None)
        self._entries: Dict[int, Tuple[float, Optional[TenantInfo]]] = {}

    async def lookup(self, bot_id: int) -> Optional[TenantInfo]:
        """Return TenantInfo for bot_id, or None if control-api says 404.

        Raises TenantLookupError if control-api is unreachable / errors --
        callers decide how to fail safe (see ingress.app).
        """
        now = self._clock()
        cached = self._entries.get(bot_id)
        if cached is not None and cached[0] > now:
            return cached[1]

        info = await self._fetch(bot_id)
        self._entries[bot_id] = (now + self._settings.cache_ttl_seconds, info)
        return info

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
