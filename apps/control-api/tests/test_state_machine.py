"""Provisioning state-machine tests.

The state machine is the heart of Task 0.3: it must be re-runnable at any point
without duplicating Railway resources, must retry with backoff, and must fail
loudly (not silently half-provision) when it runs out of attempts.
"""

from __future__ import annotations

import base64
import threading
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
        "project",  # idempotency probe: does a volume already exist?
        "volumeCreate",
        "variableCollectionUpsert",
        "variables",  # read-back: confirm SQUIRE_DEK landed (names only)
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
        "TELEGRAM_WEBHOOK_URL",
        "TELEGRAM_WEBHOOK_SECRET",
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
def test_tenant_webhook_vars_match_what_we_register_with_telegram(pool_bot, fake_railway):
    """The tenant image self-registers its webhook on boot. If the url or secret we
    hand it differed by even a character, its setWebhook would overwrite ours and
    ingress would start rejecting updates on a stale secret."""
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

    registered = json.loads(route.calls.last.request.read())
    sent = fake_railway.variables_for("variableCollectionUpsert")["input"]["variables"]

    assert sent["TELEGRAM_WEBHOOK_URL"] == registered["url"]
    assert sent["TELEGRAM_WEBHOOK_SECRET"] == registered["secret_token"]
    # And both agree with what ingress will be told via the by-bot lookup.
    assert sent["TELEGRAM_WEBHOOK_URL"] == f"https://ingress.squire.test/telegram/{BOT_ID}"
    assert sent["TELEGRAM_WEBHOOK_SECRET"] == "whsec-abc"


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
def test_trial_key_survives_a_set_variables_failure(pool_bot, fake_railway):
    """REGRESSION: the key was popped from the stash *before* set_variables ran, so
    a transient failure there lost it forever -- and because the machine only moves
    forward, `create_trial_key` never ran again. The retry then succeeded with an
    empty ANTHROPIC_API_KEY and the job reported DONE.

    This test drives exactly that sequence through the public API only (no manual
    step rewinding), so it fails against the old code.
    """
    mock_all(fake_railway)
    fake_railway.fail_on.add("variableCollectionUpsert")

    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id

    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id)
        assert job.step == ProvisionStep.SET_VARIABLES
        assert job.status == JobStatus.PENDING

    # Railway recovers; the retry must still carry a real key.
    fake_railway.fail_on.discard("variableCollectionUpsert")
    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id, force=True)
        assert job.status == JobStatus.DONE, job.last_error

    sent = fake_railway.variables_for("variableCollectionUpsert")["input"]["variables"]
    assert sent["ANTHROPIC_API_KEY"] == "sk-trial-abc"
    assert sent["ANTHROPIC_API_KEY"], "tenant must never deploy with an empty trial key"


@respx.mock
def test_trial_key_is_reminted_if_the_key_material_was_lost(pool_bot, fake_railway):
    """Process restart between create_trial_key and set_variables: the material is
    gone (it only ever lived in memory), so set_variables must revoke and re-mint
    rather than deploy an empty key."""
    mock_all(fake_railway)
    fake_railway.fail_on.add("variableCollectionUpsert")

    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
    with db.session_scope() as s:
        provisioning.advance_job(s, job_id)  # stops at set_variables

    # Simulate the restart: in-memory stash is gone, DB still says a key is active.
    provisioning._TRIAL_KEY_STASH.clear()

    delete_route = respx.post(f"{LITELLM}/key/delete").mock(
        return_value=httpx.Response(200, json={"deleted_keys": []})
    )
    fake_railway.fail_on.discard("variableCollectionUpsert")
    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id, force=True)
        assert job.status == JobStatus.DONE, job.last_error

    assert delete_route.called, "the orphaned key must be revoked before re-minting"
    sent = fake_railway.variables_for("variableCollectionUpsert")["input"]["variables"]
    assert sent["ANTHROPIC_API_KEY"] == "sk-trial-abc"


