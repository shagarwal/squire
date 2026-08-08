"""The provisioning state machine.

A tenant is provisioned by walking `PROVISION_STEP_ORDER` one step at a time.
Three properties are non-negotiable:

1. **Idempotent.** Every step checks whether its effect already happened (in our DB
   or in Railway) before doing anything. Re-running a job from any point is safe.
2. **Bounded.** Failures increment an attempt counter and schedule the next attempt
   with exponential backoff; after `max_attempts` the job is FAILED with a reason.
   It never spins on Railway's API.
3. **Crash-safe.** State lives in `provision_jobs` + `tenants`, not in memory. The
   background sweeper (`run_pending_jobs`) picks up anything a restart stranded.

No Celery, no queue, no broker -- v0 does not need one, and the whole thing fits in
one readable file.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from control_api import crypto
from control_api.clients.railway import RailwayClient
from control_api.clients.telegram import TelegramClient
from control_api.config import Settings, get_settings
from control_api.db import session_scope
from control_api.models import (
    PROVISION_STEP_ORDER,
    Bot,
    BotStatus,
    JobStatus,
    ProvisionJob,
    ProvisionStep,
    Tenant,
    TenantStatus,
    utcnow,
)
from control_api.trial_keys import TrialKeyClient

log = logging.getLogger(__name__)

#: Never wait longer than this between attempts, regardless of the backoff curve.
MAX_BACKOFF_SECONDS = 300.0

#: A job left RUNNING for longer than this is assumed to belong to a dead worker
#: and is returned to the pool. Must comfortably exceed the slowest single step.
STALE_CLAIM_SECONDS = 300.0

#: Shortest string we will treat as a secret worth redacting. Guards against
#: blanking a whole error message because some config value happened to be "" or
#: a single character.
_MIN_REDACTABLE_SECRET_LEN = 8

#: Secret values in scope for the step currently executing. Populated per step in
#: `advance_job` and added to by handlers that build credential-bearing payloads.
#: A ContextVar (not a plain global) so the background worker thread and request
#: threads never clobber each other's set. The default is None rather than an empty
#: set -- a mutable default would be shared by every context that never called
#: set(), so a stray `register_step_secret` would leak into unrelated redactions.
_STEP_SECRETS: ContextVar[set[str] | None] = ContextVar("_STEP_SECRETS", default=None)


def _baseline_secrets() -> set[str]:
    """Long-lived secrets that could appear in any third-party error message."""
    settings = get_settings()
    return {
        settings.internal_api_token,
        settings.railway_api_token,
        settings.litellm_master_key,
    }


def register_step_secret(value: str | None) -> None:
    """Mark a value as unloggable for the duration of the current step."""
    if not value:
        return
    current = _STEP_SECRETS.get()
    if current is None:
        current = set()
        _STEP_SECRETS.set(current)
    current.add(value)


def _redact(message: str) -> str:
    """Strip known secret values out of third-party error text.

    Railway's GraphQL validation errors happily echo the offending input back --
    which for `set_variables` is the whole variable payload, including SQUIRE_DEK,
    TELEGRAM_BOT_TOKEN and INTERNAL_API_TOKEN. `last_error` is persisted AND served
    over the internal API, so it must never carry key material.
    """
    for secret in _STEP_SECRETS.get() or ():
        if secret and len(secret) >= _MIN_REDACTABLE_SECRET_LEN:
            message = message.replace(secret, "[REDACTED]")
    return message


class ProvisioningError(RuntimeError):
    pass


class NoBotsAvailable(ProvisioningError):
    """The bot pool is empty. Operator action required (run a BotFather batch)."""


class TenantNotFound(ProvisioningError):
    pass


class JobNotFound(ProvisioningError):
    pass


@dataclass
class ProvisionClients:
    """Bundle of outbound clients, injectable so failure paths are easy to test."""

    railway: RailwayClient
    telegram: TelegramClient
    trial: TrialKeyClient

    @classmethod
    def build(cls) -> "ProvisionClients":
        return cls(
            railway=RailwayClient(),
            telegram=TelegramClient(),
            trial=TrialKeyClient(),
        )


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def get_tenant(session: Session, tenant_id: str) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise TenantNotFound(tenant_id)
    return tenant


def get_job(session: Session, job_id: str) -> ProvisionJob:
    job = session.get(ProvisionJob, job_id)
    if job is None:
        raise JobNotFound(job_id)
    return job


def get_bot(session: Session, bot_id: int) -> Bot | None:
    return session.get(Bot, bot_id)


# ---------------------------------------------------------------------------
# Naming / derived values
# ---------------------------------------------------------------------------


def service_name_for(tenant_id: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return f"{settings.tenant_service_name_prefix}{tenant_id}"


def internal_url_for(tenant_id: str, settings: Settings | None = None) -> str:
    """Railway private-network URL for a tenant service.

    Railway derives the private hostname from the service name, so this is
    deterministic -- no extra API round-trip, and ingress can be handed the URL the
    moment the service is created.
    """
    settings = settings or get_settings()
    name = service_name_for(tenant_id, settings)
    return f"http://{name}.{settings.railway_private_domain_suffix}:{settings.tenant_port}"


def telegram_webhook_url(bot_id: int, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return f"{settings.ingress_public_url.rstrip('/')}/telegram/{bot_id}"


def telegram_link(username: str) -> str:
    return f"https://t.me/{username}"


def backoff_seconds(attempts: int) -> float:
    """Exponential backoff: base * 2^(attempts-1), capped."""
    base = get_settings().provision_backoff_base_seconds
    return min(base * (2 ** max(attempts - 1, 0)), MAX_BACKOFF_SECONDS)


# ---------------------------------------------------------------------------
# Bot pool
# ---------------------------------------------------------------------------


def assign_bot(session: Session, tenant_id: str) -> Bot:
    """Claim one available pool bot for a tenant.

    The claim is a conditional UPDATE (`WHERE assigned_tenant_id IS NULL`) with a
    retry loop rather than `SELECT ... FOR UPDATE SKIP LOCKED`, so the same code
    works on Postgres in prod and SQLite in tests. Two concurrent provisions can
    never end up sharing a bot: the loser's UPDATE matches zero rows and it retries.
    """
    for _ in range(10):
        candidate = session.exec(
            select(Bot)
            .where(Bot.assigned_tenant_id.is_(None))  # type: ignore[union-attr]
            .where(Bot.status == BotStatus.AVAILABLE)
            .limit(1)
        ).first()
        if candidate is None:
            raise NoBotsAvailable("no bots available in the pool")

        result = session.exec(  # type: ignore[call-overload]
            Bot.__table__.update()
            .where(Bot.__table__.c.id == candidate.id)
            .where(Bot.__table__.c.assigned_tenant_id.is_(None))
            .values(assigned_tenant_id=tenant_id, status=BotStatus.ASSIGNED.value)
        )
        if result.rowcount:
            session.commit()
            session.expire_all()
            return session.get(Bot, candidate.id)  # type: ignore[return-value]
        # Lost the race -- another provision grabbed it. Try the next one.
        session.rollback()
        session.expire_all()
    raise NoBotsAvailable("could not claim a bot after repeated contention")


def release_bot(session: Session, bot: Bot) -> None:
    """Return a bot to the pool (tenant deleted). PRD §4: pool bots are recycled."""
    bot.assigned_tenant_id = None
    bot.status = BotStatus.AVAILABLE
    session.add(bot)


# ---------------------------------------------------------------------------
# Tenant creation
# ---------------------------------------------------------------------------


def create_tenant(session: Session, email: str) -> tuple[Tenant, ProvisionJob]:
    """Register a tenant, claim a bot, and queue a provisioning job.

    Idempotent per email: the alpha CLI is hand-run and re-running it must not burn
    a second pool bot. Returns the existing tenant + its most recent job instead.
    """
    email = email.strip().lower()
    existing = session.exec(select(Tenant).where(Tenant.email == email)).first()
    if existing is not None:
        job = session.exec(
            select(ProvisionJob)
            .where(ProvisionJob.tenant_id == existing.id)
            .order_by(ProvisionJob.created_at.desc())  # type: ignore[union-attr]
        ).first()
        # A FAILED job is otherwise a dead end: `advance_job` no-ops on it, so
        # re-running the CLI would return the same corpse and exit 1 forever.
        # Retrying provisioning means a fresh job, resuming from the completed
        # steps recorded on the tenant row.
        if job is None or job.status == JobStatus.FAILED:
            log.info("starting a fresh provisioning job for tenant %s", existing.id)
            job = _new_job(session, existing.id, step=job.step if job else None)
        return existing, job

    tenant_id = crypto.generate_tenant_id()
    settings = get_settings()
    tenant = Tenant(
        id=tenant_id,
        email=email,
        status=TenantStatus.PROVISIONING,
        railway_service_name=service_name_for(tenant_id, settings),
        # Known up-front from the service name; ingress can resolve it immediately.
        internal_url=internal_url_for(tenant_id, settings),
    )
    session.add(tenant)
    session.commit()

    try:
        bot = assign_bot(session, tenant_id)
    except Exception:
        # Don't leave a bot-less tenant lying around; the caller will surface 409.
        # Broad on purpose: a DB error here would otherwise orphan the row AND
        # permanently block this email (the unique index would reject a retry).
        session.rollback()
        orphan = session.get(Tenant, tenant_id)
        if orphan is not None:
            session.delete(orphan)
            session.commit()
        raise

    tenant = session.get(Tenant, tenant_id)
    tenant.bot_id = bot.id
    tenant.updated_at = utcnow()
    session.add(tenant)
    job = _new_job(session, tenant_id)
    session.commit()
    session.refresh(tenant)
    session.refresh(job)
    return tenant, job


def _new_job(
    session: Session, tenant_id: str, step: ProvisionStep | None = None
) -> ProvisionJob:
    """Queue a provisioning job.

    `step` resumes a retry where the previous job died. Starting from
    CREATE_SERVICE would also be correct (every step is idempotent), but it wastes
    a Railway round-trip per already-completed step.
    """
    job = ProvisionJob(
        id=crypto.generate_job_id(),
        tenant_id=tenant_id,
        step=step or ProvisionStep.CREATE_SERVICE,
        status=JobStatus.PENDING,
        max_attempts=get_settings().provision_max_attempts,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


# ---------------------------------------------------------------------------
# Step handlers
#
# Each handler takes (session, tenant, clients) and is responsible for its own
# idempotency. They return nothing; raising means "retry this step later".
# ---------------------------------------------------------------------------


def _step_create_service(session: Session, tenant: Tenant, clients: ProvisionClients) -> None:
    if tenant.railway_service_id:
        return  # already done

    settings = get_settings()
    name = tenant.railway_service_name or service_name_for(tenant.id, settings)

    # Adopt an orphan first: if a previous attempt created the service but died
    # before committing the id, creating again would double our Railway bill.
    service_id = clients.railway.find_service_by_name(name)
    if service_id is None:
        service_id = clients.railway.create_service(name=name, image=settings.tenant_image)

    tenant.railway_service_name = name
    tenant.railway_service_id = service_id
    tenant.internal_url = internal_url_for(tenant.id, settings)
    _touch(session, tenant)


def _step_attach_volume(session: Session, tenant: Tenant, clients: ProvisionClients) -> None:
    if tenant.railway_volume_id:
        return
    settings = get_settings()

    # Pre-flight probe first: a volume created by a previous attempt that died
    # before committing is detected positively here, rather than relying on us
    # recognising Railway's duplicate-volume error wording.
    volume_id = clients.railway.find_volume_for_service(tenant.railway_service_id)
    if volume_id is None:
        volume_id = clients.railway.attach_volume(
            tenant.railway_service_id, settings.tenant_volume_mount_path
        )
    # `None` still means "Railway said it already exists" (probe inconclusive but
    # the create was rejected as a duplicate); record a sentinel so we stop retrying.
    tenant.railway_volume_id = volume_id or "existing"
    _touch(session, tenant)


def _mint_trial_key(session: Session, tenant: Tenant, clients: ProvisionClients) -> str | None:
    """Mint (or re-mint) this tenant's trial key and stash the material in memory.

    Shared by `_step_create_trial_key` and `_step_set_variables` so the recovery
    path is reachable from either -- the state machine only moves forward, so a key
    lost after `create_trial_key` completed can only be replaced by `set_variables`.
    """
    # An alias already marked active means a previous mint's material is gone
    # (process restart, or a failed `set_variables` in an earlier process). LiteLLM
    # rejects a duplicate alias, so revoke before re-minting.
    if tenant.trial_key_active and tenant.trial_key_alias:
        log.warning("re-minting orphaned trial key for tenant %s", tenant.id)
        clients.trial.delete_trial_key(tenant.trial_key_alias)
        tenant.trial_key_active = False

    trial_key = clients.trial.create_trial_key(tenant.id)
    if trial_key is None:
        # Trial proxy not deployed yet (Task 0.5). Provisioning continues; the
        # tenant simply boots without a trial key and the concierge will ask the
        # user to connect their own LLM immediately.
        log.warning("no trial key issued for tenant %s (LiteLLM disabled)", tenant.id)
        tenant.trial_key_alias = None
        tenant.trial_key_active = False
        _touch(session, tenant)
        return None

    tenant.trial_key_alias = trial_key.alias
    tenant.trial_key_active = True
    # Stash the key material in memory ONLY, for the next step. It is never
    # assigned to a mapped column, so it cannot be persisted.
    _stash_trial_key(tenant.id, trial_key.key)
    _touch(session, tenant)
    return trial_key.key


def _step_create_trial_key(session: Session, tenant: Tenant, clients: ProvisionClients) -> None:
    if tenant.trial_key_active and _peek_trial_key(tenant.id):
        return  # already minted, material still in hand
    _mint_trial_key(session, tenant, clients)


def _step_set_variables(session: Session, tenant: Tenant, clients: ProvisionClients) -> None:
    """Write the tenant's environment onto its Railway service.

    This is where the DEK is born and immediately forgotten by the control plane.
    The variable set is an interface contract with the tenant image (Task 0.2).
    """
    settings = get_settings()
    bot = get_bot(session, tenant.bot_id) if tenant.bot_id else None
    if bot is None:
        raise ProvisioningError(f"tenant {tenant.id} has no assigned bot")

    # Peek, never pop: if the upsert below fails we must still hold the key for the
    # retry. Popping here loses it permanently -- the machine only moves forward, so
    # `create_trial_key` never runs again -- and the tenant would then deploy with an
    # empty ANTHROPIC_API_KEY while the job cheerfully reported DONE.
    trial_key = _peek_trial_key(tenant.id)
    if trial_key is None and tenant.trial_key_active:
        # Material lost (restart, or a failure in a previous process). Re-mint here,
        # because this is the last step that can still fix it.
        trial_key = _mint_trial_key(session, tenant, clients)

    dek = crypto.generate_dek() if not tenant.dek_set else None

    variables = {
        "TENANT_ID": tenant.id,
        "TELEGRAM_BOT_TOKEN": bot.token,
        # The tenant image's PTB adapter self-registers its webhook on boot. Handing
        # it the exact url+secret control-api registers below makes that call a
        # harmless re-register instead of a fight: identical arguments, so
        # last-writer-wins is a no-op and ingress's cached secret stays valid.
        # control-api remains the authoritative registrar (see _step_set_webhook).
        "TELEGRAM_WEBHOOK_URL": telegram_webhook_url(bot.id, settings),
        "TELEGRAM_WEBHOOK_SECRET": bot.webhook_secret,
        "ANTHROPIC_BASE_URL": settings.effective_trial_base_url,
        "ANTHROPIC_API_KEY": trial_key or "",
        "CONTROL_API_URL": settings.control_api_url,
        "INTERNAL_API_TOKEN": settings.internal_api_token,
        "PORT": str(settings.tenant_port),
    }
    # 32 random bytes, base64. Generated only if we have not already set one: a
    # retry must not rotate the key out from under a volume already encrypted with
    # it. We cannot read the old one back (we never stored it), so we simply omit
    # the variable -- `replace=False` leaves whatever Railway already holds.
    if dek is not None:
        variables["SQUIRE_DEK"] = dek
    else:
        assert tenant.dek_set, "SQUIRE_DEK may only be omitted once a DEK is set"

    # Keep every credential in this payload out of any error message we persist.
    # The webhook secret is included now that it travels as a Railway variable --
    # a leaked one lets anyone forge Telegram updates into a tenant.
    for secret in (
        dek,
        bot.token,
        bot.webhook_secret,
        trial_key,
        settings.internal_api_token,
    ):
        register_step_secret(secret)

    clients.railway.set_variables(tenant.railway_service_id, variables)

    # Only now is the key safely delivered; dropping it any earlier risks losing it.
    _pop_trial_key(tenant.id)

    # Confirm the variable actually landed before claiming `dek_set`. The upsert's
    # exact input shape is MEDIUM-confidence (unverified against live Railway), and
    # a 200 on a mutation we mis-shaped would otherwise leave us believing a DEK
    # exists when it does not. NAMES ONLY -- values are never read back or logged.
    if dek is not None:
        if not _confirm_variable_present(clients, tenant, "SQUIRE_DEK"):
            raise ProvisioningError(
                f"SQUIRE_DEK missing from service {tenant.railway_service_id} after upsert"
            )
        tenant.dek_set = True
    _touch(session, tenant)


def _confirm_variable_present(
    clients: ProvisionClients, tenant: Tenant, name: str
) -> bool:
    """Read back variable NAMES and check for `name`.

    Returns True when the read-back itself is unavailable: the query shape is as
    unverified as the mutation, so a broken probe must not permanently block
    provisioning. A probe that *works* and reports the variable missing is real
    evidence and does fail the step.
    """
    try:
        names = clients.railway.get_variable_names(tenant.railway_service_id)
    except Exception:  # noqa: BLE001 -- probe is advisory
        log.warning(
            "could not read back variables for %s; assuming upsert succeeded",
            tenant.railway_service_id,
            exc_info=True,
        )
        return True
    if names is None:
        return True
    return name in names


def _step_deploy(session: Session, tenant: Tenant, clients: ProvisionClients) -> None:
    settings = get_settings()
    # Serverless sleep before the first deploy so the very first boot is already
    # sleep-enabled (implementation-plan §2 / Gate G1).
    clients.railway.configure_service_instance(
        tenant.railway_service_id, sleep_application=settings.railway_tenant_sleep
    )
    clients.railway.deploy(tenant.railway_service_id)
    _touch(session, tenant)


def _step_set_webhook(session: Session, tenant: Tenant, clients: ProvisionClients) -> None:
    if tenant.webhook_set:
        return
    bot = get_bot(session, tenant.bot_id) if tenant.bot_id else None
    if bot is None:
        raise ProvisioningError(f"tenant {tenant.id} has no assigned bot")
    clients.telegram.set_webhook(
        bot.token,
        url=telegram_webhook_url(bot.id),
        secret_token=bot.webhook_secret,
    )
    tenant.webhook_set = True
    _touch(session, tenant)


_STEP_HANDLERS = {
    ProvisionStep.CREATE_SERVICE: _step_create_service,
    ProvisionStep.ATTACH_VOLUME: _step_attach_volume,
    ProvisionStep.CREATE_TRIAL_KEY: _step_create_trial_key,
    ProvisionStep.SET_VARIABLES: _step_set_variables,
    ProvisionStep.DEPLOY: _step_deploy,
    ProvisionStep.SET_WEBHOOK: _step_set_webhook,
}


# --- in-process, non-persistent handoff of the trial key between two steps ---
# The key is created in `create_trial_key` and consumed by `set_variables`. Keeping
# it in a module-level dict (never the DB) preserves the "no plaintext credentials
# in the control plane" property. If the process dies in between, the next run
# re-mints the key -- LiteLLM aliases make that harmless.
_TRIAL_KEY_STASH: dict[str, str] = {}


def _stash_trial_key(tenant_id: str, key: str) -> None:
    _TRIAL_KEY_STASH[tenant_id] = key


def _pop_trial_key(tenant_id: str) -> str | None:
    return _TRIAL_KEY_STASH.pop(tenant_id, None)


def _peek_trial_key(tenant_id: str) -> str | None:
    return _TRIAL_KEY_STASH.get(tenant_id)


def _touch(session: Session, tenant: Tenant) -> None:
    tenant.updated_at = utcnow()
    session.add(tenant)
    session.commit()


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


def _claim_job(session: Session, job_id: str) -> bool:
    """Atomically move a job PENDING -> RUNNING. True if we won the claim.

    Three independent callers race for every job: the FastAPI BackgroundTask fired
    by `POST /internal/tenants`, the `POST .../advance` endpoint that the CLI polls,
    and the background sweeper. Without this claim all three can run the same step
    concurrently -- which for `create_service` means two Railway services (two
    bills) for one tenant.

    Same conditional-UPDATE pattern as `assign_bot`: portable across Postgres and
    SQLite, and the loser simply observes rowcount == 0.
    """
    session.commit()  # flush any pending state so the UPDATE sees committed rows
    result = session.exec(  # type: ignore[call-overload]
        ProvisionJob.__table__.update()
        .where(ProvisionJob.__table__.c.id == job_id)
        .where(ProvisionJob.__table__.c.status == JobStatus.PENDING.value)
        .values(status=JobStatus.RUNNING.value, updated_at=utcnow())
    )
    session.commit()
    session.expire_all()
    return bool(result.rowcount)


def reclaim_stale_jobs(session: Session) -> int:
    """Return jobs whose worker died mid-run (RUNNING but untouched for a while).

    `updated_at` is refreshed after every completed step, so a genuinely-working job
    never looks stale as long as individual steps finish inside the window.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_CLAIM_SECONDS)
    result = session.exec(  # type: ignore[call-overload]
        ProvisionJob.__table__.update()
        .where(ProvisionJob.__table__.c.status == JobStatus.RUNNING.value)
        .where(ProvisionJob.__table__.c.updated_at < cutoff)
        .values(status=JobStatus.PENDING.value)
    )
    session.commit()
    if result.rowcount:
        log.warning("reclaimed %s stale provisioning job(s)", result.rowcount)
    return int(result.rowcount or 0)


