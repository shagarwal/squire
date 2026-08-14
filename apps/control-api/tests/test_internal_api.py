"""HTTP surface tests for the internal (service-to-service) API.

`GET /internal/tenants/by-bot/{bot_id}` is the hottest path in the whole system --
ingress calls it on *every* Telegram update -- so its response shape is pinned here
exactly as documented in the interface contract.
"""

from __future__ import annotations

import httpx
import respx

from control_api import db, provisioning
from control_api.models import Bot, BotStatus, JobStatus, Tenant, TenantStatus
from railway_fake import GQL_URL, FakeRailway

BOT_ID = 123456
BOT_TOKEN = f"{BOT_ID}:AA-secret"
LITELLM = "https://trial-proxy.squire.test"


def seed_running_tenant(bind_nonce: str | None = None) -> str:
    """Insert a fully-provisioned tenant + bot directly (no state machine)."""
    with db.session_scope() as s:
        s.add(
            Bot(
                id=BOT_ID,
                token=BOT_TOKEN,
                username="squire_alpha_bot",
                status=BotStatus.ASSIGNED,
                webhook_secret="whsec-abc",
                assigned_tenant_id="t-1",
            )
        )
        s.add(
            Tenant(
                id="t-1",
                email="alpha@squire.test",
                status=TenantStatus.RUNNING,
                bot_id=BOT_ID,
                railway_service_id="svc-1",
                railway_service_name="tenant-t-1",
                internal_url="http://tenant-t-1.railway.internal:8080",
                dek_set=True,
                trial_key_alias="squire-trial-t-1",
                trial_key_active=True,
                webhook_set=True,
                bind_nonce=bind_nonce,
            )
        )
        s.commit()
    return "t-1"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_healthz_needs_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_internal_endpoints_require_bearer_token(client):
    seed_running_tenant()
    assert client.get(f"/internal/tenants/by-bot/{BOT_ID}").status_code == 401
    assert (
        client.get(
            f"/internal/tenants/by-bot/{BOT_ID}",
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            f"/internal/tenants/by-bot/{BOT_ID}",
            headers={"Authorization": "test-internal-token"},  # missing "Bearer "
        ).status_code
        == 401
    )


# ---------------------------------------------------------------------------
# by-bot lookup (ingress hot path)
# ---------------------------------------------------------------------------


def test_by_bot_lookup_returns_the_contract_shape(client, auth):
    seed_running_tenant()
    r = client.get(f"/internal/tenants/by-bot/{BOT_ID}", headers=auth)
    assert r.status_code == 200
    assert r.json() == {
        "tenant_id": "t-1",
        "status": "running",
        "internal_url": "http://tenant-t-1.railway.internal:8080",
        "webhook_secret": "whsec-abc",
    }


def test_by_bot_lookup_404s_for_unknown_bot(client, auth):
    seed_running_tenant()
    assert client.get("/internal/tenants/by-bot/999999", headers=auth).status_code == 404


def test_by_bot_lookup_404s_for_an_unassigned_pool_bot(client, auth):
    with db.session_scope() as s:
        s.add(
            Bot(
                id=777,
                token="777:AA",
                username="idle_bot",
                status=BotStatus.AVAILABLE,
                webhook_secret="s",
            )
        )
        s.commit()
    assert client.get("/internal/tenants/by-bot/777", headers=auth).status_code == 404


def test_by_bot_lookup_is_indexed(client, auth):
    """Guard the hot path: bot_id is the PK and tenant.bot_id is indexed."""
    from sqlalchemy import inspect

    from control_api.db import get_engine

    indexes = inspect(get_engine()).get_indexes("tenant")
    indexed_columns = {tuple(ix["column_names"]) for ix in indexes}
    assert ("bot_id",) in indexed_columns


# ---------------------------------------------------------------------------
# Tenant lifecycle endpoints
# ---------------------------------------------------------------------------


@respx.mock
def test_create_tenant_returns_job_and_bot_link(client, auth):
    respx.post(GQL_URL).mock(side_effect=FakeRailway().handler)
    with db.session_scope() as s:
        s.add(
            Bot(
                id=BOT_ID,
                token=BOT_TOKEN,
                username="squire_alpha_bot",
                status=BotStatus.AVAILABLE,
                webhook_secret="whsec-abc",
            )
        )
        s.commit()

    r = client.post("/internal/tenants", json={"email": "alpha@squire.test"}, headers=auth)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "provisioning"
    assert body["bot_username"] == "squire_alpha_bot"
    assert body["telegram_link"] == "https://t.me/squire_alpha_bot"
    assert body["job_id"]

    job = client.get(f"/internal/provision-jobs/{body['job_id']}", headers=auth).json()
    assert job["status"] == "pending"
    assert job["step"] == "create_service"


def test_create_tenant_409s_when_pool_is_empty(client, auth):
    r = client.post("/internal/tenants", json={"email": "alpha@squire.test"}, headers=auth)
    assert r.status_code == 409
    assert "bot" in r.json()["detail"].lower()


