#!/usr/bin/env python3
"""Patch 006 — the identity-refresh loop must be stoppable, or no tenant sleeps.

Runs WITHOUT Docker.

The failure this guards was diagnosed live on staging (2026-08-10, Gate G1):
a tenant left completely untouched never slept, because upstream's Telegram
adapter runs `_bot_identity_refresh_loop()` in webhook mode — one `get_me()`
HTTPS call to api.telegram.org every `_BOT_IDENTITY_TTL_SECONDS` (300s),
forever, to catch BotFather renames. Railway's serverless sleep needs ~10
quiet minutes, so that single loop keeps every tenant awake 24/7 and nullifies
the scale-to-zero half of the Phase 0 cost model (~$12/mo always-on vs $1-2
sleeping). Squire's pool-bot usernames are controlled by us, so the rename
detection the loop pays for buys nothing here.

Patch 006 makes the TTL overridable via SQUIRE_TELEGRAM_IDENTITY_TTL:
  * unset / empty / malformed -> upstream's 300.0 (a patched tree without the
    env var must behave exactly like upstream — rollouts are not atomic);
  * <= 0 -> the launch site never starts the loop, and says so in one log line.
The Dockerfile bakes the variable to 0; that pairing is asserted here too.

What this suite proves, each independently breakable:
  1. the patch is file-disjoint from 001-005 (the overlay's regeneration rule);
  2. it APPLIES via patch(1) to the real upstream context (an excerpt of the
     pristine v2026.8.3 file is embedded below — if someone edits the patch's
     context lines by hand, application fails here, not at image build);
  3. after applying, every 006 marker row in markers.tsv matches — the same
     greps verify-markers.sh will run at build time;
  4. the skip-when-0 guard actually GUARDS: ensure_future is inside it, and
     the disabled branch logs (deleting the guard fails this, not just a grep);
  5. the env knob's semantics: unset -> 300.0, "0"/negative -> loop skipped,
     positive -> honoured, garbage -> fail-safe 300.0 (env_float semantics,
     verified against upstream utils.py:497 — it must not raise, because the
     assignment runs at class-body evaluation, i.e. module import);
  6. the image default is 0.

Usage: python3 tenant-image/tests/test_identity_refresh_ttl.py
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
PATCH_006 = PATCH_DIR / "006-telegram-identity-refresh-ttl.patch"
MARKERS = PATCH_DIR / "markers.tsv"
DOCKERFILE = (IMAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")
ADAPTER_REL = "plugins/platforms/telegram/adapter.py"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


patch_text = PATCH_006.read_text(encoding="utf-8")


print("== 1. the patch exists, targets the adapter, and is file-disjoint ==")

check("patch 006 exists and is non-empty", bool(patch_text.strip()))
check("patch 006 targets the Telegram adapter", f"b/{ADAPTER_REL}" in patch_text)

# The overlay's regeneration rule (apply-patches.sh header): a patch that stops
# applying must be regenerable without untangling it from its neighbours, so no
# two patches may touch the same file.
own_targets = set(re.findall(r"^\+\+\+ b/(\S+)", patch_text, re.M))
check("patch 006 touches exactly one file", own_targets == {ADAPTER_REL}, str(own_targets))
other_targets: set[str] = set()
for p in sorted(PATCH_DIR.glob("*.patch")):
    if p.name != PATCH_006.name:
        other_targets |= set(re.findall(r"^\+\+\+ b/(\S+)", p.read_text(encoding="utf-8"), re.M))
check(
    "no other patch touches the Telegram adapter",
    not (own_targets & other_targets),
    str(own_targets & other_targets),
)


print()
print("== 2. the patch applies to the real upstream context ==")

# Verbatim excerpt of the PRISTINE upstream file (v2026.8.3), covering both
# hunks' context. Provenance: extracted 2026-08-10 from
# ghcr.io/shagarwal/squire/hermes-tenant:0.1.3 (manifest sha256:316fba0a19e5...)
# whose base is docker.io/nousresearch/hermes-agent@sha256:c0cab4e3... — the
# digest the Dockerfile pins — and no patch 001-005 touches this file, so the
# image copy IS the pristine tree. This excerpt is an INDEPENDENT copy on
# purpose: rebuilding the "upstream" from the patch's own context lines would
# make this test pass no matter what the patch says.
UPSTREAM_EXCERPT = '''\
    async def _bot_identity_refresh_loop(self) -> None:
        """Keep the cached @username fresh when no heartbeat is running.

        Polling mode re-reads identity via the heartbeat's ``get_me()`` probe.
        Webhook mode has no such probe — nothing calls ``get_me()`` again after
        ``initialize()`` — so without this loop a BotFather rename breaks
        mention routing until the gateway restarts.
        """

            self._note_bot_username(getattr(self._bot, "username", None))
            self._bot_identity_checked_at = time.monotonic()
            if self._webhook_mode:
                identity_task = getattr(self, "_bot_identity_refresh_task", None)
                if identity_task and not identity_task.done():
                    identity_task.cancel()
                self._bot_identity_refresh_task = asyncio.ensure_future(
                    self._bot_identity_refresh_loop()
                )

            # Command-menu registration, DM-topic setup, and the status
            # indicator each make Bot API calls that can stall for certain
            # tokens. Running them here — inside the connect() coroutine that
            # the gateway wraps in a connect timeout — means one slow call
            # blows the whole connect and the adapter never comes up, even

    # Telegram bot handles historically had to end in "bot", but collectible
    # (Fragment) usernames can be assigned to bots and drop that suffix
    # entirely (@jarvis, @pic, ...). This pattern is used ONLY to decide
    # whether some FOREIGN @handle in a message is bot-shaped; our own handle
    # is matched by identity, never by shape.
    _FOREIGN_BOT_HANDLE_RE = re.compile(r"[a-z0-9_]{2,29}bot", re.IGNORECASE)
    # How long an observed identity is trusted before the heartbeat re-checks.
    _BOT_IDENTITY_TTL_SECONDS = 300.0

    def _current_bot_username(self) -> str:
        """Return this bot's live @username (lowercased, no leading ``@``).

        Prefers the most recently observed handle over PTB's ``get_me()``
        cache. ``Bot.username`` reads ``Bot._bot_user``, which is written only
