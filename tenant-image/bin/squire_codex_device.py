#!/opt/squire/venv/bin/python
"""Codex device-code sign-in — the 4 proprietary calls, per the 2026-08-14 spike.

UX-equivalent to RFC 8628 but NOT RFC 8628: OpenAI-proprietary endpoints,
server-generated PKCE, and 403/404-as-pending. Every constant below comes from
the spike table (openai/codex, codex-rs/login) and must not be "corrected"
toward the RFC.

The transport is injectable ((url, body_bytes, headers) -> (status, body_bytes))
so the whole flow tests without sockets. NEVER log tokens, codes, or bodies.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


class DeviceLoginNotEnabled(Exception):
    """First usercode call 404'd: 'device code authorization' is off for this
    ChatGPT account (beta, off by default). Onboarding must walk the user
    through enabling it in ChatGPT Settings -> Security, then retry."""


class DeviceFlowError(Exception):
    """Terminal failure (denied, expired, malformed response)."""


@dataclass(frozen=True)
class DeviceFlowConfig:
    client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    issuer: str = field(
        default_factory=lambda: os.environ.get("SQUIRE_CODEX_ISSUER", "https://auth.openai.com")
    )
    usercode_path: str = "/api/accounts/deviceauth/usercode"
    token_path: str = "/api/accounts/deviceauth/token"
    oauth_token_path: str = "/oauth/token"
    redirect_uri: str = "https://auth.openai.com/deviceauth/callback"
    verification_url: str = "https://auth.openai.com/codex/device"
    min_poll_seconds: int = 5
    code_lifetime_seconds: int = 900  # user code expires in 15 minutes


def _default_transport(url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


def request_user_code(cfg: DeviceFlowConfig, transport=_default_transport) -> dict:
    """Call 1: mint the device code. 404 => device login not enabled (beta)."""
    status, body = transport(
        cfg.issuer + cfg.usercode_path,
        json.dumps({"client_id": cfg.client_id}).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    if status == 404:
        raise DeviceLoginNotEnabled()
    if status != 200:
        raise DeviceFlowError(f"usercode endpoint answered HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise DeviceFlowError("usercode endpoint returned non-JSON") from exc
    if not data.get("device_auth_id") or not data.get("user_code"):
        raise DeviceFlowError("usercode response missing device_auth_id/user_code")
    return data


def poll_interval_seconds(usercode_response: dict) -> int:
    """Server sends interval as STRING seconds; serde-defaults to 0, which
    would busy-poll — clamp to >= 5 (spike)."""
    try:
        interval = int(str(usercode_response.get("interval", "5")))
    except ValueError:
        interval = 5
    return max(5, interval)


def poll_once(cfg: DeviceFlowConfig, device_auth_id: str, user_code: str,
              transport=_default_transport) -> dict | None:
    """Call 2: poll for approval. 403/404 mean PENDING (proprietary quirk);
    200 returns the server-generated PKCE triple."""
    status, body = transport(
        cfg.issuer + cfg.token_path,
        json.dumps({"device_auth_id": device_auth_id, "user_code": user_code}).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    if status in (403, 404):
        return None
    if status != 200:
        raise DeviceFlowError(f"device poll answered HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise DeviceFlowError("device poll returned non-JSON") from exc
    if not data.get("authorization_code"):
        raise DeviceFlowError("grant response missing authorization_code")
    return data


def exchange_code(cfg: DeviceFlowConfig, grant: dict, transport=_default_transport) -> dict:
    """Call 3: authorization_code -> tokens. FORM-encoded, spike-exact fields,
    including the server-generated code_verifier."""
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": grant["authorization_code"],
        "redirect_uri": cfg.redirect_uri,
        "client_id": cfg.client_id,
        "code_verifier": grant["code_verifier"],
    }).encode("utf-8")
    status, body = transport(
        cfg.issuer + cfg.oauth_token_path, form,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    if status != 200:
        raise DeviceFlowError(f"token exchange answered HTTP {status}")
    tokens = json.loads(body)
    if not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise DeviceFlowError("token exchange missing access/refresh token")
    return tokens


def refresh_tokens(cfg: DeviceFlowConfig, refresh_token: str,
                   transport=_default_transport) -> dict:
    """Call 4: refresh. JSON body (unlike the exchange). Refresh tokens are
    one-time-use/rotating: the CALLER must persist the new pair atomically
    BEFORE using the new access token (see squire-llm-connect refresh)."""
    status, body = transport(
        cfg.issuer + cfg.oauth_token_path,
        json.dumps({"client_id": cfg.client_id, "grant_type": "refresh_token",
                    "refresh_token": refresh_token}).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    if status in (400, 401):
        # Reused/expired/revoked refresh token: terminal — re-link in chat.
        raise DeviceFlowError("refresh token rejected (re-link required)")
    if status != 200:
        raise DeviceFlowError(f"refresh answered HTTP {status}")
    tokens = json.loads(body)
    if not tokens.get("access_token"):
        raise DeviceFlowError("refresh response missing access_token")
    return tokens


def jwt_claims(id_token: str) -> dict:
    """Decode the id_token payload WITHOUT signature verification — it arrived
    over TLS directly from the issuer and is used only for plan gating and the
    account id, never as an authentication decision."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def plan_allowed(claims: dict) -> bool:
    """Reject only an explicit free plan (spike: claim chatgpt_plan_type,
    values free/plus/pro/business). Missing claim => allow with the provider
    as the final arbiter — hard-failing on an absent beta claim would strand
    paying users."""
    return str(claims.get("chatgpt_plan_type", "")).lower() != "free"


def iso_utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def build_auth_json(tokens: dict) -> dict:
    """The $CODEX_HOME/auth.json shape from the spike, stored at
    $HERMES_HOME/auth.json (already a sealed name — secrets-sync encrypts it
    onto the volume with AAD 'auth.json')."""
    claims = jwt_claims(tokens.get("id_token", ""))
    account_id = str(
        claims.get("chatgpt_account_id")
        or (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id", "")
    )
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": tokens.get("id_token", ""),
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
            "account_id": account_id,
        },
        "last_refresh": iso_utc(time.time()),
    }


def needs_refresh(auth: dict, now: float) -> bool:
    """Spike policy: refresh when the access token expires within 5 minutes or
    last_refresh is more than 8 days old."""
    exp = auth.get("access_token_exp")
    if exp is None:
        exp = jwt_claims((auth.get("tokens") or {}).get("id_token", "")).get("exp")
    if isinstance(exp, (int, float)) and exp - now < 300:
        return True
    last = auth.get("last_refresh", "")
    try:
        parsed = time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (ValueError, TypeError):
        return True  # unknown age: refreshing is the safe default
    return (now - parsed) > 8 * 86400
