#!/usr/bin/env python3
"""Functional tests for the 1C credential-connect flow — runs WITHOUT Docker.

Covers, in order: the nonce store (single-use, 15-min TTL, constant-time,
atomic 0600), the /connect page served by the real shim (GET + POST against
faked provider validation endpoints), and the connected pipeline's ordering.

Usage: python3 tenant-image/tests/test_connect.py
Exit:  0 all assertions pass · 1 otherwise
"""
import json
import os
import pathlib
import stat
import sys
import tempfile
import time

IMAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(IMAGE_ROOT / "bin"))

import squire_connect  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


print("== nonce store ==")
state_dir = tempfile.mkdtemp()

nonce = squire_connect.mint_nonce(state_dir)
check("mint returns a urlsafe nonce of useful length", isinstance(nonce, str) and len(nonce) >= 40, nonce)

nonce_dir = pathlib.Path(state_dir) / "connect-nonces"
files = list(nonce_dir.glob("*.json"))
check("mint persisted exactly one nonce file", len(files) == 1, files)
if files:
    mode = stat.S_IMODE(files[0].stat().st_mode)
    check("nonce file is 0600", mode == 0o600, oct(mode))
    on_disk = json.loads(files[0].read_text())
    check("file holds the nonce and a created stamp",
          on_disk.get("nonce") == nonce and isinstance(on_disk.get("created"), float), on_disk)

check("a fresh nonce is found", squire_connect.find_nonce(state_dir, nonce) is not None)
check("a wrong nonce is not found", squire_connect.find_nonce(state_dir, "x" * 43) is None)
check("empty candidate is not found", squire_connect.find_nonce(state_dir, "") is None)

check("consume succeeds once", squire_connect.consume_nonce(state_dir, nonce) is True)
check("a consumed nonce is no longer found", squire_connect.find_nonce(state_dir, nonce) is None)
check("consume is not repeatable", squire_connect.consume_nonce(state_dir, nonce) is False)
check("the used marker file remains for audit",
      len(list(nonce_dir.glob("*.used"))) == 1, list(nonce_dir.iterdir()))

# Expiry: rewrite a minted nonce's created stamp into the past.
old = squire_connect.mint_nonce(state_dir)
old_file = next(p for p in nonce_dir.glob("*.json")
                if json.loads(p.read_text())["nonce"] == old)
payload = json.loads(old_file.read_text())
payload["created"] = time.time() - squire_connect.NONCE_TTL_SECONDS - 1
old_file.write_text(json.dumps(payload))
check("an expired nonce is not found", squire_connect.find_nonce(state_dir, old) is None)
check("an expired nonce cannot be consumed", squire_connect.consume_nonce(state_dir, old) is False)

# Two concurrent links may be outstanding; each is independently single-use.
a = squire_connect.mint_nonce(state_dir)
b = squire_connect.mint_nonce(state_dir)
check("two outstanding nonces coexist",
      squire_connect.find_nonce(state_dir, a) is not None
      and squire_connect.find_nonce(state_dir, b) is not None)
squire_connect.consume_nonce(state_dir, a)
check("consuming one leaves the other valid", squire_connect.find_nonce(state_dir, b) is not None)

print("== GET /connect/<nonce> through the real shim ==")
import http.client      # noqa: E402
import subprocess       # noqa: E402
import urllib.request   # noqa: E402

SHIM = str(IMAGE_ROOT / "bin" / "squire-webhook-shim.py")
SHIM_PORT = 18081
PUB = "tenant-test.up.railway.app"


