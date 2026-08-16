#!/usr/bin/env python3
"""Codex device-flow + squire-llm-connect CLI tests — runs WITHOUT Docker.

The four device-flow HTTP calls are exercised in-process against an injected
transport (no sockets), then the CLI is run as a subprocess against a local
fake issuer for the output-hygiene and 404-not-enabled contracts.

Usage: python3 tenant-image/tests/test_llm_connect_cli.py
Exit:  0 all assertions pass · 1 otherwise
"""
import base64
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IMAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(IMAGE_ROOT / "bin"))

import squire_codex_device as dev  # noqa: E402

CLI = str(IMAGE_ROOT / "bin" / "squire-llm-connect")

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def fake_jwt(claims: dict) -> str:
    def seg(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=")
        return raw.decode()
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


print("== spike constants are pinned ==")
cfg = dev.DeviceFlowConfig()
check("client_id from the spike", cfg.client_id == "app_EMoamEEZ73f0CkXaXp7hrann", cfg.client_id)
check("issuer default", cfg.issuer == "https://auth.openai.com", cfg.issuer)
check("verification URL", cfg.verification_url == "https://auth.openai.com/codex/device")
check("redirect_uri for the exchange",
      cfg.redirect_uri == "https://auth.openai.com/deviceauth/callback", cfg.redirect_uri)

print("== the four calls, against an injected transport ==")
calls = []


def transport_ok(url, body, headers):
    calls.append({"url": url, "body": body, "headers": headers})
    if url.endswith("/api/accounts/deviceauth/usercode"):
        return 200, json.dumps({"device_auth_id": "da-1", "user_code": "ABCD-EFGHI",
                                "interval": "0"}).encode()
    if url.endswith("/api/accounts/deviceauth/token"):
        # first poll pending (403), second grants
        n = sum(1 for c in calls if c["url"].endswith("/deviceauth/token"))
        if n == 1:
            return 403, b"{}"
        return 200, json.dumps({"authorization_code": "ac-1",
                                "code_challenge": "cc-1", "code_verifier": "cv-1"}).encode()
    if url.endswith("/oauth/token"):
        return 200, json.dumps({
            "id_token": fake_jwt({"chatgpt_plan_type": "plus", "chatgpt_account_id": "acct-9",
                                  "exp": int(time.time()) + 3600}),
            "access_token": "at-secret-1", "refresh_token": "rt-secret-1",
        }).encode()
    raise AssertionError(f"unexpected url {url}")


start = dev.request_user_code(cfg, transport=transport_ok)
check("usercode call returns code + id", start == {"device_auth_id": "da-1",
      "user_code": "ABCD-EFGHI", "interval": "0"}, start)
check("usercode call sent the client_id",
      json.loads(calls[0]["body"]) == {"client_id": cfg.client_id}, calls[0])

check("interval clamps to >=5 (serde default 0 busy-polls)",
      dev.poll_interval_seconds(start) == 5, dev.poll_interval_seconds(start))
check("interval respects server value above the floor",
      dev.poll_interval_seconds({"interval": "9"}) == 9)

pending = dev.poll_once(cfg, "da-1", "ABCD-EFGHI", transport=transport_ok)
check("403 reads as pending (None)", pending is None, pending)
grant = dev.poll_once(cfg, "da-1", "ABCD-EFGHI", transport=transport_ok)
check("grant returns the server PKCE triple",
      grant == {"authorization_code": "ac-1", "code_challenge": "cc-1",
                "code_verifier": "cv-1"}, grant)

tokens = dev.exchange_code(cfg, grant, transport=transport_ok)
check("exchange returned tokens", tokens.get("access_token") == "at-secret-1", tokens)
exchange = calls[-1]
check("exchange is a FORM post with the spike's exact fields",
      exchange["headers"].get("Content-Type") == "application/x-www-form-urlencoded"
      and b"grant_type=authorization_code" in exchange["body"]
      and b"code=ac-1" in exchange["body"]
      and b"code_verifier=cv-1" in exchange["body"]
      and b"redirect_uri=" in exchange["body"]
      and cfg.client_id.encode() in exchange["body"], exchange)

claims = dev.jwt_claims(tokens["id_token"])
check("id_token claims decode without verification", claims.get("chatgpt_plan_type") == "plus")
check("plan gate: plus passes", dev.plan_allowed(claims) is True)
check("plan gate: free is rejected", dev.plan_allowed({"chatgpt_plan_type": "free"}) is False)
check("plan gate: unknown/missing does not hard-fail",
      dev.plan_allowed({}) is True)

auth = dev.build_auth_json(tokens)
check("auth.json has the spike's shape",
      auth["auth_mode"] == "chatgpt"
      and set(auth["tokens"]) == {"id_token", "access_token", "refresh_token", "account_id"}
      and auth["tokens"]["account_id"] == "acct-9"
      and isinstance(auth["last_refresh"], str), auth)

print("== refresh policy ==")
fresh = dict(auth)
check("fresh tokens do not need refresh", dev.needs_refresh(fresh, now=time.time()) is False)
soon = dev.build_auth_json({**tokens, "id_token": tokens["id_token"]})
soon["access_token_exp"] = time.time() + 60  # within the 5-minute window
check("exp within 5 min triggers refresh", dev.needs_refresh(soon, now=time.time()) is True)
stale = dict(auth)
stale["last_refresh"] = dev.iso_utc(time.time() - 9 * 86400)  # > 8 days
check("last_refresh older than 8 days triggers refresh",
      dev.needs_refresh(stale, now=time.time()) is True)


def transport_refresh(url, body, headers):
    calls.append({"url": url, "body": body, "headers": headers})
    return 200, json.dumps({"id_token": tokens["id_token"],
                            "access_token": "at-secret-2",
                            "refresh_token": "rt-secret-2"}).encode()


new_tokens = dev.refresh_tokens(cfg, "rt-secret-1", transport=transport_refresh)
refresh_call = calls[-1]
check("refresh is a JSON post with grant_type refresh_token",
      refresh_call["headers"].get("Content-Type") == "application/json"
      and json.loads(refresh_call["body"]) == {
          "client_id": cfg.client_id, "grant_type": "refresh_token",
          "refresh_token": "rt-secret-1"}, refresh_call)
check("refresh returns rotated tokens", new_tokens["refresh_token"] == "rt-secret-2")

print("== 404 => device login not enabled (the beta caveat) ==")


def transport_404(url, body, headers):
    return 404, b"not found"


try:
    dev.request_user_code(cfg, transport=transport_404)
    check("404 raises DeviceLoginNotEnabled", False, "no exception")
except dev.DeviceLoginNotEnabled:
    check("404 raises DeviceLoginNotEnabled", True)

print("== squire-llm-connect CLI ==")
ISSUER_PORT = 18083
issuer_mode = {"enabled": True}


class FakeIssuer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path == "/api/accounts/deviceauth/usercode":
            if not issuer_mode["enabled"]:
                self._send(404, {})
                return
            self._send(200, {"device_auth_id": "da-cli", "user_code": "WXYZ-ABCDE",
                             "interval": "0"})
        elif self.path == "/api/accounts/deviceauth/token":
            self._send(200, {"authorization_code": "ac-cli",
                             "code_challenge": "cc", "code_verifier": "cv"})
        elif self.path == "/oauth/token":
            self._send(200, {"id_token": fake_jwt({"chatgpt_plan_type": "pro",
                                                   "chatgpt_account_id": "acct-cli",
                                                   "exp": int(time.time()) + 3600}),
                             "access_token": "at-cli-secret",
                             "refresh_token": "rt-cli-secret"})
        else:
            self._send(500, {"error": self.path})


issuer = ThreadingHTTPServer(("127.0.0.1", ISSUER_PORT), FakeIssuer)
issuer.daemon_threads = True
threading.Thread(target=issuer.serve_forever, daemon=True).start()

home = tempfile.mkdtemp()
os.makedirs(os.path.join(home, ".squire"), exist_ok=True)
# The real container's $HERMES_HOME/.env is a SYMLINK the entrypoint points
# into tmpfs, so credential writes follow the link off the persistent volume.
# A security fix landed in Task 4 (squire_connect._assert_tmpfs_target) that
# refuses to write credentials to a plain .env resolving inside $HERMES_HOME.
# Reproduce the tmpfs-symlink shape here (mirrors test_connect.py) and assert
# against the tmpfs target file.
env_tmpfs = tempfile.mkdtemp()
open(os.path.join(env_tmpfs, ".env"), "w").close()
os.symlink(os.path.join(env_tmpfs, ".env"), os.path.join(home, ".env"))
# auth.json (the ChatGPT OAuth tokens) is ALSO a tmpfs symlink in the real
# container -- store_chatgpt_tokens now applies the same fail-closed
# _assert_tmpfs_target guard to it, so the success path must present the
# tmpfs-symlink shape or the write is (correctly) refused. The negative test
# below proves the refusal when auth.json is a plain file on the volume.
os.symlink(os.path.join(env_tmpfs, "auth.json"), os.path.join(home, "auth.json"))
CLI_ENV = dict(os.environ)
CLI_ENV.update({
    "HERMES_HOME": home,
    "SQUIRE_STATE_DIR": os.path.join(home, ".squire"),
    "SQUIRE_CODEX_ISSUER": f"http://127.0.0.1:{ISSUER_PORT}",
    "SQUIRE_PUBLIC_DOMAIN": "tenant-cli.up.railway.app",
    "SQUIRE_SUPERVISORCTL": "/bin/true",   # pipeline (Task 6) must not touch a real supervisor
    "CONTROL_API_URL": "",
    "TENANT_ID": "t-cli-test",
})


def cli(*args, env=None):
    out = subprocess.run([sys.executable, CLI, *args], env=env or CLI_ENV,
                         capture_output=True, text=True, timeout=60)
    return out.returncode, out.stdout.strip(), out.stderr


rc, out, err = cli("mint-link")
check("mint-link exits 0", rc == 0, (rc, err))
link = json.loads(out)
check("mint-link prints a JSON url on the public domain",
      link.get("url", "").startswith("https://tenant-cli.up.railway.app/connect/"), out)
check("mint-link states the expiry", link.get("expires_in_minutes") == 15, out)

no_domain_env = dict(CLI_ENV)
no_domain_env.pop("SQUIRE_PUBLIC_DOMAIN", None)
no_domain_env.pop("RAILWAY_PUBLIC_DOMAIN", None)
rc, out, _ = cli("mint-link", env=no_domain_env)
check("mint-link without a public domain fails with a clear state",
      rc != 0 and json.loads(out).get("state") == "no_public_domain", (rc, out))

rc, out, _ = cli("status")
check("status with no flow yet", json.loads(out).get("state") == "none", out)

# --- the 404-not-enabled onboarding branch ------------------------------
issuer_mode["enabled"] = False
rc, out, _ = cli("start", "openai-device")
check("device login disabled: exit 3", rc == 3, (rc, out))
info = json.loads(out)
check("disabled: state says not_enabled", info.get("state") == "not_enabled", out)
check("disabled: instructions mention ChatGPT Settings -> Security",
      "Security" in info.get("instructions", "") and "device code" in info.get("instructions", "").lower(), out)
issuer_mode["enabled"] = True

# --- the full grant ------------------------------------------------------
rc, out, _ = cli("start", "openai-device")
check("start exits 0", rc == 0, (rc, out))
started = json.loads(out)
check("start prints ONLY the code and URL (chat-safe)",
      started.get("user_code") == "WXYZ-ABCDE"
      and started.get("verification_url") == "https://auth.openai.com/codex/device"
      and "at-cli-secret" not in out and "rt-cli-secret" not in out, out)

deadline = time.time() + 30
final = {}
while time.time() < deadline:
    _, sout, _ = cli("status")
    final = json.loads(sout)
    if final.get("state") in ("connected", "denied", "error", "timed_out"):
        break
    time.sleep(0.5)
check("background poll reached connected", final.get("state") == "connected", final)
check("status names the provider, not the tokens",
      final.get("provider") == "chatgpt" and "at-cli-secret" not in json.dumps(final), final)

auth_path = os.path.join(home, "auth.json")
check("auth.json written", os.path.exists(auth_path), auth_path)
auth = json.loads(open(auth_path).read())
check("auth.json carries the granted tokens",
      auth["tokens"]["access_token"] == "at-cli-secret"
      and auth["tokens"]["account_id"] == "acct-cli"
      and auth["auth_mode"] == "chatgpt", auth)
env_text = open(os.path.join(env_tmpfs, ".env")).read()  # follow the symlink to tmpfs
check("gateway env got the access token + codex base url",
      "OPENAI_API_KEY=at-cli-secret" in env_text
      and "OPENAI_BASE_URL=https://chatgpt.com/backend-api/codex" in env_text, env_text)

# --- fail-closed: auth.json write is refused on a non-tmpfs target -------
# If the entrypoint's auth.json symlink were ever missing, auth.json would
# resolve to a PLAIN file inside $HERMES_HOME (the persistent volume) and the
# OAuth access_token would land there as plaintext. store_chatgpt_tokens now
# guards the auth.json write with the same _assert_tmpfs_target check the .env
# write already uses, so the whole device flow must fail cleanly (error state,
# nothing stored) instead of writing the token to disk or crashing.
print("== auth.json write fails closed on a non-tmpfs (volume) target ==")
home2 = tempfile.mkdtemp()
os.makedirs(os.path.join(home2, ".squire"), exist_ok=True)
# auth.json is a PLAIN file resolving inside HERMES_HOME -- the forbidden shape.
plain_auth = os.path.join(home2, "auth.json")
open(plain_auth, "w").close()
# .env is a proper tmpfs symlink so ONLY the auth.json guard is exercised: any
# failure must come from auth.json, not from the (already-guarded) .env write.
env_tmpfs2 = tempfile.mkdtemp()
open(os.path.join(env_tmpfs2, ".env"), "w").close()
os.symlink(os.path.join(env_tmpfs2, ".env"), os.path.join(home2, ".env"))
CLI_ENV2 = dict(CLI_ENV)
CLI_ENV2.update({
    "HERMES_HOME": home2,
    "SQUIRE_STATE_DIR": os.path.join(home2, ".squire"),
})

rc, out, _ = cli("start", "openai-device", env=CLI_ENV2)
check("start exits 0 (device flow begins)", rc == 0, (rc, out))
deadline = time.time() + 30
final2 = {}
while time.time() < deadline:
    _, sout, _ = cli("status", env=CLI_ENV2)
    final2 = json.loads(sout)
    if final2.get("state") in ("connected", "denied", "error", "timed_out"):
        break
    time.sleep(0.5)
check("flow reports failure, NOT connected, when auth.json is non-tmpfs",
      final2.get("state") == "error", final2)
# The plaintext credential must NOT have landed on the volume.
plain_contents = open(plain_auth).read()
check("access token was NOT written as plaintext to the volume auth.json",
      "at-cli-secret" not in plain_contents, repr(plain_contents))
# And the guard's own error message (which names a path) must not leak the key.
check("failure detail carries no token material",
      "at-cli-secret" not in json.dumps(final2), final2)

print("== the real transport sends an explicit User-Agent (Cloudflare 530 guard) ==")
# auth.openai.com is behind Cloudflare, which rejects urllib's default
# "Python-urllib/3.x" UA with HTTP 530 — an edge error that reads exactly like
# an OpenAI outage (misdiagnosed as one live on 2026-08-16). The injected
# transport used by every test above cannot catch this, because the header is
# set inside _default_transport, so pin it against a real socket here.
ua_seen = []


class UACapture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def do_POST(self):
        ua_seen.append(self.headers.get("User-Agent"))
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


UA_PORT = 18087
ua_server = ThreadingHTTPServer(("127.0.0.1", UA_PORT), UACapture)
ua_server.daemon_threads = True
threading.Thread(target=ua_server.serve_forever, daemon=True).start()
try:
    dev._default_transport(
        f"http://127.0.0.1:{UA_PORT}/usercode", b"{}", {"Content-Type": "application/json"}
    )
    check("the real transport sent a User-Agent", bool(ua_seen and ua_seen[0]), ua_seen)
    check(
        "it is NOT urllib's default (Cloudflare 530s that one)",
        bool(ua_seen) and "Python-urllib" not in (ua_seen[0] or ""),
        ua_seen,
    )
    check(
        "it is the pinned squire UA",
        bool(ua_seen) and ua_seen[0] == dev.USER_AGENT,
        (ua_seen, dev.USER_AGENT),
    )
    # Caller-supplied headers must survive alongside the injected UA.
    ua_seen.clear()
    dev._default_transport(
        f"http://127.0.0.1:{UA_PORT}/usercode", b"{}", {"User-Agent": "caller-override/9"}
    )
    check("an explicit caller UA still wins", ua_seen == ["caller-override/9"], ua_seen)
finally:
    ua_server.shutdown()
    ua_server.server_close()

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL DEVICE-FLOW TESTS PASS")
