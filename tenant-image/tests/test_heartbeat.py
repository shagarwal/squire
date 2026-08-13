"""Functional test for squire-heartbeat.py — the counts-only telemetry contract.

Runs WITHOUT Docker: stands up a fake control-api and a fake webhook-shim
/metrics endpoint, runs the real emitter against them, and asserts what it sent.

The assertion that matters is the negative one. This is the only process in the
tenant that talks to the control plane on a timer, so if anything is ever going to
leak conversation data out of the container's isolation boundary, it is this. The
test therefore checks the payload against an explicit whitelist and asserts that
nothing in the environment — DEK, bot token, webhook secret — appears in it.

Usage: python3 tenant-image/tests/test_heartbeat.py
Exit:  0 all assertions pass · 1 otherwise
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IMAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent
EMITTER = str(IMAGE_ROOT / "bin" / "squire-heartbeat.py")

CONTROL_PORT = 18099
SHIM_PORT = 18098
TOKEN = "internal-token-for-test"

# Secrets that exist in the emitter's environment and must never be in a payload.
DEK = "ZmFrZS1kZWstZm9yLXRoZS1oZWFydGJlYXQtdGVzdC0xMjM0"
BOT_TOKEN = "123456:AA-bot-token-must-not-leak"
WEBHOOK_SECRET = "whsec-must-not-leak"

#: The wire contract, restated here independently of the emitter's own constant.
#: If the two ever disagree, one of them changed without the other, which is
#: exactly the review moment this list exists to force.
ALLOWED_FIELDS = {
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

received = []


class FakeControlAPI(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    status_to_return = 200

    def log_message(self, *a):
        return

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        received.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "ctype": self.headers.get("Content-Type"),
                "payload": json.loads(body or b"{}"),
            }
        )
        self.send_response(FakeControlAPI.status_to_return)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


class FakeShim(BaseHTTPRequestHandler):
    """Stands in for the webhook shim's GET /metrics."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(
            {"updates_forwarded": 7, "updates_failed": 2, "updates_rejected": 1}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def run_once(**env_extra):
    """Run the emitter with --once and return (returncode, stdout)."""
    env = dict(os.environ)
    env.update(
        {
            "CONTROL_API_URL": f"http://127.0.0.1:{CONTROL_PORT}",
            "INTERNAL_API_TOKEN": TOKEN,
            "TENANT_ID": "t-heartbeat-test",
            "SQUIRE_IMAGE_REF": "ghcr.io/shagarwal/squire/hermes-tenant:v2",
            "PORT": str(SHIM_PORT),
            "SQUIRE_VOLUME": str(IMAGE_ROOT),
            # Secrets that are legitimately in a real tenant's environment.
            "SQUIRE_DEK": DEK,
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_WEBHOOK_SECRET": WEBHOOK_SECRET,
            # Point the Hindsight probes at closed ports: the collectors must
            # degrade to "absent field", not crash the beat.
            "HINDSIGHT_API_PORT": "18097",
            "TELEGRAM_WEBHOOK_PORT": "18096",
            "SQUIRE_HINDSIGHT_PYTHON": "/nonexistent/python",
            "SQUIRE_HEARTBEAT_TIMEOUT": "5",
        }
    )
    for key, value in env_extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    proc = subprocess.run(
        [sys.executable, EMITTER, "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


# --- fakes ------------------------------------------------------------------
control = ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), FakeControlAPI)
control.daemon_threads = True
threading.Thread(target=control.serve_forever, daemon=True).start()

shim = ThreadingHTTPServer(("127.0.0.1", SHIM_PORT), FakeShim)
shim.daemon_threads = True
threading.Thread(target=shim.serve_forever, daemon=True).start()

print("== one beat ==")
rc, out = run_once()
check("emitter exits 0", rc == 0, (rc, out))
check("posted exactly one heartbeat", len(received) == 1, len(received))

if received:
    beat = received[0]
    payload = beat["payload"]
    check("posted to /internal/heartbeat", beat["path"] == "/internal/heartbeat", beat["path"])
    check("carries the internal bearer token", beat["auth"] == f"Bearer {TOKEN}", beat["auth"])
    check("sends JSON", beat["ctype"] == "application/json", beat["ctype"])

    check("identifies the tenant", payload.get("tenant_id") == "t-heartbeat-test", payload)
    check(
        "reports the image it is running",
        payload.get("image_ref") == "ghcr.io/shagarwal/squire/hermes-tenant:v2",
        payload,
    )
    check("reports uptime", isinstance(payload.get("uptime_seconds"), int), payload)
    check("scraped the shim's update counters", payload.get("updates_forwarded") == 7, payload)
    check("counts failures separately", payload.get("updates_failed") == 2, payload)
    check("counts rejections separately", payload.get("updates_rejected") == 1, payload)
    check("reports volume usage", isinstance(payload.get("volume_used_mb"), int), payload)

    print("  -- the assertion this file exists for --")
    extra = set(payload) - ALLOWED_FIELDS
    check("payload contains ONLY whitelisted count/gauge fields", not extra, extra)

    non_scalar = {
        k: v for k, v in payload.items() if not isinstance(v, (int, bool, str, type(None)))
    }
    check("every value is a scalar (no nested objects or lists)", not non_scalar, non_scalar)

    # `image_ref` and `tenant_id` are the only strings, and both are ours.
    strings = {k for k, v in payload.items() if isinstance(v, str)}
    check("only tenant_id and image_ref are strings", strings <= {"tenant_id", "image_ref"}, strings)

    blob = json.dumps(payload)
    check("DEK absent from the payload", DEK not in blob)
    check("bot token absent from the payload", BOT_TOKEN not in blob)
    check("webhook secret absent from the payload", WEBHOOK_SECRET not in blob)

    print("  -- collectors fail independently --")
    # Hindsight was unreachable in this run; the beat still went out.
    check("hindsight_up reported false, not omitted", payload.get("hindsight_up") is False, payload)
    check(
        "unreachable hindsight DB omits the queue gauges",
        "hindsight_ops_pending" not in payload,
        payload,
    )

