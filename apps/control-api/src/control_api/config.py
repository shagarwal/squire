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
    control_api_url: str = "http://localhost:8080"

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

    # -- Tenant runtime -----------------------------------------------------
    tenant_image: str = "ghcr.io/shagarwal/squire/hermes-tenant:latest"
    # Mounted as the tenant's `~/.hermes` (PRD §4). Configurable because the exact
    # HOME inside the tenant image is owned by Task 0.2.
    tenant_volume_mount_path: str = "/root/.hermes"
    tenant_port: int = 8080
    tenant_service_name_prefix: str = "tenant-"

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


@lru_cache
def get_settings() -> Settings:
    """Cached settings. Tests call `get_settings.cache_clear()` after monkeypatching."""
    return Settings()
