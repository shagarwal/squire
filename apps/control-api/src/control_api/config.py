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
    # (a heartbeat from every container, trial-key revocation) has
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
    # SQUIRE_HEARTBEAT_INTERVAL handed to every provisioned tenant. The image's
    # own default stays 300 (fine for dev/self-hosted, where nothing sleeps), but
    # a provisioned tenant lives on Railway, where serverless sleep only engages
    # after ~10 quiet minutes with no outbound traffic. A 300s beat punches
    # through that window forever, keeping every tenant permanently awake -- and
    # sleep is the whole Railway economics bet (Gate G1, `railway_tenant_sleep`
    # above). 1800s clears the window with margin; verified live on staging
    # 2026-08-12 together with patch 006 (identity-refresh loop off).
    tenant_heartbeat_interval_seconds: int = 1800
    # 5400 = three missed beats at the provisioned interval before `/fleet` calls
    # a tenant stale: long enough to ride out one redeploy or a serverless wake,
    # short enough that the upgrade drill notices a canary that never came back.
    # KNOWN LIMITATION: a tenant that Railway has put to sleep stops beating
    # entirely and WILL read stale here even though it is perfectly healthy --
    # /fleet cannot tell "asleep" from "dead" until it consults Railway's own
    # service state (out of scope for this change).
    heartbeat_stale_seconds: float = 5400.0
    # SQUIRE_UNBOUND_AWAKE_HOURS handed to every provisioned tenant: how long
    # an owner-less tenant keeps the fast (300s) heartbeat that holds it awake
    # for its owner's first deep-link tap. The image's own default is 48h --
    # sized for self-hosted forgiveness, but on Railway that is ~2 days of
    # billed container-hours (~$0.80/signup at 1.2GB awake RSS) spent waiting
    # for a tap that usually comes within minutes. 1h covers the realistic
    # signup -> first-tap gap; a straggler who taps later wakes the tenant
    # through the normal ingress buffer-and-wake path behind a typing
    # indicator (~3-6s observed), exactly like any sleeping bound tenant.
    # Decision + wake-path evidence: G1 measurement session, 2026-08-19/20.
    tenant_unbound_awake_hours: float = 1.0

    # -- Ingress ------------------------------------------------------------
    # Telegram webhooks are registered as `<ingress_public_url>/telegram/<bot_id>`.
    ingress_public_url: str = "http://localhost:8081"

    # -- Trial proxy (LiteLLM) ---------------------------------------------
    litellm_base_url: str = ""
    litellm_master_key: str = ""
    # What the tenant should use as ANTHROPIC_BASE_URL during the trial.
    # Defaults to the LiteLLM base URL; overridable if the proxy is fronted.
    trial_anthropic_base_url: str = ""
    # Hard cap, PRD §5.3. Raised 2.0 -> 10.0 on 2026-08-09 with the trial model
    # move Haiku -> Sonnet: at Sonnet prices ~$2 buys only ~50 turns, which would
    # have cut trials off less than a quarter of the way into the advertised
    # "75 messages a day for 3 days" (~225 turns). The cap is what makes trial
    # abuse bounded (PRD §6), so it is deliberately still a HARD cap -- just one
    # sized to the promise rather than to the old model's economics.
    trial_max_budget_usd: float = 10.0
    trial_duration: str = "72h"  # key auto-expires with the trial
    trial_daily_budget_usd: float = 4.0  # best-effort daily ceiling (~1/3 of cap + burst room)
    trial_rpm_limit: int = 20  # best-effort burst ceiling
    litellm_timeout_seconds: float = 30.0

    # -- Telegram -----------------------------------------------------------
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_timeout_seconds: float = 20.0
    # Typing-on-wake nudge (/internal/wake-typing): how many times to fire
    # sendChatAction("typing") per nudge, and the spacing between them.
    # Telegram shows the action for ~5s per call; 5 sends 4s apart keep the
    # indicator unbroken for ~21s -- the whole of a ~15-20s tenant wake.
    wake_typing_repeats: int = 5
    wake_typing_interval_seconds: float = 4.0

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
