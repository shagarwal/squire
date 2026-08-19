"""Functional test for squire-webhook-shim.py — the container contract.

Runs WITHOUT Docker: stands up a fake "telegram adapter" upstream, runs the real
shim against it, and asserts the contract that ingress and control-api are built
against. That contract (POST /webhook/telegram, body forwarded verbatim, health
answers during a cold start) is load-bearing for other services, so it gets a
test rather than a comment.

Usage: python3 tenant-image/tests/test_webhook_shim.py
Exit:  0 all assertions pass · 1 otherwise
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Resolve the image root from this file's location so the test works from any
# cwd, in a worktree, and in CI.
IMAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent
SHIM = str(IMAGE_ROOT / "bin" / "squire-webhook-shim.py")

SECRET = "s3cr3t-token"
# The fleet-wide token: authenticates delivery, must never authenticate binding.
INTERNAL = "fleet-wide-internal-token"
UPSTREAM_PORT = 18443
SHIM_PORT = 18080
UPSTREAM_PATH = "/webhook/telegram"

received = []


class FakeAdapter(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        received.append(
            {
                "path": self.path,
                "secret": self.headers.get("X-Telegram-Bot-Api-Secret-Token"),
                "body": body.decode(),
                "ctype": self.headers.get("Content-Type"),
            }
        )
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


def post(path, payload, port=SHIM_PORT, headers=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def get(path, port=SHIM_PORT, host="127.0.0.1"):
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def own_non_loopback_ip():
    """This host's own routable IP, or None if it only has loopback.

    The shim binds 0.0.0.0, so connecting to this address reaches the same server
    but arrives with a NON-loopback client address -- which is how a request from
    another tenant on Railway's shared private network would look.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # TEST-NET-1. UDP "connect" sends nothing; it only picks a source address.
        sock.connect(("192.0.2.1", 1))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    return None if ip.startswith("127.") else ip


def wait_ready(port, tries=60):
    for _ in range(tries):
        try:
            get("/health", port)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def run(env_extra, with_upstream=True):
    """Start the shim (and optionally the fake adapter); return the process."""
    env = dict(os.environ)
    env.update(
        {
            "PORT": str(SHIM_PORT),
            "SQUIRE_WEBHOOK_PATH": "/webhook/telegram",
            "TELEGRAM_WEBHOOK_PORT": str(UPSTREAM_PORT),
            "TELEGRAM_WEBHOOK_SECRET": SECRET,
            "INTERNAL_API_TOKEN": INTERNAL,
            "TENANT_ID": "t-test",
        }
    )
    env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, SHIM],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert wait_ready(SHIM_PORT), "shim never became ready"
    return proc


failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


# --- fake adapter -----------------------------------------------------------
adapter = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), FakeAdapter)
adapter.daemon_threads = True
threading.Thread(target=adapter.serve_forever, daemon=True).start()

print("== webhook mode ==")
proc = run({"SQUIRE_TELEGRAM_UPSTREAM_PATH": UPSTREAM_PATH})
try:
    status, body = get("/health")
    check("GET /health -> 200", status == 200, (status, body))
    check("health reports webhook mode", body.get("mode") == "webhook", body)
    check("health reports tenant id", body.get("tenant") == "t-test", body)
    check("health sees gateway listening", body.get("gateway_listening") is True, body)

    update = {"update_id": 42, "message": {"text": "hello"}}
    status, body = post("/webhook/telegram", update)
    check("POST /webhook/telegram -> 200", status == 200, (status, body))
    check("forwarded exactly one update", len(received) == 1, received)
    if received:
        r = received[0]
        check("forwarded verbatim", json.loads(r["body"]) == update, r["body"])
        check("forwarded to adapter path", r["path"] == UPSTREAM_PATH, r["path"])
        check("secret-token header stamped", r["secret"] == SECRET, r["secret"])
        check("content-type preserved", r["ctype"] == "application/json", r["ctype"])

    # Counts-by-outcome for the Task 0.6 heartbeat. Counts only -- no chat ids, no
    # per-user breakdown; squire-heartbeat.py scrapes this and forwards it verbatim.
    status, metrics = get("/metrics")
    check("GET /metrics -> 200", status == 200, (status, metrics))
    check("counts the forwarded update", metrics.get("updates_forwarded") == 1, metrics)
    check("no failures counted", metrics.get("updates_failed") == 0, metrics)
    check("no rejections counted", metrics.get("updates_rejected") == 0, metrics)
    check(
        "metrics expose counts and nothing else",
        set(metrics) == {"updates_forwarded", "updates_failed", "updates_rejected"},
        metrics,
    )

    # CROSS-TENANT LEAK GUARD. This listener is on 0.0.0.0 and every tenant shares
    # one Railway private network, so an open /metrics would let tenant B read
    # tenant A's message volume and activity pattern. Loopback (or an
    # authenticated caller) only.
    peer = own_non_loopback_ip()
    if peer:
        status, body = get("/metrics", host=peer)
        check("non-loopback /metrics -> 403", status == 403, (status, body))
        check("denied response carries no counters", "updates_forwarded" not in body, body)
        # /health must stay open on the same interface: a platform probe may come
        # from anywhere, and it carries no per-tenant activity.
        status, _ = get("/health", host=peer)
        check("non-loopback /health still 200", status == 200, status)
    else:
        print("  SKIP  non-loopback /metrics (host has no routable address)")

    status, _ = post("/wrong/path", update)
    check("unknown path -> 404", status == 404, status)

    status, _ = get("/nope")
    check("unknown GET -> 404", status == 404, status)
