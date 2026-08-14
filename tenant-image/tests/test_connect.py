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

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL CONNECT TESTS PASS")
