"""Request/response models for the internal API.

`TenantByBotResponse` is a hard interface contract with ingress -- ingress is being
built in parallel against exactly these four fields. Do not add, rename, or reorder
without coordinating.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_api.models import JobStatus, ProvisionStep, TenantStatus


class TenantByBotResponse(BaseModel):
    """GET /internal/tenants/by-bot/{bot_id} -- the ingress hot path."""

    tenant_id: str
    status: TenantStatus
    internal_url: str
    webhook_secret: str


class WakeTypingRequest(BaseModel):
    """POST /internal/wake-typing -- ingress's typing-on-wake nudge.

    Both fields are Telegram ROUTING identifiers, not content: bot_id is the
    same public id that names the webhook path, chat_id is where the typing
    indicator must appear. Neither is persisted or logged by the handler.

    `extra="forbid"` for the same reason as HeartbeatRequest: an ids-only
    channel must stay structurally ids-only -- an ingress build that tried to
    attach message text would get a 422, not a quietly widening pipe.
    """

    model_config = ConfigDict(extra="forbid")

    bot_id: int
    chat_id: int


class WakeTypingResponse(BaseModel):
    """202 body. Deliberately tiny -- ingress fires and forgets, so nobody
    reads this beyond tests; it exists to give the contract a pinned shape."""

    bot_id: int
    accepted: bool


class CreateTenantRequest(BaseModel):
    """POST /internal/tenants.

    Email validation is deliberately minimal rather than `EmailStr`. Two reasons:
    real signup validation (plus disposable-domain blocking, PRD §4) belongs in the
    web/auth layer in Phase 1, and strict RFC validators reject the special-use
    domains (`.test`, `.example`, `.invalid`) that the planned CI synthetic-signup
    canary needs. control-api only requires something it can use as a unique key.
    """

    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def _looks_like_an_email(cls, value: str) -> str:
        value = value.strip().lower()
        local, sep, domain = value.partition("@")
        if not sep or not local or "." not in domain or any(c.isspace() for c in value):
            raise ValueError("not a valid email address")
        return value


class TenantResponse(BaseModel):
    """Operator-facing tenant view. Carries status metadata only -- no bot token,
    no DEK, no LLM key."""

    tenant_id: str
    email: str
    status: TenantStatus
    bot_id: int | None = None
    bot_username: str | None = None
    telegram_link: str | None = None
    internal_url: str | None = None
    railway_service_id: str | None = None
    dek_set: bool = False
    trial_key_active: bool = False
    webhook_set: bool = False


class CreateTenantResponse(TenantResponse):
    job_id: str


class JobResponse(BaseModel):
    job_id: str
    tenant_id: str
    step: ProvisionStep
    status: JobStatus
    attempts: int
    max_attempts: int
    last_error: str | None = None


class RegisterBotsRequest(BaseModel):
    """Bodies come from `infra/load_bots.py`; tokens are validated via getMe."""

    tokens: list[str] = Field(min_length=1)


class RegisteredBot(BaseModel):
    bot_id: int
    username: str


class SkippedBot(BaseModel):
    bot_id: int
    reason: str


class FailedBot(BaseModel):
    bot_id: int | None = None
    error: str


class RegisterBotsResponse(BaseModel):
    registered: list[RegisteredBot]
    skipped: list[SkippedBot]
    failed: list[FailedBot]


class BotPoolStats(BaseModel):
    total: int
    available: int
    assigned: int
    disabled: int


class RevokeTrialKeyResponse(BaseModel):
    tenant_id: str
    trial_key_active: bool
    revoked: bool


class LlmConnectedRequest(BaseModel):
    """POST /internal/llm-connected -- a tenant reports its owner connected an LLM.

    `provider` is a CLOSED vocabulary on purpose: three literals fit through
    this field and nothing else, so it is structurally unable to smuggle key
    material into the control plane. `extra="forbid"` for the same reason as
    every other tenant-writable schema here.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    provider: Literal["openai", "anthropic", "chatgpt"]


class LlmConnectedResponse(BaseModel):
    tenant_id: str
    provider: str
    connected_provider_recorded: bool
    trial_key_revoked: bool


# ---------------------------------------------------------------------------
# Heartbeat / fleet (Task 0.6)
# ---------------------------------------------------------------------------

#: Sanity ceiling on every counter. Not a security control -- the caller is already
#: authenticated -- but it stops a wrapped/garbage value from being persisted as
#: fact, and it makes the "these are small counts" intent explicit.
_MAX_COUNTER = 10**12


