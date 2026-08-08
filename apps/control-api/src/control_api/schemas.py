"""Request/response models for the internal API.

`TenantByBotResponse` is a hard interface contract with ingress -- ingress is being
built in parallel against exactly these four fields. Do not add, rename, or reorder
without coordinating.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from control_api.models import JobStatus, ProvisionStep, TenantStatus


class TenantByBotResponse(BaseModel):
    """GET /internal/tenants/by-bot/{bot_id} -- the ingress hot path."""

    tenant_id: str
    status: TenantStatus
    internal_url: str
    webhook_secret: str


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
