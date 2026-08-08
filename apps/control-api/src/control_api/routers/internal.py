"""`/internal/*` -- the service-to-service API.

Consumers:
  * `ingress`            -> GET /internal/tenants/by-bot/{bot_id}  (every update)
  * `infra/provision.py` -> POST /internal/tenants, POST .../advance
  * `infra/load_bots.py` -> POST /internal/bots
  * tenant runtimes      -> POST /internal/tenants/{id}/revoke-trial-key

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
from control_api.models import Bot, BotStatus, ProvisionJob, Tenant
from control_api.schemas import (
    BotPoolStats,
    CreateTenantRequest,
    CreateTenantResponse,
    FailedBot,
    JobResponse,
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
        "telegram_link": provisioning.telegram_link(bot.username) if bot else None,
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
