"""Validation tests for apps/trial-proxy/config.yaml (Task 0.5).

Scope, deliberately narrow: this is a static config-sanity check, NOT an
integration test. It asserts the file is valid YAML and that the specific
settings this task's brief called out as critical are present and set to
the values we reasoned through in config.yaml's comments (privacy: no
message-content retention; auth/DB wiring; the $2-budget-enabling settings;
model mapping). It makes no network calls and does not start a proxy.

No litellm-package config-load smoke test: `litellm` is a large dependency
(pulls in fastapi, prisma, tiktoken, openai, and more) and this sandbox's
Python is an externally-managed system install (PEP 668 — `pip install`
system-wide is blocked without --break-system-packages, and a throwaway
venv + full litellm install is out of scope for what should be a fast,
CI-friendly validation test). Skipped per the task brief's explicit
allowance to do so with a comment when litellm isn't quickly installable.
If this ever needs a real config-load smoke test, run
`litellm --config config.yaml --config_only` (or equivalent, verify against
the pinned version's CLI) inside a venv with `pip install litellm` and
ANTHROPIC_API_KEY/LITELLM_MASTER_KEY/DATABASE_URL set to dummy values.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML required to parse config.yaml")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@pytest.fixture(scope="module")
def config() -> dict:
    """Parse config.yaml once per test module run."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_config_file_exists():
    assert CONFIG_PATH.is_file(), f"expected {CONFIG_PATH} to exist"


def test_config_is_valid_yaml(config):
    # yaml.safe_load already raised on parse above if this were invalid;
    # this just asserts we got the top-level mapping we expect, not e.g.
    # None (empty file) or a scalar/list (malformed top-level structure).
    assert isinstance(config, dict)


def test_has_model_list_with_at_least_one_entry(config):
    assert "model_list" in config
    assert isinstance(config["model_list"], list)
    assert len(config["model_list"]) >= 1


def test_trial_default_model_maps_to_haiku_via_env_api_key(config):
    """The proxy-owned "trial-default" name must map to a Haiku-class
    Anthropic model, with the API key sourced from the environment (never
    hardcoded in the repo)."""
    entries = {m["model_name"]: m for m in config["model_list"]}
    assert "trial-default" in entries, (
        "config.yaml must expose a model_name literally called "
        "'trial-default' per the task brief"
    )
    params = entries["trial-default"]["litellm_params"]
    assert "haiku" in params["model"].lower(), (
        f"trial-default must map to a Haiku-class model, got {params['model']!r}"
    )
    assert params["api_key"] == "os.environ/ANTHROPIC_API_KEY", (
        "api_key must be sourced from ANTHROPIC_API_KEY via LiteLLM's "
        "os.environ/ convention, not a literal key"
    )


def test_claude_haiku_4_5_alias_present_for_hermes_agent(config):
    """Load-bearing alias, not decorative: hermes-agent (tenant-image) does
    not know about "trial-default" -- it sends whatever bare Anthropic model
    id its own `hermes model anthropic:<id>` config was set to as the
    `model` field on each request. That id is `claude-haiku-4-5` (verified
    against the tenant template by the test below); without an explicit
    model_name entry for it here, LiteLLM 400s on every trial tenant request
    instead of silently misrouting one (which is the intended fail-closed
    behavior -- but only useful if this entry exists to catch the expected
    case)."""
    entries = {m["model_name"]: m for m in config["model_list"]}
    assert "claude-haiku-4-5" in entries, (
        "config.yaml must expose a model_name literally called "
        "'claude-haiku-4-5' as the hermes-agent-facing alias -- see the "
        "model-name contract comment above this model_list entry in config.yaml"
    )
    params = entries["claude-haiku-4-5"]["litellm_params"]
    assert params["model"] == "anthropic/claude-haiku-4-5"
    assert params["api_key"] == "os.environ/ANTHROPIC_API_KEY"


# --- the cross-service half of the same contract ---------------------------
TENANT_CONFIG_PATH = (
    CONFIG_PATH.parent.parent.parent / "tenant-image" / "home-template" / "config.yaml"
)


def test_the_tenant_images_default_model_is_reachable_through_this_proxy():
    """Close the loop the comment in config.yaml describes (Task 0.6).

    The two files are owned by different tasks and edited by different people,
    and nothing else connects them: a tenant asking for a model this proxy does
    not list gets a 400 on its very first message, which looks to the user like
    an agent that never worked at all. So assert the contract from both ends.

    This reaches outside apps/trial-proxy on purpose. The alternative -- a
    duplicated constant -- is exactly the drift this is meant to catch.
    (`.github/workflows/test.yml` therefore also runs this suite when the tenant
    template changes.)
    """
    if not TENANT_CONFIG_PATH.is_file():  # pragma: no cover - not in this checkout
        pytest.skip(f"tenant template not present at {TENANT_CONFIG_PATH}")

    with open(TENANT_CONFIG_PATH, "r", encoding="utf-8") as f:
        tenant_config = yaml.safe_load(f)

    requested = tenant_config.get("model")
    assert requested, "the tenant template must pin a model"

    # hermes' config is `provider:model`; the bare model id is what lands in the
    # request body's `model` field, and that is what LiteLLM matches on.
    provider, _, model_id = requested.partition(":")
    assert provider == "anthropic", (
        f"the tenant template requests provider {provider!r}; the trial proxy only "
        "fronts Anthropic models"
    )

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        proxy_config = yaml.safe_load(f)
    names = {m["model_name"] for m in proxy_config["model_list"]}

    assert model_id in names, (
        f"tenant-image/home-template/config.yaml requests {requested!r}, so trial "
        f"tenants send model={model_id!r} -- but this proxy only exposes {sorted(names)}. "
        "LiteLLM would 400 every trial message. Add a model_name entry here (Haiku-"
        "pinned, per the no-wildcard-routing rule) or change the tenant template."
    )