def test_get_tenant_exposes_status_but_no_secrets(client, auth):
    seed_running_tenant()
    r = client.get("/internal/tenants/t-1", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "t-1"
    assert body["status"] == "running"
    assert body["bot_username"] == "squire_alpha_bot"
    # The bot token is a Squire credential; it has no business leaving control-api.
    assert BOT_TOKEN not in r.text
    assert "dek" not in r.text.lower() or body.get("dek_set") is True


def test_tenant_link_carries_the_bind_nonce_once_provisioned(client, auth):
    """The ?start= deep link is THE handout path for the bind nonce: the tenant
    only binds a /start carrying it, so a bare link would onboard the user into
    upstream's pairing-code dead end. (Before set_variables mints a nonce the
    link is bare — see test_create_tenant_returns_job_and_bot_link — which is
    fine because that link is not yet tappable anyway.)"""
    seed_running_tenant(bind_nonce="n0nce-abc123")
    r = client.get("/internal/tenants/t-1", headers=auth)
    assert r.status_code == 200
    assert r.json()["telegram_link"] == "https://t.me/squire_alpha_bot?start=n0nce-abc123"


def test_get_unknown_tenant_404s(client, auth):
    assert client.get("/internal/tenants/nope", headers=auth).status_code == 404


@respx.mock
def test_advance_endpoint_drives_the_state_machine(client, auth):
    fake = FakeRailway()
    respx.post(GQL_URL).mock(side_effect=fake.handler)
    respx.post(f"{LITELLM}/key/generate").mock(
        return_value=httpx.Response(200, json={"key": "sk-trial"})
    )
    respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    with db.session_scope() as s:
        s.add(
            Bot(
                id=BOT_ID,
                token=BOT_TOKEN,
                username="squire_alpha_bot",
                status=BotStatus.AVAILABLE,
                webhook_secret="whsec-abc",
            )
        )
        s.commit()

    created = client.post(
        "/internal/tenants", json={"email": "alpha@squire.test"}, headers=auth
    ).json()
    r = client.post(f"/internal/provision-jobs/{created['job_id']}/advance", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"
    assert r.json()["step"] == "done"

    tenant = client.get(f"/internal/tenants/{created['tenant_id']}", headers=auth).json()
    assert tenant["status"] == "running"


# ---------------------------------------------------------------------------
# Trial key revocation
# ---------------------------------------------------------------------------


@respx.mock
def test_revoke_trial_key(client, auth):
    seed_running_tenant()
    route = respx.post(f"{LITELLM}/key/delete").mock(
        return_value=httpx.Response(200, json={"deleted_keys": ["squire-trial-t-1"]})
    )
    r = client.post("/internal/tenants/t-1/revoke-trial-key", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"tenant_id": "t-1", "trial_key_active": False, "revoked": True}
    assert route.called

    with db.session_scope() as s:
        assert provisioning.get_tenant(s, "t-1").trial_key_active is False

    # Second call is a no-op, not an error (LLM-connect and expiry can race).
    r2 = client.post("/internal/tenants/t-1/revoke-trial-key", headers=auth)
    assert r2.status_code == 200
    assert r2.json()["revoked"] is False


def test_revoke_trial_key_404s_for_unknown_tenant(client, auth):
    assert client.post("/internal/tenants/nope/revoke-trial-key", headers=auth).status_code == 404


# ---------------------------------------------------------------------------
# Bot pool registration (infra/load_bots.py)
# ---------------------------------------------------------------------------


@respx.mock
def test_register_bots_validates_tokens_against_getme(client, auth):
    good = "111:AA-good"
    bad = "222:AA-bad"
    respx.post(f"https://api.telegram.org/bot{good}/getMe").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"id": 111, "username": "good_bot"}}
        )
    )
    respx.post(f"https://api.telegram.org/bot{bad}/getMe").mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )

    r = client.post("/internal/bots", json={"tokens": [good, bad]}, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registered"] == [{"bot_id": 111, "username": "good_bot"}]
    assert body["skipped"] == []
    assert len(body["failed"]) == 1
    assert body["failed"][0]["bot_id"] == 222

    with db.session_scope() as s:
        bot = s.get(Bot, 111)
        assert bot.status == BotStatus.AVAILABLE
        assert bot.webhook_secret  # generated per bot at registration
        assert s.get(Bot, 222) is None


@respx.mock
def test_register_bots_is_idempotent(client, auth):
    good = "111:AA-good"
    respx.post(f"https://api.telegram.org/bot{good}/getMe").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"id": 111, "username": "good_bot"}}
        )
    )
    client.post("/internal/bots", json={"tokens": [good]}, headers=auth)
    r = client.post("/internal/bots", json={"tokens": [good]}, headers=auth)
    assert r.json()["registered"] == []
    assert r.json()["skipped"] == [{"bot_id": 111, "reason": "already_registered"}]


def test_bot_pool_stats(client, auth):
    seed_running_tenant()
    with db.session_scope() as s:
        s.add(
            Bot(
                id=888,
                token="888:AA",
                username="free_bot",
                status=BotStatus.AVAILABLE,
                webhook_secret="s",
            )
        )
        s.commit()
    r = client.get("/internal/bots", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"total": 2, "available": 1, "assigned": 1, "disabled": 0}


