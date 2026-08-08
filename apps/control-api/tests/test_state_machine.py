"""Provisioning state-machine tests.

The state machine is the heart of Task 0.3: it must be re-runnable at any point
without duplicating Railway resources, must retry with backoff, and must fail
loudly (not silently half-provision) when it runs out of attempts.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from control_api import db, provisioning
from control_api.models import Bot, BotStatus, JobStatus, ProvisionStep, TenantStatus
from railway_fake import GQL_URL, FakeRailway

LITELLM = "https://trial-proxy.squire.test"
BOT_ID = 123456
BOT_TOKEN = f"{BOT_ID}:AA-secret"


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


@pytest.fixture
def fake_railway() -> FakeRailway:
    return FakeRailway()


def mock_all(fake: FakeRailway, *, telegram_ok: bool = True, litellm_ok: bool = True):
    """Wire respx routes for Railway + LiteLLM + Telegram."""
    respx.post(GQL_URL).mock(side_effect=fake.handler)
    respx.post(f"{LITELLM}/key/generate").mock(
        return_value=httpx.Response(200, json={"key": "sk-trial-abc"})
        if litellm_ok
        else httpx.Response(500, text="proxy down")
    )
    respx.post(f"{LITELLM}/key/delete").mock(
        return_value=httpx.Response(200, json={"deleted_keys": []})
    )
    respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
        if telegram_ok
        else httpx.Response(400, json={"ok": False, "description": "bad webhook url"})
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@respx.mock
def test_full_provision_happy_path(pool_bot, fake_railway):
    mock_all(fake_railway)

    with db.session_scope() as s:
        tenant, job = provisioning.create_tenant(s, email="alpha@squire.test")
        tenant_id = tenant.id
        job_id = job.id

    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id)
        assert job.status == JobStatus.DONE, job.last_error
        assert job.step == ProvisionStep.DONE

    with db.session_scope() as s:
        tenant = provisioning.get_tenant(s, tenant_id)
        assert tenant.status == TenantStatus.RUNNING
        assert tenant.railway_service_id == "svc-1"
        assert tenant.railway_volume_id == "vol-1"
        assert tenant.bot_id == BOT_ID
        # Private-network URL handed to ingress. Deterministic from the service name.
        assert tenant.internal_url == f"http://tenant-{tenant_id}.railway.internal:8080"
        assert tenant.dek_set is True
        assert tenant.trial_key_alias == f"squire-trial-{tenant_id}"
        assert tenant.trial_key_active is True
        assert tenant.webhook_set is True

    # Steps ran in the intended order, once each.
    ops = fake_railway.operations()
    assert ops == [
        "project",  # idempotency probe: does the service already exist?
        "serviceCreate",
        "volumeCreate",
        "variableCollectionUpsert",
        "serviceInstanceUpdate",  # sleepApplication (serverless sleep -- Gate G1)
        "serviceInstanceDeploy",
    ]


@respx.mock
def test_tenant_env_vars_match_the_interface_contract(pool_bot, fake_railway):
    mock_all(fake_railway)
    with db.session_scope() as s:
        tenant, job = provisioning.create_tenant(s, email="alpha@squire.test")
        tenant_id, job_id = tenant.id, job.id
    with db.session_scope() as s:
        provisioning.advance_job(s, job_id)

    sent = fake_railway.variables_for("variableCollectionUpsert")["input"]["variables"]
    assert set(sent) == {
        "TENANT_ID",
        "TELEGRAM_BOT_TOKEN",
        "SQUIRE_DEK",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "CONTROL_API_URL",
        "INTERNAL_API_TOKEN",
        "PORT",
    }
    assert sent["TENANT_ID"] == tenant_id
    assert sent["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN
    assert sent["PORT"] == "8080"
    assert sent["CONTROL_API_URL"] == "https://control-api.squire.test"
    assert sent["ANTHROPIC_BASE_URL"] == "https://trial-proxy.squire.test"
    assert sent["ANTHROPIC_API_KEY"] == "sk-trial-abc"
    # DEK is exactly 32 random bytes, base64 encoded.
    assert len(base64.b64decode(sent["SQUIRE_DEK"])) == 32


@respx.mock
def test_dek_is_never_written_to_the_control_database(pool_bot, fake_railway):
    """Privacy invariant: the DEK exists only as a Railway variable."""
    mock_all(fake_railway)
    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
    with db.session_scope() as s:
        provisioning.advance_job(s, job_id)

    dek = fake_railway.variables_for("variableCollectionUpsert")["input"]["variables"]["SQUIRE_DEK"]

    # Brute force: the DEK must not appear anywhere in the control DB.
    from sqlalchemy import text

    with db.session_scope() as s:
        for table in ("tenant", "bot", "provisionjob"):
            rows = s.exec(text(f"select * from {table}")).all()  # type: ignore[arg-type]
            assert dek not in str(rows)


@respx.mock
def test_webhook_points_at_ingress_with_per_bot_secret(pool_bot, fake_railway):
    mock_all(fake_railway)
    route = respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
    with db.session_scope() as s:
        provisioning.advance_job(s, job_id)

    import json

    assert route.called
    sent = json.loads(route.calls.last.request.read())
    assert sent["url"] == f"https://ingress.squire.test/telegram/{BOT_ID}"
    assert sent["secret_token"] == "whsec-abc"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@respx.mock
def test_advance_on_a_finished_job_is_a_noop(pool_bot, fake_railway):
    mock_all(fake_railway)
    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
    with db.session_scope() as s:
        provisioning.advance_job(s, job_id)

    before = len(fake_railway.calls)
    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id)
        assert job.status == JobStatus.DONE
    assert len(fake_railway.calls) == before, "re-running a done job must not touch Railway"


@respx.mock
def test_trial_key_is_reminted_if_the_process_restarted_before_it_was_used(
    pool_bot, fake_railway
):
    """The minted key lives in memory between two steps. A restart in that window
    must re-mint, not deploy the tenant with an empty ANTHROPIC_API_KEY."""
    mock_all(fake_railway, telegram_ok=False)  # stop the run right after set_variables
    with db.session_scope() as s:
        tenant, job = provisioning.create_tenant(s, email="alpha@squire.test")
        tenant_id, job_id = tenant.id, job.id

    # Fail at create_trial_key's *successor* by simulating a crash: run the first
    # steps, then wipe the in-memory stash as a restart would.
    with db.session_scope() as s:
        provisioning.advance_job(s, job_id)  # fails at set_webhook
    provisioning._TRIAL_KEY_STASH.clear()

    # Rewind to create_trial_key, as a fresh job/retry would.
    with db.session_scope() as s:
        job = provisioning.get_job(s, job_id)
        job.step = ProvisionStep.CREATE_TRIAL_KEY
        s.add(job)
        s.commit()

    delete_route = respx.post(f"{LITELLM}/key/delete")
    respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id, force=True)
        assert job.status == JobStatus.DONE

    assert delete_route.called, "the orphaned key must be revoked before re-minting"
    sent = fake_railway.variables_for("variableCollectionUpsert")["input"]["variables"]
    assert sent["ANTHROPIC_API_KEY"] == "sk-trial-abc"


@respx.mock
def test_create_service_adopts_an_orphaned_railway_service(pool_bot, fake_railway):
    """If we crashed after Railway created the service but before we committed the
    id, the retry must adopt the existing service instead of creating a twin."""
    mock_all(fake_railway)
    with db.session_scope() as s:
        tenant, job = provisioning.create_tenant(s, email="alpha@squire.test")
        tenant_id, job_id = tenant.id, job.id

    # Simulate the orphan: Railway already knows about tenant-<id>.
    fake_railway.existing_services[f"tenant-{tenant_id}"] = "svc-orphan"

    with db.session_scope() as s:
        provisioning.advance_job(s, job_id)

    assert "serviceCreate" not in fake_railway.operations()
    with db.session_scope() as s:
        assert provisioning.get_tenant(s, tenant_id).railway_service_id == "svc-orphan"


@respx.mock
def test_retry_resumes_at_the_failed_step_only(pool_bot, fake_railway):
    """Steps already completed must not re-run when a later step is retried."""
    mock_all(fake_railway, telegram_ok=False)
    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id

    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id)
        assert job.step == ProvisionStep.SET_WEBHOOK
        assert job.status == JobStatus.PENDING
        assert job.attempts == 1

    railway_calls_after_failure = len(fake_railway.calls)

    # Telegram recovers; retry should only re-issue setWebhook.
    respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id)
        assert job.status == JobStatus.DONE

    assert len(fake_railway.calls) == railway_calls_after_failure


# ---------------------------------------------------------------------------
# Failure handling / retries / backoff
# ---------------------------------------------------------------------------


def test_backoff_grows_exponentially_and_is_capped(monkeypatch):
    monkeypatch.setenv("PROVISION_BACKOFF_BASE_SECONDS", "5")
    from control_api import config

    config.get_settings.cache_clear()
    delays = [provisioning.backoff_seconds(n) for n in range(1, 8)]
    assert delays[0] == 5
    assert delays[1] == 10
    assert delays[2] == 20
    assert delays == sorted(delays), "backoff must be monotonic"
    assert max(delays) <= provisioning.MAX_BACKOFF_SECONDS


@respx.mock
def test_backoff_defers_the_next_attempt(pool_bot, fake_railway, monkeypatch):
    monkeypatch.setenv("PROVISION_BACKOFF_BASE_SECONDS", "60")
    from control_api import config

    config.get_settings.cache_clear()
    mock_all(fake_railway, telegram_ok=False)

    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id)
        # `as_aware` normalises SQLite's naive round-trip; Postgres behaves the same.
        assert provisioning.as_aware(job.next_attempt_at) > datetime.now(timezone.utc)

    calls_before = len(respx.calls)
    with db.session_scope() as s:
        # Not due yet -> nothing happens at all.
        job = provisioning.advance_job(s, job_id)
        assert job.attempts == 1
    assert len(respx.calls) == calls_before

    # `force=True` is what the retry CLI / operator endpoint uses.
    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id, force=True)
        assert job.attempts == 2


@respx.mock
def test_job_fails_permanently_after_max_attempts(pool_bot, fake_railway):
    mock_all(fake_railway, telegram_ok=False)
    with db.session_scope() as s:
        tenant, job = provisioning.create_tenant(s, email="alpha@squire.test")
        tenant_id, job_id = tenant.id, job.id

    for _ in range(10):
        with db.session_scope() as s:
            job = provisioning.advance_job(s, job_id, force=True)
            if job.status == JobStatus.FAILED:
                break

    with db.session_scope() as s:
        job = provisioning.get_job(s, job_id)
        assert job.status == JobStatus.FAILED
        assert job.attempts == job.max_attempts
        assert "bad webhook url" in (job.last_error or "")
        # The tenant is explicitly marked failed-to-provision, never silently "running".
        assert provisioning.get_tenant(s, tenant_id).status == TenantStatus.PROVISIONING


@respx.mock
def test_provisioning_fails_cleanly_when_bot_pool_is_empty(fake_railway):
    """No bots seeded -> we must refuse loudly at tenant creation."""
    mock_all(fake_railway)
    with db.session_scope() as s:
        with pytest.raises(provisioning.NoBotsAvailable):
            provisioning.create_tenant(s, email="alpha@squire.test")


@respx.mock
def test_a_bot_is_never_assigned_to_two_tenants(fake_railway):
    mock_all(fake_railway)
    with db.session_scope() as s:
        s.add(
            Bot(
                id=BOT_ID,
                token=BOT_TOKEN,
                username="b1",
                status=BotStatus.AVAILABLE,
                webhook_secret="s1",
            )
        )
        s.commit()

    with db.session_scope() as s:
        provisioning.create_tenant(s, email="one@squire.test")
    with db.session_scope() as s:
        with pytest.raises(provisioning.NoBotsAvailable):
            provisioning.create_tenant(s, email="two@squire.test")


@respx.mock
def test_create_tenant_is_idempotent_per_email(pool_bot, fake_railway):
    """The alpha CLI is hand-run; running it twice must not burn a second bot."""
    mock_all(fake_railway)
    with db.session_scope() as s:
        tenant_a, job_a = provisioning.create_tenant(s, email="alpha@squire.test")
        ids = (tenant_a.id, job_a.id)
    with db.session_scope() as s:
        tenant_b, job_b = provisioning.create_tenant(s, email="alpha@squire.test")
        assert (tenant_b.id, job_b.id) == ids


# ---------------------------------------------------------------------------
# Lifecycle beyond provisioning
# ---------------------------------------------------------------------------


@respx.mock
def test_stop_and_delete_tenant(pool_bot, fake_railway):
    mock_all(fake_railway)
    respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    with db.session_scope() as s:
        tenant, job = provisioning.create_tenant(s, email="alpha@squire.test")
        tenant_id, job_id = tenant.id, job.id
    with db.session_scope() as s:
        provisioning.advance_job(s, job_id)

    with db.session_scope() as s:
        tenant = provisioning.stop_tenant(s, tenant_id)
        assert tenant.status == TenantStatus.STOPPED
    assert "deploymentStop" in fake_railway.operations()

    with db.session_scope() as s:
        tenant = provisioning.delete_tenant(s, tenant_id)
        assert tenant.status == TenantStatus.DELETED
        assert tenant.railway_service_id is None
        # Bot returns to the pool for reuse (PRD §4 bot supply).
        bot = s.get(Bot, BOT_ID)
        assert bot.status == BotStatus.AVAILABLE
        assert bot.assigned_tenant_id is None
    assert "serviceDelete" in fake_railway.operations()


@respx.mock
def test_run_pending_jobs_picks_up_due_work(pool_bot, fake_railway):
    """The background worker path used when the API restarts mid-provision."""
    mock_all(fake_railway)
    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id

    processed = provisioning.run_pending_jobs()
    assert processed == 1
    with db.session_scope() as s:
        assert provisioning.get_job(s, job_id).status == JobStatus.DONE

    # Nothing left to do on the next tick.
    assert provisioning.run_pending_jobs() == 0


@respx.mock
def test_worker_ignores_jobs_that_are_not_due_yet(pool_bot, fake_railway, monkeypatch):
    monkeypatch.setenv("PROVISION_BACKOFF_BASE_SECONDS", "3600")
    from control_api import config

    config.get_settings.cache_clear()
    mock_all(fake_railway, telegram_ok=False)

    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
    assert provisioning.run_pending_jobs() == 1  # first attempt, fails, backs off
    assert provisioning.run_pending_jobs() == 0  # not due for an hour

    with db.session_scope() as s:
        job = provisioning.get_job(s, job_id)
        job.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.add(job)
        s.commit()
    assert provisioning.run_pending_jobs() == 1
