"""POST /waitlist -- the public pre-launch signup route (routers/public.py)."""

from __future__ import annotations

import json

import pytest
from sqlmodel import select

from control_api.models import Waitlist
from control_api.routers import public


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    """The limiter is module-level state; isolate every test."""
    public._reset_rate_limiter()
    yield
    public._reset_rate_limiter()


def _signup(**overrides) -> dict:
    payload = {
        "email": "Ada@Example.com",
        "features": ["whatsapp", "daily_briefings"],
        "use_case": "book my travel and triage my inbox",
        "utm_source": "reddit",
        "utm_medium": "cpc",
        "utm_campaign": "byo-subscription",
        "referrer": "https://reddit.com/r/ClaudeAI",
    }
    payload.update(overrides)
    return payload


def _rows(session) -> list[Waitlist]:
    return list(session.exec(select(Waitlist)).all())


def test_signup_persists_normalised_email_features_and_utms(client, session):
    resp = client.post("/waitlist", json=_signup())
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    (row,) = _rows(session)
    assert row.email == "ada@example.com"  # lowercased
    assert json.loads(row.features) == ["daily_briefings", "whatsapp"]  # sorted set
    assert row.use_case == "book my travel and triage my inbox"
    assert (row.utm_source, row.utm_medium, row.utm_campaign) == ("reddit", "cpc", "byo-subscription")
    assert row.confirmed_at is not None  # v1 auto-confirms (no Resend flow yet)
    assert row.confirm_token is None


def test_signup_requires_no_auth(client):
    # The whole point of routers/public.py: no Authorization header anywhere.
    assert client.post("/waitlist", json={"email": "a@b.co"}).status_code == 200


def test_repeat_email_upserts_instead_of_erroring(client, session):
    client.post("/waitlist", json=_signup())
    resp = client.post("/waitlist", json=_signup(features=["telegram"], utm_source="google"))
    assert resp.status_code == 200

    (row,) = _rows(session)  # still one row
    assert json.loads(row.features) == ["telegram"]
    assert row.utm_source == "google"


def test_honeypot_returns_ok_but_stores_nothing(client, session):
    resp = client.post("/waitlist", json=_signup(website="https://spam.example"))
    assert resp.status_code == 200  # a bot must not learn it was caught
    assert _rows(session) == []


def test_rate_limit_kicks_in_at_six_from_one_ip(client, session):
    for i in range(public.RATE_LIMIT_MAX):
        assert client.post("/waitlist", json=_signup(email=f"u{i}@example.com")).status_code == 200
    resp = client.post("/waitlist", json=_signup(email="straw@example.com"))
    assert resp.status_code == 429
    assert len(_rows(session)) == public.RATE_LIMIT_MAX


@pytest.mark.parametrize(
    "email",
    ["not-an-email", "a@b", "@example.com", "spaces in@example.com", "a@ex ample.com"],
)
def test_bad_email_is_rejected(client, email):
    assert client.post("/waitlist", json={"email": email}).status_code == 422


def test_unknown_feature_is_rejected(client):
    # The closed Literal set is what makes the `features` column safe -- a value
    # outside schemas.WAITLIST_FEATURES must never reach the DB.
    resp = client.post("/waitlist", json=_signup(features=["telepathy"]))
    assert resp.status_code == 422


def test_unexpected_field_is_rejected(client):
    assert client.post("/waitlist", json=_signup(admin=True)).status_code == 422


def test_use_case_over_cap_is_rejected(client):
    assert client.post("/waitlist", json=_signup(use_case="x" * 501)).status_code == 422


def test_rate_limit_window_slides(monkeypatch):
    # Pure limiter math, no HTTP: the 6th hit inside the window is refused, the
    # same hit one window later is allowed again.
    now = 1000.0
    for _ in range(public.RATE_LIMIT_MAX):
        assert public._rate_limited("1.2.3.4", now=now) is False
    assert public._rate_limited("1.2.3.4", now=now) is True
    assert public._rate_limited("1.2.3.4", now=now + public.RATE_LIMIT_WINDOW_SECONDS + 1) is False