# ---------------------------------------------------------------------------
# POST /internal/llm-connected (1C): the tenant reports its owner connected an
# LLM; control-api records the provider NAME and revokes the trial key.
# ---------------------------------------------------------------------------


class TestLlmConnected:
    def _tenant_with_trial_key(self, session):
        tenant = Tenant(
            id="t-conn-1",
            email="conn@example.com",
            status=TenantStatus.RUNNING,
            trial_key_alias="squire-trial-t-conn-1",
            trial_key_active=True,
        )
        session.add(tenant)
        session.commit()
        return tenant

    @respx.mock
    def test_records_provider_and_revokes_trial_key(self, client, auth, session):
        self._tenant_with_trial_key(session)
        delete = respx.post("https://trial-proxy.squire.test/key/delete").mock(
            return_value=httpx.Response(200, json={"deleted_keys": 1})
        )
        response = client.post(
            "/internal/llm-connected",
            json={"tenant_id": "t-conn-1", "provider": "openai"},
            headers=auth,
        )
        assert response.status_code == 200
        assert response.json() == {
            "tenant_id": "t-conn-1",
            "provider": "openai",
            "connected_provider_recorded": True,
            "trial_key_revoked": True,
        }
        assert delete.called

        with db.session_scope() as s:
            tenant = s.get(Tenant, "t-conn-1")
            assert tenant.connected_provider == "openai"
            assert tenant.trial_key_active is False

    def test_idempotent_when_trial_key_already_gone(self, client, auth, session):
        session.add(Tenant(id="t-conn-2", email="conn2@example.com",
                           status=TenantStatus.RUNNING, trial_key_active=False))
        session.commit()
        response = client.post(
            "/internal/llm-connected",
            json={"tenant_id": "t-conn-2", "provider": "chatgpt"},
            headers=auth,
        )
        assert response.status_code == 200
        assert response.json()["trial_key_revoked"] is False
        assert response.json()["connected_provider_recorded"] is True

    def test_unknown_tenant_404(self, client, auth):
        response = client.post(
            "/internal/llm-connected",
            json={"tenant_id": "t-none", "provider": "openai"},
            headers=auth,
        )
        assert response.status_code == 404

    def test_requires_internal_token(self, client):
        assert client.post(
            "/internal/llm-connected",
            json={"tenant_id": "t", "provider": "openai"},
        ).status_code == 401

    def test_provider_vocabulary_is_closed(self, client, auth, session):
        self._tenant_with_trial_key(session)
        # Not a provider name -> 422. This is the privacy guard: the field can
        # never carry key material because only three literals fit through it.
        response = client.post(
            "/internal/llm-connected",
            json={"tenant_id": "t-conn-1", "provider": "sk-ant-api03-oops"},
            headers=auth,
        )
        assert response.status_code == 422

    def test_extra_fields_are_rejected(self, client, auth, session):
        self._tenant_with_trial_key(session)
        response = client.post(
            "/internal/llm-connected",
            json={"tenant_id": "t-conn-1", "provider": "openai",
                  "api_key": "sk-should-never-fit"},
            headers=auth,
        )
        assert response.status_code == 422


class TestHeartbeatReconciliation:
    @respx.mock
    def test_connected_beat_with_live_trial_key_revokes_it(self, client, auth, session):
        session.add(Tenant(id="t-beat-1", email="beat@example.com",
                           status=TenantStatus.RUNNING,
                           trial_key_alias="squire-trial-t-beat-1",
                           trial_key_active=True))
        session.commit()

        delete = respx.post("https://trial-proxy.squire.test/key/delete").mock(
            return_value=httpx.Response(200, json={"deleted_keys": 1})
        )
        response = client.post(
            "/internal/heartbeat",
            json={"tenant_id": "t-beat-1", "uptime_seconds": 10,
                  "gateway_up": True, "hindsight_up": True,
                  "llm_connected": True},
            headers=auth,
        )
        assert response.status_code == 200
        assert delete.called, "backstop must revoke the orphaned trial key"

        with db.session_scope() as s:
            assert s.get(Tenant, "t-beat-1").trial_key_active is False

    @respx.mock
    def test_unconnected_beat_leaves_the_trial_key_alone(self, client, auth, session):
        session.add(Tenant(id="t-beat-2", email="beat2@example.com",
                           status=TenantStatus.RUNNING,
                           trial_key_alias="squire-trial-t-beat-2",
                           trial_key_active=True))
        session.commit()

        delete = respx.post("https://trial-proxy.squire.test/key/delete").mock(
            return_value=httpx.Response(200, json={"deleted_keys": 1})
        )
        response = client.post(
            "/internal/heartbeat",
            json={"tenant_id": "t-beat-2", "uptime_seconds": 10,
                  "gateway_up": True, "hindsight_up": True,
                  "llm_connected": False},
            headers=auth,
        )
        assert response.status_code == 200
        assert not delete.called
