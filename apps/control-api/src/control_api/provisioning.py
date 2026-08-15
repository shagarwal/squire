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
    Heartbeat,
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
#: and is returned to the pool. Must comfortably exceed the slowest single step --
#: `updated_at` is refreshed after every completed step, so the unit to size this
#: against is one step, not one job.
#:
#: Worst-case arithmetic, from the per-client httpx timeouts in `config.py`
#: (railway 30s, telegram 20s, litellm 30s). The slowest step is `set_variables`,
#: whose unhappy path is:
#:
#:     re-mint a lost trial key: litellm /key/delete   30s
#:                             + litellm /key/generate 30s
#:     variableCollectionUpsert (railway)              30s
#:     get_variable_names read-back (railway)          30s
#:     -----------------------------------------------------
#:     total                                          120s
#:
#: (`create_service` is 60s: find_service_by_name + serviceCreate. Every other step
#: is one or two calls.) 300s is 2.5x the worst step, which leaves room for TCP
#: connect + retry jitter without ever reclaiming a job that is merely slow.
#:
#: Raise this if any per-client timeout above is raised; a stale window shorter
#: than a legitimate step means two workers running the same Railway mutation.
STALE_CLAIM_SECONDS = 300.0

#: Value `railway_volume_id` used to hold when a volume was known to exist but its
#: id could not be determined. Now unreachable -- `attach_volume` always returns a
#: real id -- but rows provisioned earlier may still carry it, and a volume we
#: cannot name is a volume we cannot delete (see `delete_tenant`).
_LEGACY_VOLUME_SENTINEL = "existing"

#: Tenant statuses a redeploy is allowed to touch (Task 0.6 upgrade drill).
#:
#: PROVISIONING is excluded because the state machine owns that tenant right now;
#: DELETED because there is nothing there.
#:
#: SLEEPING is excluded too, and that IS a gap worth naming: a scale-to-zero
#: tenant would be woken (and billed) by the deploy, and it emits no heartbeats
#: while asleep, so the drill could not verify it either. It therefore stays on
#: the old image until something wakes it. Phase 1 (implementation-plan §4, 1F
#: fleet automation) needs a wake-then-upgrade or on-wake-check pass; until then
#: sleeping tenants are a manual follow-up after a fleet roll.
REDEPLOYABLE_STATUSES = frozenset({TenantStatus.RUNNING, TenantStatus.STOPPED})

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


def telegram_link(username: str, bind_nonce: str | None = None) -> str:
    """The deep link the user taps to meet their agent.

    With a nonce, `?start=` makes the tap send "/start <nonce>", which is the
    only message the tenant's autopair will bind an owner from. No URL-encoding
    needed: `generate_bind_nonce` output is already within Telegram's
    `[A-Za-z0-9_-]` start-payload alphabet. A bare link (nonce still None, i.e.
    set_variables has not run yet) is served as-is -- it is not tappable into a
    live tenant at that point anyway.
    """
    if bind_nonce:
        return f"https://t.me/{username}?start={bind_nonce}"
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
    # Deleted tenants are retained as audit rows (crypto-shred keeps the row),
    # so the idempotency lookup must skip them: a churned-and-returning email
    # would otherwise match its own corpse — no bot, no service, no job that
    # advance_job will touch — and re-signup would be a silent dead end.
    existing = session.exec(
        select(Tenant).where(
            Tenant.email == email,
            Tenant.status != TenantStatus.DELETED,
        )
    ).first()
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
    # Desired-image bookkeeping for `/fleet` and the upgrade drill. The matching
    # SQUIRE_IMAGE_REF service variable is written by `_step_set_variables`, so a
    # freshly provisioned tenant reports its image on its very first heartbeat --
    # no drill required before `/fleet` can tell us what the fleet is running.
    tenant.image_ref = settings.tenant_image
    _touch(session, tenant)


