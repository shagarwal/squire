#!/opt/squire/venv/bin/python
"""Squire tenant webhook shim — owns $PORT, forwards Telegram updates inward.

The container contract other Squire services are built against is:

    POST http://<tenant>:$PORT/webhook/telegram
    body = the raw Telegram update JSON, forwarded verbatim by our ingress

Hermes' Telegram adapter can absolutely serve webhooks itself — but it derives
its listen path from TELEGRAM_WEBHOOK_URL, which is the *public ingress* URL and
therefore carries whatever path the ingress uses (e.g. /<bot_id>). Those two
paths cannot both be `/webhook/telegram` in a per-tenant ingress scheme. The
task brief's stated preference is configuration over patching, and its stated
fallback is "add a minimal shim if the adapter's path is not configurable" —
this is that shim. ~150 lines of stdlib, no hermes source touched.

It also buys three things worth having on their own:

  * a stable /health endpoint that answers during a cold start, before the
    gateway (which takes tens of seconds) is listening, plus a loopback-only
    /metrics endpoint carrying counts-by-outcome for the Task 0.6 heartbeat;
  * the X-Telegram-Bot-Api-Secret-Token header is stamped on the forwarded
    request, so python-telegram-bot's mandatory secret check passes regardless
    of whether our ingress preserves inbound headers; and
  * one public port instead of two — the adapter binds 127.0.0.1 only.

Environment
-----------
PORT                            listen port (default 8080)
SQUIRE_WEBHOOK_PATH             inbound path (default /webhook/telegram)
SQUIRE_TELEGRAM_UPSTREAM_PATH   adapter's local path; set by the entrypoint
                                from TELEGRAM_WEBHOOK_URL. Unset => the adapter
                                is polling, and we answer 503 on the hook.
TELEGRAM_WEBHOOK_PORT           adapter's local port (default 8443)
TELEGRAM_WEBHOOK_SECRET         stamped onto forwarded requests
SQUIRE_WEBHOOK_REQUIRE_AUTH     "true" => reject unauthenticated posts (see below)
INTERNAL_API_TOKEN              shared secret with ingress, if it sends one
SQUIRE_AUTOPAIR                 "false" => never bind an owner on first contact;
                                fall back to upstream's pairing-code ceremony.
                                Default true. Note that binding ALWAYS requires
                                an authenticated request regardless of this flag
                                and of SQUIRE_WEBHOOK_REQUIRE_AUTH.
HERMES_HOME                     tenant state root (default /opt/data); the
                                pairing store written by autopair lives under it
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_PATH = os.environ.get("SQUIRE_WEBHOOK_PATH", "/webhook/telegram")
UPSTREAM_PATH = os.environ.get("SQUIRE_TELEGRAM_UPSTREAM_PATH", "")
UPSTREAM_HOST = os.environ.get("SQUIRE_TELEGRAM_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("TELEGRAM_WEBHOOK_PORT", "8443"))
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
INTERNAL_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "")
TENANT_ID = os.environ.get("TENANT_ID", "")

# --- Trust-on-first-use owner binding --------------------------------------
# The shim is the only thing that sees a Telegram update BEFORE hermes does,
# which is exactly what this needs: the owner must be approved by the time the
# gateway authorizes THIS update, or the first contact gets a pairing code
# instead of a greeting. See squire_autopair.py for the full rationale and the
# security semantics. HERMES_HOME is the mounted volume, so the binding is
# durable. SQUIRE_AUTOPAIR=false restores upstream's pairing-code ceremony.
HERMES_HOME = os.environ.get("HERMES_HOME") or "/opt/data"
# `or "true"` rather than a get() default: an env var that is SET BUT EMPTY
# (trivially produced by `-e SQUIRE_AUTOPAIR=` or an unset shell variable in a
# compose file) would otherwise read as "" and silently disable owner binding.
# The symptom — every tenant asking for a pairing code — looks nothing like its
# cause. Same reasoning for HERMES_HOME above, where an empty value would send
# the pairing store to a relative path.
AUTOPAIR_ENABLED = (os.environ.get("SQUIRE_AUTOPAIR") or "true").lower() in (
    "1", "true", "yes", "on",
)

# Import by path rather than relying on cwd: supervisord starts us from an
# unspecified directory, and a silent ImportError here would mean every tenant
# quietly falls back to the pairing gate.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from squire_autopair import defang_start_command, maybe_bind_owner
except ImportError:  # pragma: no cover - defensive
    maybe_bind_owner = None
    defang_start_command = None

# Telegram updates are small; the largest realistic body is a long message with
# a big entity list. 2 MiB is generous and stops an unbounded read.
MAX_BODY = 2 * 1024 * 1024
UPSTREAM_TIMEOUT = 30.0

# Alpha default: FALSE.
#
# The tenant's port is reachable only over Railway private networking, and the
# ingress service is being built in parallel — we do not yet know whether it
# forwards Telegram's secret-token header or sends one of its own. Failing
# closed here would mean a tenant that silently drops every message until the
# two services agree. Flip this to true (and have ingress send either
# X-Telegram-Bot-Api-Secret-Token or X-Squire-Internal-Token) as part of Gate G2
# in docs/implementation-plan.md §4 — it is listed there as secrets hardening.
REQUIRE_AUTH = os.environ.get("SQUIRE_WEBHOOK_REQUIRE_AUTH", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def log(msg: str) -> None:
    # Never log request bodies. The ingress is body-non-logging by design
    # (PRD §4) and the tenant must not become the place where message content
    # leaks into a platform log aggregator.
    print(f"[squire-webhook] {msg}", flush=True)


# --- Outcome counters (Task 0.6 heartbeat) ----------------------------------
# The shim is the only place in the container that sees every inbound Telegram
# update, so it is where "how is this tenant actually doing" is countable. Three
# COUNTS, by outcome, and nothing else: no chat ids, no per-user breakdown, no
# timing histogram keyed by anything identifying. squire-heartbeat.py scrapes
# GET /metrics and forwards these to control-api.
#
# In memory only, cumulative since this process started. They reset on every
# restart, which is why the heartbeat sends uptime alongside them.
_COUNTERS = {
    "updates_forwarded": 0,  # delivered to the gateway, which accepted it
    "updates_failed": 0,  # we tried and could not deliver (gateway down, 5xx, polling)
    "updates_rejected": 0,  # refused before delivery (bad auth, malformed request)
}
_COUNTERS_LOCK = threading.Lock()


def count(name: str) -> None:
    with _COUNTERS_LOCK:
        _COUNTERS[name] += 1


def counters_snapshot() -> dict:
    with _COUNTERS_LOCK:
        return dict(_COUNTERS)


def _authorised(headers) -> bool:
    """True if the request carries a credential we recognise.

    Two accepted forms, so either ingress design works:
      * X-Telegram-Bot-Api-Secret-Token — Telegram's own header, preserved by a
        verbatim-forwarding ingress;
      * X-Squire-Internal-Token — our own service-to-service token.
    """
    if WEBHOOK_SECRET and headers.get("X-Telegram-Bot-Api-Secret-Token") == WEBHOOK_SECRET:
        return True
    if INTERNAL_TOKEN and headers.get("X-Squire-Internal-Token") == INTERNAL_TOKEN:
        return True
    return False


def _is_loopback(client_address) -> bool:
    """True when the peer is this container itself.

    Covers all of 127.0.0.0/8, ::1 and the IPv4-mapped ::ffff:127.0.0.1 form a
    dual-stack listener reports. Anything unparseable is treated as NOT loopback:
    the safe default for an access check is to deny.
    """
    try:
        host = ipaddress.ip_address(client_address[0])
    except (ValueError, IndexError, TypeError):
        return False
    if host.is_loopback:
        return True
    mapped = getattr(host, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _gateway_listening() -> bool:
    """Cheap TCP probe of the adapter's webhook listener."""
    try:
        with socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=1.0):
            return True
    except OSError:
        return False