@respx.mock
def test_concurrent_advance_creates_exactly_one_service(pool_bot, fake_railway):
    """REGRESSION: nothing claimed a job, so the BackgroundTask, the /advance
    endpoint and the sweeper could all run `create_service` at once -- two Railway
    services, two bills, for one tenant."""
    mock_all(fake_railway)

    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)  # maximise the overlap
            with db.session_scope() as s:
                provisioning.advance_job(s, job_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert fake_railway.operations().count("serviceCreate") == 1
    # And every other Railway mutation ran exactly once too.
    for op in ("volumeCreate", "variableCollectionUpsert", "serviceInstanceDeploy"):
        assert fake_railway.operations().count(op) == 1, op

    with db.session_scope() as s:
        assert provisioning.get_job(s, job_id).status == JobStatus.DONE


@respx.mock
def test_a_claimed_job_is_skipped_by_other_callers(pool_bot, fake_railway):
    """A RUNNING job is left alone rather than double-driven."""
    mock_all(fake_railway)
    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
        job.status = JobStatus.RUNNING  # pretend another worker holds it
        s.add(job)
        s.commit()

    with db.session_scope() as s:
        result = provisioning.advance_job(s, job_id, force=True)
        assert result.status == JobStatus.RUNNING
    assert fake_railway.calls == [], "must not touch Railway while another worker holds it"
    assert provisioning.run_pending_jobs() == 0


@respx.mock
def test_a_stale_claim_is_reclaimed(pool_bot, fake_railway):
    """A worker that died mid-run must not strand its job in RUNNING forever."""
    mock_all(fake_railway)
    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
        job.status = JobStatus.RUNNING
        job.updated_at = datetime.now(timezone.utc) - timedelta(
            seconds=provisioning.STALE_CLAIM_SECONDS + 60
        )
        s.add(job)
        s.commit()

    assert provisioning.run_pending_jobs() == 1
    with db.session_scope() as s:
        assert provisioning.get_job(s, job_id).status == JobStatus.DONE


@respx.mock
def test_advance_does_not_sweep_the_fleet_for_a_finished_job(pool_bot, fake_railway, monkeypatch):
    """`infra/provision.py` polls /advance in a loop, including after the job is DONE.

    Each of those calls used to run a fleet-wide `UPDATE provisionjob` that could not
    possibly change a terminal job's answer. Gate it: only a job stuck in RUNNING can
    need reclaiming, and the background sweeper still reclaims fleet-wide on its tick.
    """
    mock_all(fake_railway)
    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
    with db.session_scope() as s:
        assert provisioning.advance_job(s, job_id).status == JobStatus.DONE

    calls = []
    real_reclaim = provisioning.reclaim_stale_jobs
    monkeypatch.setattr(
        provisioning,
        "reclaim_stale_jobs",
        lambda session: (calls.append(1), real_reclaim(session))[1],
    )

    with db.session_scope() as s:
        assert provisioning.advance_job(s, job_id, force=True).status == JobStatus.DONE
    assert calls == [], "a terminal job must not trigger the stale-claim sweep"

    # ...but a job stranded in RUNNING still gets its reclaim, or it never recovers.
    with db.session_scope() as s:
        job = provisioning.get_job(s, job_id)
        job.status = JobStatus.RUNNING
        job.updated_at = datetime.now(timezone.utc) - timedelta(
            seconds=provisioning.STALE_CLAIM_SECONDS + 60
        )
        s.add(job)
        s.commit()
    with db.session_scope() as s:
        assert provisioning.advance_job(s, job_id, force=True).status == JobStatus.DONE
    assert calls, "a RUNNING job must still be reclaimable through /advance"


@respx.mock
def test_set_variables_refuses_to_deploy_a_tenant_with_no_dek(
    pool_bot, fake_railway, monkeypatch
):
    """SQUIRE_DEK is omitted only when a DEK is already set.

    If that invariant ever breaks, the tenant deploys with a volume it cannot read
    and refuses to boot. So it is a hard failure of the step -- not a bare `assert`,
    which vanishes under `python -O` and would let the deploy proceed.

    The bug is simulated the way a real one would look: DEK generation silently
    produces nothing while `dek_set` is still False.
    """
    mock_all(fake_railway)
    from control_api import crypto

    monkeypatch.setattr(crypto, "generate_dek", lambda: None)

    with db.session_scope() as s:
        tenant, _ = provisioning.create_tenant(s, email="alpha@squire.test")
        assert tenant.dek_set is False
        with pytest.raises(provisioning.ProvisioningError, match="without a DEK"):
            provisioning._step_set_variables(
                s, tenant, provisioning.ProvisionClients.build()
            )
    # Nothing was written to Railway -- the guard fires before the upsert.
    assert "variableCollectionUpsert" not in fake_railway.operations()


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


# ---------------------------------------------------------------------------
# Secret hygiene in persisted error text
# ---------------------------------------------------------------------------


@respx.mock
def test_last_error_never_leaks_secrets_from_the_variables_payload(pool_bot, fake_railway):
    """REGRESSION: Railway echoes invalid input back in GraphQL validation errors.
    For set_variables that input is the whole credential payload, and `last_error`
    is both persisted and served over the internal API."""
    mock_all(fake_railway)
    captured: dict[str, str] = {}

    def echo_the_payload(request):
        import json as _json

        payload = _json.loads(request.read())
        if "variableCollectionUpsert" in payload["query"]:
            sent = payload["variables"]["input"]
            captured.update(sent["variables"])
            # Railway-style: reject and quote the offending input verbatim.
            return httpx.Response(
                200,
                json={"errors": [{"message": f"Problem processing request: {sent}"}]},
            )
        return fake_railway.handler(request)

    respx.post(GQL_URL).mock(side_effect=echo_the_payload)

    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id)
        assert job.status == JobStatus.PENDING
        error = job.last_error or ""

    assert "Problem processing request" in error, "the useful part must survive"
    for name in (
        "SQUIRE_DEK",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "INTERNAL_API_TOKEN",
    ):
        assert captured[name] not in error, f"{name} value leaked into last_error"
    assert "[REDACTED]" in error