def _step_attach_volume(session: Session, tenant: Tenant, clients: ProvisionClients) -> None:
    # A row carrying the legacy sentinel is deliberately NOT treated as done: it has
    # no usable volume id, and deprovisioning needs one to avoid a billed orphan.
    # Re-running resolves the real id via the probe inside `attach_volume`.
    if tenant.railway_volume_id and tenant.railway_volume_id != _LEGACY_VOLUME_SENTINEL:
        return
    settings = get_settings()

    # `attach_volume` probes before creating -- mandatory, because volumeCreate is
    # not idempotent and a duplicate volume is billed forever.
    volume_id = clients.railway.attach_volume(
        tenant.railway_service_id, settings.tenant_volume_mount_path
    )
    if not volume_id:
        # Better to retry than to record an id we cannot later delete.
        raise ProvisioningError(
            f"volume for service {tenant.railway_service_id} could not be resolved"
        )
    tenant.railway_volume_id = volume_id
    _touch(session, tenant)


def _step_create_domain(session: Session, tenant: Tenant, clients: ProvisionClients) -> None:
    """Public Railway domain for the 1C /connect page (design decision 3:
    Railway-generated domains for alpha; custom domains are Phase 1 polish).

    Probe-first, like attach_volume: serviceDomainCreate's idempotency is
    unverified, and a retry must adopt rather than duplicate. The domain is
    deliberately NOT persisted on the tenant row -- _step_set_variables reads
    it back from Railway by name, and no control-plane query path needs it.
    Ordering before DEPLOY is load-bearing: Railway stamps
    RAILWAY_PUBLIC_DOMAIN into the container at deploy time, so the domain
    must exist before the first deploy runs.
    """
    if clients.railway.get_service_domain(tenant.railway_service_id):
        return
    clients.railway.create_service_domain(tenant.railway_service_id)
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

    # Deep-link bind nonce: generate-if-absent, reuse-if-present -- the same
    # retry discipline as the DEK guard below, with one deliberate difference:
    # the nonce IS persisted (models.Tenant.bind_nonce), because the
    # `?start=<nonce>` link is built from the row every time the tenant is
    # served, after this job is long done. Reuse on retry matters for the same
    # reason rotation would hurt the DEK: a link already handed out must keep
    # working. Persist BEFORE the upsert so Railway can never hold a nonce the
    # DB has lost -- the reverse ordering would strand the tenant with a nonce
    # no link will ever carry.
    if not tenant.bind_nonce:
        tenant.bind_nonce = crypto.generate_bind_nonce()
        _touch(session, tenant)

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
        # Defense-in-depth for the 1C public domain. Task 8 attaches a PUBLIC
        # Railway domain to the shim port so the /connect page is reachable;
        # that same domain exposes the webhook endpoint to the internet, where
        # the Host-gate (which trusts the client-controllable Host header) would
        # otherwise be the ONLY lock on forged Telegram updates. With this on,
        # the shim ALSO requires the per-bot secret header on every delivery.
        # This is safe because ingress re-stamps X-Telegram-Bot-Api-Secret-Token
        # with this exact TELEGRAM_WEBHOOK_SECRET on every forward (verbatim and
        # buffered-replay paths alike), so legitimate delivery still passes.
        "SQUIRE_WEBHOOK_REQUIRE_AUTH": "true",
        "ANTHROPIC_BASE_URL": settings.effective_trial_base_url,
        "ANTHROPIC_API_KEY": trial_key or "",
        # The PRIVATE address when one is configured (see `control_api_internal_url`).
        # Every tenant beats regularly; there is no reason for that to leave
        # Railway's private network and be billed as egress at both ends.
        "CONTROL_API_URL": settings.effective_control_api_url,
        "INTERNAL_API_TOKEN": settings.internal_api_token,
        "PORT": str(settings.tenant_port),
        # What image this container is running, echoed back on every heartbeat.
        # Set HERE and not only at redeploy: otherwise `/fleet` cannot say what the
        # fleet is actually running until a drill has happened, and the drill's
        # "already on the target image" check is False for every tenant -- so the
        # first `--roll` would restart the entire fleet to move it onto the image it
        # was already provisioned with. `redeploy_tenant` overwrites this.
        "SQUIRE_IMAGE_REF": tenant.image_ref or settings.tenant_image,
        # The tenant's autopair only binds an owner when the first /start
        # carries this value (delivered to the user via the ?start= deep link).
        # Closes the recycled-bot hole: a pool bot's previous owner keeps the
        # bot chat forever and would otherwise be first-in on the next tenant.
        "SQUIRE_BIND_NONCE": tenant.bind_nonce,
        # Override the image's 300s heartbeat default: Railway's serverless sleep
        # only engages after ~10 outbound-quiet minutes, so a 5-minute beat keeps
        # every tenant permanently awake and destroys the cost model (Gate G1).
        # 1800s clears the sleep window with margin -- verified live on staging
        # with patch 006 (identity-refresh loop off). The image default stays 300
        # for dev/self-hosted, where nothing sleeps.
        "SQUIRE_HEARTBEAT_INTERVAL": str(settings.tenant_heartbeat_interval_seconds),
        # 1C: the tenant's public domain, written explicitly so the connect
        # CLI never depends on Railway's deploy-time RAILWAY_PUBLIC_DOMAIN
        # injection actually happening. Read back by name from Railway (the
        # domain is not stored in the control DB). Tenant code prefers
        # RAILWAY_PUBLIC_DOMAIN and falls back to this.
        "SQUIRE_PUBLIC_DOMAIN": clients.railway.get_service_domain(
            tenant.railway_service_id
        ) or "",
    }
    # 32 random bytes, base64. Generated only if we have not already set one: a
    # retry must not rotate the key out from under a volume already encrypted with
    # it. We cannot read the old one back (we never stored it), so we simply omit
    # the variable -- `replace=False` leaves whatever Railway already holds.
    if dek is not None:
        variables["SQUIRE_DEK"] = dek
    elif not tenant.dek_set:
        # Unreachable by construction (`dek` is None only when dek_set is True), but
        # a real raise rather than an `assert`: assertions vanish under `python -O`,
        # and the failure this guards against -- deploying a tenant with no DEK at
        # all -- is one where the container refuses to boot and the volume cannot be
        # read. Fail the step and retry instead.
        raise ProvisioningError(
            f"tenant {tenant.id} would deploy without a DEK: SQUIRE_DEK was omitted "
            "but dek_set is False"
        )

    # Keep every credential in this payload out of any error message we persist.
    # The webhook secret is included now that it travels as a Railway variable --
    # a leaked one lets anyone forge Telegram updates into a tenant.
    for secret in (
        dek,
        bot.token,
        bot.webhook_secret,
        trial_key,
        settings.internal_api_token,
        # A leaked bind nonce lets its holder become the tenant's owner before
        # the real user taps Start.
        tenant.bind_nonce,
    ):
        register_step_secret(secret)

    clients.railway.set_variables(tenant.railway_service_id, variables)

    # Only now is the key safely delivered; dropping it any earlier risks losing it.
    _pop_trial_key(tenant.id)

    # Confirm the variable actually landed before claiming `dek_set`.
    #
    # `variableCollectionUpsert` is HIGH confidence (verbatim from Railway's API
    # cookbook -- see the confidence table in clients/railway.py), so this is not
    # hedging against a mis-shaped mutation. It guards the failure that stays
    # silent either way: GraphQL answers 200 even when it reports errors, and a
    # partially-applied upsert would leave us recording `dek_set=True` for a
    # service that has no DEK -- a tenant that then refuses to boot with no clue
    # why. Cheap check, catastrophic thing to get wrong.
    # NAMES ONLY -- values are never read back or logged.
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
    ProvisionStep.CREATE_DOMAIN: _step_create_domain,
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
    job = get_job(session, job_id)
    if job.status in (JobStatus.DONE, JobStatus.FAILED):
        # Terminal. Return before the reclaim sweep: `infra/provision.py` polls this
        # endpoint, and every poll after the job finished was previously paying for
        # a fleet-wide UPDATE that could not possibly change this job's answer.
        return job

    # Only a job stuck in RUNNING can be one a dead worker is holding, and this
    # job's own row is the only one whose state can unblock *this* call. The
    # background sweeper (`run_pending_jobs`) still reclaims fleet-wide on its own
    # tick, so nothing goes unrecovered -- this just stops the hot path paying for
    # it. Cheap gate: one already-loaded status check.
    if job.status is JobStatus.RUNNING:
        reclaim_stale_jobs(session)
        job = get_job(session, job_id)

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
    """Crypto-shred: destroy the tenant's Railway volume and service (and with them
    the only copy of the DEK), revoke the trial key, and recycle the pool bot.

    **Volume before service, and the order is load-bearing.** `serviceDelete` does
    not cascade to volumes -- verified live: delete the service first and the volume
    survives as a permanently billed orphan holding the tenant's encrypted data,
    which would make this "crypto-shred" neither a shred nor free. Deleting the
    volume while its service is still live removes it cleanly.

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

    # Volume FIRST -- see the docstring. Resolve the id live rather than trusting
    # the stored one: rows provisioned before the probe became mandatory may carry
    # the legacy "existing" sentinel instead of a real volume id, and an unresolved
    # volume is exactly the orphan we are trying to avoid.
    volume_id = tenant.railway_volume_id
    if volume_id in (None, _LEGACY_VOLUME_SENTINEL) and tenant.railway_service_id:
        volume_id = clients.railway.find_volume_for_service(tenant.railway_service_id)
    if volume_id and volume_id != _LEGACY_VOLUME_SENTINEL:
        clients.railway.delete_volume(volume_id)
    elif tenant.railway_service_id:
        # Nothing we can delete by id. Say so loudly: silently proceeding to
        # serviceDelete is what creates an unbilled-to-anyone orphan.
        log.warning(
            "tenant %s: could not resolve a volume id; deleting the service may "
            "leave an orphaned volume -- check the Railway project",
            tenant.id,
        )

    if tenant.railway_service_id:
        clients.railway.delete_service(tenant.railway_service_id)

    # The heartbeat row goes with the container. Keeping counters for a tenant that
    # no longer exists would leave behavioural data behind a deletion that is
    # supposed to be total (PRD §4 crypto-shred).
    beat = session.get(Heartbeat, tenant_id)
    if beat is not None:
        session.delete(beat)

    tenant.status = TenantStatus.DELETED
    # Free the email for re-signup while keeping the audit trail. The row is
    # retained (see docstring) and `email` is UNIQUE, so leaving the address in
    # place would make a churned user's re-signup a permanent dead end: the
    # create_tenant dedupe skips deleted rows, and the INSERT then dies on the
    # constraint. Tombstoning as `deleted.<tenant_id>.<original>` keeps the
    # original address recoverable for abuse checks (Phase 1E: one trial per
    # identity) and cannot collide (tenant ids are unique). The Phase 1G GDPR
    # erasure flow is the place that scrubs the address entirely -- deletion
    # here is lifecycle, not necessarily a legal erasure request. Guarded so a
    # re-run of delete_tenant does not stack prefixes.
    if not tenant.email.startswith("deleted."):
        tenant.email = f"deleted.{tenant.id}.{tenant.email}"
    tenant.bot_id = None
    tenant.railway_service_id = None
    tenant.railway_volume_id = None
    tenant.internal_url = None
    tenant.image_ref = None
    tenant.dek_set = False
    # The bot just went back to the pool; a surviving nonce (in this audit row,
    # or in a still-circulating ?start= link) must never be able to bind anyone
    # to the bot's NEXT tenant -- that tenant mints its own at set_variables.
    tenant.bind_nonce = None
    tenant.webhook_set = False
    _touch(session, tenant)
    session.refresh(tenant)
    return tenant


def resolve_image_ref(image_tag: str, settings: Settings | None = None) -> str:
    """Turn what an operator typed into a full container image reference.

    `v2`                      -> ghcr.io/shagarwal/squire/hermes-tenant:v2
    `ghcr.io/org/img:v2`      -> unchanged
    `ghcr.io:443/org/img:v2`  -> unchanged (registry port is not mistaken for a tag)

    A bare tag is resolved against the repository part of `TENANT_IMAGE`, which is
    the same image every tenant is provisioned from -- so the drill cannot
    accidentally roll the fleet onto a *different repository* by fat-fingering a tag.
    """
    settings = settings or get_settings()
    image_tag = image_tag.strip()
    if "/" in image_tag:
        return image_tag  # already a full reference

    base = settings.tenant_image
    # A digest pin (`repo@sha256:...`) is a perfectly reasonable TENANT_IMAGE -- it
    # is the discipline the tenant Dockerfile's own FROM uses. Strip the digest
    # FIRST: without this, the tag-splitting below would cut at the colon inside
    # `@sha256:` and produce `ghcr.io/org/img@sha256:v2`, which is not a reference
    # at all and would fail at deploy time rather than here.
    base = base.split("@", 1)[0]
    # Then split the tag off, but only if the colon is in the final path segment;
    # otherwise it is a registry port (`ghcr.io:443/...`) and the reference carries
    # no tag at all.
    last_segment = base.rsplit("/", 1)[-1]
    if ":" in last_segment:
        base = base[: len(base) - len(last_segment)] + last_segment.split(":", 1)[0]
    return f"{base}:{image_tag}"


def redeploy_tenant(
    session: Session,
    tenant_id: str,
    image_tag: str,
    clients: ProvisionClients | None = None,
) -> tuple[Tenant, str, bool]:
    """Re-point one tenant's Railway service at a new image and deploy it.

    This is the whole mechanism behind Task 0.6's upgrade drill: canary one tenant,
    verify it via `/fleet`, roll the rest, and roll back by calling this again with
    the previous tag. There is no separate rollback path -- a rollback is just a
    redeploy of an older reference, which is why this function has no notion of
    "newer".

    Order matters. `SQUIRE_IMAGE_REF` is written first (with Railway's own
    auto-deploy suppressed) so the container that comes up already knows which image
    it is and reports it on its first heartbeat. Verifying convergence any other way
    means trusting that a deploy we asked for is the deploy that happened.

    A STOPPED tenant gets the image update but NOT the deploy, and the returned flag
    says so. It was halted deliberately (trial expiry, non-payment); starting it
    again because a fleet upgrade swept past would resurrect a container the product
    decided to switch off, and bill for it. The new reference is already recorded on
    the service, so it takes effect whenever the tenant is legitimately resumed.
    """
    tenant = get_tenant(session, tenant_id)
    # Whitelist, not "anything that isn't deleted". A PROVISIONING tenant already
    # HAS a service id after the first step, so a service-id check alone would let
    # a redeploy race the state machine: our deploy and `_step_deploy` would fire
    # against the same service seconds apart, and set_variables could land after
    # the container it was meant to configure had already started. Wait for
    # provisioning to finish; the drill skips such tenants and reports them.
    if tenant.status not in REDEPLOYABLE_STATUSES:
        raise ProvisioningError(
            f"tenant {tenant_id} is {tenant.status.value}, not one of "
            f"{sorted(s.value for s in REDEPLOYABLE_STATUSES)} -- refusing to redeploy"
        )
    if not tenant.railway_service_id:
        raise ProvisioningError(f"tenant {tenant_id} has no Railway service to redeploy")

    image_ref = resolve_image_ref(image_tag)
    clients = clients or ProvisionClients.build()

    clients.railway.set_variables(
        tenant.railway_service_id, {"SQUIRE_IMAGE_REF": image_ref}
    )
    clients.railway.configure_service_instance(tenant.railway_service_id, image=image_ref)

    deployed = tenant.status is TenantStatus.RUNNING
    if deployed:
        clients.railway.deploy(tenant.railway_service_id)

    tenant.image_ref = image_ref
    _touch(session, tenant)
    session.refresh(tenant)
    log.info(
        "tenant %s set to %s (deploy %s)",
        tenant_id,
        image_ref,
        "triggered" if deployed else "skipped -- tenant is stopped",
    )
    return tenant, image_ref, deployed


# ---------------------------------------------------------------------------
# Fleet heartbeat (Task 0.6)
# ---------------------------------------------------------------------------


def record_heartbeat(session: Session, fields: dict) -> Heartbeat:
    """Upsert one tenant's latest heartbeat.

    `fields` comes from a validated `HeartbeatRequest`, so every value here is
    already known to be a bounded int, a bool, or the image reference -- this
    function never sees free-form input.
    """
    tenant_id = fields["tenant_id"]
    tenant = get_tenant(session, tenant_id)  # raises TenantNotFound -> 404

    row = session.get(Heartbeat, tenant_id)
    if row is None:
        row = Heartbeat(tenant_id=tenant_id)
    for name, value in fields.items():
        if name != "tenant_id":
            setattr(row, name, value)
    row.received_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    # 1C reconciliation backstop: a converted tenant whose
    # /internal/llm-connected call was lost still gets its trial key revoked
    # on the next beat. Idempotent (revoke_trial_key no-ops once inactive);
    # worst case on a missed beat, the trial cap still bounds spend.
    if fields.get("llm_connected") and tenant.trial_key_active:
        log.info("heartbeat reconciliation: tenant %s connected an LLM but the "
                 "trial key is still active — revoking", tenant_id)
        revoke_trial_key(session, tenant_id)
    return row


@dataclass
class FleetEntry:
    """One tenant's fleet view: registry row + latest heartbeat, joined."""

    tenant: Tenant
    heartbeat: Heartbeat | None
    age_seconds: float | None
    fresh: bool


