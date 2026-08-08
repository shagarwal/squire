"""Tests for the two operator CLIs in `infra/`.

These are thin control-api clients, so the tests mock control-api itself.
`infra/provision.py` is the entire alpha signup path -- if it prints the wrong
t.me link, alpha onboarding silently breaks.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

import load_bots
import provision

API = "https://control-api.squire.test"


@pytest.fixture(autouse=True)
def _cli_env(monkeypatch):
    monkeypatch.setenv("CONTROL_API_URL", API)
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-internal-token")


# ---------------------------------------------------------------------------
# provision.py
# ---------------------------------------------------------------------------


@respx.mock
def test_provision_creates_polls_and_prints_link(capsys):
    create = respx.post(f"{API}/internal/tenants").mock(
        return_value=httpx.Response(
            201,
            json={
                "tenant_id": "t-1",
                "job_id": "j-1",
                "status": "provisioning",
                "bot_username": "squire_alpha_bot",
                "telegram_link": "https://t.me/squire_alpha_bot",
            },
        )
    )
    # First advance is still mid-flight, second finishes.
    respx.post(f"{API}/internal/provision-jobs/j-1/advance").mock(
        side_effect=[
            httpx.Response(200, json={"job_id": "j-1", "status": "pending", "step": "deploy"}),
            httpx.Response(200, json={"job_id": "j-1", "status": "done", "step": "done"}),
        ]
    )
    respx.get(f"{API}/internal/tenants/t-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "tenant_id": "t-1",
                "status": "running",
                "bot_username": "squire_alpha_bot",
                "telegram_link": "https://t.me/squire_alpha_bot",
            },
        )
    )

    exit_code = provision.main(["--email", "alpha@squire.test", "--poll-interval", "0"])
    assert exit_code == 0

    assert create.calls.last.request.headers["authorization"] == "Bearer test-internal-token"
    assert json.loads(create.calls.last.request.read()) == {"email": "alpha@squire.test"}

    out = capsys.readouterr().out
    assert "https://t.me/squire_alpha_bot" in out
    assert "running" in out


@respx.mock
def test_provision_reports_failure_with_reason(capsys):
    respx.post(f"{API}/internal/tenants").mock(
        return_value=httpx.Response(
            201,
            json={
                "tenant_id": "t-1",
                "job_id": "j-1",
                "status": "provisioning",
                "bot_username": "b",
                "telegram_link": "https://t.me/b",
            },
        )
    )
    respx.post(f"{API}/internal/provision-jobs/j-1/advance").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": "j-1",
                "status": "failed",
                "step": "deploy",
                "last_error": "Railway said no",
            },
        )
    )
    exit_code = provision.main(["--email", "alpha@squire.test", "--poll-interval", "0"])
    assert exit_code == 1
    assert "Railway said no" in capsys.readouterr().out


@respx.mock
def test_provision_times_out_rather_than_hanging(capsys):
    respx.post(f"{API}/internal/tenants").mock(
        return_value=httpx.Response(
            201,
            json={
                "tenant_id": "t-1",
                "job_id": "j-1",
                "status": "provisioning",
                "bot_username": "b",
                "telegram_link": "https://t.me/b",
            },
        )
    )
    respx.post(f"{API}/internal/provision-jobs/j-1/advance").mock(
        return_value=httpx.Response(200, json={"job_id": "j-1", "status": "pending", "step": "deploy"})
    )
    exit_code = provision.main(
        ["--email", "alpha@squire.test", "--poll-interval", "0", "--timeout", "0"]
    )
    assert exit_code == 2
    assert "timed out" in capsys.readouterr().out.lower()


@respx.mock
def test_provision_surfaces_empty_bot_pool(capsys):
    respx.post(f"{API}/internal/tenants").mock(
        return_value=httpx.Response(409, json={"detail": "no bots available in the pool"})
    )
    exit_code = provision.main(["--email", "alpha@squire.test", "--poll-interval", "0"])
    assert exit_code == 1
    assert "no bots available" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# load_bots.py
# ---------------------------------------------------------------------------


def test_read_tokens_skips_blanks_and_comments(tmp_path):
    f = tmp_path / "bots.txt"
    f.write_text(
        "\n".join(
            [
                "# BotFather batch 2026-08-07",
                "111:AAA",
                "",
                "   222:BBB   ",
                "# trailing comment",
            ]
        )
    )
    assert load_bots.read_tokens(str(f)) == ["111:AAA", "222:BBB"]


def test_read_tokens_rejects_malformed_tokens(tmp_path):
    f = tmp_path / "bots.txt"
    f.write_text("not-a-token\n")
    with pytest.raises(ValueError, match="not-a-token"):
        load_bots.read_tokens(str(f))


@respx.mock
def test_load_bots_posts_tokens_and_reports(tmp_path, capsys):
    f = tmp_path / "bots.txt"
    f.write_text("111:AAA\n222:BBB\n")
    route = respx.post(f"{API}/internal/bots").mock(
        return_value=httpx.Response(
            200,
            json={
                "registered": [{"bot_id": 111, "username": "one_bot"}],
                "skipped": [{"bot_id": 222, "reason": "already_registered"}],
                "failed": [],
            },
        )
    )
    exit_code = load_bots.main([str(f)])
    assert exit_code == 0
    assert json.loads(route.calls.last.request.read()) == {"tokens": ["111:AAA", "222:BBB"]}

    out = capsys.readouterr().out
    assert "one_bot" in out
    assert "already_registered" in out
    # Tokens are secrets -- never echo them to a terminal / CI log.
    assert "111:AAA" not in out


@respx.mock
def test_load_bots_exits_nonzero_when_a_token_is_rejected(tmp_path):
    f = tmp_path / "bots.txt"
    f.write_text("111:AAA\n")
    respx.post(f"{API}/internal/bots").mock(
        return_value=httpx.Response(
            200,
            json={
                "registered": [],
                "skipped": [],
                "failed": [{"bot_id": 111, "error": "Unauthorized"}],
            },
        )
    )
    assert load_bots.main([str(f)]) == 1
