"""LiteLLM trial-key client tests -- all HTTP mocked.

The hard $2 budget is the entire trial abuse story (PRD §5.3), so it is asserted
explicitly rather than left to configuration drift.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from control_api.trial_keys import TrialKeyClient, TrialKeyError, trial_key_alias

BASE = "https://trial-proxy.squire.test"


def test_alias_is_derived_from_tenant_id():
    # We delete keys *by alias* so the control DB never has to store the key itself.
    assert trial_key_alias("t-abc") == "squire-trial-t-abc"


@respx.mock
def test_generate_uses_master_key_and_hard_budget():
    route = respx.post(f"{BASE}/key/generate").mock(
        return_value=httpx.Response(200, json={"key": "sk-tenant-abc", "expires": "..."})
    )
    result = TrialKeyClient().create_trial_key("t-abc")
    assert result.key == "sk-tenant-abc"
    assert result.alias == "squire-trial-t-abc"

    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer sk-master-test"

    sent = json.loads(req.read())
    assert sent["max_budget"] == 2.0  # hard cap, PRD §5.3
    assert sent["metadata"]["tenant_id"] == "t-abc"
    assert sent["key_alias"] == "squire-trial-t-abc"
    assert sent["duration"] == "72h"  # trial length


@respx.mock
def test_generate_raises_on_error():
    respx.post(f"{BASE}/key/generate").mock(return_value=httpx.Response(500, text="nope"))
    with pytest.raises(TrialKeyError):
        TrialKeyClient().create_trial_key("t-abc")


@respx.mock
def test_delete_by_alias():
    route = respx.post(f"{BASE}/key/delete").mock(
        return_value=httpx.Response(200, json={"deleted_keys": ["squire-trial-t-abc"]})
    )
    assert TrialKeyClient().delete_trial_key("squire-trial-t-abc") is True

    sent = json.loads(route.calls.last.request.read())
    assert sent == {"key_aliases": ["squire-trial-t-abc"]}


@respx.mock
def test_delete_is_idempotent_when_key_is_already_gone():
    respx.post(f"{BASE}/key/delete").mock(
        return_value=httpx.Response(400, json={"detail": "Keys not found"})
    )
    assert TrialKeyClient().delete_trial_key("squire-trial-t-abc") is False


def test_client_disabled_when_litellm_not_configured(monkeypatch):
    """Phase 0 may run before trial-proxy exists; provisioning must still work."""
    from control_api import config

    monkeypatch.setenv("LITELLM_BASE_URL", "")
    config.get_settings.cache_clear()
    client = TrialKeyClient()
    assert client.enabled is False
    assert client.create_trial_key("t-abc") is None
    assert client.delete_trial_key("squire-trial-t-abc") is False
