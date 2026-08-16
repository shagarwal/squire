#!/usr/bin/env python3
"""Patch 005 (status-notice chokepoint hunk) — hermes operator chatter must not
leak to non-technical end users.

Runs WITHOUT Docker.

The class of failure this guards: raw hermes OPERATOR status notices reaching
Squire end users at the worst moments. Three earlier instances were gated one
at a time — "⚠️ Gateway shutting down" and "♻️ Gateway online" (stop() +
gateway_restart_notification), "⚠️ Provider authentication failed" (a REWRITE
that MUST survive). This is the GENERAL fix for the remaining class, most
recently the auto-compaction banner replayed on the first turn:

    "ℹ Codex gpt-5.4 caps context at 272K, so auto-compaction was raised
     to 92% (from 80%) …"

(agent_init._build_codex_gpt5_autoraise_notice, stashed in
agent._compression_warning and re-emitted via status_callback("lifecycle", …)).

`_prepare_gateway_status_message` in gateway/run.py is the single chokepoint
every agent status callback funnels through before a chat surface sees it
(event_type ∈ {"lifecycle", "warn", "compacted"} — there is NO dedicated
"error" category; provider failures are recognised by TEXT SHAPE and rewritten
one branch above). Patch 005 (gateway/run.py already belongs to 005 under the
overlay's one-patch-per-file rule) inserts a gate right before the terminal
`return text` — i.e. AFTER the provider-error rewrite, so the suppress-vs-keep
boundary is structural:

  * SQUIRE_STATUS_NOTICES unset / empty / "0" / "off" -> suppress (return None).
    DEFAULT-QUIET: a self-hosted hermes without Squire's env never runs this
    overlay, so the quiet default cannot regress anyone else, and it fails safe
    for out-of-order image/config rollouts.
  * any other value (e.g. "all") -> upstream behaviour unchanged (return text) —
    an escape hatch for debugging.

Crucially the gate sits AFTER `_gateway_provider_error_reply`, so a genuine
user-facing provider error is still rewritten and delivered; only the
operator-flavoured info/lifecycle/aux/compression residue is silenced. The
Dockerfile bakes the variable to 0 so the policy is visible; that pairing is
asserted here too.

What this suite proves, each independently breakable:
  1. the gate hunk rides in patch 005 and run.py stays single-patch-owned;
  2. the hunk APPLIES via patch(1) to the real upstream context (a verbatim
     excerpt of the pristine v2026.8.3 function is embedded below — edit the
     patch's context lines by hand and application fails here, not at build);
  3. after applying, every status-gate marker row in markers.tsv matches — the
     same greps verify-markers.sh runs at build time — and the upstream anchor
     row also holds PRE-patch (an anchor that only exists post-patch guards
     nothing);
  4. the gate actually GATES: `return None` sits inside the env check, and the
     check sits AFTER the provider-error rewrite (so user-facing errors still
     pass) and is a pure insertion (no pristine line deleted);
  5. the env knob's semantics: unset/empty/"0"/"off" (whitespace tolerated) ->
     quiet, anything else ("all", "1", junk) -> upstream path;
  6. the image default is 0.

Usage: python3 tenant-image/tests/test_status_notices_gate.py
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
check("patch 005 carries the status-notices gate", "SQUIRE_STATUS_NOTICES" in patch_text)

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
# 606-636: the whole _prepare_gateway_status_message chokepoint plus the two
# trailing blank lines and the next def, covering the gate hunk's full
# before/after context. Provenance: copied 2026-08-16 from the pristine
# v2026.8.3 tree apply-patches.sh was verified on. This excerpt is an
# INDEPENDENT copy on purpose: rebuilding "upstream" from the patch's own
# context lines would make this test pass no matter what the patch says.
UPSTREAM_EXCERPT = '''\
def _prepare_gateway_status_message(platform: Any, event_type: str, message: str) -> Optional[str]:
    """Filter/sanitize agent status callbacks before platform delivery.

    Local/CLI sessions keep the raw diagnostic stream. Messaging gateway
    surfaces should not receive transient auxiliary/compression chatter.
    """
    text = str(message or "").strip()
    if not text:
        return None
    if _gateway_surface_passes_raw_text(platform):
        return text

    text = _redact_gateway_user_facing_secrets(text)
    if _TELEGRAM_NOISY_STATUS_RE.search(text):
        # Opt-in #52995: `compression.progress_notices: true` lets ROUTINE
        # compression progress statuses through to chat platforms. The
        # membership check is derived from the #69550 template constants, so
        # non-compression noise (aux failures, provider retry chatter, ...)
        # stays suppressed even when the gate is open. Default False keeps
        # the silent-by-design behavior byte-identical.
        if not (
            _gateway_compression_progress_notices_enabled()
            and _COMPRESSION_PROGRESS_STATUS_RE.search(text)
        ):
            return None
    if _looks_like_gateway_provider_error(text):
        return _gateway_provider_error_reply(text)
    return text


def render_notice_line(notice) -> str:
'''

# Isolate the gate's own hunk from 005 (the patch also carries the concierge,
# home-channel and shutdown hunks, whose context lives thousands of lines away
# and cannot be embedded here wholesale). A hunk is self-contained by
# construction, so header + one hunk is itself a valid patch. The status hunk
# is the one carrying the distinctive chokepoint marker.
hunks = re.split(r"(?m)^(?=@@ )", patch_text)
gate_hunks = [h for h in hunks if h.startswith("@@ ") and "THE STATUS-NOTICE CHOKEPOINT" in h]
check("the status gate is exactly one hunk of 005", len(gate_hunks) == 1, f"found {len(gate_hunks)}")
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

check("the gate landed", "SQUIRE_STATUS_NOTICES" in patched)


print()
print("== 3. every status-gate marker row matches; the anchor holds pre-patch ==")

# The exact greps verify-markers.sh will run at image build time, executed
# against the freshly patched excerpt. `-F` fixed strings, same as the script.
rows = [
    r.split("\t") for r in MARKERS.read_text(encoding="utf-8").splitlines()
    if r and not r.startswith("#") and "status" in r.split("\t")[0]
]
check("markers.tsv carries the status-gate rows", len(rows) >= 3, f"found {len(rows)}")
check("there is an upstream anchor row", any(r[0] == "upstream-status-chokepoint" for r in rows))
check("there is an env-knob marker", any(r[0] == "squire-005-status-notices-env" for r in rows))
check("there is a chokepoint marker", any(r[0] == "squire-005-status-chokepoint" for r in rows))
for row in rows:
    check(f"marker row `{row[0]}` has 4 columns", len(row) == 4, str(row))
    if len(row) == 4:
        rid, rfile, pattern, required = row
        check(f"`{rid}` targets gateway/run.py", rfile == RUN_REL, rfile)
        check(f"`{rid}` is required", required == "required", required)
        check(f"`{rid}` matches the patched tree (grep -F)", pattern in patched, pattern)

# The anchor must ALSO hold pre-patch — it is the "upstream still has the
# chokepoint we gate" check, and an anchor only present post-patch guards
# nothing.
anchor_rows = [r for r in rows if len(r) == 4 and r[0] == "upstream-status-chokepoint"]
check(
    "the anchor row is satisfied by the PRISTINE tree too",
    bool(anchor_rows) and anchor_rows[0][2] in UPSTREAM_EXCERPT,
)


print()
print("== 4. the gate actually gates, and errors still pass ==")

# Structural, not just substring: the `return None` must sit INSIDE the env
# check (deeper indentation, before the block ends), and — the whole point of
# the boundary — the gate must run AFTER the provider-error rewrite, so a
# user-facing error is rewritten-and-returned before control ever reaches the
# gate. Moving the gate above the rewrite, or deleting the return, fails here.
lines = patched.splitlines()
gate_idx = next(
    (i for i, l in enumerate(lines)
     if l.strip() == 'if os.environ.get("SQUIRE_STATUS_NOTICES", "0").strip().lower() in ("", "0", "off"):'),
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
    check("the quiet branch returns None (suppresses)", any(l.strip() == "return None" for l in block))

    # The provider-error rewrite return must appear BEFORE the gate: that is
    # the structural guarantee that genuine user-facing errors survive.
    rewrite_idx = next(
        (i for i, l in enumerate(lines)
         if l.strip() == "return _gateway_provider_error_reply(text)"),
        None,
    )
    check(
        "the provider-error rewrite runs BEFORE the gate (errors still pass)",
        rewrite_idx is not None and rewrite_idx < gate_idx,
    )
    # The terminal `return text` (upstream behaviour) must remain, reached only
    # when the gate is OPEN.
    check("the upstream terminal `return text` survives", any(l.strip() == "return text" for l in lines[gate_idx:]))

# The chokepoint body must be untouched apart from the inserted gate: we
# pre-empt the terminal return, we do not rewrite the function. No pristine
# line may be deleted.
check(
    "the gate hunk deletes nothing (pure insertion)",
    not re.search(r"(?m)^-(?!--)", MINI_PATCH),
)
# The already-suppressed noisy-status path and the compression opt-in survive
# for the gate-open path: no DELETED line may touch them. Plain substring
# checks would misfire — the gate's own comment names these mechanisms.
check("upstream's noisy-status suppression is untouched",
      not re.search(r"(?m)^-(?!--).*_TELEGRAM_NOISY_STATUS_RE", patch_text))
check("upstream's compression progress opt-in stays honoured",
      not re.search(r"(?m)^-(?!--).*_gateway_compression_progress_notices_enabled", patch_text))


print()
print("== 5. env-knob semantics: unset/empty/'0'/'off' are QUIET, anything else is upstream ==")

# Execute the actual patched condition under a controlled environment. If the
# gate ever switches to a helper with different semantics, these start failing
# — which is the point. Note the inverted polarity vs patch 006: HERE the
# absent-var default is the Squire behaviour (quiet), because a non-Squire tree
# never runs this overlay at all (see module docstring).
cond_src = lines[gate_idx].strip()[3:].rstrip(":") if gate_idx is not None else None


def quiet_for(env_value: str | None) -> bool:
    fake_env = {} if env_value is None else {"SQUIRE_STATUS_NOTICES": env_value}

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
    check("'off' -> quiet", quiet_for("off"))
    check("'OFF' -> quiet (case-insensitive)", quiet_for("OFF"))
    check("' 0 ' -> quiet (whitespace tolerated)", quiet_for(" 0 "))
    check("'all' -> upstream behaviour (operator notice passes)", not quiet_for("all"))
    check("'1' -> upstream behaviour", not quiet_for("1"))
    check("'true' -> upstream behaviour", not quiet_for("true"))
    check("any other junk value fails toward upstream", not quiet_for("yes please"))


print()
print("== 6. the image bakes the knob to 0, per-tenant overridable ==")

check(
    "Dockerfile sets SQUIRE_STATUS_NOTICES=0",
    re.search(r"^ENV SQUIRE_STATUS_NOTICES=0\s*$", DOCKERFILE, re.M) is not None,
)
check(
    "the Dockerfile explains the operator-chatter stake next to the knob",
    "auto-compaction" in DOCKERFILE.split("ENV SQUIRE_STATUS_NOTICES")[0][-1600:].lower()
    or "operator" in DOCKERFILE.split("ENV SQUIRE_STATUS_NOTICES")[0][-1600:].lower(),
)


print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL STATUS-NOTICES GATE TESTS PASS")
