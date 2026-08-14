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

import hmac
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
    from squire_autopair import (
        defang_start_command,
        maybe_bind_owner,
        strip_start_payload,
    )
except ImportError:  # pragma: no cover - defensive
    maybe_bind_owner = None
    defang_start_command = None
    strip_start_payload = None

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

# --- Public-domain Host gating (1C) -----------------------------------------
# Attaching a Railway public domain (for the /connect page) exposes this port
# to the internet. Everything except /connect/* must then answer ONLY for
# requests that addressed the shim by a private name. Railway injects
# RAILWAY_PUBLIC_DOMAIN once a domain exists; provisioning also writes
# SQUIRE_PUBLIC_DOMAIN explicitly so this gate cannot depend on platform
# injection timing. Unset (dev, plain docker run) => no gating at all.
PUBLIC_DOMAIN = (
    os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("SQUIRE_PUBLIC_DOMAIN") or ""
).strip().lower()


def _request_host(headers) -> str:
    """The Host header, lowercased, port stripped. '[::1]:80' handled too."""
    host = (headers.get("Host") or "").strip().lower()
    if host.startswith("["):  # bracketed IPv6
        return host.split("]", 1)[0].lstrip("[")
    return host.rsplit(":", 1)[0] if ":" in host else host


def _host_is_private(headers) -> bool:
    """True when the request addressed us by a name only the private network,
    the container itself, or Railway's own health prober would use.

    Fail-closed: with a public domain configured, an unrecognised Host is
    treated as public. An attacker reaching the public edge cannot make the
    edge route an arbitrary Host to this container, but this code must not
    rely on that — 'not provably private' is the safe reading.
    """
    host = _request_host(headers)
    if host in ("", "localhost", "healthcheck.railway.app"):
        return True
    if host.endswith(".railway.internal"):
        return True
    try:
        ipaddress.ip_address(host)
        return True  # IP literal: private-network or loopback addressing
    except ValueError:
        return False


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
    """True if the request carries a DELIVERY credential we recognise.

    Two accepted forms, so either ingress design works:
      * X-Telegram-Bot-Api-Secret-Token — Telegram's own header, preserved by a
        verbatim-forwarding ingress;
      * X-Squire-Internal-Token — our own service-to-service token.

    This gates DELIVERY only, and only when SQUIRE_WEBHOOK_REQUIRE_AUTH is on.
    The fleet-wide internal token is legitimately accepted here — losing a
    message because ingress used its own token instead of the per-bot secret is
    a worse failure than accepting it. It must NOT be used to gate ownership
    binding; that is what _authorised_for_binding exists to prevent.

    compare_digest, not ==, on both arms: this now sits next to
    ownership-adjacent logic, and a constant-time compare is the standing
    convention for bearer-shaped secrets here (matches ingress/app.py).
    """
    tg = headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
    if WEBHOOK_SECRET and hmac.compare_digest(tg, WEBHOOK_SECRET):
        return True
    internal = headers.get("X-Squire-Internal-Token") or ""
    if INTERNAL_TOKEN and hmac.compare_digest(internal, INTERNAL_TOKEN):
        return True
    return False


def _authorised_for_binding(headers) -> bool:
    """True ONLY for the per-bot Telegram secret. Never the fleet token.

    Binding an owner is trust-on-first-use: it converts "sent this request" into
    "permanently owns this tenant". The fleet-wide INTERNAL_API_TOKEN is readable
    by every trial tenant's own agent (it is in the container environment), so
    accepting it here would let any tenant's agent forge a first-contact update
    to any not-yet-bound tenant and seize it — the exact takeover this guard
    exists to stop, and the one _authorised (which accepts that token for
    delivery) cannot be reused for.

    The per-bot secret is known only to Telegram, control-api, and this one
    tenant; ingress re-stamps it on every forward. So it, and only it, may bind.

    Empty WEBHOOK_SECRET returns False: compare_digest("", "") is True, and a
    tenant with no configured secret must be un-bindable, not bindable by a
    request that also sent nothing.
    """
    tg = headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
    return bool(WEBHOOK_SECRET) and hmac.compare_digest(tg, WEBHOOK_SECRET)


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