def fleet_status(session: Session, status: TenantStatus | None = None) -> list[FleetEntry]:
    """Join every tenant with its latest heartbeat.

    One query per table and a dict join, rather than a SQL outer join: the fleet is
    tens of rows in Phase 0 and hundreds in Phase 1, and this stays readable and
    dialect-neutral. Revisit if `/fleet` ever lands on a hot path (it should not --
    it is an operator/CLI endpoint).
    """
    stale_after = get_settings().heartbeat_stale_seconds
    now = datetime.now(timezone.utc)

    query = select(Tenant).order_by(Tenant.created_at)  # type: ignore[arg-type]
    if status is not None:
        query = query.where(Tenant.status == status)
    tenants = list(session.exec(query))

    beats = {b.tenant_id: b for b in session.exec(select(Heartbeat))}

    entries: list[FleetEntry] = []
    for tenant in tenants:
        beat = beats.get(tenant.id)
        age = None if beat is None else (now - as_aware(beat.received_at)).total_seconds()
        entries.append(
            FleetEntry(
                tenant=tenant,
                heartbeat=beat,
                age_seconds=age,
                fresh=age is not None and age <= stale_after,
            )
        )
    return entries


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


def record_llm_connected(
    session: Session, tenant_id: str, provider: str,
    clients: ProvisionClients | None = None,
) -> bool:
    """The conversion moment, control-plane side: record the provider NAME and
    revoke the trial key immediately (PRD §2: their traffic never touches our
    infrastructure again). Returns whether a key was actually revoked --
    idempotent, because the tenant retries and the heartbeat backstop can race.
    """
    tenant = get_tenant(session, tenant_id)
    tenant.connected_provider = provider
    _touch(session, tenant)
    return revoke_trial_key(session, tenant_id, clients=clients)
