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

!! UNVERIFIED ASSUMPTION, AND AN EXPENSIVE ONE IF WRONG !!
The paragraph above assumes Railway's sleep is driven by INBOUND request
idleness, so our outbound beat does not keep the container awake. If sleep is
instead triggered by any process/network activity, then beating every 5 minutes
means no tenant ever sleeps — and this monitoring feature would silently destroy
the unit economics (Gate G1) it exists to measure. Verify on the first live
deploy: leave one tenant idle past the sleep window and confirm from Railway
usage that it slept. If it did not, raise SQUIRE_HEARTBEAT_INTERVAL above the
sleep window or gate beats on activity since the last one. See the matching note
in tenant-image/Dockerfile.

AWAKE UNTIL BOUND (fix/awake-until-bound)
-----------------------------------------
Live testing (2026-08) settled the "unverified assumption" above the expensive
way: outbound beats DO count as activity for Railway's serverless sleep, which
is why provisioned tenants carry SQUIRE_HEARTBEAT_INTERVAL=1800 — beat slower
than the ~10 min quiet window so the tenant can sleep. But that same setting
put a freshly provisioned tenant to sleep ~13 min after boot, BEFORE its owner
ever tapped the Telegram deep link — so the owner's very first message ate a
~15 s cold-start on top of LLM latency, and the silence read as "it didn't
work". A tenant that has never met its owner must not be asleep.

So the cadence is now bind-aware: while the approved-owner store is EMPTY
(nobody has tapped the link), beat every min(configured, 300) seconds — that
outbound traffic keeps the container warm, so the first tap lands instantly.
The moment an owner is bound (checked cheaply every loop iteration, so a bind
mid-life flips the cadence without a restart), the configured slow interval
applies unchanged and the tenant earns its sleep. SQUIRE_UNBOUND_AWAKE_HOURS
caps the warm window so an abandoned signup cannot burn container-hours
forever.

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
SQUIRE_HEARTBEAT_INTERVAL    seconds between beats once an owner is bound
                             (default 300; provisioned tenants get 1800)
SQUIRE_UNBOUND_AWAKE_HOURS   how long an owner-less tenant stays on the fast
                             cadence before giving up and sleeping (default 48)
HERMES_HOME                  mounted volume; where the autopair approved-owner
                             store lives (default /opt/data)
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

# --- Awake-until-bound cadence ----------------------------------------------
# While no owner is bound, beat at least this often. Railway's serverless sleep
# engages after ~10 minutes of quiet and outbound traffic resets that clock
# (verified live 2026-08 — see AWAKE UNTIL BOUND in the module docstring), so a
# beat every <=5 minutes keeps an owner-less tenant warm for its first tap.
UNBOUND_BEAT_CEILING = 300

# Runaway cap: an abandoned signup must not keep a container awake forever.
# 48h default = a generous trial window for a slow owner to tap the link, after
# which the tenant falls back to the configured slow cadence and sleeps (wake-
# on-tap still works via the shim's probe fix). The product answer for owners
# who never said hello is the Phase-1B "never-said-hello" email nudge
# (docs/implementation-plan.md §1B), not container-hours burned on standby.
UNBOUND_AWAKE_HOURS = _env_number("SQUIRE_UNBOUND_AWAKE_HOURS", 48, float)

# Same `or` form as the shim: HERMES_HOME set-but-empty must not silently point
# the store lookup at a relative path.
HERMES_HOME = os.environ.get("HERMES_HOME") or "/opt/data"

# "Bound" is decided exactly the way the autopair machinery decides it: the
# approved-owner store on the volume. Import squire_autopair by path (same
# pattern as squire-webhook-shim.py) rather than trusting cwd — supervisord
# starts us from an unspecified directory. If the import fails we treat the
# tenant as BOUND, i.e. the configured slow cadence: fail toward the cheap
# mode (tenant sleeps, wake-on-tap still works), never toward burn.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from squire_autopair import approved_path, load_approved
except ImportError:  # pragma: no cover - defensive
    approved_path = None
    load_approved = None

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
    "llm_connected",
}

#: control-api's HeartbeatRequest caps image_ref at 512 characters. One
#: over-long value would 422 the ENTIRE beat -- losing the liveness signal and
#: every counter with it -- so truncate rather than let a malformed reference
#: blind the fleet view for that tenant. A ref this long is already broken; the
#: heartbeat is not the place to find out.
MAX_IMAGE_REF_LEN = 512

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