def advance_job(
    session: Session,
    job_id: str,
    clients: ProvisionClients | None = None,
    force: bool = False,
) -> ProvisionJob:
    """Run steps for one job until it finishes, fails, or must back off.

    `force=True` ignores the backoff schedule -- that is what the operator retry
    endpoint and the provisioning CLI use. It does NOT bypass the concurrency claim;
    a job another worker is actively running is returned untouched.
    """
    reclaim_stale_jobs(session)

    job = get_job(session, job_id)
    if job.status in (JobStatus.DONE, JobStatus.FAILED):
        return job

    now = datetime.now(timezone.utc)
    if not force and as_aware(job.next_attempt_at) > now:
        return job  # not due yet

    if not _claim_job(session, job_id):
        # Someone else holds it (or it just finished). Report current state.
        return get_job(session, job_id)

    job = get_job(session, job_id)
    tenant = get_tenant(session, job.tenant_id)
    clients = clients or ProvisionClients.build()

    try:
        while job.step is not ProvisionStep.DONE:
            handler = _STEP_HANDLERS[job.step]
            # Secrets that must never reach `last_error` (see `_redact`). Reset per
            # step; handlers add to it as they build credential-bearing payloads.
            token = _STEP_SECRETS.set(_baseline_secrets())
            try:
                handler(session, tenant, clients)
            except Exception as exc:  # noqa: BLE001 -- every failure is retryable state
                return _record_failure(session, job, exc)
            finally:
                _STEP_SECRETS.reset(token)

            job.step = _next_step(job.step)
            job.attempts = 0  # progress resets the retry budget for the next step
            job.last_error = None
            # Refreshing `updated_at` each step is what keeps a long but healthy
            # run from being reclaimed as stale.
            job.updated_at = utcnow()
            session.add(job)
            session.commit()

        job.status = JobStatus.DONE
        job.updated_at = utcnow()
        session.add(job)
        tenant.status = TenantStatus.RUNNING
        tenant.updated_at = utcnow()
        session.add(tenant)
        session.commit()
        session.refresh(job)
        log.info("tenant %s provisioned", tenant.id)
        return job
    finally:
        # Belt and braces: never leave a job stuck in RUNNING because of an
        # unexpected error path (`_record_failure` already sets PENDING/FAILED).
        _release_if_running(session, job_id)


