"""Public (unauthenticated) routes -- currently just the pre-launch waitlist.

The browser never talks to this service directly: apps/web's Caddy reverse-proxies
`/api/*` here over Railway's private network, so requests arrive same-origin from
the marketing page's point of view (no CORS) and this route is only reachable
through the web service plus whatever public domain control-api itself has.

Abuse posture for a public write endpoint, in order of the checks below:
  1. schema hard caps + `extra="forbid"` (schemas.WaitlistRequest),
  2. honeypot field -- filled means bot; accept-and-drop so it learns nothing,
  3. per-IP in-memory rate limit -- single-replica assumption, same as the
     provisioning worker (main._start_worker); Phase 1 can move it to Postgres
     if replicas ever appear.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from control_api.db import get_session
from control_api.models import Waitlist, utcnow
from control_api.schemas import WaitlistRequest, WaitlistResponse

log = logging.getLogger(__name__)

router = APIRouter(tags=["public"])

# -- Rate limiting -----------------------------------------------------------

RATE_LIMIT_MAX = 5  # submissions ...
RATE_LIMIT_WINDOW_SECONDS = 3600.0  # ... per IP per hour

# ip -> [monotonic timestamps of accepted submissions inside the window]
_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    """First X-Forwarded-For hop (Railway/Caddy set it), else the socket peer."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str, now: float | None = None) -> bool:
    """Sliding-window check; records the hit when allowed."""
    now = time.monotonic() if now is None else now
    with _hits_lock:
        window = [t for t in _hits.get(ip, []) if now - t < RATE_LIMIT_WINDOW_SECONDS]
        if len(window) >= RATE_LIMIT_MAX:
            _hits[ip] = window
            return True
        window.append(now)
        _hits[ip] = window
        # Unbounded-growth guard: drop other IPs' fully-expired entries once the
        # map gets big. O(n) but n is bounded by uniques-per-hour, which a
        # waitlist page will not push past thousands.
        if len(_hits) > 10_000:
            for stale_ip in [k for k, v in _hits.items() if all(now - t >= RATE_LIMIT_WINDOW_SECONDS for t in v)]:
                del _hits[stale_ip]
        return False


def _reset_rate_limiter() -> None:
    """Tests only."""
    with _hits_lock:
        _hits.clear()


# -- The route ---------------------------------------------------------------


@router.post("/waitlist", response_model=WaitlistResponse)
def join_waitlist(
    payload: WaitlistRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> WaitlistResponse:
    """Upsert a waitlist signup.

    Repeat submission with the same email UPDATES features/use_case/UTMs rather
    than erroring: "I changed my mind about which boxes to tick" should just
    work, and the page can render the same success state either way.
    """
    # Honeypot: pretend success, store nothing (see module docstring).
    if payload.website:
        log.info("waitlist honeypot tripped")
        return WaitlistResponse()

    if _rate_limited(_client_ip(request)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many signups from this address; try again later",
        )

    features_json = json.dumps(sorted(set(payload.features)))
    row = session.exec(select(Waitlist).where(Waitlist.email == payload.email)).first()
    if row is None:
        row = Waitlist(
            id=uuid.uuid4().hex,
            email=payload.email,
            # v1 auto-confirms -- no Resend flow yet (see models.Waitlist docstring).
            confirmed_at=utcnow(),
        )
    row.features = features_json
    row.use_case = payload.use_case
    row.utm_source = payload.utm_source
    row.utm_medium = payload.utm_medium
    row.utm_campaign = payload.utm_campaign
    row.referrer = payload.referrer
    session.add(row)
    return WaitlistResponse()