finally:
    proc.terminate()
    proc.wait(timeout=10)

print("== auth enforcement (REQUIRE_AUTH=true) ==")
received.clear()
proc = run(
    {
        "SQUIRE_TELEGRAM_UPSTREAM_PATH": UPSTREAM_PATH,
        "SQUIRE_WEBHOOK_REQUIRE_AUTH": "true",
        "INTERNAL_API_TOKEN": "internal-tok",
    }
)
try:
    status, _ = post("/webhook/telegram", {"update_id": 1})
    check("no credential -> 401", status == 401, status)
    status, _ = post(
        "/webhook/telegram",
        {"update_id": 2},
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )
    check("telegram secret accepted", status == 200, status)
    status, _ = post(
        "/webhook/telegram",
        {"update_id": 3},
        headers={"X-Squire-Internal-Token": "internal-tok"},
    )
    check("internal token accepted", status == 200, status)
    check("both authorised posts forwarded", len(received) == 2, len(received))
    _, metrics = get("/metrics")
    check("the 401 is counted as a rejection", metrics.get("updates_rejected") == 1, metrics)
    check("rejections are not counted as forwarded", metrics.get("updates_forwarded") == 2, metrics)

    # With REQUIRE_AUTH on, an authenticated caller may scrape from off-box --
    # that is the escape hatch for an operator or a future central collector.
    peer = own_non_loopback_ip()
    if peer:
        req = urllib.request.Request(
            f"http://{peer}:{SHIM_PORT}/metrics",
            headers={"X-Squire-Internal-Token": "internal-tok"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            check("authenticated non-loopback /metrics allowed", r.status == 200, r.status)
finally:
    proc.terminate()
    proc.wait(timeout=10)

print("== polling mode (no TELEGRAM_WEBHOOK_URL) ==")
proc = run({})
try:
    status, body = get("/health")
    check("health still 200", status == 200, status)
    check("health reports polling mode", body.get("mode") == "polling", body)
    status, body = post("/webhook/telegram", {"update_id": 9})
    check("webhook POST -> 503", status == 503, (status, body))
finally:
    proc.terminate()
    proc.wait(timeout=10)

print("== gateway down (upstream refused) ==")
adapter.shutdown()
adapter.server_close()
proc = run({"SQUIRE_TELEGRAM_UPSTREAM_PATH": UPSTREAM_PATH})
try:
    status, body = get("/health")
    check("health 200 while gateway down", status == 200, status)
    check("health reports gateway not listening", body.get("gateway_listening") is False, body)
    status, body = post("/webhook/telegram", {"update_id": 10})
    check("webhook POST -> retryable 503", status == 503, (status, body))
    _, metrics = get("/metrics")
    # This is the counter that tells the fleet a tenant is dropping messages while
    # still answering health checks -- the failure mode nothing else would catch.
    check("an undeliverable update is counted as failed", metrics.get("updates_failed") == 1, metrics)
finally:
    proc.terminate()
    proc.wait(timeout=10)

print("== first-contact owner binding (end to end through the shim) ==")
# tests/test_autopair.py covers the binding logic exhaustively. This proves the
# WIRING: that a real POST through the real shim binds the owner BEFORE the
# update is forwarded. Ordering is the entire point — upstream authorizes this
# same update downstream, so a binding that lands late is a pairing code.
import tempfile  # noqa: E402

received.clear()
# The "gateway down" section above deliberately killed the fake adapter; bring a
# fresh one up so this section exercises the delivery path rather than a 503.
adapter2 = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), FakeAdapter)
adapter2.daemon_threads = True
threading.Thread(target=adapter2.serve_forever, daemon=True).start()

