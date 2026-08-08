"""Unit tests for the buffer-and-wake retry queue (ingress.buffer)."""
from __future__ import annotations

from ingress.buffer import RetryBuffer, TenantBuffer


def test_tenant_buffer_drops_oldest_on_overflow(settings):
    # settings fixture uses queue_max_size=3
    buf = TenantBuffer("tenant-1", "http://tenant-1.internal", maxsize=3)

    overflow_flags = [
        buf.enqueue(b"update-1", now=0.0, initial_delay=2.0),
        buf.enqueue(b"update-2", now=1.0, initial_delay=2.0),
        buf.enqueue(b"update-3", now=2.0, initial_delay=2.0),
    ]
    assert overflow_flags == [False, False, False]
    assert len(buf) == 3

    # Queue is full (maxlen=3); the 4th append must evict update-1.
    overflowed = buf.enqueue(b"update-4", now=3.0, initial_delay=2.0)
    assert overflowed is True
    assert len(buf) == 3
    assert [item.body for item in buf.items] == [b"update-2", b"update-3", b"update-4"]


async def test_retry_buffer_redelivers_on_success(settings, fake_clock):
    attempts = []

    async def forward_fn(internal_url: str, body: bytes) -> bool:
        attempts.append((internal_url, body))
        return True  # tenant is awake, delivery succeeds immediately

    rb = RetryBuffer(settings, forward_fn, clock=fake_clock)
    rb.enqueue("tenant-1", "http://tenant-1.internal", bot_id=42, body=b"hello")
    assert rb.queue_depth("tenant-1") == 1

    # Not due yet (initial_delay=2.0s from settings fixture).
    await rb.retry_once()
    assert attempts == []
    assert rb.queue_depth("tenant-1") == 1

    fake_clock.advance(2.0)
    await rb.retry_once()

    assert attempts == [("http://tenant-1.internal", b"hello")]
    assert rb.queue_depth("tenant-1") == 0


async def test_retry_buffer_backs_off_and_eventually_succeeds(settings, fake_clock):
    # Fail the first two attempts, then succeed.
    results = iter([False, False, True])

    async def forward_fn(internal_url: str, body: bytes) -> bool:
        return next(results)

    rb = RetryBuffer(settings, forward_fn, clock=fake_clock)
    rb.enqueue("tenant-1", "http://tenant-1.internal", bot_id=42, body=b"hello")

    fake_clock.advance(2.0)  # initial_delay
    await rb.retry_once()
    assert rb.queue_depth("tenant-1") == 1  # still failing, still queued

    fake_clock.advance(4.0)  # delay doubled to 4.0
    await rb.retry_once()
    assert rb.queue_depth("tenant-1") == 1

    fake_clock.advance(8.0)  # delay doubled to 8.0
    await rb.retry_once()
    assert rb.queue_depth("tenant-1") == 0  # delivered


async def test_retry_buffer_gives_up_after_max_age(settings, fake_clock):
    async def forward_fn(internal_url: str, body: bytes) -> bool:
        return False  # tenant never comes back

    rb = RetryBuffer(settings, forward_fn, clock=fake_clock)
    rb.enqueue("tenant-1", "http://tenant-1.internal", bot_id=42, body=b"hello")

    # settings fixture: retry_give_up_seconds=300
    fake_clock.advance(301)
    await rb.retry_once()

    assert rb.queue_depth("tenant-1") == 0  # dropped, not delivered


async def test_retry_preserves_fifo_order_per_tenant(settings, fake_clock):
    delivered = []

    async def forward_fn(internal_url: str, body: bytes) -> bool:
        delivered.append(body)
        return True

    rb = RetryBuffer(settings, forward_fn, clock=fake_clock)
    rb.enqueue("tenant-1", "http://tenant-1.internal", bot_id=1, body=b"first")
    fake_clock.advance(0.5)
    rb.enqueue("tenant-1", "http://tenant-1.internal", bot_id=1, body=b"second")

    fake_clock.advance(2.0)  # both past their (independent) initial_delay
    await rb.retry_once()

    assert delivered == [b"first", b"second"]
