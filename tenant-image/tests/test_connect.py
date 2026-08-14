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

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL CONNECT TESTS PASS")