home = tempfile.mkdtemp()
approved = pathlib.Path(home) / "platforms" / "pairing" / "telegram-approved.json"
proc = run({"SQUIRE_TELEGRAM_UPSTREAM_PATH": UPSTREAM_PATH, "HERMES_HOME": home})
try:
    first = {
        "update_id": 900,
        "message": {
            "message_id": 1,
            "from": {"id": 424242, "is_bot": False, "first_name": "owner"},
            "chat": {"id": 424242, "type": "private"},
            "text": "/start",
        },
    }
    AUTH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}

    # --- an UNAUTHENTICATED first contact must never bind ------------------
    # This is the account-takeover path: without the check, anyone who can reach
    # the tenant's port forges a first message to a not-yet-bound tenant and
    # becomes its permanent owner, with no shell for the customer to recover.
    # It must still be DELIVERED (refusing delivery on a header disagreement
    # would break every tenant) — it just falls back to the pairing code.
    forged = json.loads(json.dumps(first))
    forged["update_id"] = 899
    forged["message"]["from"]["id"] = 6666
    forged["message"]["chat"]["id"] = 6666
    status, _ = post("/webhook/telegram", forged)
    check("unauthenticated update still delivered", status == 200, status)
    check("unauthenticated update did NOT bind an owner", not approved.exists(),
          str(approved))
    if received:
        fwdf = json.loads(received[0]["body"])
        check("unauthenticated /start forwarded VERBATIM",
              fwdf["message"]["text"] == "/start", fwdf["message"].get("text"))

    # A wrong secret is no better than none.
    received.clear()
    post("/webhook/telegram", forged, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
    check("wrong secret did NOT bind an owner", not approved.exists(), str(approved))

    # THE CRITICAL CASE the first re-review caught: the fleet-wide internal token
    # authenticates DELIVERY but must NEVER bind, because every trial tenant's
    # own agent can read it from its environment. A forged update carrying ONLY
    # the internal token must be forwarded verbatim and bind nobody.
    received.clear()
    status, _ = post("/webhook/telegram", forged,
                     headers={"X-Squire-Internal-Token": INTERNAL})
    check("fleet-token-only update still delivered", status == 200, status)
    check("fleet token did NOT bind an owner", not approved.exists(), str(approved))
    if received:
        fwdi = json.loads(received[0]["body"])
        check("fleet-token /start forwarded VERBATIM",
              fwdi["message"]["text"] == "/start", fwdi["message"].get("text"))

    # --- the real, authenticated first contact -----------------------------
    received.clear()
    status, _ = post("/webhook/telegram", first, headers=AUTH)
    check("first contact still forwards (200)", status == 200, status)
    check("owner was bound during that request", approved.exists(), str(approved))
    if approved.exists():
        store = json.loads(approved.read_text())
        check("bound the sender's id", list(store) == ["424242"], list(store))
    check("update reached the gateway", len(received) == 1, len(received))

    # The gateway must receive text, not a command: upstream swallows "/start"
    # with an empty reply, so a verbatim relay here would authorize the owner and
    # then say nothing at all.
    if received:
        fwd = json.loads(received[0]["body"])
        check("forwarded body has /start defanged",
              fwd["message"]["text"] == "start", fwd["message"].get("text"))
        check("forwarded update is otherwise intact",
              fwd["update_id"] == 900 and fwd["message"]["from"]["id"] == 424242, fwd)

    # A different user afterwards must NOT be bound — the gate is closed.
    second = json.loads(json.dumps(first))
    second["update_id"] = 901
    second["message"]["from"]["id"] = 999999
    second["message"]["chat"]["id"] = 999999
    received.clear()
    post("/webhook/telegram", second, headers=AUTH)
    store = json.loads(approved.read_text())
    check("second user not auto-approved", list(store) == ["424242"], list(store))

    # "One message wide" is a claim about the FORWARDED BODY, not just the store.
    # Asserting only on the store let a mutant survive that bound once but went on
    # defanging every later /start: the store stayed correct while every
    # subsequent user's command was silently rewritten. Inspect the bytes.
    if received:
        fwd2 = json.loads(received[0]["body"])
        check("second /start forwarded VERBATIM (still a command)",
              fwd2["message"]["text"] == "/start", fwd2["message"].get("text"))
        check("second update otherwise intact",
              fwd2["message"]["from"]["id"] == 999999, fwd2["message"]["from"])

    # --- nonce hygiene: the payload must not survive on ANY non-bound path --
    # The owner re-taps the t.me/<bot>?start=<nonce> deep link after binding.
    # The store is non-empty so nothing binds — but the raw body carries the
    # LIVE nonce, and forwarding it verbatim would write the credential into
    # the transcript and Hindsight memory. The payload is stripped while the
    # message stays a command ("/start"), keeping upstream's platform-ping
    # semantics for an established chat.
    retap = json.loads(json.dumps(first))
    retap["update_id"] = 902
    retap["message"]["text"] = "/start retap-live-nonce"
    received.clear()
    status, _ = post("/webhook/telegram", retap, headers=AUTH)
    check("re-tap after bind still delivers", status == 200, status)
    check("re-tap after bind: payload absent from forwarded body",
          received and "retap-live-nonce" not in received[0]["body"],
          received[0]["body"] if received else "nothing forwarded")
    if received:
        fwdr = json.loads(received[0]["body"])
        check("re-tap after bind: /start still a command",
              fwdr["message"]["text"] == "/start", fwdr["message"].get("text"))

    # An unauthenticated delivery may never bind, but it is still forwarded —
    # and a nonce riding on it (e.g. an ingress that dropped the secret header
    # on the real first tap) must be stripped there too.
    unauth = json.loads(json.dumps(first))
    unauth["update_id"] = 903
    unauth["message"]["text"] = "/start unauth-live-nonce"
    received.clear()
    status, _ = post("/webhook/telegram", unauth)
    check("unauthenticated payload update still delivered", status == 200, status)
    check("unauthenticated path: payload absent from forwarded body",
          received and "unauth-live-nonce" not in received[0]["body"],
          received[0]["body"] if received else "nothing forwarded")
    if received:
        fwdu = json.loads(received[0]["body"])
        check("unauthenticated path: /start still a command",
              fwdu["message"]["text"] == "/start", fwdu["message"].get("text"))
finally:
    proc.terminate()
    proc.wait(timeout=10)
    adapter2.shutdown()
    adapter2.server_close()
    out = proc.stdout.read() if proc.stdout else ""
    # The owner's Telegram id is an account identifier and this output goes to a
    # shared aggregator, so the log line must be present but the id must not.
    check("shim logged the binding", "bound tenant owner" in out, out[-200:])
    check("owner id absent from logs", "424242" not in out, out[-200:])
    check("refusal to bind is logged loudly", "NOT binding owner" in out, out[-300:])
    check("per-bot secret absent from logs", SECRET not in out, out[-200:])

print("== SQUIRE_AUTOPAIR=false restores the upstream pairing ceremony ==")
received.clear()
home2 = tempfile.mkdtemp()
approved2 = pathlib.Path(home2) / "platforms" / "pairing" / "telegram-approved.json"
adapter3 = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), FakeAdapter)
adapter3.daemon_threads = True
threading.Thread(target=adapter3.serve_forever, daemon=True).start()
proc = run({"SQUIRE_TELEGRAM_UPSTREAM_PATH": UPSTREAM_PATH, "HERMES_HOME": home2,
            "SQUIRE_AUTOPAIR": "false"})