def test_no_model_list_entry_hardcodes_a_non_haiku_anthropic_model(config):
    """Guard against silently widening the trial key's blast radius: every
    model this trial proxy can reach must resolve to a Haiku-class model.
    (We deliberately don't use LiteLLM's provider-wildcard routing for this
    exact reason -- see config.yaml's model_list comments.)"""
    for entry in config["model_list"]:
        underlying_model = entry["litellm_params"]["model"]
        assert "haiku" in underlying_model.lower(), (
            f"model_list entry {entry.get('model_name')!r} maps to "
            f"{underlying_model!r}, which is not Haiku-class -- every "
            f"model reachable via this trial proxy must be Haiku-class "
            f"per the $2 hard-budget / low-abuse-value design"
        )
        assert underlying_model.startswith("anthropic/"), (
            f"model_list entry {entry.get('model_name')!r} maps to "
            f"{underlying_model!r}, expected an anthropic/* model id"
        )


def test_general_settings_master_key_wired_to_env(config):
    gs = config.get("general_settings", {})
    assert gs.get("master_key") == "os.environ/LITELLM_MASTER_KEY", (
        "general_settings.master_key must be os.environ/LITELLM_MASTER_KEY "
        "so control-api's admin-API calls (POST /key/generate, /key/delete) "
        "can authenticate, and so no real key is ever committed to the repo"
    )


def test_general_settings_database_url_wired_to_env(config):
    gs = config.get("general_settings", {})
    assert gs.get("database_url") == "os.environ/DATABASE_URL", (
        "general_settings.database_url must be os.environ/DATABASE_URL -- "
        "virtual keys (and therefore the $2 per-tenant budget) require "
        "LiteLLM's Postgres-backed key store; without this, control-api's "
        "POST /key/generate calls have nothing to persist a tenant's key, "
        "budget, or spend to"
    )


def test_prisma_schema_self_migration_explicitly_enabled(config):
    """Virtual keys are useless without their Postgres tables existing. The
    plain `litellm` image self-applies its Prisma schema on every normal
    boot (confirmed against the pinned version's actual source -- see the
    Dockerfile's version-pin comment), gated on this being false. It
    defaults to false already, but is set explicitly here so that intent is
    visible in the repo rather than relying on an undocumented library
    default -- this test guards against someone flipping it (or a copied
    snippet setting it true) by accident."""
    gs = config.get("general_settings", {})
    assert gs.get("disable_prisma_schema_update") is False, (
        "general_settings.disable_prisma_schema_update must be explicitly "
        "false -- true would stop the proxy from ever creating/updating "
        "the virtual-keys schema on a fresh Postgres DB"
    )


def test_privacy_no_message_content_retention(config):
    """Trial tenants' conversation content must not be stored in our proxy
    DB or logged to any callback -- see docs/prd.md's "we cannot see their
    credentials, data, or conversations" guarantee and config.yaml's
    privacy-section comments."""
    gs = config.get("general_settings", {})
    ls = config.get("litellm_settings", {})

    assert gs.get("store_prompts_in_spend_logs") is False, (
        "general_settings.store_prompts_in_spend_logs must be explicitly "
        "false -- otherwise LiteLLM persists prompt/response text into the "
        "spend_logs table in our own Postgres DB"
    )
    assert ls.get("turn_off_message_logging") is True, (
        "litellm_settings.turn_off_message_logging must be true -- stops "
        "message/response bodies from being sent to any logging callback"
    )


def test_fail_closed_on_db_unavailable(config):
    """Budget/revocation enforcement is the entire point of this proxy, so
    a DB hiccup must block traffic rather than silently let an over-budget
    or revoked tenant key through."""
    gs = config.get("general_settings", {})
    assert gs.get("allow_requests_on_db_unavailable") is False, (
        "general_settings.allow_requests_on_db_unavailable must be false "
        "(fail closed) -- see config.yaml's comments on why fail-open here "
        "would defeat the $2 hard-budget guarantee"
    )


def test_no_global_budget_reset_cadence_configured(config):
    """The trial's $2 cap is a flat, one-time budget across the whole 72h
    trial -- NOT a recurring daily/monthly allowance. A proxy-wide
    default_team_params.budget_duration (or similar reset cadence) would
    silently refill a tenant's budget out from under control-api's
    per-key `max_budget` -- see config.yaml's comments. This test guards
    against that setting being added here by accident."""
    gs = config.get("general_settings", {})
    default_team_params = gs.get("default_team_params", {}) or {}
    assert "budget_duration" not in default_team_params, (
        "do not set a proxy-wide budget_duration default -- the trial's "
        "$2 cap must not be reset on a recurring cadence; budget_duration "
        "(or deliberately omitting it) belongs at key-creation time in "
        "control-api's POST /key/generate call, not here"
    )
