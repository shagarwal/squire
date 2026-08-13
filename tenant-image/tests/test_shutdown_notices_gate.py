#!/usr/bin/env python3
"""Patch 005 (shutdown-notices hunk) — sleeping must be silent, or every idle
period messages every user.

Runs WITHOUT Docker.

The failure this guards was observed live (2026-08-13): Railway's serverless
sleep SIGTERMs a tenant ~13 quiet minutes after the user's last message, and
upstream's stop() then calls `_notify_active_sessions_of_shutdown`, which sends
"⚠️ Gateway shutting down — Your current task will be interrupted." to active
session chats AND the home channel. Patch 005 adopts the owner's DM as the home
channel, so the founder's test user received that raw operator text ten minutes
after going quiet — and would again on EVERY idle period. Sleep is the billing
model, not an incident, and the audience is non-technical.

The fix rides in patch 005 (gateway/run.py already belongs to 005 under the
overlay's one-patch-per-file rule): an early return at the very top of
`_notify_active_sessions_of_shutdown` — the ONLY user-facing send site in
stop(), verified by reading the whole stop() body — gated on
SQUIRE_SHUTDOWN_NOTICES:
  * unset / empty / "0" -> send NOTHING (neither the per-active-session ping
    nor the home-channel broadcast). DEFAULT-QUIET on purpose: a self-hosted
    hermes without Squire's env never runs this overlay at all, so the quiet
    default cannot regress anyone else, and it fails safe for out-of-order
    image/config rollouts.
  * any other value -> upstream behaviour entirely unchanged (dedup, drain
    suppress_notification, per-platform gateway_restart_notification all run).
The Dockerfile bakes the variable to 0 so the policy is visible; that pairing
is asserted here too.

What this suite proves, each independently breakable:
  1. the gate hunk rides in patch 005 and run.py stays single-patch-owned;
  2. the hunk APPLIES via patch(1) to the real upstream context (a verbatim
     excerpt of the pristine v2026.8.3 method head is embedded below — edit
     the patch's context lines by hand and application fails here, not at
     image build);
  3. after applying, every shutdown-gate marker row in markers.tsv matches —
     the same greps verify-markers.sh runs at build time — and the upstream
     anchor row also holds PRE-patch (an anchor that only exists post-patch
     guards nothing);
  4. the gate actually GATES: the `return` sits inside the env check, the
     check sits before the running-agents snapshot and before the "⚠️" text
     is even built, and the quiet branch logs one line naming the knob;
  5. the env knob's semantics: unset/empty/"0" (whitespace tolerated) ->
     quiet, anything else -> upstream path;
  6. the image default is 0.

Usage: python3 tenant-image/tests/test_shutdown_notices_gate.py
Exit:  0 all assertions pass · 1 otherwise
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile

IMAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent
PATCH_DIR = IMAGE_ROOT / "patches"
PATCH_005 = PATCH_DIR / "005-first-contact-concierge-note.patch"
MARKERS = PATCH_DIR / "markers.tsv"
DOCKERFILE = (IMAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")
RUN_REL = "gateway/run.py"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


patch_text = PATCH_005.read_text(encoding="utf-8")


print("== 1. the gate rides in patch 005; run.py stays single-patch-owned ==")

check("patch 005 exists and is non-empty", bool(patch_text.strip()))
check("patch 005 carries the shutdown-notices gate", "SQUIRE_SHUTDOWN_NOTICES" in patch_text)

# The overlay's regeneration rule (apply-patches.sh header): one patch per
# target file. The gate must NOT have become a seventh patch touching run.py.
own_targets = set(re.findall(r"^\+\+\+ b/(\S+)", patch_text, re.M))
check("patch 005 still touches exactly gateway/run.py", own_targets == {RUN_REL}, str(own_targets))
other_targets: set[str] = set()
for p in sorted(PATCH_DIR.glob("*.patch")):
    if p.name != PATCH_005.name:
        other_targets |= set(re.findall(r"^\+\+\+ b/(\S+)", p.read_text(encoding="utf-8"), re.M))
check("no other patch touches gateway/run.py", RUN_REL not in other_targets, str(other_targets))


print()
print("== 2. the gate hunk applies to the real upstream context ==")

# Verbatim excerpt of the PRISTINE upstream gateway/run.py (v2026.8.3), lines
# 9134-9153: the head of _notify_active_sessions_of_shutdown, covering the gate
# hunk's full before/after context. Provenance: copied 2026-08-13 from the
# pristine v2026.8.3 tree the incident was diagnosed against (the same tree
# apply-patches.sh was verified on). This excerpt is an INDEPENDENT copy on
# purpose: rebuilding "upstream" from the patch's own context lines would make
# this test pass no matter what the patch says.
UPSTREAM_EXCERPT = '''\
    async def _notify_active_sessions_of_shutdown(self) -> None:
        """Send shutdown/restart notifications to active chats and home channels.

        Called at the very start of stop() — adapters are still connected so
        messages can be delivered. Best-effort: individual send failures are
        logged and swallowed so they never block the shutdown sequence.
        """
        active = self._snapshot_running_agents()
        restart_source = self._restart_command_source if self._restart_requested else None

        action = "restarting" if self._restart_requested else "shutting down"
        hint = (
            "Your current task will be interrupted. "
            "Send any message after restart and I'll try to resume where you left off."
            if self._restart_requested
            else "Your current task will be interrupted."
        )
        msg = f"⚠️ Gateway {action} — {hint}"

        notified: set[tuple[str, str, Optional[str]]] = set()
'''

# Isolate the gate's own hunk from 005 (the patch also carries the concierge
# and home-channel hunks, whose context lives ~8000 lines away and cannot be
# embedded here wholesale). A hunk is self-contained by construction, so
# header + one hunk is itself a valid patch.
hunks = re.split(r"(?m)^(?=@@ )", patch_text)
gate_hunks = [h for h in hunks if h.startswith("@@ ") and "SQUIRE_SHUTDOWN_NOTICES" in h]
check("the gate is exactly one hunk of 005", len(gate_hunks) == 1, f"found {len(gate_hunks)}")
MINI_PATCH = "--- a/gateway/run.py\n+++ b/gateway/run.py\n" + (gate_hunks[0] if gate_hunks else "")


def apply_gate(tree: pathlib.Path) -> tuple[int, str]:
    """Apply the gate hunk the same way apply-patches.sh does: patch -p1 --forward."""
    target = tree / RUN_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(UPSTREAM_EXCERPT, encoding="utf-8")
    mini = tree / "gate.patch"
    mini.write_text(MINI_PATCH, encoding="utf-8")
    proc = subprocess.run(
        ["patch", "-p1", "--batch", "--forward",
         "--directory", str(tree), "--input", str(mini)],
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


patched = ""
with tempfile.TemporaryDirectory() as td:
    tree = pathlib.Path(td)
    rc, out = apply_gate(tree)
    check("the gate hunk applies cleanly via patch(1)", rc == 0, out[:200])
    if rc == 0:
        patched = (tree / RUN_REL).read_text(encoding="utf-8")

check("the gate landed", "SQUIRE_SHUTDOWN_NOTICES" in patched)


print()
print("== 3. every shutdown-gate marker row matches; the anchor holds pre-patch ==")

# The exact greps verify-markers.sh will run at image build time, executed
# against the freshly patched excerpt. `-F` fixed strings, same as the script.
rows = [
    r.split("\t") for r in MARKERS.read_text(encoding="utf-8").splitlines()
    if r and not r.startswith("#") and "shutdown" in r.split("\t")[0]
]
check("markers.tsv carries the shutdown-gate rows", len(rows) >= 3, f"found {len(rows)}")
check("there is an upstream anchor row", any(r[0] == "upstream-shutdown-notify" for r in rows))
check("there is an env-knob marker", any(r[0] == "squire-005-shutdown-notices-env" for r in rows))
check("there is a quiet-branch marker", any(r[0] == "squire-005-shutdown-quiet" for r in rows))
for row in rows:
    check(f"marker row `{row[0]}` has 4 columns", len(row) == 4, str(row))
    if len(row) == 4:
        rid, rfile, pattern, required = row
        check(f"`{rid}` targets gateway/run.py", rfile == RUN_REL, rfile)
        check(f"`{rid}` is required", required == "required", required)
        check(f"`{rid}` matches the patched tree (grep -F)", pattern in patched, pattern)

# The anchor must ALSO hold pre-patch — it is the "upstream still has the
# method we gate" check, and an anchor only present post-patch guards nothing.
anchor_rows = [r for r in rows if len(r) == 4 and r[0] == "upstream-shutdown-notify"]
check(
    "the anchor row is satisfied by the PRISTINE tree too",
    bool(anchor_rows) and anchor_rows[0][2] in UPSTREAM_EXCERPT,
)


print()
print("== 4. the gate actually gates ==")

# Structural, not just substring: the `return` must sit INSIDE the env check
# (deeper indentation, before the block ends), and the check must run before
# anything observable happens — before the running-agents snapshot and before
# the "⚠️" message text is even built. Deleting the return while keeping the
# log line — or moving the gate below the sends — fails here, not in a grep.
lines = patched.splitlines()
gate_idx = next(
    (i for i, l in enumerate(lines)
     if l.strip() == 'if os.environ.get("SQUIRE_SHUTDOWN_NOTICES", "0").strip() in ("", "0"):'),
    None,
)
check("the env gate exists with the expected default-quiet condition", gate_idx is not None)
if gate_idx is not None:
    gate_indent = len(lines[gate_idx]) - len(lines[gate_idx].lstrip())
    block = []
    for l in lines[gate_idx + 1:]:
        if l.strip() and (len(l) - len(l.lstrip())) <= gate_indent:
            break
        block.append(l)
    check("the quiet branch returns (sends nothing)", any(l.strip() == "return" for l in block))
    check(
        "the quiet branch logs, naming the knob",
        any("logger.info(" in l for l in block)
        and any("SQUIRE_SHUTDOWN_NOTICES" in l for l in block)
        and any("shutdown notices suppressed" in l for l in block),
    )
    snapshot_idx = next(
        (i for i, l in enumerate(lines) if "_snapshot_running_agents()" in l), None
    )
    # The construction line specifically — the gate's own comment quotes the
    # "⚠️ Gateway ..." text, so a bare substring search would match the
    # comment above the gate and always "pass".
    msg_idx = next(
        (i for i, l in enumerate(lines) if l.strip().startswith('msg = f"⚠️ Gateway')), None
    )
    check(
        "the gate runs BEFORE the running-agents snapshot",
        snapshot_idx is not None and gate_idx < snapshot_idx,
    )
    check(
        "the gate runs BEFORE the ⚠️ message is built",
        msg_idx is not None and gate_idx < msg_idx,
    )

# The method body must be untouched apart from the inserted gate: we pre-empt
# the sends, we do not rewrite them. No pristine line may be deleted.
check(
    "the gate hunk deletes nothing (pure insertion)",
    not re.search(r"(?m)^-(?!--)", MINI_PATCH),
)
# And both downstream suppression mechanisms survive for the gate-open path:
# no DELETED line (a leading single `-`) may touch them. Plain substring
# checks would misfire — the gate's own comment names both mechanisms.
check("upstream's drain suppress_notification path is untouched",
      not re.search(r"(?m)^-(?!--).*drain_notification_suppressed", patch_text))
check("upstream's gateway_restart_notification config stays honoured",
      not re.search(r"(?m)^-(?!--).*gateway_restart_notification", patch_text))


print()
print("== 5. env-knob semantics: unset/empty/'0' are QUIET, anything else is upstream ==")

# Execute the actual patched condition under a controlled environment. If the
# gate ever switches to a helper with different semantics, these start failing
# — which is the point. Note the inverted polarity vs patch 006: HERE the
# absent-var default is the Squire behaviour (quiet), because a non-Squire
# tree never runs this overlay at all (see module docstring).
cond_src = lines[gate_idx].strip()[3:].rstrip(":") if gate_idx is not None else None


def quiet_for(env_value: str | None) -> bool:
    fake_env = {} if env_value is None else {"SQUIRE_SHUTDOWN_NOTICES": env_value}

    class _FakeEnviron:
        def get(self, key: str, default: str = "") -> str:
            return fake_env.get(key, default)

    class _FakeOS:
        environ = _FakeEnviron()

    return bool(eval(cond_src, {"os": _FakeOS()}))  # noqa: S307 — the one patched line


if cond_src:
    check("unset -> quiet (the image default even without the ENV)", quiet_for(None))
    check("empty string -> quiet", quiet_for(""))
    check("'0' -> quiet (the baked image value)", quiet_for("0"))
    check("' 0 ' -> quiet (whitespace tolerated)", quiet_for(" 0 "))
    check("'1' -> upstream behaviour (notices sent)", not quiet_for("1"))
    check("'true' -> upstream behaviour", not quiet_for("true"))
    check("any other junk value fails LOUD, i.e. toward upstream", not quiet_for("yes please"))


print()
print("== 6. the image bakes the knob to 0, per-tenant overridable ==")

check(
    "Dockerfile sets SQUIRE_SHUTDOWN_NOTICES=0",
    re.search(r"^ENV SQUIRE_SHUTDOWN_NOTICES=0\s*$", DOCKERFILE, re.M) is not None,
)
check(
    "the Dockerfile explains the sleep stake next to the knob",
    "sleep" in DOCKERFILE.split("ENV SQUIRE_SHUTDOWN_NOTICES")[0][-1500:].lower(),
)


print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL SHUTDOWN-NOTICES GATE TESTS PASS")
