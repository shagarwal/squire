"""Configuration, entirely from environment variables (Railway service variables).

Everything is optional at import time so the app can boot in a half-configured
environment (e.g. before the trial proxy exists) and fail loudly only at the point
where a missing value actually matters.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # -- Control plane -----------------------------------------------------
    database_url: str = "sqlite:///./control.db"
    # Single shared secret for ALL internal service-to-service HTTP
    # (ingress -> control-api, tenant -> control-api, CLIs -> control-api).
    internal_api_token: str = "dev-internal-token"
    # Public base URL of this service.
    control_api_url: str = "http://localhost:8080"
    # What TENANTS are told to call. Should be the Railway private-network address
    # (http://control-api.railway.internal:8080): tenant -> control-api traffic
    # (heartbeats every 5 minutes from every container, trial-key revocation) has
    # no business leaving the private network, crossing the public internet, and
    # being billed as egress on both ends. Falls back to the public URL when unset,
    # so a half-configured environment still works rather than silently pointing
    # every tenant at nothing.
    control_api_internal_url: str = ""

    # -- Railway ------------------------------------------------------------
    railway_api_token: str = ""
    railway_graphql_url: str = "https://backboard.railway.com/graphql/v2"
    railway_project_id: str = ""
    railway_environment_id: str = ""
    # Railway exposes services to each other as `<service-name>.railway.internal`.
    railway_private_domain_suffix: str = "railway.internal"
    # Serverless sleep is the whole Railway economics bet (implementation-plan §2,
    # Gate G1). On by default; flip off if wake latency proves unacceptable.
    railway_tenant_sleep: bool = True
    railway_timeout_seconds: float = 30.0
    # Railway's volume list lags a few seconds behind a volumeCreate, and
    # volumeCreate is NOT idempotent (a second call silently makes a duplicate,
    # billed forever). `attach_volume` therefore re-probes after this delay before
    # concluding a service has no volume. Set to 0 to disable the second probe.
    railway_volume_probe_delay_seconds: float = 3.0

    # -- Tenant runtime -----------------------------------------------------
    # An EXPLICIT TAG, never `:latest`. `/fleet` reports what each tenant says it
    # is running, and the upgrade drill decides what to roll by comparing image
    # references -- both are blind if every tenant reports the same floating tag,
    # and a Railway redeploy of `:latest` would silently change what a tenant runs
    # with no record of when or to what.
    #
    # This tag must exist on GHCR before the first provision: it is published by
    # pushing a `tenant-image-v0.1.0` git tag (see .github/workflows/tenant-image.yml).
    # Normally overridden per-deploy with the version actually being shipped.
    tenant_image: str = "ghcr.io/shagarwal/squire/hermes-tenant:v0.1.0"
    # CROSS-SERVICE CONSTANT -- must equal the tenant image's volume/HOME.
    #
    # This is where Railway mounts the tenant's persistent volume, and the tenant
    # image sets HOME to the same path (tenant-image/Dockerfile: ENV HOME=/opt/data,
    # ENV HERMES_HOME=/opt/data, ENV SQUIRE_VOLUME=/opt/data). The entrypoint's
    # durability gate hard-fails when that path is not a real mount and TENANT_ID is
    # set, because pg0 would otherwise put the tenant's memory database on ephemeral
    # container storage and every redeploy would silently erase it. So a mismatch
    # here does not degrade anything -- it means every provisioned tenant refuses to
    # boot.
    #
    # `tests/test_cross_service_contracts.py` reads the Dockerfile and fails if
    # these two ever drift apart again.
    tenant_volume_mount_path: str = "/opt/data"
    tenant_port: int = 8080
    tenant_service_name_prefix: str = "tenant-"

    # -- Fleet heartbeat (Task 0.6) -----------------------------------------
    # A tenant beats every SQUIRE_HEARTBEAT_INTERVAL seconds (300 in the image).
    # 900 = three missed beats before `/fleet` calls a tenant stale: long enough to
    # ride out one redeploy or a sleeping serverless container waking up, short
    # enough that the upgrade drill notices a canary that never came back.
    heartbeat_stale_seconds: float = 900.0

    # -- Ingress ------------------------------------------------------------
    # Telegram webhooks are registered as `<ingress_public_url>/telegram/<bot_id>`.
    ingress_public_url: str = "http://localhost:8081"

    # -- Trial proxy (LiteLLM) ---------------------------------------------
    litellm_base_url: str = ""
    litellm_master_key: str = ""
    # What the tenant should use as ANTHROPIC_BASE_URL during the trial.
    # Defaults to the LiteLLM base URL; overridable if the proxy is fronted.
    trial_anthropic_base_url: str = ""
    trial_max_budget_usd: float = 2.0  # hard cap, PRD §5.3
    trial_duration: str = "72h"  # key auto-expires with the trial
    trial_daily_budget_usd: float = 1.0  # best-effort daily ceiling
    trial_rpm_limit: int = 20  # best-effort burst ceiling
    litellm_timeout_seconds: float = 30.0

    # -- Telegram -----------------------------------------------------------
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_timeout_seconds: float = 20.0

    # -- Provisioning state machine ----------------------------------------
    provision_max_attempts: int = 5
    provision_backoff_base_seconds: float = 5.0
    # Run the state machine in a FastAPI BackgroundTask right after tenant creation.
    provision_auto_advance: bool = True
    # In-process sweeper that picks up jobs stranded by a restart.
    provision_worker_enabled: bool = True
    provision_worker_interval_seconds: float = 10.0

    @property
    def effective_trial_base_url(self) -> str:
        """ANTHROPIC_BASE_URL handed to a trial tenant."""
        return self.trial_anthropic_base_url or self.litellm_base_url

    @property
    def effective_control_api_url(self) -> str:
        """CONTROL_API_URL handed to a tenant container."""
        return self.control_api_internal_url or self.control_api_url


@lru_cache
def get_settings() -> Settings:
    """Cached settings. Tests call `get_settings.cache_clear()` after monkeypatching."""
    return Settings()