print("== backup age is reported when a backup has succeeded ==")
received.clear()
with tempfile.TemporaryDirectory() as state_dir:
    success_file = os.path.join(state_dir, "last-backup-success")
    with open(success_file, "w", encoding="utf-8") as fh:
        fh.write(str(int(time.time()) - 7200))  # two hours ago
    rc, out = run_once(SQUIRE_BACKUP_SUCCESS_FILE=success_file)
    check("beat sent with a backup timestamp present", rc == 0 and len(received) == 1, rc)
    if received:
        age = received[0]["payload"].get("backup_last_success_age_seconds")
        # A backup that quietly stops working produces no error anywhere; this is
        # the only number that would surface it.
        check("reports the backup age", isinstance(age, int) and 7100 < age < 7300, age)

    # A corrupt timestamp must omit the field, not send garbage or crash the beat.
    received.clear()
    with open(success_file, "w", encoding="utf-8") as fh:
        fh.write("not-a-timestamp")
    rc, _ = run_once(SQUIRE_BACKUP_SUCCESS_FILE=success_file)
    omitted = received and "backup_last_success_age_seconds" not in received[0]["payload"]
    check("a corrupt timestamp omits the field", rc == 0 and omitted, received)

print("== malformed numeric env falls back instead of crash-looping ==")
received.clear()
rc, out = run_once(SQUIRE_HEARTBEAT_INTERVAL="not-a-number", PORT="80 80")
check("still beats with garbage numeric env", rc == 0 and len(received) == 1, (rc, out))
check("warns about the bad value", "is not a number" in out, out)

print("== secrets are never logged ==")
check("DEK absent from emitter output", DEK not in out)
check("bearer token absent from emitter output", TOKEN not in out)

print("== not configured: exit 0, send nothing ==")
received.clear()
rc, out = run_once(CONTROL_API_URL=None)
check("unconfigured emitter exits 0", rc == 0, (rc, out))
check("unconfigured emitter sends nothing", len(received) == 0, received)
check("says it is idle", "not configured" in out, out)

received.clear()
rc, out = run_once(TENANT_ID=None)
check("no TENANT_ID -> exit 0 and send nothing", rc == 0 and not received, (rc, received))

print("== control-api rejects the beat ==")
received.clear()
FakeControlAPI.status_to_return = 422
rc, out = run_once()
check("a rejected beat is a non-zero --once exit", rc != 0, rc)
check("logs the status without the response body", "HTTP 422" in out, out)
FakeControlAPI.status_to_return = 200

print("== control-api unreachable ==")
received.clear()
rc, out = run_once(CONTROL_API_URL="http://127.0.0.1:18095")
check("unreachable control-api does not crash the emitter", rc == 1, rc)
check("says it could not reach control-api", "could not reach control-api" in out, out)

print("== awake-until-bound cadence (fix/awake-until-bound) ==")
# An UNBOUND tenant (approved-owner store empty — nobody has tapped the deep
# link yet) must beat every min(configured, 300)s so Railway's serverless sleep
# (~10 min inbound-quiet window) never engages before the owner's first tap.
# Once BOUND, the configured slow interval applies unchanged. These tests import
# the emitter as a module and drive the cadence logic directly — no real
# sleeping, no network.
import importlib.util