class Handler(BaseHTTPRequestHandler):
    # Telegram/ingress speak HTTP/1.1; without this the stdlib replies 1.0 and
    # closes the connection on every update.
    protocol_version = "HTTP/1.1"
    server_version = "squire-webhook/1.0"

    # Without a timeout, a client that opens a keep-alive connection and then
    # goes quiet pins a handler thread forever. Under HTTP/1.1 every caller gets
    # keep-alive by default, so a flaky network between ingress and the tenant
    # would slowly consume the thread pool until the shim stops answering —
    # while /health, on a fresh connection, still looked fine.
    timeout = 30

    # Silence BaseHTTPRequestHandler's default stderr access log — it prints the
    # request line, which for us is fine, but it also runs on malformed input
    # and is noisy. We log deliberately instead.
    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        return

    def _respond(self, status: int, payload: dict, extra_headers=()):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path.split("?", 1)[0] in ("/health", "/healthz"):
            self._respond(
                200,
                {
                    "status": "ok",
                    "tenant": TENANT_ID or None,
                    "mode": "webhook" if UPSTREAM_PATH else "polling",
                    # Reported, not gated on: the shim must answer 200 while the
                    # gateway is still booting, or a platform health probe kills
                    # the container mid-cold-start.
                    "gateway_listening": _gateway_listening(),
                },
            )
            return
        if self.path.split("?", 1)[0] == "/metrics":
            # LOOPBACK (or an authenticated caller) ONLY.
            #
            # This listener is bound to 0.0.0.0, and every tenant in the project
            # shares one Railway private network — so an unauthenticated /metrics
            # here would let tenant B read tenant A's message volume and activity
            # pattern. That is a cross-tenant leak of behavioural data, and the
            # fact that it is "only counts" does not make it theirs to read.
            #
            # /health stays open because a platform probe may reach it from
            # anywhere and it carries no per-tenant activity. /metrics has no
            # such requirement: squire-heartbeat.py scrapes it over 127.0.0.1.
            if not (_is_loopback(self.client_address) or _authorised(self.headers)):
                log("rejected non-loopback /metrics request")
                self._respond(403, {"error": "forbidden"})
                return
            self._respond(200, counters_snapshot())
            return
        self._respond(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]
        if path != WEBHOOK_PATH:
            self._respond(404, {"error": "not found"})
            return

        if REQUIRE_AUTH and not _authorised(self.headers):
            log("rejected unauthenticated webhook POST")
            count("updates_rejected")
            self._respond(401, {"error": "unauthorised"})
            return

        if not UPSTREAM_PATH:
            # Adapter is in polling mode — there is nothing to forward to.
            count("updates_failed")
            self._respond(
                503,
                {"error": "tenant is not in webhook mode (TELEGRAM_WEBHOOK_URL unset)"},
                extra_headers=(("Retry-After", "5"),),
            )
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            count("updates_rejected")
            self._respond(400, {"error": "bad Content-Length"})
            return
        if length <= 0 or length > MAX_BODY:
            count("updates_rejected")
            self._respond(413, {"error": "body missing or too large"})
            return

        body = self.rfile.read(length)

        # Bind the owner BEFORE forwarding. Ordering is the whole trick: upstream's
        # PairingStore.is_approved() re-reads the approved JSON on every call with
        # no caching, so a write completed here is visible to the gateway while it
        # authorizes this very update. Do it after forwarding and the first contact
        # gets a pairing code; do it on a timer and it gets one too.
        #
        # Never fatal, and never blocks delivery: maybe_bind_owner swallows its own
        # errors, and a failure here just means the tenant pairs the upstream way.
        # AUTHENTICATION IS MANDATORY FOR BINDING, regardless of REQUIRE_AUTH.
        #
        # REQUIRE_AUTH governs DELIVERY and stays false: an unauthenticated POST
        # is still forwarded, because refusing delivery on a header disagreement
        # would silently break every tenant. Binding is a different matter.
        #
        # Trust-on-first-use turns "can reach this port" into "is the permanent
        # owner of this tenant". Before autopair, forging an update to a
        # not-yet-bound tenant got you a useless pairing code; with it, it is
        # account takeover, and the real customer has no shell to recover from
        # it. So an unauthenticated update may be delivered but may NEVER bind:
        # it degrades to upstream's pairing-code path, which is visible to the
        # owner and recoverable by an operator.
        #
        # The credential is the PER-BOT secret (ingress re-stamps it on every
        # forward). Deliberately not the fleet-wide INTERNAL_API_TOKEN: every
        # trial tenant's own agent can read that out of its environment, so it
        # is exactly the credential an attacker already holds.
        may_bind = AUTOPAIR_ENABLED and maybe_bind_owner is not None and _authorised(self.headers)
        if AUTOPAIR_ENABLED and maybe_bind_owner is not None and not may_bind:
            # Loud, because this is either an attack or a misconfigured ingress,
            # and the symptom the user reports ("it asked me for a pairing code")
            # is otherwise indistinguishable between the two.
            log("unauthenticated update: NOT binding owner (falling back to "
                "upstream pairing). If this is a real first contact, ingress is "
                "not stamping X-Telegram-Bot-Api-Secret-Token.")

        if may_bind:
            try:
                update = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                update = None
            if isinstance(update, dict):
                bound = maybe_bind_owner(HERMES_HOME, update)
                if bound:
                    # Only on the update that bound the owner. Upstream swallows
                    # "/start" as a platform ping (run.py:14961 returns ""), so
                    # without this the deep-link tap authorizes the owner and
                    # then answers with silence — worse than the pairing code it
                    # replaced. See defang_start_command for why this is a slash
                    # strip and not a synthesised greeting.
                    if defang_start_command(update):
                        body = json.dumps(update).encode("utf-8")
                        log("first contact: /start relayed as text so the "
                            "concierge greeting can fire")
                    # Deliberately does NOT log the Telegram user id. It is the
                    # tenant owner's account identifier, this output goes to a
                    # shared log aggregator, and the heartbeat holds no user ids
                    # by design — this must not become the exception.
                    # Logged, deliberately NOT counted. _COUNTERS is a fixed
                    # three-outcome set that squire-heartbeat.py forwards to
                    # control-api; adding a key here would both KeyError and
                    # widen the metrics contract to report a per-tenant lifecycle
                    # event that control-api has no need to know.
                    log("first contact: bound tenant owner (id redacted)")

        conn = None
        try:
            conn = http.client.HTTPConnection(
                UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT
            )
            headers = {
                "Content-Type": self.headers.get("Content-Type", "application/json"),
                "Content-Length": str(len(body)),
            }
            # Stamp the secret the adapter registered with setWebhook. PTB
            # compares this against its own secret_token and 403s on mismatch,
            # so this is what makes a verbatim-forwarded update accepted.
            if WEBHOOK_SECRET:
                headers["X-Telegram-Bot-Api-Secret-Token"] = WEBHOOK_SECRET
            conn.request("POST", UPSTREAM_PATH, body=body, headers=headers)
            resp = conn.getresponse()
            resp.read()  # drain so the connection can close cleanly
            status = resp.status
        except (OSError, http.client.HTTPException) as exc:
            # The gateway is still booting, restarting, or wedged. 503 +
            # Retry-After lets the caller (ingress, or Telegram itself) redeliver
            # rather than dropping the user's message on the floor.
            log(f"upstream unavailable: {exc.__class__.__name__}: {exc}")
            count("updates_failed")
            self._respond(
                503,
                {"error": "gateway not ready"},
                extra_headers=(("Retry-After", "2"),),
            )
            return
        finally:
            # Always close, including on the error path. Without this a run of
            # upstream failures leaks a socket per request until the shim runs
            # out of file descriptors — which happens precisely during a cold
            # start or a gateway crash loop, i.e. when the shim is the only
            # thing still answering.
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        if status >= 400:
            log(f"upstream returned {status}")
            count("updates_failed")
        else:
            count("updates_forwarded")
        self._respond(200 if status < 400 else status, {"ok": status < 400})


def main() -> int:
    if not WEBHOOK_SECRET:
        # Not fatal for /health, and the entrypoint always generates one, so
        # this only fires if the shim is run standalone.
        log("warning: TELEGRAM_WEBHOOK_SECRET is empty — forwarded requests will be unsigned")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log(
        f"listening on 0.0.0.0:{PORT}{WEBHOOK_PATH} -> "
        f"{UPSTREAM_HOST}:{UPSTREAM_PORT}{UPSTREAM_PATH or ' (polling mode)'} "
        f"require_auth={REQUIRE_AUTH}"
    )

    # Shut down cleanly on SIGTERM so supervisord's stop is quick.
    stop = threading.Event()

    def _shutdown(_signum, _frame):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    import signal

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
