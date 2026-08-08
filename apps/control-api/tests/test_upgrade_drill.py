"""Tests for `infra/upgrade_drill.py` -- the Task 0.6 canary/roll/rollback CLI.

Same shape as `test_infra_cli.py`: control-api itself is mocked, because this is a
thin client over two endpoints. The properties worth pinning are the ones an
operator relies on at 2am:

  * a failing canary leaves the rest of the fleet alone;
  * a roll aborts once `--max-failures` tenants fail, rather than restarting the
    whole fleet onto a bad image;
  * "converged" means the TENANT says it is on the new image, not that Railway
    accepted our request;
  * a rollback pushes through failures instead of aborting.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

import upgrade_drill

API = "https://control-api.squire.test"
OLD = "ghcr.io/shagarwal/squire/hermes-tenant:v1"
NEW = "ghcr.io/shagarwal/squire/hermes-tenant:v2"


@pytest.fixture(autouse=True)
def _cli_env(monkeypatch):
    monkeypatch.setenv("CONTROL_API_URL", API)
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-internal-token")


def row(tenant_id: str, reported: str = OLD, fresh: bool = True) -> dict:
    return {
        "tenant_id": tenant_id,
        "email": f"{tenant_id}@squire.test",
        "status": "running",
        "image_ref": OLD,
        "reported_image_ref": reported,
        "last_heartbeat_at": "2026-08-08T12:00:00Z",
        "heartbeat_age_seconds": 30.0,
        "heartbeat_fresh": fresh,
    }


def fleet_response(*rows: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "summary": {
                "tenants": len(rows),
                "running": len(rows),
                "heartbeating": sum(1 for r in rows if r["heartbeat_fresh"]),
                "stale": 0,
                "never_seen": 0,
            },
            "tenants": list(rows),
        },
    )


def mock_fleet(*sequence: httpx.Response):
    """`/fleet` is polled with and without a `?status=` filter, so route both.

    The last response in the sequence repeats for every further poll -- the CLI
    polls until convergence or timeout, and a fixed-length side_effect list would
    make these tests fail with a StopIteration that says nothing useful.
    """
    responses = list(sequence)

    def handler(request):
        return responses.pop(0) if len(responses) > 1 else responses[0]

    respx.get(url__startswith=f"{API}/internal/fleet").mock(side_effect=handler)


def mock_redeploy(
    tenant_id: str, response: httpx.Response | None = None, image_ref: str = NEW
):
    return respx.post(f"{API}/internal/tenants/{tenant_id}/redeploy").mock(
        return_value=response
        or httpx.Response(
            200,
            json={
                "tenant_id": tenant_id,
                "image_ref": image_ref,
                "deployment_triggered": True,
            },
        )
    )


ARGS = ["--poll-interval", "0"]


# ---------------------------------------------------------------------------
# Canary
# ---------------------------------------------------------------------------


@respx.mock
def test_canary_redeploys_one_tenant_and_verifies_the_reported_image(capsys):
    route = mock_redeploy("t-1")
    others = mock_redeploy("t-2")
    mock_fleet(
        fleet_response(row("t-1"), row("t-2")),  # initial listing
        fleet_response(row("t-1"), row("t-2")),  # not converged yet
        fleet_response(row("t-1", reported=NEW), row("t-2")),  # converged
    )

    code = upgrade_drill.main([*ARGS, "--image-tag", "v2", "--canary", "t-1"])
    assert code == 0

    assert json.loads(route.calls.last.request.read()) == {"image_tag": "v2"}
    assert route.calls.last.request.headers["authorization"] == "Bearer test-internal-token"
    assert not others.called, "without --roll, only the canary is touched"
    out = capsys.readouterr().out
    assert "t-1: OK" in out
    assert "Re-run with --roll" in out


@respx.mock
def test_a_failing_canary_leaves_the_fleet_untouched(capsys):
    mock_redeploy("t-1")
    t2 = mock_redeploy("t-2")
    # The canary never reports the new image: a deploy that "succeeded" into a
    # container that cannot boot looks exactly like this.
    mock_fleet(*[fleet_response(row("t-1"), row("t-2")) for _ in range(6)])

    code = upgrade_drill.main(
        [*ARGS, "--image-tag", "v2", "--canary", "t-1", "--roll", "--timeout", "0"]
    )
    assert code == 2
    assert not t2.called, "the roll must not start when the canary failed"
    out = capsys.readouterr().out
    assert "CANARY FAILED" in out
    assert "NOT CONVERGED" in out


@respx.mock
def test_canary_must_be_a_running_tenant(capsys):
    mock_fleet(fleet_response(row("t-1")))
    code = upgrade_drill.main([*ARGS, "--image-tag", "v2", "--canary", "t-nope"])
    assert code == 1
    assert "not a running tenant" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Roll
# ---------------------------------------------------------------------------


@respx.mock
def test_roll_walks_the_rest_of_the_fleet_and_skips_the_converged(capsys):
    mock_redeploy("t-1")
    t2 = mock_redeploy("t-2")
    t3 = mock_redeploy("t-3")
    mock_fleet(
        # Initial listing: the canary already ran, t-3 is somehow already on v2.
        fleet_response(row("t-1", reported=NEW), row("t-2"), row("t-3", reported=NEW)),
        fleet_response(row("t-1", reported=NEW), row("t-2", reported=NEW), row("t-3", reported=NEW)),
        fleet_response(row("t-1", reported=NEW), row("t-2", reported=NEW), row("t-3", reported=NEW)),
    )

    code = upgrade_drill.main([*ARGS, "--image-tag", "v2", "--canary", "t-1", "--roll"])
    assert code == 0
    assert t2.called
    assert not t3.called, "a tenant already on the target image must not be restarted"
    out = capsys.readouterr().out
    assert "already on the target image" in out
    assert "converged 3/3" in out


@respx.mock
def test_roll_aborts_once_max_failures_is_reached(capsys):
    mock_redeploy("t-1")
    mock_redeploy("t-2", httpx.Response(409, json={"detail": "tenant is deleted"}))
    t3 = mock_redeploy("t-3")
    mock_fleet(
        fleet_response(row("t-1", reported=NEW), row("t-2"), row("t-3")),
        fleet_response(row("t-1", reported=NEW), row("t-2"), row("t-3")),
    )

    code = upgrade_drill.main(
        [*ARGS, "--image-tag", "v2", "--canary", "t-1", "--roll", "--max-failures", "1"]
    )
    assert code == 2
    assert not t3.called, "the roll must stop, not keep going through failures"
    out = capsys.readouterr().out
    assert "ABORTED after 1 failure" in out
    assert "1 tenant(s) untouched" in out


@respx.mock
def test_dry_run_issues_no_redeploys(capsys):
    route = mock_redeploy("t-1")
    mock_fleet(
        fleet_response(row("t-1"), row("t-2")),
        fleet_response(row("t-1"), row("t-2")),
    )
    code = upgrade_drill.main([*ARGS, "--image-tag", "v2", "--roll", "--dry-run"])
    assert code == 0
    assert not route.called
    assert "would redeploy" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


@respx.mock
def test_rollback_pushes_through_failures_and_includes_the_canary(capsys):
    t1 = mock_redeploy("t-1", httpx.Response(500, text="railway down"))
    t2 = mock_redeploy("t-2", image_ref=OLD)
    mock_fleet(
        fleet_response(row("t-1", reported=NEW), row("t-2", reported=NEW)),
        fleet_response(row("t-1", reported=NEW), row("t-2", reported=OLD)),
    )

    code = upgrade_drill.main(
        [*ARGS, "--image-tag", "v1", "--canary", "t-1", "--rollback"]
    )
    # One tenant could not be rolled back -- reported, non-zero, but the other
    # tenant was still rolled rather than the whole thing aborting.
    assert code == 1
    assert t1.called and t2.called
    out = capsys.readouterr().out
    assert "FAILED    t-1" in out
    assert "ABORTED" not in out


# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------


def test_requires_something_to_do():
    with pytest.raises(SystemExit):
        upgrade_drill.parse_args(["--image-tag", "v2"])


def test_image_tag_is_required():
    with pytest.raises(SystemExit):
        upgrade_drill.parse_args(["--canary", "t-1"])


@respx.mock
def test_empty_fleet_is_a_clean_no_op(capsys):
    mock_fleet(fleet_response())
    assert upgrade_drill.main([*ARGS, "--image-tag", "v2", "--roll"]) == 0
    assert "nothing to roll" in capsys.readouterr().out