def _gateway_listening(timeout: float = 1.0) -> bool:
    """Cheap TCP probe of the adapter's webhook listener.

    A bare connect + close: proves something is accepting on the port without
    consuming a request from the gateway's webhook queue (an HTTP request here
    would be processed as a webhook). Used by /health (reported, not gated on)
    and as the pre-side-effect guard in do_POST — the caller there passes a
    shorter timeout so a booting container answers ingress quickly.
    """
    try:
        with socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=timeout):
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
        # Public-domain gate: with a domain attached, only /connect/* is public.
        if PUBLIC_DOMAIN and not _host_is_private(self.headers):
            if not self.path.split("?", 1)[0].startswith("/connect/"):
                log("rejected public-Host GET outside /connect")
                self._respond(403, {"error": "forbidden"})
                return
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

        # Public-domain gate (see do_GET). A forged Telegram update arriving
        # via the public domain — even with a stolen delivery secret — is the
        # injection path this exists to close. Counted so probing is visible.
        if PUBLIC_DOMAIN and not _host_is_private(self.headers):
            if not path.startswith("/connect/"):
                log("rejected public-Host POST outside /connect")
                count("updates_rejected")
                self._respond(403, {"error": "forbidden"})
                return

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

        # --- Side-effect atomicity: no upstream, no side effects ------------
        # The autopair block below performs a DURABLE write (owner binding) and
        # in-place body rewrites, and only afterwards does the relay discover
        # whether the gateway is listening. On a serverless wake the user's tap
        # IS the boot trigger, so ingress routinely delivers the buffered
        # first-contact "/start <nonce>" while the gateway is still booting.
        # Without this probe the shim would bind the owner, 503 the delivery,
        # and then ingress's REDELIVERY of the same update would take the
        # non-binding path — stripping the payload and forwarding a bare
        # "/start", which upstream swallows as a platform ping. Net effect: the
        # owner's first contact produced silence (live incident, staging tenant
        # c559d1c9, 2026-08-13).
        #
        # So: probe the upstream FIRST, on every forwarded update. If it is not
        # accepting, answer the same retryable 503 the relay would have — just
        # decided before anything mutated — and let ingress redeliver. Bind,
        # rewrite, and relay then all happen on one successful delivery. The
        # existing post-relay 503 path stays as the backstop for the
        # probe-passed-then-died race, whose window this shrinks from the whole
        # boot (~30s) to milliseconds. Body already read above: required to
        # keep the HTTP/1.1 keep-alive connection parseable, and harmless.
        if not _gateway_listening(timeout=0.5):
            log("upstream not ready, declining before side effects")
            count("updates_failed")
            self._respond(
                503,
                {"error": "gateway not ready"},
                extra_headers=(("Retry-After", "2"),),
            )
            return

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
        # The credential is the PER-BOT secret ONLY — _authorised_for_binding,
        # NOT _authorised. _authorised also accepts the fleet-wide internal
        # token, which every trial tenant's agent can read from its environment;
        # binding on that would be the exact takeover this guards against. Using
        # the delivery check here is the bug the first re-review caught.
        may_bind = (AUTOPAIR_ENABLED and maybe_bind_owner is not None
                    and _authorised_for_binding(self.headers))
        if AUTOPAIR_ENABLED and maybe_bind_owner is not None and not may_bind:
            # Loud, because this is either an attack or a misconfigured ingress,
            # and the symptom the user reports ("it asked me for a pairing code")
            # is otherwise indistinguishable between the two.
            log("unauthenticated update: NOT binding owner (falling back to "
                "upstream pairing). If this is a real first contact, ingress is "
                "not stamping X-Telegram-Bot-Api-Secret-Token.")

        # Parse the update ONCE for the two in-place rewrites below (owner
        # binding, then payload hygiene). Parsed when binding might run — legacy
        # first contact can bind from ANY private message, not just /start — or
        # when the body could carry a /start. The bytes probe keeps the
        # overwhelmingly common case (an ordinary message on an established
        # tenant with binding not in play) unparsed and forwarded
        # byte-for-byte verbatim, which is the contract ingress is built
        # against. b"\\/start" covers the legal JSON escape of the slash;
        # Telegram itself never emits it, but the payload strip must not be
        # dodgeable by re-encoding.
        update = None
        if may_bind or b"/start" in body or b"\\/start" in body:
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                update = parsed

        bound = None
        if may_bind and update is not None:
            # maybe_bind_owner needs the raw text: when a bind nonce is
            # configured, the /start payload IS the credential it verifies —
            # so binding runs BEFORE the payload hygiene below removes it.
            bound = maybe_bind_owner(HERMES_HOME, update)
            if bound:
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

        # The /start payload must not survive into the forwarded body on ANY
        # path — bound or not. It may be the tenant's LIVE bind nonce (the
        # owner re-tapping the ?start= deep link once bound, SQUIRE_AUTOPAIR
        # off, an unreadable store, an unauthenticated delivery), and whatever
        # the gateway receives becomes a user turn in the transcript and in
        # Hindsight memory — a credential must never land there. Two rewrites,
        # mutually exclusive:
        #   * bound      -> defang: "/start <p>" becomes the text "start", so
        #     the concierge greeting can fire. Upstream swallows "/start" as a
        #     platform ping (run.py:14961 returns "") and an empty response is
        #     never sent — without this the deep-link tap authorizes the owner
        #     and then answers with silence. See defang_start_command for why
        #     this is a slash strip and not a synthesised greeting.
        #   * not bound  -> strip: "/start <p>" becomes "/start", which KEEPS
        #     upstream's swallow-the-ping semantics for an established chat. A
        #     payload-less /start is untouched, so ordinary traffic still
        #     forwards verbatim.
        mutated = False
        if update is not None:
            if bound and defang_start_command is not None:
                if defang_start_command(update):
                    mutated = True
                    log("first contact: /start relayed as text so the "
                        "concierge greeting can fire")
            elif strip_start_payload is not None and strip_start_payload(update):
                mutated = True
                # Payload and sender id deliberately absent from the log line.
                log("stripped deep-link payload from a non-binding /start "
                    "before forwarding")
        if mutated:
            body = json.dumps(update).encode("utf-8")

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
