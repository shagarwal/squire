"""
Forwarding an inbound Telegram update to the tenant container that owns it,
over Railway private networking.
"""
from __future__ import annotations

import httpx


async def forward_update(
    client: httpx.AsyncClient,
    internal_url: str,
    body: bytes,
    *,
    connect_timeout: float,
    total_timeout: float,
) -> bool:
    """POST `body` verbatim to `<internal_url>/webhook/telegram`.

    We deliberately do not parse or mutate the body -- it is passed through
    as opaque bytes, both here and in the retry buffer, so there is no code
    path where this service inspects Telegram payload fields.

    Returns True on any 2xx response from the tenant. Returns False on
    timeout, connection refused, or any other network-level failure, *and*
    on non-2xx responses -- all of those are treated identically by the
    caller as "tenant not currently reachable, buffer and retry" (a sleeping
    tenant, a mid-restart tenant, and a tenant returning 500 all look the
    same from here and all deserve a retry rather than a dropped update).
    """
    url = internal_url.rstrip("/") + "/webhook/telegram"
    timeout = httpx.Timeout(total_timeout, connect=connect_timeout)
    try:
        resp = await client.post(
            url,
            content=body,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx.HTTPError:
        return False
    return 200 <= resp.status_code < 300