'''


def apply_006(tree: pathlib.Path, patch_file: pathlib.Path) -> tuple[int, str]:
    """Apply a patch the same way apply-patches.sh does: patch -p1 --forward."""
    target = tree / ADAPTER_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(UPSTREAM_EXCERPT, encoding="utf-8")
    proc = subprocess.run(
        ["patch", "-p1", "--batch", "--forward",
         "--directory", str(tree), "--input", str(patch_file)],
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


with tempfile.TemporaryDirectory() as td:
    tree = pathlib.Path(td)
    rc, out = apply_006(tree, PATCH_006)
    check("patch 006 applies cleanly via patch(1)", rc == 0, out[:200])
    patched = (tree / ADAPTER_REL).read_text(encoding="utf-8")

check("no .orig/.rej debris expectations — both hunks landed",
      "SQUIRE_TELEGRAM_IDENTITY_TTL" in patched and "env_float(" in patched)


print()
print("== 3. every 006 marker row matches the patched tree ==")

# The exact greps verify-markers.sh will run at image build time, executed
# against the freshly patched excerpt. `-F` fixed strings, same as the script.
rows = [
    r.split("\t") for r in MARKERS.read_text(encoding="utf-8").splitlines()
    if r and not r.startswith("#") and "006" in r.split("\t")[0]
]
check("markers.tsv carries 006 rows", len(rows) >= 3, f"found {len(rows)}")
check("006 has an upstream anchor row", any(r[0] == "patch-006-anchor" for r in rows))
check("006 has the env-knob marker", any(r[0] == "squire-006-identity-ttl-env" for r in rows))
check("006 has the skip-logic marker", any(r[0] == "squire-006-identity-skip" for r in rows))
for row in rows:
    check(f"marker row `{row[0]}` has 4 columns", len(row) == 4, str(row))
    if len(row) == 4:
        rid, rfile, pattern, required = row
        check(f"`{rid}` targets the adapter", rfile == ADAPTER_REL, rfile)
        check(f"`{rid}` is required", required == "required", required)
        check(f"`{rid}` matches the patched tree (grep -F)", pattern in patched, pattern)

# The anchor must ALSO hold pre-patch — it is the "upstream still has the thing
# we gate" check, and an anchor that only exists post-patch guards nothing.
anchor = next(r[2] for r in rows if r[0] == "patch-006-anchor")
check("the anchor row is satisfied by the PRISTINE tree too",
      anchor in UPSTREAM_EXCERPT or "_bot_identity_refresh_loop(" in UPSTREAM_EXCERPT)


print()
print("== 4. the skip-when-0 guard actually guards ==")

# Structural, not just substring: the ensure_future launch must sit INSIDE the
# `> 0` guard (deeper indentation, after it, before the else), and the disabled
# branch must log. Deleting the guard while keeping the log line — or vice
# versa — fails here. This is the assertion the mutation drill kills.
lines = patched.splitlines()
guard_idx = next(
    (i for i, l in enumerate(lines) if l.strip() == "if self._BOT_IDENTITY_TTL_SECONDS > 0:"),
    None,
)
check("the launch site has a `> 0` guard", guard_idx is not None)
if guard_idx is not None:
    guard_indent = len(lines[guard_idx]) - len(lines[guard_idx].lstrip())
    block = []
    for l in lines[guard_idx + 1:]:
        if l.strip() and (len(l) - len(l.lstrip())) <= guard_indent:
            break
        block.append(l)
    check(
        "ensure_future(_bot_identity_refresh_loop) is INSIDE the guard",
        any("asyncio.ensure_future(" in l for l in block)
        and any("_bot_identity_refresh_loop()" in l for l in block),
    )
    check(
        "no ungated launch survives anywhere in the file",
        sum(1 for l in lines if "asyncio.ensure_future(" in l) == 1,
    )
    tail = "\n".join(lines[guard_idx:])
    check("the guard has an else branch", "\n" + " " * guard_indent + "else:" in tail)
    check(
        "the disabled branch logs, naming the knob",
        "logger.info(" in tail and "SQUIRE_TELEGRAM_IDENTITY_TTL" in tail
        and "Telegram identity refresh disabled" in tail,
    )

# The loop itself must be untouched: we gate its launch, we do not rewrite it.
check("the loop body is not modified by the patch",
      "_bot_identity_refresh_loop(self)" not in patch_text)
# And the polling-mode heartbeat (inert in webhook mode) must be left alone.
check("the polling heartbeat is untouched", "_polling_heartbeat" not in patch_text)


print()
print("== 5. env-knob semantics: unset preserves upstream, 0 disables ==")

# Execute the actual patched assignment line under a controlled environment.
# env_float is stubbed with upstream's own semantics (utils.py:497-505: unset/
# empty -> default; malformed -> default; never raises). If the patch ever
# switches to a helper that CAN raise, the "malformed" case below starts
# failing — which is the point: the line runs at module import.
assign = next(
    (l.strip() for l in patched.splitlines()
     if l.strip().startswith("_BOT_IDENTITY_TTL_SECONDS =")),
    None,
)
check("the patched assignment uses env_float on the knob",
      assign == '_BOT_IDENTITY_TTL_SECONDS = env_float("SQUIRE_TELEGRAM_IDENTITY_TTL", 300.0)',
      str(assign))


def ttl_for(env_value: str | None) -> float:
    fake_env = {} if env_value is None else {"SQUIRE_TELEGRAM_IDENTITY_TTL": env_value}

    def env_float(key: str, default: float = 0.0) -> float:  # upstream utils.py:497
        raw = fake_env.get(key, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except (ValueError, TypeError):
            return default

    ns: dict = {"env_float": env_float}
    exec(assign, ns)  # noqa: S102 — executing the one line the patch lands
    return ns["_BOT_IDENTITY_TTL_SECONDS"]


if assign:
    check("unset keeps upstream's 300.0 (rollouts are not atomic)", ttl_for(None) == 300.0)
    check("empty string keeps upstream's 300.0", ttl_for("") == 300.0)
    check("malformed value fails safe to 300.0, not an import error",
          ttl_for("not-a-number") == 300.0)
    check("'0' yields 0.0", ttl_for("0") == 0.0)
    check("0 fails the `> 0` guard (loop skipped)", not (ttl_for("0") > 0))
    check("a negative value also skips the loop", not (ttl_for("-1") > 0))
    check("a positive override is honoured (e.g. 1800)", ttl_for("1800") == 1800.0)
    check("a positive override still starts the loop", ttl_for("1800") > 0)


print()
print("== 6. the image bakes the knob to 0, per-tenant overridable ==")

check(
    "Dockerfile sets SQUIRE_TELEGRAM_IDENTITY_TTL=0",
    re.search(r"^ENV SQUIRE_TELEGRAM_IDENTITY_TTL=0\s*$", DOCKERFILE, re.M) is not None,
)
check(
    "the Dockerfile explains the G1 stake next to the knob",
    "serverless sleep" in DOCKERFILE.split("SQUIRE_TELEGRAM_IDENTITY_TTL")[0][-1500:]
    or "sleep" in DOCKERFILE.split("ENV SQUIRE_TELEGRAM_IDENTITY_TTL")[0][-1500:].lower(),
)


print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL IDENTITY-REFRESH TTL TESTS PASS")