try:
    optout = {
        "update_id": 950,
        "message": {
            "message_id": 1,
            "from": {"id": 777001, "is_bot": False, "first_name": "owner"},
            "chat": {"id": 777001, "type": "private"},
            "text": "/start",
        },
    }
    status, _ = post("/webhook/telegram", optout,
                     headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    check("opt-out still delivers", status == 200, status)
    check("opt-out binds nobody", not approved2.exists(), str(approved2))
    if received:
        fwd3 = json.loads(received[0]["body"])
        check("opt-out forwards /start verbatim",
              fwd3["message"]["text"] == "/start", fwd3["message"].get("text"))

    # Payload hygiene is independent of the autopair switch: a deep-link tap
    # on an opted-out tenant still carries the nonce, and it must still die at
    # the shim rather than reach the transcript.
    optout2 = json.loads(json.dumps(optout))
    optout2["update_id"] = 951
    optout2["message"]["text"] = "/start optout-live-nonce"
    received.clear()
    status, _ = post("/webhook/telegram", optout2,
                     headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    check("opt-out payload update still delivers", status == 200, status)
    check("opt-out: payload absent from forwarded body",
          received and "optout-live-nonce" not in received[0]["body"],
          received[0]["body"] if received else "nothing forwarded")
    if received:
        fwd4 = json.loads(received[0]["body"])
        check("opt-out: /start still a command",
              fwd4["message"]["text"] == "/start", fwd4["message"].get("text"))
finally:
    proc.terminate()
    proc.wait(timeout=10)
    adapter3.shutdown()
    adapter3.server_close()

print("== corrupt approved store: refuses to bind, payload still never forwards ==")
# maybe_bind_owner bails out early on an unreadable store (it cannot prove the
# store is empty). The payload strip must not depend on how far binding got.
received.clear()
home3 = tempfile.mkdtemp()
approved3 = pathlib.Path(home3) / "platforms" / "pairing" / "telegram-approved.json"
approved3.parent.mkdir(parents=True)
approved3.write_text("{ this is not json")
adapter4 = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), FakeAdapter)
adapter4.daemon_threads = True
threading.Thread(target=adapter4.serve_forever, daemon=True).start()
proc = run({"SQUIRE_TELEGRAM_UPSTREAM_PATH": UPSTREAM_PATH, "HERMES_HOME": home3})
try:
    corrupt = {
        "update_id": 960,
        "message": {
            "message_id": 1,
            "from": {"id": 888001, "is_bot": False, "first_name": "owner"},
            "chat": {"id": 888001, "type": "private"},
            "text": "/start corrupt-live-nonce",
        },
    }
    status, _ = post("/webhook/telegram", corrupt,
                     headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    check("corrupt store still delivers", status == 200, status)
    check("corrupt store left untouched",
          approved3.read_text() == "{ this is not json", approved3.read_text())
    check("corrupt store: payload absent from forwarded body",
          received and "corrupt-live-nonce" not in received[0]["body"],
          received[0]["body"] if received else "nothing forwarded")
    if received:
        fwd5 = json.loads(received[0]["body"])
        check("corrupt store: /start still a command",
              fwd5["message"]["text"] == "/start", fwd5["message"].get("text"))
finally:
    proc.terminate()
    proc.wait(timeout=10)
    adapter4.shutdown()
    adapter4.server_close()

print("== wake-race first contact: NO side effects before upstream is reachable ==")
# The live incident (staging tenant c559d1c9, 2026-08-13 00:31 UTC): the user's
# deep-link tap IS what wakes the container from serverless sleep, so ingress
# delivers the buffered first-contact "/start <nonce>" while the gateway is
# still booting. The shim used to bind the owner FIRST (a durable write), THEN
# discover the relay fails -> 503. On ingress's redelivery the store was
# non-empty, so the non-binding path stripped the payload and forwarded a bare
# "/start", which upstream swallows as a platform ping -> the owner's first
# contact produced silence. The fix: probe the upstream BEFORE any side effect;
# if it is not listening, answer 503 untouched and let redelivery carry
# bind + defang + relay together on one successful delivery.
received.clear()
home4 = tempfile.mkdtemp()
approved4 = pathlib.Path(home4) / "platforms" / "pairing" / "telegram-approved.json"
# Deliberately NO fake adapter running yet — that is the whole point. Mirror
# the incident exactly: a nonce-provisioned tenant (staging c559d1c9 was one).
proc = run({
    "SQUIRE_TELEGRAM_UPSTREAM_PATH": UPSTREAM_PATH,
    "HERMES_HOME": home4,
    "SQUIRE_BIND_NONCE": "wake-live-nonce",
})
try:
    AUTH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    wake = {
        "update_id": 970,
        "message": {
            "message_id": 1,
            "from": {"id": 555001, "is_bot": False, "first_name": "owner"},
            "chat": {"id": 555001, "type": "private"},
            "text": "/start wake-live-nonce",
        },
    }

    # --- delivery while the gateway is still booting ------------------------
    status, _ = post("/webhook/telegram", wake, headers=AUTH)
    check("upstream down: first contact answers 503", status == 503, status)
    check("upstream down: owner NOT bound (store still empty)",
          not approved4.exists(), str(approved4))

    # An ordinary text message while down: 503 too, nothing mutated. This
    # already held via the relay-exception path; pinned so the probe can never
    # make it worse.
    plain = {
        "update_id": 971,
        "message": {
            "message_id": 2,
            "from": {"id": 555001, "is_bot": False, "first_name": "owner"},
            "chat": {"id": 555001, "type": "private"},
            "text": "hello",
        },
    }
    status, _ = post("/webhook/telegram", plain, headers=AUTH)
    check("upstream down: ordinary message answers 503", status == 503, status)
    check("upstream down: still no owner bound", not approved4.exists(), str(approved4))
    _, metrics = get("/metrics")
    check("upstream down: both counted failed, nothing forwarded/rejected",
          metrics == {"updates_forwarded": 0, "updates_failed": 2,
                      "updates_rejected": 0}, metrics)

    # --- ingress redelivers the SAME update once the gateway is up ----------
    adapter5 = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), FakeAdapter)
    adapter5.daemon_threads = True
    threading.Thread(target=adapter5.serve_forever, daemon=True).start()
    try:
        status, _ = post("/webhook/telegram", wake, headers=AUTH)
        check("redelivery with upstream up -> 200", status == 200, status)
        check("redelivery bound the owner", approved4.exists(), str(approved4))
        if approved4.exists():
            store4 = json.loads(approved4.read_text())
            check("redelivery bound the sender's id",
                  list(store4) == ["555001"], list(store4))
        check("redelivery reached the gateway", len(received) == 1, len(received))
        # The greeting-triggering relay: bind + defang on the SAME delivery.
        # "start" as text, not "/start" as a command — a bare command here is
        # exactly the silence the incident produced.
        if received:
            fwdw = json.loads(received[0]["body"])
            check("redelivery: /start defanged to text 'start'",
                  fwdw["message"]["text"] == "start", fwdw["message"].get("text"))
            check("redelivery: nonce absent from forwarded body",
                  "wake-live-nonce" not in received[0]["body"], received[0]["body"])
    finally:
        adapter5.shutdown()
        adapter5.server_close()
