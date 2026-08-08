#!/opt/squire/venv/bin/python
"""Hindsight wedge/OOM auto-recovery — the container port of the guide's §4 loop.

docs/reference/hindsight-optimization-guide.md §4 describes the failure mode
precisely: under memory pressure the daemon goes *alive but unresponsive*, and a
plain restart is not enough, because the pending/processing rows in
`async_operations` are recovered on boot and drive it straight back into the
same OOM. The fix is restart **plus** clearing the wedged queue.

Differences from the systemd-timer original, all forced by the container:

  * systemctl -> supervisorctl (same idea, different supervisor).
  * No `pkill -9 hindsight-api`: supervisord owns the process and stopping it
    through supervisorctl is both sufficient and safer than shooting a pid we
    do not own.
  * The stuck-task UPDATE runs through psycopg2 from the hindsight venv instead
    of shelling out to a psql whose path we would have to hunt for. This is why
    HINDSIGHT_API_DATABASE_URL pins pg0 to a fixed port in the Dockerfile — an
    auto-allocated port would leave us with no way to reach the database.

Every failure path is non-fatal: this is a janitor, and a janitor that crashes
the container would be worse than the mess it cleans up.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HEALTH_URL = "http://127.0.0.1:%s/health" % os.environ.get("HINDSIGHT_API_PORT", "9177")
SERVICE = os.environ.get("SQUIRE_HINDSIGHT_PROGRAM", "hindsight")
SUPERVISORCTL = "/opt/squire/venv/bin/supervisorctl"
SUPERVISOR_CONF = "/opt/squire/supervisord.conf"
HINDSIGHT_PY = "/opt/squire/hindsight-venv/bin/python"

# Guide: "Run it on a 2-minute timer" with a 3-minute boot delay. Hindsight's
# first start also has to initdb and run migrations, so the grace matters.
INTERVAL = int(os.environ.get("SQUIRE_HINDSIGHT_HEALTHCHECK_INTERVAL", "120"))
BOOT_GRACE = int(os.environ.get("SQUIRE_HINDSIGHT_HEALTHCHECK_BOOT_GRACE", "180"))
HEALTH_TIMEOUT = 10

# Consecutive failures before acting. The guide's timer restarted on the first
# failed probe; one extra strike costs 2 minutes and avoids restarting a daemon
# that is merely busy with a long consolidation.
FAILURES_BEFORE_RESTART = int(os.environ.get("SQUIRE_HINDSIGHT_FAILURE_THRESHOLD", "2"))

# pg0's fixed-port instance, as configured in the Dockerfile. Credentials are
# pg0's built-in defaults (hindsight/hindsight) and are only reachable on
# loopback inside this container.
PG_DSN = os.environ.get(
    "SQUIRE_HINDSIGHT_PG_DSN",
    "postgresql://hindsight:hindsight@127.0.0.1:5432/hindsight",
)

CLEAR_STUCK_SQL = """
UPDATE async_operations
SET status = 'failed',
    error_message = 'auto-cleared by squire healthcheck after OOM/wedge'
WHERE status IN ('pending', 'processing');
"""


def log(msg: str) -> None:
    print(f"[hindsight-healthcheck] {msg}", flush=True)


def healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=HEALTH_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def clear_stuck_tasks() -> None:
    """Fail the wedged queue so the restarted daemon does not re-enter the OOM.

    Runs in the hindsight venv (psycopg2 lives there, not in the control venv)
    via a subprocess, which also means a hung driver cannot wedge this loop.
    """
    script = (
        "import sys, psycopg2\n"
        "conn = psycopg2.connect(sys.argv[1], connect_timeout=10)\n"
        "conn.autocommit = True\n"
        "cur = conn.cursor()\n"
        "cur.execute(sys.argv[2])\n"
        "print(cur.rowcount)\n"
        "conn.close()\n"
    )
    try:
        out = subprocess.run(
            [HINDSIGHT_PY, "-c", script, PG_DSN, CLEAR_STUCK_SQL],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"could not clear stuck tasks ({exc}) — restarting anyway")
        return

    if out.returncode != 0:
        # Very common and benign on a cold container: pg0 is not up yet, or the
        # schema has never been created. Restarting is still the right move.
        log(f"stuck-task cleanup skipped: {(out.stderr or '').strip()[:200]}")
        return
    log(f"cleared {(out.stdout or '?').strip()} stuck task(s) from the queue")


def restart_service() -> None:
    try:
        result = subprocess.run(
            [SUPERVISORCTL, "-c", SUPERVISOR_CONF, "restart", SERVICE],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        log(f"restart issued: {(result.stdout or result.stderr or '').strip()[:200]}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"restart failed: {exc}")


def main() -> int:
    log(f"starting — probing {HEALTH_URL} every {INTERVAL}s after a {BOOT_GRACE}s grace")
    time.sleep(BOOT_GRACE)

    consecutive = 0
    while True:
        if healthy():
            if consecutive:
                log("recovered")
            consecutive = 0
        else:
            consecutive += 1
            log(f"health check failed ({consecutive}/{FAILURES_BEFORE_RESTART})")
            if consecutive >= FAILURES_BEFORE_RESTART:
                clear_stuck_tasks()
                restart_service()
                consecutive = 0
                # Give the restart room to come up before probing again.
                time.sleep(BOOT_GRACE)
                continue
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