class HeartbeatRequest(BaseModel):
    """POST /internal/heartbeat -- emitted by `tenant-image/bin/squire-heartbeat.py`.

    `extra="forbid"` is the load-bearing line in this file. It is what makes the
    heartbeat channel structurally counts-only: a future tenant build that tried to
    attach a chat id, a message preview or an exception string gets a 422 instead of
    quietly teaching the control plane to hold conversation data (PRD §4).

    Every field except `uptime_seconds` and the two liveness booleans is optional,
    because each collector on the tenant side can fail independently. A tenant that
    cannot read its cgroup must still be able to say "I am alive" -- otherwise the
    first thing to break also removes the signal that something broke.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    uptime_seconds: int = Field(ge=0, le=_MAX_COUNTER)
    image_ref: str | None = Field(default=None, max_length=512)

    gateway_up: bool
    hindsight_up: bool

    memory_rss_mb: int | None = Field(default=None, ge=0, le=_MAX_COUNTER)
    volume_used_mb: int | None = Field(default=None, ge=0, le=_MAX_COUNTER)
    volume_total_mb: int | None = Field(default=None, ge=0, le=_MAX_COUNTER)

    updates_forwarded: int | None = Field(default=None, ge=0, le=_MAX_COUNTER)
    updates_failed: int | None = Field(default=None, ge=0, le=_MAX_COUNTER)
    updates_rejected: int | None = Field(default=None, ge=0, le=_MAX_COUNTER)

    hindsight_ops_pending: int | None = Field(default=None, ge=0, le=_MAX_COUNTER)
    hindsight_ops_processing: int | None = Field(default=None, ge=0, le=_MAX_COUNTER)
    hindsight_ops_failed: int | None = Field(default=None, ge=0, le=_MAX_COUNTER)

    # Age of the last successful restic run. Accepted from day one even though no
    # tenant can populate it yet (no B2 account): `extra="forbid"` means adding a
    # field later is a COORDINATED change -- every tenant sending it would 422
    # until control-api ships and deploys. Reserving the name now costs nothing and
    # removes that ordering constraint from the day backups are switched on.
    backup_last_success_age_seconds: int | None = Field(
        default=None, ge=0, le=_MAX_COUNTER
    )

    #: 1C reconciliation backstop: the tenant's own "owner has connected an
    #: LLM" flag, derived from credential artifacts on the tenant side. A
    #: True against a still-active trial key triggers revocation.
    llm_connected: bool | None = None


class HeartbeatResponse(BaseModel):
    """Deliberately tiny: the tenant has no use for a fleet view."""

    tenant_id: str
    received: bool


class FleetTenant(BaseModel):
    """One row of `GET /internal/fleet`. Status metadata only -- no credentials."""

    tenant_id: str
    email: str
    status: TenantStatus

    #: Desired (what control-api last deployed) vs reported (what the tenant says it
    #: is running). The upgrade drill treats them agreeing, plus a fresh heartbeat,
    #: as "this tenant converged onto the new image".
    image_ref: str | None = None
    reported_image_ref: str | None = None

    last_heartbeat_at: datetime | None = None
    heartbeat_age_seconds: float | None = None
    heartbeat_fresh: bool = False

    uptime_seconds: int | None = None
    gateway_up: bool | None = None
    hindsight_up: bool | None = None
    memory_rss_mb: int | None = None
    volume_used_mb: int | None = None
    volume_total_mb: int | None = None
    updates_forwarded: int | None = None
    updates_failed: int | None = None
    updates_rejected: int | None = None
    hindsight_ops_pending: int | None = None
    hindsight_ops_processing: int | None = None
    hindsight_ops_failed: int | None = None
    backup_last_success_age_seconds: int | None = None


class FleetSummary(BaseModel):
    tenants: int
    running: int
    heartbeating: int  # fresh beat within the staleness window
    stale: int  # beat seen once, but not recently
    never_seen: int  # no beat ever


class FleetResponse(BaseModel):
    summary: FleetSummary
    tenants: list[FleetTenant]


class RedeployRequest(BaseModel):
    """POST /internal/tenants/{id}/redeploy -- the upgrade drill's only lever.

    `image_tag` accepts either a bare tag (`v2`, resolved against the repository
    part of `TENANT_IMAGE`) or a full reference (`ghcr.io/org/img:v2`). Operators
    type the former; automation tends to produce the latter.
    """

    image_tag: str = Field(min_length=1, max_length=512)

    @field_validator("image_tag")
    @classmethod
    def _looks_like_an_image_reference(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("image_tag is empty")
        # This string ends up in a Railway API payload and in operator logs. It is
        # never passed to a shell, but rejecting whitespace and control characters
        # keeps it from *becoming* a shell injection the day someone adds a CLI.
        if any(c.isspace() or ord(c) < 0x20 for c in value):
            raise ValueError("image_tag must not contain whitespace or control characters")
        return value


class RedeployResponse(BaseModel):
    tenant_id: str
    image_ref: str  # the fully-resolved reference we set on the service
    #: False for a STOPPED tenant: it gets the new image but is deliberately not
    #: started, because a fleet upgrade must not resurrect a container the product
    #: switched off (trial expiry, non-payment).
    deployment_triggered: bool