def load_emitter(name, env_extra=None):
    """Import the emitter under a controlled environment.

    Interval/env constants are read at import time, so each scenario that needs
    different env gets its own module instance. Env is restored afterwards so
    the earlier subprocess-based sections are unaffected.
    """
    saved = {}
    for key, value in (env_extra or {}).items():
        saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        spec = importlib.util.spec_from_file_location(name, EMITTER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return mod


def check_call(label, fn, expect):
    """check(), but a raised exception (e.g. missing attribute while the
    feature does not exist yet) is a FAIL rather than a crashed test run —
    fail-first must still report the later sections."""
    try:
        got = fn()
    except Exception as exc:  # noqa: BLE001 - deliberate: report, don't crash
        check(label, False, f"raised {exc!r}")
        return
    check(label, got == expect, got)


def write_approved(home, body='{"123456": {"approved_at": 1}}'):
    """Create the same store squire_autopair writes (modern path, telegram)."""
    pairing = os.path.join(home, "platforms", "pairing")
    os.makedirs(pairing, exist_ok=True)
    with open(os.path.join(pairing, "telegram-approved.json"), "w", encoding="utf-8") as fh:
        fh.write(body)


hb = load_emitter("hb_cadence", {"SQUIRE_HEARTBEAT_INTERVAL": "1800",
                                 "SQUIRE_UNBOUND_AWAKE_HOURS": None})

with tempfile.TemporaryDirectory() as home:
    hb.HERMES_HOME = home
    check_call("unbound tenant beats fast: min(1800, 300) == 300",
               lambda: hb.current_interval(), 300)

    # A configured interval already faster than the ceiling is kept as-is.
    hb.INTERVAL = 120
    check_call("configured interval below the ceiling wins: min(120, 300) == 120",
               lambda: hb.current_interval(), 120)
    hb.INTERVAL = 1800

    # Bind the owner the way squire_autopair does; cadence must flip to slow.
    write_approved(home)
    check_call("bound tenant uses the configured interval unchanged",
               lambda: hb.current_interval(), 1800)

with tempfile.TemporaryDirectory() as home:
    hb.HERMES_HOME = home
    # Runaway cap: an abandoned signup (unbound past SQUIRE_UNBOUND_AWAKE_HOURS,
    # default 48h) must fall back to the slow interval — never awake forever.
    real_start = getattr(hb, "START_MONOTONIC", None)
    hb.START_MONOTONIC = time.monotonic() - 49 * 3600
    check_call("unbound past the 48h cap falls back to the slow interval",
               lambda: hb.current_interval(), 1800)
    if real_start is not None:
        hb.START_MONOTONIC = real_start
    check_call("sanity: same store, uptime under the cap, fast again",
               lambda: hb.current_interval(), 300)

    # A store that exists but cannot be parsed must fail toward the CHEAP mode
    # (slow beat / tenant sleeps), never toward burn.
    write_approved(home, body="{corrupt json!!")
    check_call("unreadable store reads as bound -> slow interval",
               lambda: hb.current_interval(), 1800)

with tempfile.TemporaryDirectory() as home:
    hb.HERMES_HOME = home
    # If the squire_autopair import failed at startup, the emitter must degrade
    # to the plain configured cadence (treat as bound), not crash or burn.
    saved_load = getattr(hb, "load_approved", "missing")
    saved_path = getattr(hb, "approved_path", "missing")
    hb.load_approved = None
    hb.approved_path = None
    check_call("autopair import failure degrades to the slow interval",
               lambda: hb.current_interval(), 1800)
    hb.load_approved = saved_load
    hb.approved_path = saved_path

# Malformed cap env falls back to the 48h default instead of crashing —
# same _env_number discipline as every other numeric knob in this file.
hb_bad_env = load_emitter("hb_bad_cap", {"SQUIRE_UNBOUND_AWAKE_HOURS": "banana"})
check_call("SQUIRE_UNBOUND_AWAKE_HOURS=banana falls back to the 48h default",
           lambda: hb_bad_env.UNBOUND_AWAKE_HOURS, 48)

print("== cadence flips mid-loop, without a restart ==")
# The loop must re-check bound-ness EVERY iteration: an owner who taps the deep
# link three hours after provisioning flips the running loop from fast to slow
# with no restart. Driven with a fake stop event so no real time passes.


class FakeStop(threading.Event):
    """Records each wait() delay; runs one scripted action per iteration and
    stops the loop when the script is exhausted."""

    def __init__(self, actions):
        super().__init__()
        self.actions = list(actions)
        self.delays = []

    def wait(self, timeout=None):
        self.delays.append(timeout)
        if not self.actions:
            self.set()
            return True
        self.actions.pop(0)()
        return False


with tempfile.TemporaryDirectory() as home:
    hb.HERMES_HOME = home
    hb.beat = lambda: True  # no network during the loop test
    stop = FakeStop([
        lambda: None,                  # iteration 1: still unbound
        lambda: write_approved(home),  # owner binds mid-life
        lambda: None,                  # iteration 3: now bound
    ])
    try:
        hb.run_loop(stop)
        delays = stop.delays
        # Jitter is +/-10%, so assert bands rather than exact values.
        check("loop exists and recorded delays", len(delays) == 4, delays)
        fast = delays[:2]
        slow = delays[2:]
        check("unbound iterations wait ~300s (270..330 with jitter)",
              all(d is not None and 270 <= d <= 330 for d in fast), delays)
        check("post-bind iterations wait ~1800s (1620..1980 with jitter)",
              all(d is not None and 1620 <= d <= 1980 for d in slow), delays)
    except Exception as exc:  # noqa: BLE001 - fail-first reporting, not a crash
        check("run_loop(stop) drives the cadence", False, f"raised {exc!r}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL HEARTBEAT TESTS PASS")
