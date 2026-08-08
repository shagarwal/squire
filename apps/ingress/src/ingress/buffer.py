"""
Buffer-and-wake path.

When forwarding an update to a tenant's container fails (timeout /
connection refused -- most commonly because the tenant is asleep or
restarting), ingress still returns 200 to Telegram immediately, so Telegram
doesn't retry-storm us, and instead enqueues the raw update body for a
background worker to redeliver once the tenant wakes up.

v0 / alpha note: this buffer is in-memory only and is lost on ingress
restart or redeploy. That's an accepted limitation for alpha -- durable
buffering (e.g. a small Postgres- or Redis-backed queue) is a later
hardening item, once ingress itself needs multi-instance or zero-downtime
deploys. Until then, an ingress restart during a tenant's sleep window can
drop in-flight buffered updates; the user would just need to send another
message.

Delivery is at-least-once, not exactly-once: if a forward's HTTP response
is lost after the tenant already processed the update (e.g. we hit our
timeout right as the tenant was replying), we have no way to distinguish
that from a true failure, so we'll redeliver -- the tenant may see the same
update twice. This is an inherent trade-off of a fire-and-retry buffer
without idempotency keys/dedup on the tenant side, not a bug; documenting
it here rather than solving it in v0.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Deque, Dict

from .config import Settings
from .logging import log_event

# forward_fn(internal_url, body) -> True if delivered, False if still failing.
ForwardFn = Callable[[str, bytes], Awaitable[bool]]


@dataclass
class QueuedUpdate:
    body: bytes
    enqueued_at: float
    next_attempt_at: float
    delay: float
    # Carried along purely so retry/give-up log events can include bot_id
    # (the fixed log schema wants it wherever we have it) without having to
    # thread it back through TenantBuffer/RetryBuffer's tenant_id-keyed
    # structures.
    bot_id: int


class TenantBuffer:
    """Bounded FIFO queue of pending updates for one tenant.

    Backed by collections.deque(maxlen=...): once the queue is full,
    appending a new item automatically evicts the oldest one from the other
    end -- exactly the "drop-oldest on overflow" behaviour the interface
    contract asks for, with no extra bookkeeping needed.
    """

    def __init__(self, tenant_id: str, internal_url: str, maxsize: int) -> None:
        self.tenant_id = tenant_id
        self.internal_url = internal_url
        self.items: Deque[QueuedUpdate] = deque(maxlen=maxsize)

    def enqueue(self, body: bytes, bot_id: int, now: float, initial_delay: float) -> bool:
        """Append an update. Returns True if this eviction overflowed (dropped oldest)."""
        overflowed = len(self.items) == (self.items.maxlen or 0)
        self.items.append(
            QueuedUpdate(
                body=body,
                enqueued_at=now,
                next_attempt_at=now + initial_delay,
                delay=initial_delay,
                bot_id=bot_id,
            )
        )
        return overflowed

    def __len__(self) -> int:
        return len(self.items)


class RetryBuffer:
    """Owns all per-tenant TenantBuffers and drives redelivery."""

    def __init__(
        self,
        settings: Settings,
        forward_fn: ForwardFn,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._forward_fn = forward_fn
        self._clock = clock
        self._buffers: Dict[str, TenantBuffer] = {}

    def get_or_create(self, tenant_id: str, internal_url: str) -> TenantBuffer:
        buf = self._buffers.get(tenant_id)
        if buf is None:
            buf = TenantBuffer(tenant_id, internal_url, self._settings.queue_max_size)
            self._buffers[tenant_id] = buf
        else:
            # internal_url can shift across redeploys / fresh cache lookups;
            # always retry against the freshest known address.
            buf.internal_url = internal_url
        return buf

    def queue_depth(self, tenant_id: str) -> int:
        buf = self._buffers.get(tenant_id)
        return len(buf) if buf is not None else 0

    def enqueue(self, tenant_id: str, internal_url: str, bot_id: int, body: bytes) -> None:
        buf = self.get_or_create(tenant_id, internal_url)
        now = self._clock()
        overflowed = buf.enqueue(body, bot_id, now, self._settings.retry_initial_delay)
        log_event("buffer_enqueue", bot_id=bot_id, tenant_id=tenant_id, queue_depth=len(buf))
        if overflowed:
            # Count-only: we never log which update was dropped, just that one was.
            log_event("buffer_overflow_drop", bot_id=bot_id, tenant_id=tenant_id, queue_depth=len(buf))

    async def retry_once(self) -> None:
        """Single pass over all tenant buffers, redelivering due items.

        Split out from run_forever() so tests can drive retries
        deterministically (advance a fake clock, call retry_once(), assert)
        instead of depending on real wall-clock sleeps.
        """
        now = self._clock()
        for buf in list(self._buffers.values()):
            await self._retry_tenant(buf, now)

    async def _retry_tenant(self, buf: TenantBuffer, now: float) -> None:
        # Only ever attempt the front of the queue -- redelivery must stay
        # in FIFO order per tenant, and we don't want to fire N concurrent
        # requests at a tenant that's still down.
        while buf.items:
            item = buf.items[0]

            age = now - item.enqueued_at
            if age > self._settings.retry_give_up_seconds:
                buf.items.popleft()
                log_event(
                    "buffer_give_up",
                    bot_id=item.bot_id,
                    tenant_id=buf.tenant_id,
                    queue_depth=len(buf),
                )
                continue

            if item.next_attempt_at > now:
                break  # front item not due yet; everything behind it is even newer

            ok = await self._forward_fn(buf.internal_url, item.body)
            if ok:
                buf.items.popleft()
                log_event(
                    "buffer_redeliver_ok",
                    bot_id=item.bot_id,
                    tenant_id=buf.tenant_id,
                    queue_depth=len(buf),
                )
                continue  # tenant looks awake now; keep draining the rest of its queue

            item.delay = min(item.delay * 2, self._settings.retry_max_delay)
            item.next_attempt_at = now + item.delay
            log_event(
                "buffer_redeliver_fail",
                bot_id=item.bot_id,
                tenant_id=buf.tenant_id,
                queue_depth=len(buf),
            )
            break  # still down; stop here and try again next pass

    async def run_forever(self) -> None:
        """Background task entrypoint: retry_once() on a fixed interval, forever.

        Wrapped in try/except so that a single bad pass (an unexpected
        exception from a forward_fn call, a bug in retry logic, whatever)
        can't silently kill this task. Without the guard, an uncaught
        exception here ends the task for the process lifetime -- the app
        would keep accepting and 200-ing webhooks and *look* healthy while
        buffer-and-wake quietly stopped working entirely. We log and keep
        looping instead; a single tenant/item causing a persistent crash
        will just keep failing that one item's redelivery (or get dropped
        by the age-based give-up) rather than taking down retries for every
        other tenant.
        """
        while True:
            await asyncio.sleep(self._settings.retry_loop_interval)
            try:
                await self.retry_once()
            except Exception:
                # Deliberately broad: this loop must never die. We don't
                # have a body/payload to leak here anyway -- log_event()'s
                # schema has no room for exception details beyond the fixed
                # structural fields, which is the point.
                log_event("retry_loop_error")
