"""Heartbeat ingest, `/fleet` status, and the upgrade-drill redeploy endpoint.

These three are Task 0.6's control-plane surface. Two properties get most of the
attention here:

1. **The heartbeat is counts-only.** The tenant sends gauges and counters and
   nothing else. That is enforced structurally (`extra="forbid"` on the request
   model, plus the column whitelist in `test_privacy_schema.py`), and asserted
   here so a future tenant build cannot start smuggling a chat id or a message
   preview into the control plane without a test going red.
2. **The redeploy endpoint is the upgrade drill's only lever.** It must set the
   image, tell the tenant which image it is running (so `/fleet` can prove the
   canary actually converged), and trigger exactly one deploy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import respx
from sqlmodel import select

from control_api import db
from control_api.models import Bot, BotStatus, Heartbeat, Tenant, TenantStatus
from railway_fake import GQL_URL, FakeRailway

BOT_ID = 123456
IMAGE_V1 = "ghcr.io/shagarwal/squire/hermes-tenant:v1"
IMAGE_V2 = "ghcr.io/shagarwal/squire/hermes-tenant:v2"


def seed_tenant(
    tenant_id: str = "t-1",
    email: str | None = None,
    status: TenantStatus = TenantStatus.RUNNING,
    service_id: str | None = "svc-1",
) -> str:
    """Insert a provisioned tenant directly (no state machine)."""
    with db.session_scope() as s:
        s.add(
            Tenant(
                id=tenant_id,
                email=email or f"{tenant_id}@squire.test",
                status=status,
                railway_service_id=service_id,
                railway_service_name=f"tenant-{tenant_id}",
                internal_url=f"http://tenant-{tenant_id}.railway.internal:8080",
                dek_set=True,
                image_ref=IMAGE_V1,
            )
        )
        s.commit()
    return tenant_id


def heartbeat_payload(tenant_id: str = "t-1", **overrides) -> dict:
    payload = {
        "tenant_id": tenant_id,
        "uptime_seconds": 120,
        "image_ref": IMAGE_V1,
        "gateway_up": True,
        "hindsight_up": True,
        "memory_rss_mb": 812,
        "volume_used_mb": 430,
        "volume_total_mb": 5120,
        "updates_forwarded": 12,
        "updates_failed": 1,
        "updates_rejected": 0,
        "hindsight_ops_pending": 2,
        "hindsight_ops_processing": 1,
        "hindsight_ops_failed": 0,
        "backup_last_success_age_seconds": 3600,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Heartbeat ingest
# ---------------------------------------------------------------------------


def test_heartbeat_requires_the_internal_bearer_token(client):
    seed_tenant()
    assert client.post("/internal/heartbeat", json=heartbeat_payload()).status_code == 401


def test_heartbeat_404s_for_an_unknown_tenant(client, auth):
    r = client.post("/internal/heartbeat", json=heartbeat_payload("nope"), headers=auth)
    assert r.status_code == 404


def test_heartbeat_stores_one_latest_row_per_tenant(client, auth):
    seed_tenant()
    first = client.post("/internal/heartbeat", json=heartbeat_payload(), headers=auth)
    assert first.status_code == 200, first.text

    second = client.post(
        "/internal/heartbeat",
        json=heartbeat_payload(uptime_seconds=900, updates_forwarded=99),
        headers=auth,
    )
    assert second.status_code == 200

    with db.session_scope() as s:
        rows = list(s.exec(select(Heartbeat)))
        assert len(rows) == 1, "heartbeats are latest-state, not an append-only log"
        assert rows[0].uptime_seconds == 900
        assert rows[0].updates_forwarded == 99


def test_heartbeat_rejects_anything_that_is_not_a_declared_counter(client, auth):
    """The structural half of the privacy promise.

    A tenant build that tried to attach conversational context -- a chat id, a
    message preview, an error string with user text in it -- must be rejected at
    the door rather than silently persisted into a JSON column later.
    """
    seed_tenant()
    for smuggled in ({"chat_id": 42}, {"last_message": "hello"}, {"error_text": "oops"}):
        r = client.post(
            "/internal/heartbeat",
            json=heartbeat_payload(**smuggled),
            headers=auth,
        )
        assert r.status_code == 422, f"{smuggled} was accepted: {r.text}"


def test_heartbeat_rejects_impossible_counters(client, auth):
    seed_tenant()
    r = client.post(
        "/internal/heartbeat", json=heartbeat_payload(updates_forwarded=-1), headers=auth
    )
    assert r.status_code == 422


def test_heartbeat_accepts_a_partially_blind_tenant(client, auth):
    """Every collector on the tenant side is individually allowed to fail.

    A tenant that cannot read its cgroup or reach the Hindsight DB still reports
    what it does know -- otherwise the first thing to break would also take away
    the signal telling us something broke.
    """
    seed_tenant()
    minimal = {
        "tenant_id": "t-1",
        "uptime_seconds": 5,
        "gateway_up": False,
        "hindsight_up": False,
    }
    r = client.post("/internal/heartbeat", json=minimal, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == "t-1"
    assert body["received"] is True


# ---------------------------------------------------------------------------
# /fleet
# ---------------------------------------------------------------------------


def test_fleet_requires_auth(client):
    assert client.get("/internal/fleet").status_code == 401


def test_fleet_reports_status_freshness_and_counters(client, auth):
    seed_tenant("t-1")
    seed_tenant("t-2")
    client.post("/internal/heartbeat", json=heartbeat_payload("t-1"), headers=auth)

    r = client.get("/internal/fleet", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()

    by_id = {t["tenant_id"]: t for t in body["tenants"]}
    seen, unseen = by_id["t-1"], by_id["t-2"]

    assert seen["status"] == "running"
    assert seen["image_ref"] == IMAGE_V1  # what control-api last deployed
    assert seen["reported_image_ref"] == IMAGE_V1  # what the tenant says it runs
    assert seen["heartbeat_fresh"] is True
    assert seen["heartbeat_age_seconds"] is not None
    assert seen["updates_forwarded"] == 12
    assert seen["memory_rss_mb"] == 812
    # A backup that quietly stopped is invisible unless someone looks at this.
    assert seen["backup_last_success_age_seconds"] == 3600

    # Never heard from: nulls, not zeros. "0 messages" and "no idea" are very
    # different operational states and must not look alike.
    assert unseen["last_heartbeat_at"] is None
    assert unseen["heartbeat_age_seconds"] is None
    assert unseen["heartbeat_fresh"] is False
    assert unseen["updates_forwarded"] is None

    assert body["summary"] == {
        "tenants": 2,
        "running": 2,
        "heartbeating": 1,
        "stale": 0,
        "never_seen": 1,
    }


def test_fleet_marks_an_old_heartbeat_stale(client, auth, settings):
    seed_tenant()
    client.post("/internal/heartbeat", json=heartbeat_payload(), headers=auth)

    # Rewind the stored heartbeat well past the staleness window.
    with db.session_scope() as s:
        row = s.get(Heartbeat, "t-1")
        row.received_at = datetime.now(timezone.utc) - timedelta(
            seconds=settings.heartbeat_stale_seconds * 3
        )
        s.add(row)
        s.commit()

    body = client.get("/internal/fleet", headers=auth).json()
    tenant = body["tenants"][0]
    assert tenant["heartbeat_fresh"] is False
    assert tenant["heartbeat_age_seconds"] > settings.heartbeat_stale_seconds
    assert body["summary"]["stale"] == 1
    assert body["summary"]["heartbeating"] == 0


def test_fleet_can_filter_by_status(client, auth):
    seed_tenant("t-1")
    seed_tenant("t-2", status=TenantStatus.STOPPED)

    running = client.get("/internal/fleet?status=running", headers=auth).json()
    assert [t["tenant_id"] for t in running["tenants"]] == ["t-1"]
    assert running["summary"]["tenants"] == 1


def test_fleet_carries_no_credentials(client, auth):
    """Same rule as `GET /internal/tenants/{id}`: status metadata only."""
    with db.session_scope() as s:
        s.add(
            Bot(
                id=BOT_ID,
                token=f"{BOT_ID}:AA-secret",
                username="squire_alpha_bot",
                status=BotStatus.ASSIGNED,
                webhook_secret="whsec-abc",
                assigned_tenant_id="t-1",
            )
        )
        s.commit()
    seed_tenant()
    text = client.get("/internal/fleet", headers=auth).text
    assert "AA-secret" not in text
    assert "whsec-abc" not in text


# ---------------------------------------------------------------------------
# Redeploy (the upgrade drill's lever)
# ---------------------------------------------------------------------------


@respx.mock
def test_redeploy_sets_the_image_and_deploys_once(client, auth):
    fake = FakeRailway()
    respx.post(GQL_URL).mock(side_effect=fake.handler)
    seed_tenant()

    r = client.post(
        "/internal/tenants/t-1/redeploy", json={"image_tag": IMAGE_V2}, headers=auth
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "tenant_id": "t-1",
        "image_ref": IMAGE_V2,
        "deployment_triggered": True,
    }

    # The variable is written BEFORE the deploy so the new container comes up
    # already knowing which image it is -- that is what /fleet verifies against.
    assert fake.operations() == [
        "variableCollectionUpsert",
        "serviceInstanceUpdate",
        "serviceInstanceDeploy",
    ]
    sent = fake.variables_for("variableCollectionUpsert")["input"]
    assert sent["variables"] == {"SQUIRE_IMAGE_REF": IMAGE_V2}
    assert sent["skipDeploys"] is True, "we trigger the deploy ourselves, once"

    update = fake.variables_for("serviceInstanceUpdate")
    assert update["serviceId"] == "svc-1"
    assert update["input"]["source"] == {"image": IMAGE_V2}

    with db.session_scope() as s:
        assert s.get(Tenant, "t-1").image_ref == IMAGE_V2


@respx.mock
def test_redeploy_resolves_a_bare_tag_against_the_configured_repository(client, auth):
    """`--image-tag v2` is what an operator actually types under pressure."""
    fake = FakeRailway()
    respx.post(GQL_URL).mock(side_effect=fake.handler)
    seed_tenant()

    r = client.post("/internal/tenants/t-1/redeploy", json={"image_tag": "v2"}, headers=auth)
    assert r.status_code == 200, r.text
    # conftest sets TENANT_IMAGE=ghcr.io/shagarwal/squire/hermes-tenant:v0
    assert r.json()["image_ref"] == "ghcr.io/shagarwal/squire/hermes-tenant:v2"


def test_redeploy_404s_for_an_unknown_tenant(client, auth):
    assert (
        client.post(
            "/internal/tenants/nope/redeploy", json={"image_tag": "v2"}, headers=auth
        ).status_code
        == 404
    )


def test_redeploy_409s_when_the_tenant_has_no_railway_service(client, auth):
    seed_tenant("t-1", status=TenantStatus.RUNNING, service_id=None)
    r = client.post("/internal/tenants/t-1/redeploy", json={"image_tag": "v2"}, headers=auth)
    assert r.status_code == 409
    assert "service" in r.json()["detail"].lower()


@respx.mock
def test_redeploy_409s_for_a_tenant_the_state_machine_still_owns(client, auth):
    """A PROVISIONING tenant already HAS a service id after the first step.

    Redeploying it would race `_step_deploy`: two deploys against one service
    seconds apart, with `set_variables` possibly landing after the container it was
    meant to configure had already booted. The status check, not the service-id
    check, is what prevents that.
    """
    fake = FakeRailway()
    respx.post(GQL_URL).mock(side_effect=fake.handler)
    seed_tenant("t-1", status=TenantStatus.PROVISIONING, service_id="svc-1")

    r = client.post("/internal/tenants/t-1/redeploy", json={"image_tag": "v2"}, headers=auth)
    assert r.status_code == 409
    assert "provisioning" in r.json()["detail"].lower()
    assert fake.calls == [], "must not touch Railway for a tenant mid-provision"


@respx.mock
def test_redeploy_409s_for_a_sleeping_tenant(client, auth):
    """Deploying a scale-to-zero tenant would wake it (and bill for it), and it
    emits no heartbeats while asleep so the drill could not verify it anyway."""
    fake = FakeRailway()
    respx.post(GQL_URL).mock(side_effect=fake.handler)
    seed_tenant("t-1", status=TenantStatus.SLEEPING)
    r = client.post("/internal/tenants/t-1/redeploy", json={"image_tag": "v2"}, headers=auth)
    assert r.status_code == 409
    assert fake.calls == []


@respx.mock
def test_redeploy_of_a_stopped_tenant_updates_the_image_but_does_not_start_it(
    client, auth
):
    """A stopped tenant was halted on purpose (trial expiry, non-payment).

    A fleet upgrade sweeping past must not resurrect it -- that is a container the
    product decided to switch off, and starting it costs money. It still gets the
    new image reference, so a legitimate resume lands on the right version.
    """
    fake = FakeRailway()
    respx.post(GQL_URL).mock(side_effect=fake.handler)
    seed_tenant("t-1", status=TenantStatus.STOPPED)

    r = client.post("/internal/tenants/t-1/redeploy", json={"image_tag": "v2"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["deployment_triggered"] is False
    assert "serviceInstanceDeploy" not in fake.operations()
    assert fake.variables_for("serviceInstanceUpdate")["input"]["source"] == {
        "image": IMAGE_V2
    }
    with db.session_scope() as s:
        assert s.get(Tenant, "t-1").image_ref == IMAGE_V2


def test_redeploy_409s_for_a_deleted_tenant(client, auth):
    seed_tenant("t-1", status=TenantStatus.DELETED, service_id=None)
    r = client.post("/internal/tenants/t-1/redeploy", json={"image_tag": "v2"}, headers=auth)
    assert r.status_code == 409


def test_bare_tags_resolve_against_a_digest_pinned_tenant_image(monkeypatch):
    """TENANT_IMAGE may be digest-pinned -- that is the discipline the tenant
    Dockerfile's own FROM uses. Splitting the tag off naively would cut inside
    `@sha256:` and emit `repo@sha256:v2`, which is not a reference at all."""
    from control_api import config, provisioning

    digest = "a" * 64
    monkeypatch.setenv("TENANT_IMAGE", f"ghcr.io/shagarwal/squire/hermes-tenant@sha256:{digest}")
    config.get_settings.cache_clear()
    try:
        assert (
            provisioning.resolve_image_ref("v2")
            == "ghcr.io/shagarwal/squire/hermes-tenant:v2"
        )
    finally:
        config.get_settings.cache_clear()


def test_image_reference_resolution_handles_registry_ports_and_full_refs(monkeypatch):
    from control_api import config, provisioning

    monkeypatch.setenv("TENANT_IMAGE", "ghcr.io:443/org/img:v0")
    config.get_settings.cache_clear()
    try:
        assert provisioning.resolve_image_ref("v2") == "ghcr.io:443/org/img:v2"
        # A full reference is passed through untouched, whatever the base is.
        assert provisioning.resolve_image_ref("other.io/x/y:v9") == "other.io/x/y:v9"
    finally:
        config.get_settings.cache_clear()


def test_redeploy_rejects_a_malformed_image_reference(client, auth):
    seed_tenant()
    for bad in ("", "  ", "v2 && rm -rf /", "tag\nwith-newline"):
        r = client.post(
            "/internal/tenants/t-1/redeploy", json={"image_tag": bad}, headers=auth
        )
        assert r.status_code == 422, f"{bad!r} was accepted"