def llm_connected() -> bool:
    """True once the tenant holds its owner's own LLM credential (1C).

    The RECONCILIATION BACKSTOP for the connect flow: if the pipeline's
    /internal/llm-connected call was lost, control-api sees this flag against
    a still-active trial key on the next beat and revokes it then.

    Privacy discipline: this reads credential FILES, which nothing else in
    this program does — so what it may extract is pinned hard. It checks key
    NAMES against the same marker set squire-hindsight-env.sh uses, plus the
    auth.json token artifact the .env-only markers famously miss, and emits
    ONE BOOLEAN. No value, length, or prefix ever leaves this function.
    """
    markers = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    try:
        with open(os.path.join(HERMES_HOME, ".env"), encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.partition("=")
                if sep and key.strip() in markers and value.strip():
                    return True
    except OSError:
        pass
    try:
        with open(os.path.join(HERMES_HOME, "auth.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        tokens = data.get("tokens") if isinstance(data, dict) else None
        if isinstance(tokens, dict) and tokens.get("access_token"):
            return True
    except (OSError, ValueError):
        pass
    return False


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
        "llm_connected": llm_connected(),
    }
    if IMAGE_REF:
        payload["image_ref"] = IMAGE_REF[:MAX_IMAGE_REF_LEN]

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


# ---------------------------------------------------------------------------
# Cadence — fast while unbound, configured once an owner exists
# ---------------------------------------------------------------------------


def owner_bound() -> bool:
    """True once the approved-owner store has at least one entry.

    Cheap (one small file read) because run_loop calls this every iteration: a
    bind mid-life must flip the cadence without a restart. Any doubt — import
    failed, store unreadable/corrupt — reads as BOUND so we fail toward the
    cheap slow cadence, never toward keeping a container awake on bad data.
    """
    if approved_path is None or load_approved is None:
        return True
    try:
        # load_approved returns {} for a missing file (the genuinely-unbound
        # case) but RAISES on an unreadable or corrupt one — exactly the
        # distinction we want, since only a positively-empty store means fast.
        return bool(load_approved(approved_path(HERMES_HOME)))
    except Exception:
        return True


def current_interval() -> int:
    """Seconds until the next beat, re-decided before every wait.

    Bound: the configured interval, unchanged. Unbound: min(configured, 300)
    so the tenant stays warm for its owner's first tap — until container
    uptime passes SQUIRE_UNBOUND_AWAKE_HOURS, after which the configured slow
    interval applies and the abandoned signup is allowed to sleep. Uptime is
    this process's clock; supervisord starts it at container boot, and a
    heartbeat restart resetting the window is an acceptable coarseness.
    """
    if owner_bound():
        return INTERVAL
    if time.monotonic() - START_MONOTONIC > UNBOUND_AWAKE_HOURS * 3600:
        return INTERVAL
    return min(INTERVAL, UNBOUND_BEAT_CEILING)


def run_loop(stop: threading.Event) -> None:
    """Beat until stopped, re-checking the bound-aware cadence each iteration.

    Extracted from main() so the tests can drive it with a fake stop event and
    observe the chosen delays without real sleeping.
    """
    # First beat immediately: the upgrade drill's whole verification depends on
    # a freshly redeployed tenant reporting its image within seconds, not
    # within a full interval.
    beat()
    last_interval = None
    while not stop.is_set():
        interval = current_interval()
        if interval != last_interval:
            # One line per cadence change, not per beat: this is the live
            # signal that an owner bound (fast -> slow) or the unbound cap
            # kicked in.
            log(f"cadence: beating every ~{interval}s")
            last_interval = interval
        # Jitter so a fleet that all redeployed together does not then beat in
        # lockstep forever after. +/-10% is enough to smear the herd.
        delay = interval * random.uniform(0.9, 1.1)
        if stop.wait(delay):
            break
        beat()


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

    log(f"beating to {CONTROL_API_URL}/internal/heartbeat "
        f"(bound interval {INTERVAL}s, unbound ceiling {UNBOUND_BEAT_CEILING}s)")
    run_loop(stop)

    log("stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
