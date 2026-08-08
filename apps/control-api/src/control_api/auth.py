"""Internal service-to-service authentication.

One shared bearer token (`INTERNAL_API_TOKEN`) for every internal caller: ingress,
tenant runtimes, and the operator CLIs. Anything else is 401.

This is deliberately crude for Phase 0. It is adequate because every caller lives on
Railway's private network, and it keeps the ingress hot path free of any per-request
crypto. Phase 1 (Gate G2) is where per-caller credentials belong.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from control_api.config import get_settings

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid or missing internal API token",
    headers={"WWW-Authenticate": "Bearer"},
)


def require_internal_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency guarding every `/internal/*` route."""
    if not authorization:
        raise _UNAUTHORIZED
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _UNAUTHORIZED
    expected = get_settings().internal_api_token
    # Constant-time compare: the token is a long-lived shared secret.
    if not expected or not hmac.compare_digest(token, expected):
        raise _UNAUTHORIZED
