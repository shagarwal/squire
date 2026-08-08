"""
Ingress v0 -- FastAPI app factory.

Receives Telegram webhooks for every tenant bot at POST /telegram/<bot_id>,
looks the bot up in control-api (cached), validates the per-tenant webhook
secret, and forwards the raw update to the owning tenant container over
Railway private networking. Failures fall into the buffer-and-wake path
(ingress.buffer) instead of surfacing an error to Telegram.

Structural privacy guarantee: this module never passes the request body (or
any parsed Telegram payload field) into ingress.logging.log_event(). The
body only ever flows as opaque bytes into the forwarder and the retry
buffer. If auditing this claim: grep this file (and buffer.py, forwarder.py)
for `log_event(` and confirm every call site only supplies
bot_id/tenant_id/status_code/latency_ms/queue_depth -- log_event()'s
signature doesn't even accept anything else (see ingress/logging.py).
"""
from __future__ import annotations

import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from typing import Callable, Optional

import httpx
from fastapi import FastAPI, Header, Request, Response

from .buffer import RetryBuffer
from .config import Settings, load_settings
from .forwarder import forward_update
from .logging import log_event
from .tenant_cache import TenantCache, TenantLookupError


def create_app(
    settings: Optional[Settings] = None,
    client: Optional[httpx.AsyncClient] = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build the ingress FastAPI app.

    `settings` and `client` are injectable so tests can point control-api /
    tenant-forward calls at a mocked httpx transport instead of the network,
    and `clock` is injectable so cache-TTL / retry-backoff tests don't need
    real wall-clock sleeps. Production (ingress.main) calls this with no
    arguments, which loads settings from the environment and uses a real
    httpx.AsyncClient.
    """
    settings = settings or load_settings()
    owns_client = client is None
    client = client or httpx.AsyncClient()

    cache = TenantCache(client, settings, clock=clock)

    async def _do_forward(internal_url: str, body: bytes) -> bool:
        return await forward_update(
            client,
            internal_url,
            body,
            connect_timeout=settings.forward_connect_timeout,
            total_timeout=settings.forward_total_timeout,
        )

    buffer = RetryBuffer(settings, _do_forward, clock=clock)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        retry_task = asyncio.create_task(buffer.run_forever())
        try:
            yield
        finally:
            retry_task.cancel()
            try:
                await retry_task
            except asyncio.CancelledError:
                pass
            if owns_client:
                await client.aclose()

    app = FastAPI(lifespan=lifespan)

    # Exposed for tests / operational introspection -- not a public HTTP
    # contract, just attribute access on the app object.
    app.state.settings = settings
    app.state.cache = cache
    app.state.buffer = buffer
    app.state.client = client

    @app.post("/telegram/{bot_id}")
    async def telegram_webhook(
        bot_id: int,
        request: Request,
        x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
    ) -> Response:
        start = time.monotonic()
        body = await request.body()

        try:
            tenant = await cache.lookup(bot_id)
        except TenantLookupError:
            # control-api itself is unreachable. There's nothing useful we
            # can do without a tenant record (no webhook_secret to check, no
            # internal_url to forward to), so fail safe: 200 Telegram so it
            # doesn't retry-storm us, but log loudly since this is a real
            # operational problem (not a normal "unknown bot").
            log_event("control_api_unreachable", bot_id=bot_id, status_code=200)
            return Response(status_code=200)

        if tenant is None:
            # Unknown bot_id -- either a stale/foreign webhook or a race
            # during provisioning. Swallow (200) so Telegram doesn't hammer
            # retries, but log the miss (bot_id only, per contract).
            log_event("unknown_bot", bot_id=bot_id, status_code=200)
            return Response(status_code=200)

        provided_secret = x_telegram_bot_api_secret_token or ""
        # `not tenant.webhook_secret` guards against an auth bypass: if
        # control-api ever returns an empty webhook_secret (bad data, a
        # provisioning bug, whatever), a *missing* Telegram header also
        # comes through as "" above, and hmac.compare_digest("", "") is
        # True -- an empty secret would otherwise authenticate a request
        # with no secret header at all. Treat an empty configured secret as
        # "nothing can ever match it", not "anything matches it".
        #
        # The compare itself is constant-time -- this is a bearer-token-
        # shaped secret check, not a place to leak timing info about how
        # many characters matched.
        if not tenant.webhook_secret or not hmac.compare_digest(provided_secret, tenant.webhook_secret):
            log_event(
                "secret_mismatch",
                bot_id=bot_id,
                tenant_id=tenant.tenant_id,
                status_code=403,
            )
            return Response(status_code=403)

        ok = await _do_forward(tenant.internal_url, body)
        latency_ms = (time.monotonic() - start) * 1000

        if ok:
            log_event(
                "forward_ok",
                bot_id=bot_id,
                tenant_id=tenant.tenant_id,
                status_code=200,
                latency_ms=latency_ms,
            )
            return Response(status_code=200)

        # Forward failed -- most likely a sleeping/restarting tenant.
        # Buffer-and-wake: tell Telegram we're fine, let the background
        # retry worker redeliver once the tenant is reachable again.
        buffer.enqueue(tenant.tenant_id, tenant.internal_url, bot_id, body)
        log_event(
            "forward_failed_buffered",
            bot_id=bot_id,
            tenant_id=tenant.tenant_id,
            status_code=200,
            latency_ms=latency_ms,
            queue_depth=buffer.queue_depth(tenant.tenant_id),
        )
        return Response(status_code=200)

    return app
