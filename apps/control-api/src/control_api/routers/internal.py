"""`/internal/*` -- the service-to-service API.

Consumers:
  * `ingress`                -> GET /internal/tenants/by-bot/{bot_id} (every update)
  * `infra/provision.py`     -> POST /internal/tenants, POST .../advance
  * `infra/load_bots.py`     -> POST /internal/bots
  * `infra/upgrade_drill.py` -> POST /internal/tenants/{id}/redeploy, GET /internal/fleet
  * tenant runtimes          -> POST /internal/tenants/{id}/revoke-trial-key,
                                POST /internal/heartbeat

Everything here is behind the shared `INTERNAL_API_TOKEN` bearer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlmodel import Session, func, select

from control_api import crypto, provisioning
from control_api.auth import require_internal_token
from control_api.clients.telegram import TelegramClient, bot_id_from_token
from control_api.config import get_settings
from control_api.db import get_session, session_scope
from control_api.models import Bot, BotStatus, ProvisionJob, Tenant, TenantStatus
from control_api.schemas import (
    BotPoolStats,
    CreateTenantRequest,
    CreateTenantResponse,
    FailedBot,
    FleetResponse,
    FleetSummary,
    FleetTenant,
    HeartbeatRequest,
    HeartbeatResponse,
    JobResponse,
    RedeployRequest,
    RedeployResponse,
    RegisterBotsRequest,
    RegisterBotsResponse,
    RegisteredBot,
    RevokeTrialKeyResponse,
    SkippedBot,
    TenantByBotResponse,
    TenantResponse,
)

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_token)],
)


# ---------------------------------------------------------------------------
# Ingress hot path
# ---------------------------------------------------------------------------


@router.get("/tenants/by-bot/{bot_id}", response_model=TenantByBotResponse)
def tenant_by_bot(bot_id: int, session: Session = Depends(get_session)) -> TenantByBotResponse:
    """Resolve a Telegram bot id to its tenant.

    Called on EVERY Telegram update, so it is two primary-key/indexed lookups and
    nothing else: `bot.id` is the PK, `tenant.bot_id` is a unique index.
    """
    bot = session.get(Bot, bot_id)
    if bot is None or bot.assigned_tenant_id is None:
        raise HTTPException(status_code=404, detail="bot not assigned to any tenant")

    tenant = session.exec(select(Tenant).where(Tenant.bot_id == bot_id)).first()
    if tenant is None or not tenant.internal_url:
        raise HTTPException(status_code=404, detail="tenant not found for bot")

    return TenantByBotResponse(
        tenant_id=tenant.id,
        status=tenant.status,
        internal_url=tenant.internal_url,
        webhook_secret=bot.webhook_secret,
    )


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------


def _tenant_response(session: Session, tenant: Tenant, **extra) -> dict:
    bot = session.get(Bot, tenant.bot_id) if tenant.bot_id else None
    return {
        "tenant_id": tenant.id,
        "email": tenant.email,
        "status": tenant.status,
        "bot_id": tenant.bot_id,
        "bot_username": bot.username if bot else None,
        # Carries ?start=<bind_nonce> once provisioning has minted one -- the
        # tenant's autopair only binds the owner from a /start with that payload,
        # so this link is the credential handout path, not just a convenience.
        "telegram_link": (
            provisioning.telegram_link(bot.username, tenant.bind_nonce) if bot else None
        ),
        "internal_url": tenant.internal_url,
        "railway_service_id": tenant.railway_service_id,
        "dek_set": tenant.dek_set,
        "trial_key_active": tenant.trial_key_active,
        "webhook_set": tenant.webhook_set,
        **extra,
    }


@router.post(
    "/tenants", response_model=CreateTenantResponse, status_code=status.HTTP_201_CREATED
)
def create_tenant(
    payload: CreateTenantRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> dict:
    """Register a tenant and queue provisioning. Idempotent per email."""
    try:
        tenant, job = provisioning.create_tenant(session, email=str(payload.email))
    except provisioning.NoBotsAvailable as exc:
        # 409, not 500: the fix is operational (load a new BotFather batch).
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if get_settings().provision_auto_advance:
        background.add_task(_advance_in_background, job.id)

    return _tenant_response(session, tenant, job_id=job.id)


@router.get("/tenants/{tenant_id}", response_model=TenantResponse)
def get_tenant(tenant_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        tenant = provisioning.get_tenant(session, tenant_id)
    except provisioning.TenantNotFound as exc:
        raise HTTPException(status_code=404, detail="tenant not found") from exc
    return _tenant_response(session, tenant)


@router.post("/tenants/{tenant_id}/stop", response_model=TenantResponse)
def stop_tenant(tenant_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        tenant = provisioning.stop_tenant(session, tenant_id)
    except provisioning.TenantNotFound as exc:
        raise HTTPException(status_code=404, detail="tenant not found") from exc
    return _tenant_response(session, tenant)


@router.delete("/tenants/{tenant_id}", response_model=TenantResponse)
def delete_tenant(tenant_id: str, session: Session = Depends(get_session)) -> dict:
    """Crypto-shred a tenant: Railway service + volume + DEK gone, bot recycled."""
    try:
        tenant = provisioning.delete_tenant(session, tenant_id)
    except provisioning.TenantNotFound as exc:
        raise HTTPException(status_code=404, detail="tenant not found") from exc
    return _tenant_response(session, tenant)


@router.post(
    "/tenants/{tenant_id}/revoke-trial-key", response_model=RevokeTrialKeyResponse
)
def revoke_trial_key(
    tenant_id: str, session: Session = Depends(get_session)
) -> RevokeTrialKeyResponse:
    """Called by the tenant when the user connects their own LLM, and by the trial
    expiry job. Idempotent -- those two can race."""
    try:
        revoked = provisioning.revoke_trial_key(session, tenant_id)
    except provisioning.TenantNotFound as exc:
        raise HTTPException(status_code=404, detail="tenant not found") from exc
    return RevokeTrialKeyResponse(
        tenant_id=tenant_id, trial_key_active=False, revoked=revoked
    )


@router.post("/tenants/{tenant_id}/redeploy", response_model=RedeployResponse)
def redeploy_tenant(
    tenant_id: str, payload: RedeployRequest, session: Session = Depends(get_session)
) -> RedeployResponse:
    """Re-point a tenant at a container image and deploy it.

    The upgrade drill's only lever (`infra/upgrade_drill.py`): canary one tenant,
    verify via `/fleet`, roll the fleet, and roll back by calling this again with the
    previous tag. Not idempotent in the "no-op if already there" sense -- calling it
    twice with the same tag triggers two deploys, which is the correct behaviour for
    an operator who wants a tenant restarted.

    409 for any tenant not in `provisioning.REDEPLOYABLE_STATUSES`: still
    provisioning (the state machine owns it), sleeping, or deleted. A STOPPED tenant
    is accepted but has its image updated WITHOUT a deploy -- see `redeploy_tenant`
    -- and reports `deployment_triggered: false`.
    """
    try:
        _, image_ref, deployed = provisioning.redeploy_tenant(
            session, tenant_id, image_tag=payload.image_tag
        )
    except provisioning.TenantNotFound as exc:
        raise HTTPException(status_code=404, detail="tenant not found") from exc
    except provisioning.ProvisioningError as exc:
        # 409, not 500: the tenant exists, it is just not in a state that can be
        # redeployed (still provisioning, sleeping, or crypto-shredded).
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedeployResponse(
        tenant_id=tenant_id, image_ref=image_ref, deployment_triggered=deployed
    )


# ---------------------------------------------------------------------------
# Fleet heartbeat (Task 0.6)
#
# `/internal/heartbeat` is written by every tenant container; `/internal/fleet` is
# read by operators and by infra/upgrade_drill.py. Both sit under /internal because
# that is where the shared-bearer-token dependency lives -- the plan calls the
# latter "the /fleet status endpoint", and this is that endpoint, authed like
# everything else rather than hanging unauthenticated off the root.
# ---------------------------------------------------------------------------


@router.post("/heartbeat", response_model=HeartbeatResponse)
def heartbeat(
    payload: HeartbeatRequest, session: Session = Depends(get_session)
) -> HeartbeatResponse:
    """Ingest one tenant's counts-only heartbeat.

    `HeartbeatRequest` forbids unknown fields, so this handler cannot be the place
    conversation content sneaks in: anything not on the whitelist is a 422 before we
    get here. Storage is latest-per-tenant (`models.Heartbeat`), not a log.
    """
    try:
        provisioning.record_heartbeat(session, payload.model_dump())
    except provisioning.TenantNotFound as exc:
        # A container beating for a tenant we do not know about is worth surfacing:
        # it means a service outlived its deletion, which is a billing leak.
        log.warning("heartbeat from unknown tenant %s", payload.tenant_id)
        raise HTTPException(status_code=404, detail="tenant not found") from exc
    return HeartbeatResponse(tenant_id=payload.tenant_id, received=True)


@router.get("/fleet", response_model=FleetResponse)
def fleet(
    status: TenantStatus | None = None, session: Session = Depends(get_session)
) -> FleetResponse:
    """Per-tenant status, heartbeat freshness and counters.

    `status` filters to one tenant status (the drill asks for `running`). Carries
    registry metadata and counters only -- no bot token, no webhook secret, no DEK.
    """
    entries = provisioning.fleet_status(session, status=status)

    rows: list[FleetTenant] = []
    for entry in entries:
        beat = entry.heartbeat
        rows.append(
            FleetTenant(
                tenant_id=entry.tenant.id,
                email=entry.tenant.email,
                status=entry.tenant.status,
                image_ref=entry.tenant.image_ref,
                reported_image_ref=beat.image_ref if beat else None,
                last_heartbeat_at=beat.received_at if beat else None,
                heartbeat_age_seconds=entry.age_seconds,
                heartbeat_fresh=entry.fresh,
                # Everything below is None when we have never heard from the tenant.
                # "no data" and "zero" are different operational states.
                uptime_seconds=beat.uptime_seconds if beat else None,
                gateway_up=beat.gateway_up if beat else None,
                hindsight_up=beat.hindsight_up if beat else None,
                memory_rss_mb=beat.memory_rss_mb if beat else None,
                volume_used_mb=beat.volume_used_mb if beat else None,
                volume_total_mb=beat.volume_total_mb if beat else None,
                updates_forwarded=beat.updates_forwarded if beat else None,
                updates_failed=beat.updates_failed if beat else None,
                updates_rejected=beat.updates_rejected if beat else None,
                hindsight_ops_pending=beat.hindsight_ops_pending if beat else None,
                hindsight_ops_processing=beat.hindsight_ops_processing if beat else None,
                hindsight_ops_failed=beat.hindsight_ops_failed if beat else None,
                backup_last_success_age_seconds=(
                    beat.backup_last_success_age_seconds if beat else None
                ),
            )
        )

    summary = FleetSummary(
        tenants=len(rows),
        running=sum(1 for r in rows if r.status is TenantStatus.RUNNING),
        heartbeating=sum(1 for r in rows if r.heartbeat_fresh),
        stale=sum(1 for r in rows if r.last_heartbeat_at and not r.heartbeat_fresh),
        never_seen=sum(1 for r in rows if r.last_heartbeat_at is None),
    )
    return FleetResponse(summary=summary, tenants=rows)


# ---------------------------------------------------------------------------
# Provisioning jobs
# ---------------------------------------------------------------------------


def _job_response(job: ProvisionJob) -> JobResponse:
    return JobResponse(
        job_id=job.id,
        tenant_id=job.tenant_id,
        step=job.step,
        status=job.status,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        last_error=job.last_error,
    )


@router.get("/provision-jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, session: Session = Depends(get_session)) -> JobResponse:
    try:
        return _job_response(provisioning.get_job(session, job_id))
    except provisioning.JobNotFound as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc


@router.post("/provision-jobs/{job_id}/advance", response_model=JobResponse)
def advance_job(job_id: str, session: Session = Depends(get_session)) -> JobResponse:
    """Drive the state machine synchronously.

    `force=True` bypasses the backoff timer -- this endpoint is only reached by an
    operator (the CLI polls it), so an explicit retry should not have to wait.
    """
    try:
        job = provisioning.advance_job(session, job_id, force=True)
    except provisioning.JobNotFound as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    return _job_response(job)


def _advance_in_background(job_id: str) -> None:
    """BackgroundTask entrypoint -- needs its own session."""
    try:
        with session_scope() as session:
            provisioning.advance_job(session, job_id)
    except Exception:  # noqa: BLE001 -- the sweeper will retry
        log.exception("background provisioning failed for job %s", job_id)


# ---------------------------------------------------------------------------
# Bot pool
# ---------------------------------------------------------------------------


@router.post("/bots", response_model=RegisterBotsResponse)
def register_bots(
    payload: RegisterBotsRequest, session: Session = Depends(get_session)
) -> RegisterBotsResponse:
    """Load a BotFather batch into the pool.

    Every token is validated against Telegram `getMe` before it is stored, so a
    typo'd or revoked token can never be handed to a paying tenant. Registration is
    idempotent by bot id.
    """
    telegram = TelegramClient()
    registered: list[RegisteredBot] = []
    skipped: list[SkippedBot] = []
    failed: list[FailedBot] = []

    for token in payload.tokens:
        try:
            bot_id = bot_id_from_token(token)
        except ValueError as exc:
            failed.append(FailedBot(bot_id=None, error=str(exc)))
            continue

        if session.get(Bot, bot_id) is not None:
            skipped.append(SkippedBot(bot_id=bot_id, reason="already_registered"))
            continue

        try:
            me = telegram.get_me(token)
        except Exception as exc:  # noqa: BLE001 -- report per token, keep going
            failed.append(FailedBot(bot_id=bot_id, error=str(exc)))
            continue

        username = me.get("username") or f"bot{bot_id}"
        session.add(
            Bot(
                id=bot_id,
                token=token,
                username=username,
                status=BotStatus.AVAILABLE,
                # One webhook secret per bot, generated here and shared with ingress
                # via the by-bot lookup.
                webhook_secret=crypto.generate_webhook_secret(),
            )
        )
        session.commit()
        registered.append(RegisteredBot(bot_id=bot_id, username=username))

    return RegisterBotsResponse(registered=registered, skipped=skipped, failed=failed)


@router.get("/bots", response_model=BotPoolStats)
def bot_pool_stats(session: Session = Depends(get_session)) -> BotPoolStats:
    """Low-watermark monitoring for the pool (PRD §4 bot supply)."""
    rows = session.exec(select(Bot.status, func.count()).group_by(Bot.status)).all()
    counts = {status_value: count for status_value, count in rows}

    def n(s: BotStatus) -> int:
        # SQLModel may hand back either the enum or its raw value depending on dialect.
        return int(counts.get(s, counts.get(s.value, 0)))

    return BotPoolStats(
        total=sum(int(c) for c in counts.values()),
        available=n(BotStatus.AVAILABLE),
        assigned=n(BotStatus.ASSIGNED),
        disabled=n(BotStatus.DISABLED),
    )