finally:
    proc.terminate()
    proc.wait(timeout=10)
    out = proc.stdout.read() if proc.stdout else ""
    check("probe decline is logged distinctly",
          "declining before side effects" in out, out[-300:])
    check("nonce absent from logs", "wake-live-nonce" not in out, out[-300:])

print("== probe-passed-then-died race: existing 503 backstop still holds ==")
# A listener that ACCEPTS the TCP connect (so the pre-probe passes) but closes
# the connection immediately (so the HTTP relay dies). In this window the side
# effects MAY already have run — that is accepted: it is today's entire
# boot-length (~30s) exposure reduced to the milliseconds between probe and
# relay, and the existing exception path still answers a retryable 503 rather
# than dropping the update.
racer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
racer.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
racer.bind(("127.0.0.1", UPSTREAM_PORT))
racer.listen(8)


def _accept_and_slam():
    while True:
        try:
            conn, _ = racer.accept()
        except OSError:  # racer closed -> test section over
            return
        conn.close()


threading.Thread(target=_accept_and_slam, daemon=True).start()
proc = run({"SQUIRE_TELEGRAM_UPSTREAM_PATH": UPSTREAM_PATH})
try:
    status, _ = post("/webhook/telegram", {"update_id": 980,
                                           "message": {"text": "hello"}})
    check("probe passes but relay dies -> still retryable 503",
          status == 503, status)
