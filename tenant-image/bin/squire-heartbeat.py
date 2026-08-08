#!/opt/squire/venv/bin/python
"""Squire tenant heartbeat — counts-only fleet telemetry (Task 0.6).

Every SQUIRE_HEARTBEAT_INTERVAL seconds this posts a small bag of integers to
`POST $CONTROL_API_URL/internal/heartbeat` with the shared internal bearer token.
It is the only thing in this container that talks to the control plane on a timer,
and it is deliberately the dullest program in the image.

WHAT IT SENDS, AND WHAT IT CANNOT SEND
--------------------------------------
Counts and gauges. Update counts by outcome (scraped from the webhook shim's
/metrics), Hindsight queue depths by status, container RSS, volume usage, uptime,
two liveness booleans, and the image reference the container was handed.

There is no code path here that reads a message, a chat id, a user id, a file from
the volume, or an environment secret. That is a property of this file, but it is
not left to trust: control-api's `HeartbeatRequest` sets `extra="forbid"`, so a
field that is not on the agreed whitelist is a 422 rather than a new column.
`PAYLOAD_FIELDS` below is this side of that same contract.

WHY THE TENANT PUSHES INSTEAD OF THE CONTROL PLANE POLLING
----------------------------------------------------------
Tenants sleep (Railway serverless scale-to-zero is the Gate G1 economics bet).
Polling a sleeping tenant would wake it, which would defeat the thing being
measured. A push costs one request per interval from a container that is awake
anyway, and silence is itself the signal — `/fleet` reports a missing heartbeat.

EVERY COLLECTOR FAILS INDEPENDENTLY
-----------------------------------
Each one returns None on any error and the field is simply omitted. A tenant that
cannot read its cgroup must still report that it is alive: otherwise the first
thing to break also removes the signal telling us something broke.

Environment
-----------
CONTROL_API_URL              control plane base URL (unset => emitter disabled)
INTERNAL_API_TOKEN           shared service-to-service bearer (unset => disabled)
TENANT_ID                    identifies the tenant (unset => disabled)
SQUIRE_IMAGE_REF             image this container is running; set by control-api's
                             redeploy endpoint, and what the upgrade drill verifies
SQUIRE_HEARTBEAT_INTERVAL    seconds between beats (default 300)
PORT                         webhook shim's port, scraped for update counters
HINDSIGHT_API_PORT           memory daemon health port (default 9177)
SQUIRE_VOLUME                mounted volume, for the disk gauge (default /opt/data)
SQUIRE_STATE_DIR             where squire-backup.sh records its last success

Malformed numeric values fall back to the defaults with a warning rather than
raising at import: a crash here is a supervisord restart loop, and telemetry is
the last thing that should be able to cause one.

Usage: squire-heartbeat.py [--once]
"""

from __future__ import annotations

import json
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

def log(msg: str) -> None:
    print(f"[squire-heartbeat] {msg}", flush=True)


def _env_number(name: str, default, cast):
    """Read a numeric environment variable, falling back loudly on garbage.

    A typo'd `PORT=80 80` used to be a ValueError at import time -- which, under
    supervisord, is a program that dies before it can log anything useful and then
    restarts forever. Telemetry is the last thing that should be able to do that,
    so a malformed value logs once and uses the default.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        log(f"warning: {name}={raw!r} is not a number — using {default}")
        return default
    if value <= 0:
        log(f"warning: {name}={raw!r} must be positive — using {default}")
        return default
    return value


CONTROL_API_URL = os.environ.get("CONTROL_API_URL", "").rstrip("/")
INTERNAL_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "")
TENANT_ID = os.environ.get("TENANT_ID", "")
IMAGE_REF = os.environ.get("SQUIRE_IMAGE_REF", "")

INTERVAL = _env_number("SQUIRE_HEARTBEAT_INTERVAL", 300, int)
HTTP_TIMEOUT = _env_number("SQUIRE_HEARTBEAT_TIMEOUT", 15.0, float)

SHIM_PORT = _env_number("PORT", 8080, int)
SHIM_HOST = os.environ.get("SQUIRE_HEARTBEAT_SHIM_HOST", "127.0.0.1")
HINDSIGHT_PORT = _env_number("HINDSIGHT_API_PORT", 9177, int)
GATEWAY_PORT = _env_number("TELEGRAM_WEBHOOK_PORT", 8443, int)
VOLUME = os.environ.get("SQUIRE_VOLUME", "/opt/data")
STATE_DIR = os.environ.get("SQUIRE_STATE_DIR", f"{VOLUME}/.squire")
BACKUP_SUCCESS_FILE = os.environ.get(
    "SQUIRE_BACKUP_SUCCESS_FILE", f"{STATE_DIR}/last-backup-success"
)

HINDSIGHT_PY = os.environ.get(
    "SQUIRE_HINDSIGHT_PYTHON", "/opt/squire/hindsight-venv/bin/python"
)
PG_DSN = os.environ.get(
    "SQUIRE_HINDSIGHT_PG_DSN",
    "postgresql://hindsight:hindsight@127.0.0.1:5432/hindsight",
)

#: The wire contract with control-api's `HeartbeatRequest`. Anything not in this
#: set is dropped before the request is built — so a collector that starts
#: returning something unexpected cannot widen what leaves the container.
PAYLOAD_FIELDS = {
    "tenant_id",
    "uptime_seconds",
    "image_ref",
    "gateway_up",
    "hindsight_up",
    "memory_rss_mb",
    "volume_used_mb",
    "volume_total_mb",
    "updates_forwarded",
    "updates_failed",
    "updates_rejected",
    "hindsight_ops_pending",
    "hindsight_ops_processing",
    "hindsight_ops_failed",
    "backup_last_success_age_seconds",
}

START_MONOTONIC = time.monotonic()


# ---------------------------------------------------------------------------
# Collectors — each returns None (or a partial dict) rather than raising
# ---------------------------------------------------------------------------


def _get_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:
            if not 200 <= response.status < 300:
                return None
            body = json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def shim_counters() -> dict:
    """Update counts by outcome, from the webhook shim's /metrics."""
    body = _get_json(f"http://{SHIM_HOST}:{SHIM_PORT}/metrics") or {}
    return {
        name: int(body[name])
        for name in ("updates_forwarded", "updates_failed", "updates_rejected")
        if isinstance(body.get(name), int)
    }


