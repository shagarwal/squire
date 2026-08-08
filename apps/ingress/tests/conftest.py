"""
Shared test fixtures.

No test in this suite makes a real HTTP call: control-api lookups and
tenant-forward calls both go through the same injected httpx.AsyncClient,
which we point at an httpx.MockTransport whose handler each test controls.
"""
from __future__ import annotations

import pytest

from ingress.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        control_api_url="http://control-api.internal",
        internal_api_token="test-internal-token",
        cache_ttl_seconds=60.0,
        forward_connect_timeout=5.0,
        forward_total_timeout=10.0,
        queue_max_size=3,  # small on purpose so overflow tests don't need 100 items
        retry_initial_delay=2.0,
        retry_max_delay=30.0,
        retry_give_up_seconds=300.0,
        retry_loop_interval=1.0,
    )


class FakeClock:
    """Manually-advanced clock so cache-TTL / backoff tests are deterministic."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()