def _release_if_running(session: Session, job_id: str) -> None:
    try:
        session.exec(  # type: ignore[call-overload]
            ProvisionJob.__table__.update()
            .where(ProvisionJob.__table__.c.id == job_id)
            .where(ProvisionJob.__table__.c.status == JobStatus.RUNNING.value)
            .values(status=JobStatus.PENDING.value, updated_at=utcnow())
        )
        session.commit()
    except Exception:  # noqa: BLE001 -- the stale reclaim is the backstop
        log.exception("could not release job %s", job_id)


def _next_step(step: ProvisionStep) -> ProvisionStep:
    return PROVISION_STEP_ORDER[PROVISION_STEP_ORDER.index(step) + 1]


def _record_failure(session: Session, job: ProvisionJob, exc: Exception) -> ProvisionJob:
    """Count the attempt, schedule a retry, or give up."""
    session.rollback()
    job = get_job(session, job.id)
    job.attempts += 1
    job.last_error = _redact(f"{type(exc).__name__}: {exc}")[:1000]
    job.updated_at = utcnow()
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.FAILED
        log.error(
            "provisioning job %s FAILED at step %s: %s", job.id, job.step, job.last_error
        )
    else:
        job.status = JobStatus.PENDING
        delay = backoff_seconds(job.attempts)
        job.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        log.warning(
            "provisioning job %s step %s failed (attempt %s/%s), retrying in %ss: %s",
            job.id,
            job.step,
            job.attempts,
            job.max_attempts,
            delay,
            job.last_error,
        )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def as_aware(value: datetime) -> datetime:
    """SQLite round-trips datetimes without tzinfo; normalise before comparing."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def run_pending_jobs(limit: int = 20) -> int:
    """Sweep due jobs. Returns how many were advanced.

    Called by the background worker thread; also usable from a cron/one-shot.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        # Jobs abandoned by a dead worker come back to PENDING here.
        reclaim_stale_jobs(session)
        # RUNNING jobs are deliberately skipped -- another worker owns them.
        due = session.exec(
            select(ProvisionJob)
            .where(ProvisionJob.status == JobStatus.PENDING)
            .order_by(ProvisionJob.next_attempt_at)  # type: ignore[arg-type]
            .limit(limit)
        ).all()
        job_ids = [j.id for j in due if as_aware(j.next_attempt_at) <= now]

    processed = 0
    for job_id in job_ids:
        try:
            with session_scope() as session:
                advance_job(session, job_id)
            processed += 1
        except Exception:  # noqa: BLE001 -- one bad job must not stop the sweep
            log.exception("worker failed on job %s", job_id)
    return processed