def hindsight_up() -> bool:
    return _get_json(f"http://127.0.0.1:{HINDSIGHT_PORT}/health") is not None


def gateway_up() -> bool:
    """TCP probe of the Telegram adapter's local listener.

    Same cheap check the shim makes. In polling mode the adapter does not listen,
    so this reads False on a perfectly healthy dev container — which is fine,
    because provisioned tenants are always in webhook mode.
    """
    try:
        with socket.create_connection(("127.0.0.1", GATEWAY_PORT), timeout=2.0):
            return True
    except OSError:
        return False


def memory_rss_mb() -> int | None:
    """Container-wide memory from the cgroup, not this process's RSS.

    The number that matters for the G1 economics gate is what Railway bills for:
    the gateway + Hindsight + PostgreSQL together. cgroup v2 first (what Railway
    runs), v1 as a fallback, None when neither is readable.
    """
    for path in (
        "/sys/fs/cgroup/memory.current",  # v2
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",  # v1
    ):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return int(fh.read().strip()) // (1024 * 1024)
        except (OSError, ValueError):
            continue
    return None


def volume_mb() -> dict:
    """Used/total bytes on the mounted volume.

    Volume growth is the other half of the per-tenant cost question, and a volume
    that fills up is a silent tenant death (Postgres stops, secrets stop sealing).
    """
    try:
        stat = os.statvfs(VOLUME)
    except OSError:
        return {}
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bavail * stat.f_frsize
    return {
        "volume_total_mb": total // (1024 * 1024),
        "volume_used_mb": (total - free) // (1024 * 1024),
    }


def backup_age_seconds() -> int | None:
    """Seconds since the last SUCCESSFUL restic run, or None if there has not been
    one (which is every tenant today -- no B2 account exists yet).

    squire-backup.sh writes a unix timestamp to this file after each successful
    backup. A backup that quietly stops working produces no error anywhere; this
    number is the only thing that would show it, and it belongs on the heartbeat
    rather than in a log nobody reads.

    Wall clock, not monotonic: the timestamp is written by a different process and
    has to survive restarts. A clock step therefore moves this number, which is an
    acceptable trade for a value read at day-scale.
    """
    try:
        with open(BACKUP_SUCCESS_FILE, "r", encoding="utf-8") as fh:
            written = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    # Clamp at zero rather than sending a negative age: control-api's schema
    # rejects negatives (ge=0), and a clock skew of a few seconds must not cost us
    # the whole heartbeat.
    return max(0, int(time.time()) - written)


