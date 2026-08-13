"""Shared pytest fixtures.

Two rules govern every test in this suite:

1. **No live HTTP, ever.** All outbound calls (Railway GraphQL, Telegram Bot API,
   LiteLLM admin API) go through httpx and are intercepted by `respx`. A test that
   hits the network is a bug.
2. **Real DB code path.** Tests run against a throwaway SQLite file (not in-memory)
   so that the background worker / `session_scope()` helpers -- which open their own
   connections -- behave exactly as they do in production against Postgres.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

# --- Environment must be configured *before* control_api modules are imported,
# --- because Settings is read lazily but cached per-process.
TEST_INTERNAL_TOKEN = "test-internal-token"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch) -> Iterator[None]:
    """Point every setting at deterministic test values and a fresh SQLite file."""
    db_path = tmp_path / "control.db"
    env = {
        "DATABASE_URL": f"sqlite:///{db_path}",
        "INTERNAL_API_TOKEN": TEST_INTERNAL_TOKEN,
        "RAILWAY_API_TOKEN": "railway-test-token",
        "RAILWAY_GRAPHQL_URL": "https://backboard.railway.com/graphql/v2",
        "RAILWAY_PROJECT_ID": "proj-123",
        "RAILWAY_ENVIRONMENT_ID": "env-123",
        # No real Railway means no post-create list lag, so the second volume probe
        # is pure sleep. Tests that care about the retry set their own delay.
        "RAILWAY_VOLUME_PROBE_DELAY_SECONDS": "0",
        "TENANT_IMAGE": "ghcr.io/shagarwal/squire/hermes-tenant:v0",
        "INGRESS_PUBLIC_URL": "https://ingress.squire.test",
        "CONTROL_API_URL": "https://control-api.squire.test",
        "LITELLM_BASE_URL": "https://trial-proxy.squire.test",
        "LITELLM_MASTER_KEY": "sk-master-test",
        # Provisioning must never kick off on its own inside tests -- every test
        # drives the state machine explicitly so failures are attributable.
        "PROVISION_AUTO_ADVANCE": "false",
        "PROVISION_WORKER_ENABLED": "false",
        # Zero backoff keeps retry tests fast; the backoff *math* is tested separately.
        "PROVISION_BACKOFF_BASE_SECONDS": "0",
        # TestClient runs BackgroundTasks inline, so the wake-typing loop's
        # sleeps would be REAL sleeps inside every test request that hits
        # /internal/wake-typing. Zero the interval (same trick as the Railway
        # volume-probe delay above); the schedule *math* is tested separately
        # by driving _typing_loop with an injected sleep.
        "WAKE_TYPING_INTERVAL_SECONDS": "0",
        # Pin the repeat count rather than inheriting the code default, so the
        # endpoint test asserting "5 sends" tests config plumbing, not a magic
        # number that happens to match.
        "WAKE_TYPING_REPEATS": "5",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    # Drop cached settings/engine so the new env takes effect.
    from control_api import config, db

    config.get_settings.cache_clear()
    db.reset_engine()

    db.init_db()
    yield

    config.get_settings.cache_clear()
    db.reset_engine()


@pytest.fixture
def settings():
    from control_api.config import get_settings

    return get_settings()


@pytest.fixture
def session() -> Iterator:
    """A plain SQLModel session against the test database."""
    from control_api.db import session_scope

    with session_scope() as s:
        yield s


@pytest.fixture
def app():
    from control_api.main import create_app

    return create_app()


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth() -> dict[str, str]:
    """Headers for the shared internal service-to-service bearer token."""
    return {"Authorization": f"Bearer {TEST_INTERNAL_TOKEN}"}


# --------------------------------------------------------------------------
# Helpers used across tests
# --------------------------------------------------------------------------


def make_bot_token(bot_id: int) -> str:
    """Telegram tokens look like `<numeric bot id>:<secret>`."""
    return f"{bot_id}:AA-test-secret-for-{bot_id}"


@pytest.fixture
def seeded_bot():
    """Insert one available pool bot and return it (detached dict)."""
    from control_api import db
    from control_api.models import Bot, BotStatus

    with db.session_scope() as s:
        bot = Bot(
            id=123456,
            token=make_bot_token(123456),
            username="squire_alpha_bot",
            status=BotStatus.AVAILABLE,
            webhook_secret="whsec-test-123456",
        )
        s.add(bot)
        s.commit()
        s.refresh(bot)
        return bot.model_dump()


os.environ.setdefault("PYTHONHASHSEED", "0")