def shim_get(path, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", SHIM_PORT, timeout=10)
    headers = {"Host": host} if host else {}
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    ctype = resp.getheader("Content-Type") or ""
    conn.close()
    return resp.status, ctype, body


def start_shim(home):
    env = dict(os.environ)
    env.update({
        "PORT": str(SHIM_PORT),
        "HERMES_HOME": home,
        "SQUIRE_STATE_DIR": os.path.join(home, ".squire"),
        "SQUIRE_PUBLIC_DOMAIN": PUB,
        "TELEGRAM_WEBHOOK_SECRET": "s3cr3t",
        "TENANT_ID": "t-connect-test",
    })
    proc = subprocess.Popen([sys.executable, SHIM], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{SHIM_PORT}/health", timeout=2)
            return proc
        except Exception:
            time.sleep(0.25)
    raise AssertionError("shim never became ready")


home = tempfile.mkdtemp()
page_state = os.path.join(home, ".squire")
live = squire_connect.mint_nonce(page_state)
proc = start_shim(home)
try:
    status, ctype, body = shim_get(f"/connect/{live}", host=PUB)
    check("live nonce -> 200 HTML page", status == 200 and "text/html" in ctype, (status, ctype))
    check("page offers the OpenAI key path", "OpenAI" in body, body[:400])
    check("page offers the Anthropic key path", "Anthropic" in body, body[:400])
    check("page never offers a Claude subscription path",
          "subscription" not in body.lower() and "setup-token" not in body.lower(), body[:400])
    check("page posts back to the same nonce", f"/connect/{live}" in body, body[:400])
    check("page is self-contained (no external assets)",
          "http://" not in body and "https://" not in body
          and "<script src" not in body and "<link" not in body, body[:400])

    status, _, body = shim_get("/connect/definitely-not-a-nonce", host=PUB)
    check("invalid nonce -> 200 friendly page, not an error dump",
          status == 200 and "ask your" in body.lower(), (status, body[:300]))
    check("invalid page mentions a fresh link", "fresh link" in body.lower(), body[:300])

    # GET must NOT consume — the user may reload before submitting.
    status, _, _ = shim_get(f"/connect/{live}", host=PUB)
    check("reloading the page keeps the nonce live", status == 200
          and squire_connect.find_nonce(page_state, live) is not None, status)

    # The page is reachable from the private side too (wake path goes via edge,
    # but nothing about the page should REQUIRE the public Host).
    status, _, _ = shim_get(f"/connect/{live}")
    check("connect page also answers without a public Host", status == 200, status)
finally:
    proc.terminate()
    proc.wait(timeout=10)
    out = proc.stdout.read() if proc.stdout else ""
    check("nonce never appears in shim logs", live not in out, out[-300:])

print("== POST /connect/<nonce>: validate -> store -> consume ==")
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
import threading            # noqa: E402
import urllib.parse         # noqa: E402

FAKE_PROVIDER_PORT = 18082
provider_calls = []


class FakeProvider(BaseHTTPRequestHandler):
    """Answers both providers' models-list validation endpoints."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def do_GET(self):
        provider_calls.append({
            "path": self.path,
            "bearer": self.headers.get("Authorization"),
            "x_api_key": self.headers.get("x-api-key"),
            "version": self.headers.get("anthropic-version"),
        })
        # Finding 3: /redirect-src issues a cross-path 302 to /redirect-dst.
        # A validation that follows it would re-send the Authorization /
        # x-api-key header to the target — the leak we are guarding against.
        # /redirect-dst records any call so the test can assert it is NEVER hit.
        if self.path.startswith("/redirect-src"):
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{FAKE_PROVIDER_PORT}/redirect-dst",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # /openai/ok and /anthropic/ok accept; /openai/bad rejects with 401.
        status = 401 if "/bad" in self.path else 200
        body = b'{"data": []}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


fake_provider = ThreadingHTTPServer(("127.0.0.1", FAKE_PROVIDER_PORT), FakeProvider)
fake_provider.daemon_threads = True
threading.Thread(target=fake_provider.serve_forever, daemon=True).start()


def shim_post(nonce, provider, key, host=PUB, port=SHIM_PORT, xff=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    form = urllib.parse.urlencode({"provider": provider, "api_key": key}).encode()
    headers = {
        "Host": host,
        "Content-Type": "application/x-www-form-urlencoded",
        "Content-Length": str(len(form)),
    }
    # Finding 1: let a test set X-Forwarded-For so it can stand in for Railway's
    # edge, which appends the real client IP as the RIGHTMOST entry. `xff` is the
    # raw header value (may be a comma-joined list to exercise rightmost logic).
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    conn.request("POST", f"/connect/{nonce}", body=form, headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp.status, body


home2 = tempfile.mkdtemp()
state2 = os.path.join(home2, ".squire")
# The real container's .env is a SYMLINK into tmpfs. Reproduce that shape so
# the test proves credential writes follow the link instead of replacing it
# with a plaintext file on the "volume".
tmpfs = tempfile.mkdtemp()
open(os.path.join(tmpfs, ".env"), "w").close()
os.symlink(os.path.join(tmpfs, ".env"), os.path.join(home2, ".env"))


def start_shim2(**overrides):
    env = dict(os.environ)
    env.update({
        "PORT": str(SHIM_PORT),
        "HERMES_HOME": home2,
        "SQUIRE_STATE_DIR": state2,
        "SQUIRE_PUBLIC_DOMAIN": PUB,
        "TELEGRAM_WEBHOOK_SECRET": "s3cr3t",
        "TENANT_ID": "t-connect-test",
        "SQUIRE_CONNECT_OPENAI_VALIDATE_URL":
            f"http://127.0.0.1:{FAKE_PROVIDER_PORT}/openai/ok",
        "SQUIRE_CONNECT_ANTHROPIC_VALIDATE_URL":
            f"http://127.0.0.1:{FAKE_PROVIDER_PORT}/anthropic/ok",
        # Task 6 wires the full pipeline; these keep restart/notify inert here.
        "SQUIRE_SUPERVISORCTL": "/bin/true",
        "CONTROL_API_URL": "",
    })
    env.update(overrides)
    proc = subprocess.Popen([sys.executable, SHIM], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{SHIM_PORT}/health", timeout=2)
            return proc
        except Exception:
            time.sleep(0.25)
    raise AssertionError("shim never became ready")


OPENAI_KEY = "sk-test-openai-key-0123456789abcdef0123"
ANTHROPIC_KEY = "sk-ant-api03-test-key-0123456789abcdef"

proc = start_shim2()
try:
    # --- happy path: OpenAI key -------------------------------------------
    n1 = squire_connect.mint_nonce(state2)
    status, body = shim_post(n1, "openai", OPENAI_KEY)
    check("valid OpenAI key -> done page", status == 200 and "back to Telegram" in body,
          (status, body[:300]))
    check("provider was actually called to validate",
          any(c["path"] == "/openai/ok" and c["bearer"] == f"Bearer {OPENAI_KEY}"
              for c in provider_calls), provider_calls)
    env_text = open(os.path.join(tmpfs, ".env")).read()
    check("key stored in .env", f"OPENAI_API_KEY={OPENAI_KEY}" in env_text, env_text)
    check(".env is still a symlink (write followed it to tmpfs)",
          os.path.islink(os.path.join(home2, ".env")), "symlink was replaced")
    check("nonce consumed by success", squire_connect.find_nonce(state2, n1) is None)

    # A reused nonce gets the friendly invalid page and stores nothing.
    before = env_text
    status, body = shim_post(n1, "openai", "sk-test-second-key-000000000000000000")
    check("reused nonce -> invalid page", status == 200 and "ask your" in body.lower(),
          (status, body[:200]))
    check("reused nonce stored nothing",
          open(os.path.join(tmpfs, ".env")).read() == before, "env changed")

    # --- invalid key: stated plainly, nothing stored, nonce STILL valid ---
    # The fake provider 401s on the /bad path, so restart the shim pointing
    # the OpenAI validation URL at it.
    provider_calls.clear()
    n2 = squire_connect.mint_nonce(state2)
    proc.terminate(); proc.wait(timeout=10)
    proc = start_shim2(SQUIRE_CONNECT_OPENAI_VALIDATE_URL=
                       f"http://127.0.0.1:{FAKE_PROVIDER_PORT}/openai/bad")
    status, body = shim_post(n2, "openai", "sk-test-a-key-the-provider-rejects-000")
    check("rejected key -> plain statement on the form page",
          status == 200 and "didn't accept" in body, (status, body[:300]))
    check("rejected key stored nothing",
          "rejects" not in open(os.path.join(tmpfs, ".env")).read(), "stored a bad key")
    check("nonce still valid after a rejected key",
          squire_connect.find_nonce(state2, n2) is not None)

    # --- happy path: Anthropic key (fresh shim, back on the OK endpoint) --
    proc.terminate(); proc.wait(timeout=10)
    proc = start_shim2()
    status, body = shim_post(n2, "anthropic", ANTHROPIC_KEY)
    check("valid Anthropic key -> done page", status == 200 and "Anthropic" in body,
          (status, body[:300]))
    env_text = open(os.path.join(tmpfs, ".env")).read()
    check("Anthropic key stored", f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}" in env_text, env_text)
    check("direct base URL stored with it (overrides the trial proxy)",
          "ANTHROPIC_BASE_URL=https://api.anthropic.com" in env_text, env_text)
    check("anthropic validation sent x-api-key + version header",
          any(c["path"] == "/anthropic/ok" and c["x_api_key"] == ANTHROPIC_KEY
              and c["version"] for c in provider_calls), provider_calls)

    # --- Finding 1: throttle isolates clients by their REAL IP (XFF) -------
    # Behind Railway's edge, self.client_address is the proxy hop — identical
    # for every external user. The shim must key the per-IP failure backoff on
    # the RIGHTMOST X-Forwarded-For entry (the value Railway's own edge
    # appended), so that (a) one abuser cannot lock out the real owner and (b) a
    # client cannot prepend a victim's IP to forge another user's key.
    CONNECT_MAX_FAILS = 5  # mirrors the shim constant of the same name
    XFF_A = "203.0.113.10"
    XFF_B = "203.0.113.20"
    # Exhaust client A's failure budget with nonce misses (each miss records a
    # failure). After CONNECT_MAX_FAILS misses the next request from A is 429.
    for i in range(CONNECT_MAX_FAILS):
        shim_post("miss-a", "openai", f"sk-test-a-{i:030d}", xff=XFF_A)
    status_a, _ = shim_post("miss-a", "openai", "sk-test-a-final-00000000000000", xff=XFF_A)
    check("client A is throttled once its own budget is exhausted",
          status_a == 429, status_a)
    # A DIFFERENT client (different rightmost XFF) is unaffected — per-client
    # isolation. It gets the friendly invalid-nonce page, not a 429.
    status_b, body_b = shim_post("miss-b", "openai", "sk-test-b-000000000000000000", xff=XFF_B)
    check("a different client is NOT throttled by A's failures (per-client isolation)",
          status_b != 429, (status_b, body_b[:120]))
    # Rightmost wins: a request whose XFF *ends* in A's IP is still A, no matter
    # what precedes it — so a client cannot prepend to escape or forge.
    status_a2, _ = shim_post("miss-a", "openai", "sk-test-a-spoof-00000000000000",
                             xff=f"9.9.9.9, {XFF_A}")
    check("rightmost XFF entry is the throttle key (prepended IPs ignored)",
          status_a2 == 429, status_a2)

    # --- abuse limit: repeated misses back off ----------------------------
    # No XFF at all: the throttle must still work, falling back to
    # client_address (127.0.0.1 here). This is the original coverage, retained.
    got_429 = False
    for i in range(8):
        status, _ = shim_post("not-a-nonce", "openai", f"sk-test-guess-{i:030d}")
        if status == 429:
            got_429 = True
            break
    check("repeated nonce misses from one IP (no XFF) hit a 429 backoff", got_429, status)

    # --- Finding 3: validation must NOT follow redirects -------------------
    # urlopen's default opener follows 3xx and re-sends Authorization / x-api-key
    # to the redirect target (possibly a different host). The fake provider
    # 302-redirects /redirect-src -> /redirect-dst; a correct validator refuses
    # the redirect, so /redirect-dst is NEVER hit and the key never leaves for it.
    REDIRECT_KEY = "sk-test-redirect-key-00000000000000000"
    provider_calls.clear()
    n_redir = squire_connect.mint_nonce(state2)
    proc.terminate(); proc.wait(timeout=10)
    proc = start_shim2(SQUIRE_CONNECT_OPENAI_VALIDATE_URL=
                       f"http://127.0.0.1:{FAKE_PROVIDER_PORT}/redirect-src")
    status, body = shim_post(n_redir, "openai", REDIRECT_KEY)
    check("provider redirect -> reachable-failure message on the form page",
          status == 200 and "couldn't be reached" in body, (status, body[:300]))
    check("the key was NOT re-sent to the redirect target",
          not any(c["path"] == "/redirect-dst" for c in provider_calls), provider_calls)
    check("a redirected validation stored nothing",
          REDIRECT_KEY not in open(os.path.join(tmpfs, ".env")).read(), "stored on redirect")
    check("nonce still live after a redirect failure",
          squire_connect.find_nonce(state2, n_redir) is not None)

    # --- Finding 2: fail closed if the write would NOT land on tmpfs ------
    # The entrypoint makes $HERMES_HOME/.env a symlink into tmpfs. If it is
    # instead a PLAIN FILE on the volume, writing the key there is the one
    # forbidden outcome (plaintext credential on the persistent volume). The
    # code must refuse — the user sees a friendly save error, nothing is
    # written, and the nonce stays live for a retry.
    home3 = tempfile.mkdtemp()
    state3 = os.path.join(home3, ".squire")
    plain_env = os.path.join(home3, ".env")
    open(plain_env, "w").close()  # a real file on the "volume", NOT a symlink
    proc.terminate(); proc.wait(timeout=10)
    proc = start_shim2(HERMES_HOME=home3, SQUIRE_STATE_DIR=state3)
    n_vol = squire_connect.mint_nonce(state3)
    status, body = shim_post(n_vol, "openai", OPENAI_KEY)
    check("plain-file .env -> friendly save error (not a 500, not the done page)",
          status == 200 and "went wrong saving" in body.lower()
          and "back to telegram" not in body.lower(), (status, body[:300]))
    check("no credential written to the plain .env on the volume",
          OPENAI_KEY not in open(plain_env).read(), "credential leaked to volume")
    check("nonce still live after a refused (non-tmpfs) write",
          squire_connect.find_nonce(state3, n_vol) is not None)
finally:
    proc.terminate()
    proc.wait(timeout=10)
    out = proc.stdout.read() if proc.stdout else ""
    check("no credential material in shim logs",
          OPENAI_KEY not in out and ANTHROPIC_KEY not in out, out[-300:])
    fake_provider.shutdown()
    fake_provider.server_close()

print("== connected pipeline: switch model -> restart gateway -> notify ==")
home3 = tempfile.mkdtemp()
# A config.yaml with the trial model line plus other keys that MUST survive.
config_path = os.path.join(home3, "config.yaml")
open(config_path, "w").write(
    'model: "anthropic:claude-sonnet-5"\n'
    "hooks:\n  pre_llm_call: something-load-bearing\n"
    "timezone: UTC\n"
)
open(os.path.join(home3, ".env"), "w").close()

# Fake supervisorctl: records its argv so ordering and arguments are provable.
bindir = tempfile.mkdtemp()
ctl_log = os.path.join(bindir, "ctl.log")
ctl = os.path.join(bindir, "supervisorctl")
open(ctl, "w").write(f"#!/bin/sh\necho \"$@\" >> {ctl_log}\n")
os.chmod(ctl, 0o755)

# Fake control-api capturing /internal/llm-connected.
notified = []


class FakeControl(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        notified.append({"path": self.path,
                         "auth": self.headers.get("Authorization"),
                         "payload": json.loads(self.rfile.read(n) or b"{}")})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


CONTROL_PORT = 18084
control = ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), FakeControl)
control.daemon_threads = True
threading.Thread(target=control.serve_forever, daemon=True).start()

os.environ["SQUIRE_SUPERVISORCTL"] = ctl
os.environ["SQUIRE_SUPERVISORD_CONF"] = "/tmp/fake-supervisord.conf"
os.environ["CONTROL_API_URL"] = f"http://127.0.0.1:{CONTROL_PORT}"
os.environ["INTERNAL_API_TOKEN"] = "internal-tok-test"
os.environ["TENANT_ID"] = "t-pipeline-test"

order = []
real_switch = squire_connect.switch_gateway_model
real_restart = squire_connect.restart_gateway
real_notify = squire_connect.notify_control_api
squire_connect.switch_gateway_model = lambda p, h=None: (order.append("switch"), real_switch(p, h))[1]
squire_connect.restart_gateway = lambda: (order.append("restart"), real_restart())[1]
squire_connect.notify_control_api = lambda p: (order.append("notify"), real_notify(p))[1]
try:
    squire_connect.run_connected_pipeline("openai", hermes_home=home3)
finally:
    squire_connect.switch_gateway_model = real_switch
    squire_connect.restart_gateway = real_restart
    squire_connect.notify_control_api = real_notify

check("pipeline order is switch -> restart -> notify",
      order == ["switch", "restart", "notify"], order)

config_text = open(config_path).read()
check("model line rewritten to the OpenAI default",
      'model: "openai:gpt-4.1"' in config_text, config_text)
check("the rest of config.yaml survived (targeted line edit, no YAML round-trip)",
      "pre_llm_call: something-load-bearing" in config_text
      and "timezone: UTC" in config_text, config_text)
check("gateway restarted via supervisorctl",
      os.path.exists(ctl_log) and "restart gateway" in open(ctl_log).read(),
      open(ctl_log).read() if os.path.exists(ctl_log) else "no ctl call")
check("control-api notified on the pinned route",
      notified and notified[0]["path"] == "/internal/llm-connected", notified)
check("notify payload is exactly {tenant_id, provider}",
      notified and notified[0]["payload"] == {"tenant_id": "t-pipeline-test",
                                              "provider": "openai"}, notified)
check("notify carries the internal bearer",
      notified and notified[0]["auth"] == "Bearer internal-tok-test", notified)

# Model choice per provider (env-overridable, defaults locked here).
# The API-KEY providers keep the scalar `model:` line.
open(config_path, "w").write('model: "anthropic:claude-sonnet-5"\n')
squire_connect.switch_gateway_model("anthropic", home3)
check("anthropic keeps the sonnet model (direct, no proxy)",
      'model: "anthropic:claude-sonnet-5"' in open(config_path).read())

# --- ChatGPT subscription: hermes needs a MAPPING, not a scalar -------------
# Live 2026-08-16: we wrote `model: "openai:gpt-5-codex"`, which hermes does not
# resolve to its openai-codex provider at all — it fell through to OpenRouter
# and every message died with "Missing Authentication header". Verified against
# a known-working hermes install: the openai-codex provider is selected by a
# mapping, and api_mode/base_url are hermes's to decide, not ours.
TRIAL_CONFIG = ('model: "anthropic:claude-sonnet-5"\n'
                "\n"
                "hooks:\n  pre_llm_call:\n"
                '    - command: "/opt/squire/bin/squire-concierge-hook.py"\n'
                "      timeout: 10\n"
                'timezone: "UTC"\n')
open(config_path, "w").write(TRIAL_CONFIG)
squire_connect.switch_gateway_model("chatgpt", home3)
chat_text = open(config_path).read()
check("chatgpt writes the model MAPPING hermes actually resolves",
      'model:\n  provider: "openai-codex"\n  default: "gpt-5.4"\n' in chat_text, chat_text)
check("the old scalar model line is gone",
      "anthropic:claude-sonnet-5" not in chat_text
      and "openai:gpt-5-codex" not in chat_text, chat_text)
check("the hooks block and the rest of config.yaml survive byte-for-byte",
      "pre_llm_call:" in chat_text
      and '- command: "/opt/squire/bin/squire-concierge-hook.py"' in chat_text
      and "timeout: 10" in chat_text
      and 'timezone: "UTC"' in chat_text, chat_text)
check("no api_mode/base_url/api_key/openai_runtime is written (hermes owns those)",
      "api_mode" not in chat_text and "base_url" not in chat_text
      and "api_key" not in chat_text and "openai_runtime" not in chat_text, chat_text)
# Backend-REJECTED slugs for a ChatGPT account must never be the default.
check("the default slug is not one upstream lists as rejected",
      not any(bad in chat_text for bad in
              ("gpt-5.2-codex", "gpt-5.1-codex-max", "gpt-5.1-codex-mini")), chat_text)

# The slug stays env-overridable so a model rename never needs an image rebuild.
open(config_path, "w").write(TRIAL_CONFIG)
os.environ["SQUIRE_CONNECT_MODEL_CHATGPT"] = "gpt-5.3-codex"
try:
    squire_connect.switch_gateway_model("chatgpt", home3)
finally:
    os.environ.pop("SQUIRE_CONNECT_MODEL_CHATGPT", None)
override_text = open(config_path).read()
check("SQUIRE_CONNECT_MODEL_CHATGPT overrides only the slug",
      'model:\n  provider: "openai-codex"\n  default: "gpt-5.3-codex"\n' in override_text,
      override_text)

# Switching AWAY from chatgpt must remove the whole mapping block, not just its
# first line — an orphaned `  provider:` under a scalar is invalid YAML and the
# gateway would refuse to boot.
squire_connect.switch_gateway_model("openai", home3)
back_text = open(config_path).read()
check("switching back to a key provider replaces the whole mapping block",
      'model: "openai:gpt-4.1"' in back_text
      and "openai-codex" not in back_text and "default:" not in back_text, back_text)
check("the hooks block survived the mapping -> scalar switch too",
      "pre_llm_call:" in back_text and 'timezone: "UTC"' in back_text, back_text)

# A notify failure must not blow up the pipeline (heartbeat backstop covers it).
control.shutdown()
control.server_close()
notified.clear()
try:
    squire_connect.run_connected_pipeline("openai", hermes_home=home3)
    check("pipeline survives an unreachable control-api", True)
except Exception as exc:  # noqa: BLE001
    check("pipeline survives an unreachable control-api", False, repr(exc))
for var in ("SQUIRE_SUPERVISORCTL", "SQUIRE_SUPERVISORD_CONF",
            "CONTROL_API_URL", "INTERNAL_API_TOKEN", "TENANT_ID"):
    os.environ.pop(var, None)

# ---------------------------------------------------------------------------
# Proactive owner celebration: the moment the credential lands (in a detached
# poller or the /connect handler, OUTSIDE any chat turn), the tenant sends the
# "you're connected" message to the owner's DM via `hermes send` and advances
# the concierge flow to ask_timezone so the agent does NOT double-celebrate.
# ---------------------------------------------------------------------------
print("== proactive owner celebration (notify_owner_connected) ==")

# squire_autopair holds the approved-owner store; reuse its exact helpers so
# this test resolves the owner id the same way production does.
import squire_autopair  # noqa: E402

# Words the celebration must NEVER contain: trial/pricing pitch + operator
# jargon. (These are the substrings the test enforces; keep the copy clear of
# all of them.)
FORBIDDEN_WORDS = [
    "trial", "price", "pricing", "cost", "$", "subscription", "pay", "budget",
    # operator jargon
    "gateway", "supervisor", "container", "tmpfs", "proxy", "revoke", "nonce",
    "webhook",
]


def make_fake_hermes(bindir):
    """A fake `hermes` binary that records its argv (NUL-separated so the text
    arg's own newlines don't corrupt the record) and exits with the rc named by
    SQUIRE_FAKE_HERMES_RC (default 0)."""
    argv_log = os.path.join(bindir, "hermes-argv")
    script = os.path.join(bindir, "hermes")
    open(script, "w").write(
        "#!/bin/sh\n"
        'for a in "$@"; do printf "%s\\000" "$a" >> "' + argv_log + '"; done\n'
        'exit ${SQUIRE_FAKE_HERMES_RC:-0}\n'
    )
    os.chmod(script, 0o755)
    return script, argv_log


def read_argv(argv_log):
    """Decode the NUL-separated argv the fake hermes recorded."""
    if not os.path.exists(argv_log):
        return None
    raw = open(argv_log, "rb").read()
    parts = raw.split(b"\x00")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    return [p.decode("utf-8") for p in parts]


# --- Test 1: resolves the owner and invokes `hermes send --to telegram:<id>` --
home_n = tempfile.mkdtemp()
OWNER_ID = "998877665"
# Seed the approved-owner store exactly as autopair writes it.
squire_autopair._atomic_write_0600(
    squire_autopair.approved_path(home_n),
    {OWNER_ID: {"user_name": "owner", "approved_at": 1.0, "approved_by": "test"}},
)
# Seed a concierge-state file mid-flow (state=connected, plus other keys that
# must survive the merge).
state_dir_n = os.path.join(home_n, ".squire")
os.makedirs(state_dir_n, exist_ok=True)
cs_path = os.path.join(state_dir_n, "concierge-state.json")
open(cs_path, "w").write(json.dumps(
    {"state": "connected", "name": "Sam", "llm": "openai_api_key"}))

bindir_n = tempfile.mkdtemp()
fake_hermes, argv_log = make_fake_hermes(bindir_n)

_saved_state_dir = os.environ.get("SQUIRE_STATE_DIR")
os.environ["SQUIRE_STATE_DIR"] = state_dir_n  # same file the hook reads
os.environ["SQUIRE_HERMES_BIN"] = fake_hermes
os.environ.pop("SQUIRE_FAKE_HERMES_RC", None)
try:
    ok = squire_connect.notify_owner_connected("chatgpt", hermes_home=home_n)
    check("notify_owner_connected returns True on a successful send", ok is True, ok)

    argv = read_argv(argv_log)
    check("hermes was invoked once", argv is not None, argv)
    if argv:
        check("argv is `send --to telegram:<owner id>`",
              argv[:3] == ["send", "--to", f"telegram:{OWNER_ID}"], argv[:3])
        text = argv[3] if len(argv) >= 4 else ""
        check("celebration names the provider label (ChatGPT)", "ChatGPT" in text, text)
        check("celebration asks the timezone question", "timezone" in text.lower(), text)
        low = text.lower()
        offenders = [w for w in FORBIDDEN_WORDS if w in low]
        check("celebration has no trial/pricing/operator words", offenders == [], offenders)
        check("celebration uses no underscores (MarkdownV2 renders them literally)",
              "_" not in text, text)

    # --- Test 2: concierge state advanced to ask_timezone, other keys merged --
    advanced = json.loads(open(cs_path).read())
    check("concierge state advanced connected -> ask_timezone",
          advanced.get("state") == "ask_timezone", advanced)
    check("pre-existing concierge keys survive the merge",
          advanced.get("name") == "Sam", advanced)
    check("llm field set to the connected provider id",
          advanced.get("llm") == "chatgpt", advanced)
finally:
    if _saved_state_dir is None:
        os.environ.pop("SQUIRE_STATE_DIR", None)
    else:
        os.environ["SQUIRE_STATE_DIR"] = _saved_state_dir

# --- Test 3: no owner in the store -> False, no send, non-fatal --------------
home_empty = tempfile.mkdtemp()
bindir_e = tempfile.mkdtemp()
fake_hermes_e, argv_log_e = make_fake_hermes(bindir_e)
os.environ["SQUIRE_HERMES_BIN"] = fake_hermes_e
res_empty = squire_connect.notify_owner_connected("openai", hermes_home=home_empty)
check("unbound tenant -> notify returns False (non-fatal)", res_empty is False, res_empty)
check("unbound tenant -> no hermes send attempted", read_argv(argv_log_e) is None,
      read_argv(argv_log_e))

# --- Test 4: hermes send non-zero exit -> False, pipeline still completes ----
home_f = tempfile.mkdtemp()
squire_autopair._atomic_write_0600(
    squire_autopair.approved_path(home_f),
    {OWNER_ID: {"user_name": "owner", "approved_at": 1.0}},
)
open(os.path.join(home_f, "config.yaml"), "w").write('model: "anthropic:claude-sonnet-5"\n')
open(os.path.join(home_f, ".env"), "w").close()
bindir_f = tempfile.mkdtemp()
fake_hermes_f, argv_log_f = make_fake_hermes(bindir_f)
os.environ["SQUIRE_HERMES_BIN"] = fake_hermes_f
os.environ["SQUIRE_FAKE_HERMES_RC"] = "1"
os.environ["SQUIRE_SUPERVISORCTL"] = "/bin/true"
os.environ["CONTROL_API_URL"] = ""
try:
    res_fail = squire_connect.notify_owner_connected("anthropic", hermes_home=home_f)
    check("hermes send rc!=0 -> notify returns False", res_fail is False, res_fail)
    try:
        squire_connect.run_connected_pipeline("anthropic", hermes_home=home_f)
        check("pipeline completes even when the proactive send fails", True)
    except Exception as exc:  # noqa: BLE001
        check("pipeline completes even when the proactive send fails", False, repr(exc))
    check("a failed send leaves a diagnosable breadcrumb (poller stdout is DEVNULL)",
          os.path.exists(os.path.join(home_f, ".squire", "celebration-error.json")),
          os.listdir(os.path.join(home_f, ".squire")) if os.path.isdir(
              os.path.join(home_f, ".squire")) else "no .squire")
finally:
    os.environ.pop("SQUIRE_FAKE_HERMES_RC", None)
    os.environ.pop("SQUIRE_HERMES_BIN", None)
    os.environ.pop("SQUIRE_SUPERVISORCTL", None)
    os.environ.pop("CONTROL_API_URL", None)

# --- Test 4b: the hermes binary is resolved ABSOLUTELY, never bare "hermes" ---
# Live 2026-08-16: notify_owner_connected shelled bare "hermes". It runs from the
# DETACHED device-flow poller — a supervisord grandchild inheriting supervisord's
# frozen environment — so the name did not resolve, FileNotFoundError was
# swallowed, and the celebration silently never sent while every other part of
# the connect succeeded. supervisord.conf documents this exact trap for
# [program:gateway]; this pins that we do not fall into it again.
print("== hermes binary is resolved absolutely (supervisord PATH trap) ==")
check("the default hermes binary is an absolute path",
      squire_connect.HERMES_BIN_DEFAULT.startswith("/"), squire_connect.HERMES_BIN_DEFAULT)
check("it is the venv binary supervisord uses, not the privilege-drop shim",
      squire_connect.HERMES_BIN_DEFAULT == "/opt/hermes/.venv/bin/hermes",
      squire_connect.HERMES_BIN_DEFAULT)
check("the resolver never returns a bare name",
      os.path.isabs(squire_connect._hermes_binary()), squire_connect._hermes_binary())

# --- Test 4c: `hermes send` gets the CURRENT .env, not the frozen one --------
# Live 2026-08-19, the OTHER half of the trap 4b pins. The celebration failed
# with "hermes send: Platform 'telegram' is not configured" while the identical
# command run by hand inside the container worked. The pipeline runs in a thread
# of [program:webhook] (priority 20), which supervisord starts with the
# environment supervisord itself froze at boot — captured BEFORE secrets-sync
# (priority 25) seals the platform credentials into tmpfs. So the token is never
# in the inherited env no matter how long the tenant has been up. 4b fixed the
# binary NAME not resolving; this fixes the CREDENTIALS not resolving.
print("== hermes send is handed freshly-sourced credentials ==")

home_env = tempfile.mkdtemp()
open(os.path.join(home_env, ".env"), "w").write(
    "# a comment line\n"
    "\n"
    "TELEGRAM_BOT_TOKEN=fresh-token-123\n"
    "QUOTED_VALUE='quoted'\n"
    "NOT_A_PAIR\n"
    "STALE_KEY=fresh-wins\n"
)
vals = squire_connect._env_file_values(home_env)
check("_env_file_values reads KEY=VALUE pairs",
      vals.get("TELEGRAM_BOT_TOKEN") == "fresh-token-123", vals)
check("_env_file_values strips surrounding quotes",
      vals.get("QUOTED_VALUE") == "quoted", vals)
check("_env_file_values skips comments, blanks and non-pairs",
      "NOT_A_PAIR" not in vals and "" not in vals, vals)
check("_env_file_values on a missing .env returns {} (never raises)",
      squire_connect._env_file_values(tempfile.mkdtemp()) == {}, "raised or non-empty")

os.environ["STALE_KEY"] = "frozen-loses"
try:
    senv = squire_connect._send_env(home_env)
    check("the fresh .env value beats supervisord's frozen inherited one",
          senv.get("STALE_KEY") == "fresh-wins", senv.get("STALE_KEY"))
    check("the platform credential reaches the send environment",
          senv.get("TELEGRAM_BOT_TOKEN") == "fresh-token-123",
          senv.get("TELEGRAM_BOT_TOKEN"))
    check("HERMES_HOME is pinned to the tenant home",
          senv.get("HERMES_HOME") == home_env, senv.get("HERMES_HOME"))
    check("HOME is pinned too (hermes send resolves ~/.hermes)",
          senv.get("HOME") == home_env, senv.get("HOME"))
finally:
    os.environ.pop("STALE_KEY", None)

# The credential must survive into the CHILD PROCESS, not merely into a dict:
# a subprocess.run() that forgets env= would pass every assertion above and
# still ship the exact bug this fixes.
home_e2e = tempfile.mkdtemp()
squire_autopair._atomic_write_0600(
    squire_autopair.approved_path(home_e2e),
    {OWNER_ID: {"user_name": "owner", "approved_at": 1.0}},
)
open(os.path.join(home_e2e, ".env"), "w").write("TELEGRAM_BOT_TOKEN=e2e-token-xyz\n")
bindir_e2e = tempfile.mkdtemp()
env_log = os.path.join(bindir_e2e, "hermes-env")
script_e2e = os.path.join(bindir_e2e, "hermes")
open(script_e2e, "w").write(
    "#!/bin/sh\n"
    'printf "%s" "${TELEGRAM_BOT_TOKEN:-MISSING}" > "' + env_log + '"\n'
    "exit 0\n"
)
os.chmod(script_e2e, 0o755)
os.environ["SQUIRE_HERMES_BIN"] = script_e2e
try:
    squire_connect.notify_owner_connected("chatgpt", hermes_home=home_e2e)
    seen_tok = open(env_log).read() if os.path.exists(env_log) else "NO RUN"
    check("the child `hermes send` process actually receives the credential",
          seen_tok == "e2e-token-xyz", seen_tok)
finally:
    os.environ.pop("SQUIRE_HERMES_BIN", None)

# --- Test 4d: the celebration goes out BEFORE the gateway restart -----------
# hermes send needs no running gateway for a bot-token platform, so sending
# after restart_gateway() bought nothing and cost startsecs=45 of dead air on
# the most important 45 seconds of the product — and made the DM hostage to a
# gateway that might never come back (live FATAL, 2026-08-19).
print("== celebration precedes the gateway restart ==")
home_ord = tempfile.mkdtemp()
squire_autopair._atomic_write_0600(
    squire_autopair.approved_path(home_ord),
    {OWNER_ID: {"user_name": "owner", "approved_at": 1.0}},
)
open(os.path.join(home_ord, "config.yaml"), "w").write('model: "anthropic:claude-sonnet-5"\n')
open(os.path.join(home_ord, ".env"), "w").close()
bindir_ord = tempfile.mkdtemp()
order_log = os.path.join(bindir_ord, "order")
h_ord = os.path.join(bindir_ord, "hermes")
open(h_ord, "w").write('#!/bin/sh\necho send >> "' + order_log + '"\nexit 0\n')
os.chmod(h_ord, 0o755)
ctl_ord = os.path.join(bindir_ord, "supervisorctl")
open(ctl_ord, "w").write('#!/bin/sh\necho restart >> "' + order_log + '"\nexit 0\n')
os.chmod(ctl_ord, 0o755)
os.environ["SQUIRE_HERMES_BIN"] = h_ord
os.environ["SQUIRE_SUPERVISORCTL"] = ctl_ord
os.environ["CONTROL_API_URL"] = ""
try:
    squire_connect.run_connected_pipeline("chatgpt", hermes_home=home_ord)
    seq = ([ln.strip() for ln in open(order_log).read().splitlines() if ln.strip()]
           if os.path.exists(order_log) else [])
    check("pipeline sends the celebration before restarting the gateway",
          seq[:2] == ["send", "restart"], seq)
finally:
    os.environ.pop("SQUIRE_HERMES_BIN", None)
    os.environ.pop("SQUIRE_SUPERVISORCTL", None)
    os.environ.pop("CONTROL_API_URL", None)

# --- Test 5: config.yaml carries the compression notice gate & still parses --
print("== config.yaml compression notice gate ==")
try:
    import yaml  # noqa: E402
    _have_yaml = True
except Exception:
    _have_yaml = False

cfg_path = IMAGE_ROOT / "home-template" / "config.yaml"
cfg_text = cfg_path.read_text()
check("config.yaml has compression.codex_gpt55_autoraise_notice: false",
      "codex_gpt55_autoraise_notice: false" in cfg_text, cfg_text[-400:])
if _have_yaml:
    parsed = yaml.safe_load(cfg_text)
    check("config.yaml still parses as YAML", isinstance(parsed, dict), type(parsed))
    check("compression gate parses to the boolean false",
          parsed.get("compression", {}).get("codex_gpt55_autoraise_notice") is False,
          parsed.get("compression"))
    check("the hooks block is intact after the edit",
          isinstance(parsed.get("hooks", {}).get("pre_llm_call"), list)
          and parsed["hooks"]["pre_llm_call"][0].get("command")
          == "/opt/squire/bin/squire-concierge-hook.py", parsed.get("hooks"))
else:
    check("PyYAML present to validate config.yaml", False, "PyYAML not importable")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL CONNECT TESTS PASS")