def hindsight_ops() -> dict:
    """Queue depth in Hindsight's `async_operations`, by status.

    Runs through the hindsight venv's psycopg2 in a subprocess, for exactly the
    reasons squire-hindsight-healthcheck.py does: the driver lives in the other
    venv, and a hung driver cannot then wedge this loop.

    Counts only — the query selects `count(*) ... GROUP BY status` and can return
    nothing else. A wedged queue (guide §4) shows up here as a pending count that
    stops falling, which is the earliest fleet-wide signal we have that memory
    extraction has stalled.
    """
    script = (
        "import sys, psycopg2\n"
        "conn = psycopg2.connect(sys.argv[1], connect_timeout=5)\n"
        "cur = conn.cursor()\n"
        "cur.execute(\"SELECT status, count(*) FROM async_operations GROUP BY status\")\n"
        "print(';'.join('%s=%d' % (r[0], r[1]) for r in cur.fetchall()))\n"
        "conn.close()\n"
    )
    try:
        out = subprocess.run(
            [HINDSIGHT_PY, "-c", script, PG_DSN],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if out.returncode != 0:
        # Cold container, or the schema does not exist yet. Not an error worth
        # logging every five minutes.
        return {}

    by_status: dict[str, int] = {}
    for chunk in (out.stdout or "").strip().split(";"):
        name, _, value = chunk.partition("=")
        if value.isdigit():
            by_status[name.strip().lower()] = int(value)
    return {
        "hindsight_ops_pending": by_status.get("pending", 0),
        "hindsight_ops_processing": by_status.get("processing", 0),
        "hindsight_ops_failed": by_status.get("failed", 0),
    }


# ---------------------------------------------------------------------------
# Beat
# ---------------------------------------------------------------------------


def build_payload() -> dict:
    payload: dict = {
        "tenant_id": TENANT_ID,
        "uptime_seconds": int(time.monotonic() - START_MONOTONIC),
        "gateway_up": gateway_up(),
        "hindsight_up": hindsight_up(),
    }
    if IMAGE_REF:
        payload["image_ref"] = IMAGE_REF

    rss = memory_rss_mb()
    if rss is not None:
        payload["memory_rss_mb"] = rss

    payload.update(volume_mb())
    payload.update(shim_counters())
    payload.update(hindsight_ops())

    backup_age = backup_age_seconds()
    if backup_age is not None:
        payload["backup_last_success_age_seconds"] = backup_age

    # Belt and braces against a future collector returning a surprise key: the
    # whitelist, not the collectors, decides what leaves this container.
    return {k: v for k, v in payload.items() if k in PAYLOAD_FIELDS}


def send(payload: dict) -> bool:
    """POST one beat. Returns False on any failure -- never raises, never retries.

    No retry on purpose: the next beat is a few minutes away and carries the same
    (cumulative) information, so a retry storm against a struggling control plane
    would buy nothing.
    """
    request = urllib.request.Request(
        f"{CONTROL_API_URL}/internal/heartbeat",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {INTERNAL_TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        # Log the status, never the response body: it is control-api's text, but
        # this log goes to Railway's aggregator and there is no reason to widen
        # what lands there.
        log(f"control-api rejected the heartbeat: HTTP {exc.code}")
        return False
    except (urllib.error.URLError, OSError) as exc:
        log(f"could not reach control-api: {exc.__class__.__name__}")
        return False


def beat() -> bool:
    payload = build_payload()
    ok = send(payload)
    if ok:
        log(
            "beat: up={uptime_seconds}s gateway={gateway_up} hindsight={hindsight_up} "
            "rss={rss}MB updates={fwd}/{failed}/{rejected}".format(
                uptime_seconds=payload["uptime_seconds"],
                gateway_up=payload["gateway_up"],
                hindsight_up=payload["hindsight_up"],
                rss=payload.get("memory_rss_mb", "?"),
                fwd=payload.get("updates_forwarded", "?"),
                failed=payload.get("updates_failed", "?"),
                rejected=payload.get("updates_rejected", "?"),
            )
        )
    return ok


def main(argv: list[str]) -> int:
    once = "--once" in argv[1:]

    if not (CONTROL_API_URL and INTERNAL_TOKEN and TENANT_ID):
        # Standalone `docker run` with no control plane. Exit 0 rather than
        # crash-looping: supervisord is configured with autorestart=unexpected,
        # so a clean exit leaves the program EXITED and the container healthy.
        log("heartbeat not configured (CONTROL_API_URL/INTERNAL_API_TOKEN/TENANT_ID) — idle")
        return 0

    if once:
        return 0 if beat() else 1

    stop = threading.Event()

    def _stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log(f"beating every {INTERVAL}s to {CONTROL_API_URL}/internal/heartbeat")
    # First beat immediately: the upgrade drill's whole verification depends on a
    # freshly redeployed tenant reporting its image within seconds, not within a
    # full interval.
    beat()
    while not stop.is_set():
        # Jitter so a fleet that all redeployed together does not then beat in
        # lockstep forever after. +/-10% is enough to smear the herd.
        delay = INTERVAL * random.uniform(0.9, 1.1)
        if stop.wait(delay):
            break
        beat()

    log("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
