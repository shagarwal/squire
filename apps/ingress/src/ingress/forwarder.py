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
    webhook_secret: str = "",
) -> bool:
    """POST `body` verbatim to `<internal_url>/webhook/telegram`.

    We deliberately do not parse or mutate the body -- it is passed through
    as opaque bytes, both here and in the retry buffer, so there is no code
    path where this service inspects Telegram payload fields.

    `webhook_secret` is re-stamped as X-Telegram-Bot-Api-Secret-Token so the
    tenant can tell an ingress-forwarded update from an arbitrary POST to its
    port. We verified this same secret on the way in (app.py) and then dropped
    it; the tenant needs it too, and for a stronger reason than it looks.

    WHY THIS IS A SECURITY CONTROL AND NOT PLUMBING
    -----------------------------------------------
    The tenant image binds its owner on first contact (trust on first use). If
    the tenant cannot distinguish a real forwarded update from a forged one,
    anyone able to reach a tenant's port can send a synthetic "first message"
    to a not-yet-bound tenant and become its permanent owner -- and the real
    customer has no shell to recover it. Before trust-on-first-use the same
    forgery only yielded a useless pairing code; afterwards it is account
    takeover, so the tenant must be able to refuse.

    Deliberately the PER-BOT secret, not the fleet-wide internal token: every
    trial tenant's own agent can read INTERNAL_API_TOKEN out of its environment,
    so the fleet token is precisely the credential an attacker already has. The
    per-bot secret is known only to Telegram, control-api and that one tenant.

    The value is never logged -- see logging.log_event's field allowlist and
    tests/test_no_body_logging.py.

    Returns True on any 2xx response from the tenant. Returns False on
    timeout, connection refused, or any other network-level failure, *and*
    on non-2xx responses -- all of those are treated identically by the
    caller as "tenant not currently reachable, buffer and retry" (a sleeping
    tenant, a mid-restart tenant, and a tenant returning 500 all look the
    same from here and all deserve a retry rather than a dropped update).
    """
    url = internal_url.rstrip("/") + "/webhook/telegram"
    timeout = httpx.Timeout(total_timeout, connect=connect_timeout)
    headers = {"Content-Type": "application/json"}
    if webhook_secret:
        headers["X-Telegram-Bot-Api-Secret-Token"] = webhook_secret
    try:
        resp = await client.post(
            url,
            content=body,
            headers=headers,
            timeout=timeout,
        )
    except httpx.HTTPError:
        return False
    return 200 <= resp.status_code < 300