finally:
    proc.terminate()
    proc.wait(timeout=10)
    racer.close()
    out = proc.stdout.read() if proc.stdout else ""
    check("race path went through the relay backstop, not the probe",
          "upstream unavailable" in out, out[-300:])

print("== Host gating: a public domain must not expose the webhook ==")
# Attaching a Railway public domain (1C connect page) points the INTERNET at
# this port. A forged Telegram update delivered via the public domain is the
# account-takeover / message-injection path, so when a public domain is
# configured, /webhook/telegram and /health answer ONLY for private-looking
# Hosts (railway.internal, IP literals, localhost). /connect/* is the single
# public surface. With no public domain configured (every section above),
# nothing changes.
import http.client  # noqa: E402

PUB = "tenant-test.up.railway.app"


def raw_request(method, path, body=None, host=None, extra=None):
    """http.client so we control the Host header exactly (urllib rewrites it)."""
    conn = http.client.HTTPConnection("127.0.0.1", SHIM_PORT, timeout=10)
    headers = {"Content-Type": "application/json"}
    if host is not None:
        headers["Host"] = host
    headers.update(extra or {})
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Length"] = str(len(data))
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, payload

# Harness cleanup: the "probe-passed-then-died race" section above leaves its
# _accept_and_slam daemon thread blocked in racer.accept(). racer.close() does
# NOT wake a thread already inside accept(), so that closed-but-still-listening
# socket keeps UPSTREAM_PORT bound and our fresh adapter6 cannot claim it. A
# throwaway connect is accepted by the zombie listener, the thread closes it and
# loops, and its next accept() on the now-fully-closed fd raises -> thread exits
# and the port frees. Retry the bind a few times to ride out the wakeup.
for _attempt in range(20):
    try:
        _drain = socket.create_connection(("127.0.0.1", UPSTREAM_PORT), timeout=0.5)
        _drain.close()
    except OSError:
        pass
    try:
        adapter6 = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), FakeAdapter)
        break
    except OSError:
        time.sleep(0.2)
