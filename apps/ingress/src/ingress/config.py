"""
Environment-driven configuration for the ingress service.

This is the single place that knows the names of the env vars Railway sets
for this service. Everything else in the app takes a `Settings` instance as
an explicit argument (constructor / factory param) rather than reading
os.environ directly -- that's what lets tests build a fully-isolated app
with fake URLs/timeouts/queue sizes without touching the real environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # -- required, no sane default --
    control_api_url: str
    internal_api_token: str

    # -- have sane defaults for local/dev, overridable via env in prod --
    port: int = 8000

    # How long a successful (or 404) tenant lookup is trusted before we
    # re-ask control-api. This is on the hot path of every inbound message,
    # so keep it short but non-zero.
    cache_ttl_seconds: float = 60.0

    # Hard cap on distinct bot_ids held in the tenant cache at once. bot_id
    # is attacker-controlled (it's a public URL path segment), so this
    # bounds memory growth from someone hammering junk bot_ids -- see
    # TenantCache._store's oldest-inserted eviction.
    tenant_cache_max_entries: int = 10_000

    # Forwarding a Telegram update to the tenant container. Kept short: a
    # sleeping tenant should fail fast into the buffer-and-wake path rather
    # than holding the Telegram webhook connection open.
    forward_connect_timeout: float = 5.0
    forward_total_timeout: float = 10.0

    # Buffer-and-wake path (see ingress.buffer).
    queue_max_size: int = 100
    retry_initial_delay: float = 2.0
    retry_max_delay: float = 30.0
    retry_give_up_seconds: float = 300.0  # ~5 minutes
    retry_loop_interval: float = 1.0  # how often the background worker wakes up

    # Typing-on-wake nudge (see ingress.wake_nudge). The nudge is pure UX with
    # a strict sub-second budget: if control-api can't take the POST quickly,
    # give up -- never let it back up behind the buffer path.
    wake_nudge_timeout: float = 1.0
    # Cap on distinct (bot_id, chat_id) pairs tracked per tenant per wake
    # episode -- a blunt memory guard, same spirit as tenant_cache_max_entries.
    wake_nudge_max_chats_per_episode: int = 32


def load_settings() -> Settings:
    """Build Settings from the real process environment.

    Only called from ingress.main (the uvicorn entrypoint) -- never at
    import time of any other module -- so importing the package for tests
    never requires CONTROL_API_URL/INTERNAL_API_TOKEN to be set.
    """
    return Settings(
        control_api_url=os.environ["CONTROL_API_URL"].rstrip("/"),
        internal_api_token=os.environ["INTERNAL_API_TOKEN"],
        port=int(os.environ.get("PORT", "8000")),
        cache_ttl_seconds=float(os.environ.get("INGRESS_CACHE_TTL_SECONDS", "60")),
        tenant_cache_max_entries=int(os.environ.get("INGRESS_TENANT_CACHE_MAX_ENTRIES", "10000")),
        forward_connect_timeout=float(os.environ.get("INGRESS_FORWARD_CONNECT_TIMEOUT", "5")),
        forward_total_timeout=float(os.environ.get("INGRESS_FORWARD_TOTAL_TIMEOUT", "10")),
        queue_max_size=int(os.environ.get("INGRESS_QUEUE_MAX_SIZE", "100")),
        retry_initial_delay=float(os.environ.get("INGRESS_RETRY_INITIAL_DELAY", "2")),
        retry_max_delay=float(os.environ.get("INGRESS_RETRY_MAX_DELAY", "30")),
        retry_give_up_seconds=float(os.environ.get("INGRESS_RETRY_GIVE_UP_SECONDS", "300")),
        retry_loop_interval=float(os.environ.get("INGRESS_RETRY_LOOP_INTERVAL", "1")),
        wake_nudge_timeout=float(os.environ.get("INGRESS_WAKE_NUDGE_TIMEOUT", "1")),
        wake_nudge_max_chats_per_episode=int(
            os.environ.get("INGRESS_WAKE_NUDGE_MAX_CHATS_PER_EPISODE", "32")
        ),
    )
