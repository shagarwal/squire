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
    gateway (which takes tens of seconds) is listening;
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
"""

from __future__ import annotations

import http.client
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
        self._respond(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]
        if path != WEBHOOK_PATH:
            self._respond(404, {"error": "not found"})
            return

        if REQUIRE_AUTH and not _authorised(self.headers):
            log("rejected unauthenticated webhook POST")
            self._respond(401, {"error": "unauthorised"})
            return

        if not UPSTREAM_PATH:
            # Adapter is in polling mode — there is nothing to forward to.
            self._respond(
                503,
                {"error": "tenant is not in webhook mode (TELEGRAM_WEBHOOK_URL unset)"},
                extra_headers=(("Retry-After", "5"),),
            )
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, {"error": "bad Content-Length"})
            return
        if length <= 0 or length > MAX_BODY:
            self._respond(413, {"error": "body missing or too large"})
            return

        body = self.rfile.read(length)

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
            conn.close()
        except (OSError, http.client.HTTPException) as exc:
            # The gateway is still booting, restarting, or wedged. 503 +
            # Retry-After lets the caller (ingress, or Telegram itself) redeliver
            # rather than dropping the user's message on the floor.
            log(f"upstream unavailable: {exc.__class__.__name__}: {exc}")
            self._respond(
                503,
                {"error": "gateway not ready"},
                extra_headers=(("Retry-After", "2"),),
            )
            return

        if status >= 400:
            log(f"upstream returned {status}")
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