else:
    raise RuntimeError("could not rebind UPSTREAM_PORT for the Host-gating section")
adapter6.daemon_threads = True
threading.Thread(target=adapter6.serve_forever, daemon=True).start()
received.clear()
proc = run({"SQUIRE_TELEGRAM_UPSTREAM_PATH": UPSTREAM_PATH,
            "SQUIRE_PUBLIC_DOMAIN": PUB})
try:
    update = {"update_id": 5000, "message": {"text": "forged"}}

    # The attack: a Telegram-shaped POST arriving with the public domain's Host.
    status, _ = raw_request("POST", "/webhook/telegram", update, host=PUB)
    check("public Host: webhook POST -> 403", status == 403, status)
    check("public Host: nothing was forwarded upstream", len(received) == 0, received)

    # Even WITH valid delivery credentials: the public edge is not ingress.
    status, _ = raw_request("POST", "/webhook/telegram", update, host=PUB,
                            extra={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    check("public Host + valid secret: still 403", status == 403, status)
    check("still nothing forwarded", len(received) == 0, received)

    # Port suffix and case must not dodge the gate.
    status, _ = raw_request("POST", "/webhook/telegram", update, host=PUB.upper() + ":443")
    check("public Host with port/case: still 403", status == 403, status)

    # An unknown public-looking Host fails closed too.
    status, _ = raw_request("POST", "/webhook/telegram", update, host="evil.example.com")
    check("unknown non-private Host: 403 (fail closed)", status == 403, status)

    # /health follows the webhook: private only, once a public domain exists.
    status, _ = raw_request("GET", "/health", host=PUB)
    check("public Host: /health -> 403", status == 403, status)

    # The gate counts rejections so the fleet can see probing.
    _, metrics = get("/metrics")
    check("public-Host webhook posts counted as rejected",
          metrics.get("updates_rejected", 0) >= 3, metrics)

    # Private-looking Hosts keep working: this is how ingress and Railway's
    # private network actually address the shim.
    status, _ = raw_request("POST", "/webhook/telegram",
                            {"update_id": 5001, "message": {"text": "hello"}},
                            host="tenant-abc.railway.internal:8080")
    check("railway.internal Host: webhook delivers", status == 200, status)
    status, _ = raw_request("GET", "/health", host="tenant-abc.railway.internal")
    check("railway.internal Host: /health 200", status == 200, status)
    status, _ = raw_request("GET", "/health", host="127.0.0.1:18080")
    check("IP-literal Host: /health 200", status == 200, status)
    status, _ = raw_request("GET", "/health", host="healthcheck.railway.app")
    check("Railway healthcheck Host: /health 200", status == 200, status)
finally:
    proc.terminate()
    proc.wait(timeout=10)
    adapter6.shutdown()
    adapter6.server_close()

print("== /internal/llm-connected: poller delegation endpoint ==")
# The ChatGPT device-flow poller runs under the gateway's scrubbed tool env
# (no TELEGRAM_BOT_TOKEN), so it delegates the conversion pipeline HERE — the
# shim inherits supervisord's env, which has the token (live 2026-08-19). This
# section proves: loopback POST 202 + the pipeline genuinely runs (observable
# fake `hermes send`), unknown provider 400 + no run, public Host 403.
import tempfile  # noqa: E402

sys.path.insert(0, str(IMAGE_ROOT / "bin"))
import squire_autopair  # noqa: E402  - reuse production store shape exactly

DELEG_OWNER = "445566778"
deleg_home = tempfile.mkdtemp()
squire_autopair._atomic_write_0600(
    squire_autopair.approved_path(deleg_home),
    {DELEG_OWNER: {"user_name": "owner", "approved_at": 1.0}},
)
# Pipeline touches config.yaml (model switch) and .env — seed both.
open(os.path.join(deleg_home, "config.yaml"), "w").write(
    'model: "anthropic:claude-sonnet-5"\n')
open(os.path.join(deleg_home, ".env"), "w").close()

deleg_bindir = tempfile.mkdtemp()
deleg_argv = os.path.join(deleg_bindir, "hermes-argv")
deleg_hermes = os.path.join(deleg_bindir, "hermes")
open(deleg_hermes, "w").write(
    "#!/bin/sh\n"
    'for a in "$@"; do printf "%s\\000" "$a" >> "' + deleg_argv + '"; done\n'
    "exit 0\n"
)
os.chmod(deleg_hermes, 0o755)

proc = run({
    "SQUIRE_PUBLIC_DOMAIN": PUB,          # so the Host gate is live too
    "SQUIRE_HERMES_BIN": deleg_hermes,
    "SQUIRE_SUPERVISORCTL": "/bin/true",
    "CONTROL_API_URL": "",
    "HERMES_HOME": deleg_home,
    "SQUIRE_STATE_DIR": os.path.join(deleg_home, ".squire"),
})
try:
    # Loopback + private Host -> accepted, and the pipeline PROVABLY runs:
    # the fake hermes records its argv from inside the shim's thread.
    status, _ = raw_request("POST", "/internal/llm-connected",
                            {"provider": "chatgpt"})
    check("loopback delegation -> 202", status == 202, status)
    deadline = time.time() + 10
    argv = None
    while time.time() < deadline:
        if os.path.exists(deleg_argv):
            raw = open(deleg_argv, "rb").read().split(b"\x00")
            argv = [p.decode() for p in raw if p]
            break
        time.sleep(0.2)
    check("pipeline ran in the shim: hermes send invoked", argv is not None, argv)
    if argv:
        check("…addressed to the approved owner",
              argv[:3] == ["send", "--to", f"telegram:{DELEG_OWNER}"], argv[:3])

    # Garbage provider: rejected, and no second pipeline fires.
    os.unlink(deleg_argv)
    status, _ = raw_request("POST", "/internal/llm-connected",
                            {"provider": "evil"})
    check("unknown provider -> 400", status == 400, status)
    time.sleep(1.0)
    check("unknown provider -> pipeline did NOT run",
          not os.path.exists(deleg_argv), "argv file exists")

    # The public domain must not expose the trigger: forged Host -> 403.
    status, _ = raw_request("POST", "/internal/llm-connected",
                            {"provider": "chatgpt"}, host=PUB)
    check("public Host -> 403", status == 403, status)
    check("public Host -> pipeline did NOT run",
          not os.path.exists(deleg_argv), "argv file exists")
finally:
    proc.terminate()
    proc.wait(timeout=10)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL SHIM TESTS PASS")