@respx.mock
def test_retry_after_set_variables_failure_keeps_the_same_dek(pool_bot, fake_railway):
    """Once a DEK is set, a retry must omit SQUIRE_DEK entirely -- rotating it would
    orphan a volume already encrypted with the old key."""
    mock_all(fake_railway)
    with db.session_scope() as s:
        tenant, job = provisioning.create_tenant(s, email="alpha@squire.test")
        tenant_id, job_id = tenant.id, job.id
    with db.session_scope() as s:
        provisioning.advance_job(s, job_id)

    first = fake_railway.variables_for("variableCollectionUpsert")["input"]["variables"]
    assert "SQUIRE_DEK" in first
    with db.session_scope() as s:
        assert provisioning.get_tenant(s, tenant_id).dek_set is True

    # Re-run the step directly, as a retry of a resumed job would.
    with db.session_scope() as s:
        tenant = provisioning.get_tenant(s, tenant_id)
        provisioning._step_set_variables(s, tenant, provisioning.ProvisionClients.build())
    second = fake_railway.variables_for("variableCollectionUpsert")["input"]["variables"]
    assert "SQUIRE_DEK" not in second, "a set DEK must never be rotated by a retry"


@respx.mock
def test_dek_set_requires_the_variable_to_be_readable_back(pool_bot, fake_railway):
    """A mis-shaped upsert that still returns 200 must not be recorded as success."""
    mock_all(fake_railway)
    fake_railway.variable_names = {"TENANT_ID"}  # SQUIRE_DEK conspicuously absent

    with db.session_scope() as s:
        tenant, job = provisioning.create_tenant(s, email="alpha@squire.test")
        tenant_id, job_id = tenant.id, job.id
    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id)
        assert job.status == JobStatus.PENDING
        assert job.step == ProvisionStep.SET_VARIABLES
        assert "SQUIRE_DEK missing" in (job.last_error or "")
        assert provisioning.get_tenant(s, tenant_id).dek_set is False


@respx.mock
def test_volume_preflight_probe_prevents_a_duplicate_volume(pool_bot, fake_railway):
    """Idempotency must not depend only on matching Railway's error wording."""
    mock_all(fake_railway)
    fake_railway.existing_volume_service_ids.add("svc-1")

    with db.session_scope() as s:
        tenant, job = provisioning.create_tenant(s, email="alpha@squire.test")
        tenant_id, job_id = tenant.id, job.id
    with db.session_scope() as s:
        provisioning.advance_job(s, job_id)

    assert "volumeCreate" not in fake_railway.operations()
    with db.session_scope() as s:
        assert provisioning.get_tenant(s, tenant_id).railway_volume_id == "vol-existing"


# ---------------------------------------------------------------------------
# Failed-job recovery
# ---------------------------------------------------------------------------


@respx.mock
def test_a_failed_job_can_be_retried_with_a_fresh_job(pool_bot, fake_railway):
    """REGRESSION: a FAILED job was a dead end -- create_tenant handed back the same
    corpse, /advance no-opped on it, and the CLI exited 1 forever."""
    mock_all(fake_railway, telegram_ok=False)
    with db.session_scope() as s:
        tenant, job = provisioning.create_tenant(s, email="alpha@squire.test")
        tenant_id, failed_job_id = tenant.id, job.id

    for _ in range(10):
        with db.session_scope() as s:
            status = provisioning.advance_job(s, failed_job_id, force=True).status
        if status == JobStatus.FAILED:
            break
    assert status == JobStatus.FAILED

    # Telegram recovers; re-running the CLI must make progress again.
    respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    with db.session_scope() as s:
        same_tenant, new_job = provisioning.create_tenant(s, email="alpha@squire.test")
        assert same_tenant.id == tenant_id, "must not create a second tenant"
        assert new_job.id != failed_job_id, "a FAILED job must not be handed back"
        new_job_id = new_job.id
        # Resumes where the failure happened rather than replaying Railway calls.
        assert new_job.step == ProvisionStep.SET_WEBHOOK

    with db.session_scope() as s:
        job = provisioning.advance_job(s, new_job_id, force=True)
        assert job.status == JobStatus.DONE
    with db.session_scope() as s:
        assert provisioning.get_tenant(s, tenant_id).status == TenantStatus.RUNNING
    # The retry created no duplicate Railway resources.
    assert fake_railway.operations().count("serviceCreate") == 1
