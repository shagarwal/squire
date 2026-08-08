"""Worker-thread lifecycle and a full-stack provisioning integration test.

Every other test drives the state machine explicitly with auto-advance and the
sweeper disabled. These two turn both back on -- which is the production
configuration, and the one where the concurrency bug the reviewer found actually
bit -- and assert the tenant still comes out provisioned exactly once.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest
import respx

from control_api import db, provisioning
from control_api.models import Bot, BotStatus, JobStatus, TenantStatus
from railway_fake import GQL_URL, FakeRailway

LITELLM = "https://trial-proxy.squire.test"
BOT_ID = 123456
BOT_TOKEN = f"{BOT_ID}:AA-secret"


@pytest.fixture
def live_worker_env(monkeypatch):
    """Production-shaped config: BackgroundTask advance AND the sweeper both on."""
    monkeypatch.setenv("PROVISION_AUTO_ADVANCE", "true")
    monkeypatch.setenv("PROVISION_WORKER_ENABLED", "true")
    monkeypatch.setenv("PROVISION_WORKER_INTERVAL_SECONDS", "0.05")
    from control_api import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def pool_bot():
    with db.session_scope() as s:
        s.add(
            Bot(
                id=BOT_ID,
                token=BOT_TOKEN,
                username="squire_alpha_bot",
                status=BotStatus.AVAILABLE,
                webhook_secret="whsec-abc",
            )
        )
        s.commit()


def mock_all(fake: FakeRailway) -> None:
    respx.post(GQL_URL).mock(side_effect=fake.handler)
    respx.post(f"{LITELLM}/key/generate").mock(
        return_value=httpx.Response(200, json={"key": "sk-trial-abc"})
    )
    respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )


def _worker_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "provision-worker"]


def test_worker_thread_starts_ticks_and_joins(live_worker_env, app):
    """The sweeper must actually run, and must not outlive the app."""
    from fastapi.testclient import TestClient

    ticks = 0
    original = provisioning.run_pending_jobs

    def counting_sweep(*args, **kwargs):
        nonlocal ticks
        ticks += 1
        return original(*args, **kwargs)

    provisioning.run_pending_jobs = counting_sweep  # type: ignore[assignment]
    try:
        assert _worker_threads() == [], "no worker before startup"
        with TestClient(app) as client:
            assert len(_worker_threads()) == 1, "worker starts with the app"
            assert client.get("/healthz").status_code == 200
            deadline = time.monotonic() + 5
            while ticks < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ticks >= 2, f"sweeper did not tick (ticks={ticks})"
        assert _worker_threads() == [], "worker must join on shutdown"
    finally:
        provisioning.run_pending_jobs = original  # type: ignore[assignment]


@respx.mock
def test_end_to_end_provision_with_auto_advance_and_worker_both_live(
    live_worker_env, pool_bot, auth
):
    """Full stack: POST /internal/tenants, with the BackgroundTask and the sweeper
    both racing to drive the same job. Exactly one Railway service must result."""
    from fastapi.testclient import TestClient

    from control_api.main import create_app

    fake = FakeRailway()
    mock_all(fake)

    with TestClient(create_app()) as client:
        response = client.post(
            "/internal/tenants", json={"email": "alpha@squire.test"}, headers=auth
        )
        assert response.status_code == 201, response.text
        tenant_id = response.json()["tenant_id"]
        job_id = response.json()["job_id"]

        # The BackgroundTask runs on response close; the sweeper may also fire.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = client.get(f"/internal/provision-jobs/{job_id}", headers=auth).json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.05)

        assert job["status"] == "done", job
        tenant = client.get(f"/internal/tenants/{tenant_id}", headers=auth).json()
        assert tenant["status"] == "running"

        # Ingress can resolve the tenant the moment provisioning finishes.
        by_bot = client.get(f"/internal/tenants/by-bot/{BOT_ID}", headers=auth)
        assert by_bot.status_code == 200
        assert by_bot.json()["tenant_id"] == tenant_id
        assert by_bot.json()["status"] == "running"

    # The whole point: no duplicated Railway resources despite three drivers.
    for op in ("serviceCreate", "volumeCreate", "variableCollectionUpsert",
               "serviceInstanceDeploy"):
        assert fake.operations().count(op) == 1, (op, fake.operations())

    with db.session_scope() as s:
        assert provisioning.get_job(s, job_id).status == JobStatus.DONE
        assert provisioning.get_tenant(s, tenant_id).status == TenantStatus.RUNNING
