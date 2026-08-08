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

# Circuit-breaker limits — see the commentary in main().
MAX_ACTIONS_PER_HOUR = int(os.environ.get("SQUIRE_HINDSIGHT_MAX_ACTIONS_PER_HOUR", "4"))
MAX_BACKOFF = int(os.environ.get("SQUIRE_HINDSIGHT_MAX_BACKOFF", "1800"))

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


def warn_on_extraction_auth_failures() -> None:
    """Surface the silent failure: extraction 401ing against a revoked key.

    squire-secrets-sync.sh re-wires the daemon when .env changes, but if that
    ever fails — or the user's own key is simply wrong — the symptom is
    invisible: the agent keeps replying, memories just stop forming. Nothing
    crashes, no probe goes red, and the first anyone hears about it is a user
    saying the assistant "feels like it forgot everything".

    So we look for it explicitly, in the one place it is recorded.
    """
    script = (
        "import sys, psycopg2\n"
        "conn = psycopg2.connect(sys.argv[1], connect_timeout=10)\n"
        "cur = conn.cursor()\n"
        "cur.execute(\"\"\"\n"
        "  SELECT count(*) FROM async_operations\n"
        "   WHERE status = 'failed'\n"
        "     AND updated_at > now() - interval '1 hour'\n"
        "     AND (error_message ILIKE '%401%'\n"
        "       OR error_message ILIKE '%unauthor%'\n"
        "       OR error_message ILIKE '%authentication%'\n"
        "       OR error_message ILIKE '%invalid%api%key%'\n"
        "       OR error_message ILIKE '%credit%'\n"
        "       OR error_message ILIKE '%quota%')\n"
        "\"\"\")\n"
        "print(cur.fetchone()[0])\n"
        "conn.close()\n"
    )
    try:
        out = subprocess.run(
            [HINDSIGHT_PY, "-c", script, PG_DSN],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if out.returncode != 0:
            return  # schema not there yet, or DB down — the health probe owns that
        n = int((out.stdout or "0").strip() or 0)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return

    if n:
        log(
            f"WARNING: {n} memory operation(s) failed with an auth/quota error in the "
            "last hour. The LLM credentials Hindsight is using are almost certainly "
            "stale or revoked — check that squire-secrets-sync re-wired the daemon "
            "after the tenant connected their own provider. Memory extraction is "
            "silently not happening."
        )


def main() -> int:
    log(f"starting — probing {HEALTH_URL} every {INTERVAL}s after a {BOOT_GRACE}s grace")
    time.sleep(BOOT_GRACE)

    consecutive = 0

    # CIRCUIT BREAKER
    # ---------------
    # Two guards, both learned from what this loop does when it is WRONG rather
    # than when the daemon is wrong.
    #
    # 1. ever_healthy — never act until we have seen the daemon healthy at least
    #    once. If the probe itself is misconfigured (wrong port, daemon renamed,
    #    a URL typo) the old loop would wipe the task queue and restart every
    #    ~7 minutes forever: destroying legitimate extraction work, fighting
    #    supervisord's own restart logic, and looking like the daemon was
    #    flapping when in fact only the checker was broken. A never-healthy
    #    daemon is supervisord's problem — it has startretries for exactly this.
    #
    # 2. rate limit + exponential backoff — even a genuinely wedging daemon must
    #    not be recycled indefinitely. After MAX_ACTIONS_PER_HOUR we stop acting
    #    and just report, because past that point restarting is not fixing
    #    anything and the queue-clearing is pure data loss.
    ever_healthy = False
    action_times: list[float] = []
    backoff = INTERVAL

    while True:
        if healthy():
            if not ever_healthy:
                log("daemon healthy for the first time — recovery actions are now armed")
            elif consecutive:
                log("recovered")
            ever_healthy = True
            consecutive = 0
            backoff = INTERVAL
            warn_on_extraction_auth_failures()
        else:
            consecutive += 1
            log(f"health check failed ({consecutive}/{FAILURES_BEFORE_RESTART})")

            if consecutive >= FAILURES_BEFORE_RESTART:
                if not ever_healthy:
                    log(
                        "daemon has NEVER been healthy since this loop started — not "
                        "clearing the queue or restarting. Either the daemon cannot "
                        "start (supervisord's startretries owns that) or this probe is "
                        f"misconfigured ({HEALTH_URL}). Refusing to destroy extraction "
                        "work on a guess."
                    )
                    consecutive = 0
                    time.sleep(INTERVAL)
                    continue

                now = time.monotonic()
                action_times[:] = [t for t in action_times if now - t < 3600]
                if len(action_times) >= MAX_ACTIONS_PER_HOUR:
                    log(
                        f"already recycled the daemon {len(action_times)} time(s) this "
                        "hour — circuit open, no further automatic action. Something "
                        "needs a human: repeated restarts are no longer fixing it and "
                        "each queue-clear loses real work."
                    )
                    consecutive = 0
                    time.sleep(backoff)
                    continue

                action_times.append(now)
                clear_stuck_tasks()
                restart_service()
                consecutive = 0
                # Exponential backoff, capped: give each successive attempt more
                # room rather than hammering a daemon that needs time.
                time.sleep(max(BOOT_GRACE, backoff))
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