# ---------------------------------------------------------------------------
# Lifecycle beyond provisioning
# ---------------------------------------------------------------------------


def stop_tenant(
    session: Session, tenant_id: str, clients: ProvisionClients | None = None
) -> Tenant:
    """Halt a tenant's Railway deployment (trial expiry, non-payment)."""
    tenant = get_tenant(session, tenant_id)
    clients = clients or ProvisionClients.build()
    if tenant.railway_service_id:
        clients.railway.stop_service(tenant.railway_service_id)
    tenant.status = TenantStatus.STOPPED
    _touch(session, tenant)
    session.refresh(tenant)
    return tenant


def delete_tenant(
    session: Session, tenant_id: str, clients: ProvisionClients | None = None
) -> Tenant:
    """Crypto-shred: destroy the Railway service (and with it the volume + the only
    copy of the DEK), revoke the trial key, and recycle the pool bot.

    The tenant *row* is retained with status=deleted as an audit record -- it holds
    no content and no credentials, only registry metadata.
    """
    tenant = get_tenant(session, tenant_id)
    clients = clients or ProvisionClients.build()

    if tenant.trial_key_active and tenant.trial_key_alias:
        clients.trial.delete_trial_key(tenant.trial_key_alias)
        tenant.trial_key_active = False

    if tenant.bot_id:
        bot = get_bot(session, tenant.bot_id)
        if bot is not None:
            try:
                clients.telegram.delete_webhook(bot.token)
            except Exception:  # noqa: BLE001 -- never block deletion on Telegram
                log.warning("deleteWebhook failed for bot %s", bot.id, exc_info=True)
            release_bot(session, bot)

    if tenant.railway_service_id:
        clients.railway.delete_service(tenant.railway_service_id)

    tenant.status = TenantStatus.DELETED
    tenant.bot_id = None
    tenant.railway_service_id = None
    tenant.railway_volume_id = None
    tenant.internal_url = None
    tenant.dek_set = False
    tenant.webhook_set = False
    _touch(session, tenant)
    session.refresh(tenant)
    return tenant


def revoke_trial_key(
    session: Session, tenant_id: str, clients: ProvisionClients | None = None
) -> bool:
    """Revoke a tenant's trial key (user connected their own LLM, or trial expired).

    Idempotent: returns False when there was nothing to revoke.
    """
    tenant = get_tenant(session, tenant_id)
    if not tenant.trial_key_active or not tenant.trial_key_alias:
        return False
    clients = clients or ProvisionClients.build()
    revoked = clients.trial.delete_trial_key(tenant.trial_key_alias)
    tenant.trial_key_active = False
    _touch(session, tenant)
    return revoked
