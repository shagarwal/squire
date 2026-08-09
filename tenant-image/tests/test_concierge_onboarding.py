#!/usr/bin/env python3
"""Deterministic onboarding — runs WITHOUT Docker.

This guards the first conversation every Squire tenant ever has. The failure it
exists to prevent already happened in production: the founder messaged the first
live tenant and got a generic reply with no introduction, no explanation of what
the agent was for, and no indication of what to do next — "just texting into a
black hole".

Everything shipped and nothing fired, for three compounding reasons, and each
one gets its own assertions here:

  1. MECHANISM. The onboarding trigger was a conditional the model had to
     proactively evaluate ("if this file is missing…") buried at line 79 of
     SOUL.md. It is now a seeded state file plus a `pre_llm_call` hook that
     STATES the current step every turn, plus an imperative at the very top of
     SOUL.md. Three independent things must fail before a user gets nothing.

  2. COMPETITION. Upstream's gateway was injecting its own first-contact
     directive into the same user-message slot, capping the introduction at
     "one or two sentences". config.yaml turns off the strong variant and
     patch 005 suppresses the rest while onboarding is live.

  3. CONTENT. Even a perfectly-firing mechanism would have produced the same
     black hole, because `greet` literally said "do not list what you can do".
     The copy assertions below are as load-bearing as the mechanical ones.

Usage: python3 tenant-image/tests/test_concierge_onboarding.py
Exit:  0 all assertions pass · 1 otherwise
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml

IMAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK = IMAGE_ROOT / "bin" / "squire-concierge-hook.py"
TEMPLATE = IMAGE_ROOT / "home-template"
SKILL_DIR = TEMPLATE / "skills" / "concierge"
STATE_MACHINE = SKILL_DIR / "state-machine.yaml"
SOUL = TEMPLATE / "SOUL.md"
CONFIG = TEMPLATE / "config.yaml"
PATCH_005 = IMAGE_ROOT / "patches" / "005-first-contact-concierge-note.patch"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except Exception:
        return False


# The hook's filename has hyphens, so it is not importable by name. Load it by
# path — we want the real module the image ships, not a copy.
_spec = importlib.util.spec_from_file_location("squire_concierge_hook", HOOK)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)  # type: ignore[union-attr]

MACHINE = yaml.safe_load(STATE_MACHINE.read_text(encoding="utf-8"))
CONFIG_YAML = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
SOUL_TEXT = SOUL.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Harness: run the hook exactly as hermes does — as a subprocess, with the turn
# payload on stdin, reading stdout as JSON.
# ---------------------------------------------------------------------------

PAYLOAD = json.dumps(
    {
        "hook_event_name": "pre_llm_call",
        "session_id": "sess_test",
        "user_message": "hey",
        "extra": {"is_first_turn": True},
    }
)


def run_hook(state_file_content: str | None, *, env_style: str = "state_dir",
             counter: str | None = None) -> tuple[int, str]:
    """Run the hook against a temp state dir. Returns (returncode, stdout)."""
    with tempfile.TemporaryDirectory() as td:
        home = pathlib.Path(td)
        state_dir = home / ".squire"
        state_dir.mkdir()
        if state_file_content is not None:
            (state_dir / "concierge-state.json").write_text(
                state_file_content, encoding="utf-8"
            )
        if counter is not None:
            (state_dir / "concierge-hook-turns").write_text(counter, encoding="utf-8")

        env = {"PATH": os.environ.get("PATH", "")}
        if env_style == "state_dir":
            env["SQUIRE_STATE_DIR"] = str(state_dir)
        else:  # exercise the HERMES_HOME fallback path
            env["HERMES_HOME"] = str(home)

        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=PAYLOAD, env=env, capture_output=True, text=True, timeout=30,
        )
        return proc.returncode, proc.stdout


def context_for(state: str, **kw) -> str:
    """Injected context for a state, or "" when the hook stays silent."""
    rc, out = run_hook(json.dumps({"state": state}), **kw)
    if not out.strip():
        return ""
    return json.loads(out)["context"]


print("== 1. the hook fires when onboarding is unfinished ==")

rc, out = run_hook(json.dumps({"state": "greet"}))
check("greet state produces output", bool(out.strip()))
check("greet output is valid JSON", _is_json(out), out[:80])
check("greet output uses the `context` key", "context" in (json.loads(out) if out.strip() else {}))
check("hook exits 0 when injecting", rc == 0, f"rc={rc}")

greet_ctx = context_for("greet")
check("injected context names the current step", "`greet`" in greet_ctx, greet_ctx[:120])
check(
    "injected context says onboarding is the ONLY job this turn",
    "ONLY job this turn" in greet_ctx,
)
check(
    "injected context marks itself as runtime-authored, not user text",
    "not from the user" in greet_ctx,
)

# The state write is the difference between "survives a restart" and "re-asks
# your name forever". A soft "remember to update the file" is the kind of
# instruction a small model silently drops, so the hook must hand over a
# literal command.
check(
    "injected context contains a literal next-state write command",
    'printf' in greet_ctx and '"state":"ask_name"' in greet_ctx,
)
check(
    "the write command targets the real state file path",
    "concierge-state.json" in greet_ctx,
)

# Every state in the flow must be drivable, not just the first one.
for state in hook._NEXT_STATE:
    ctx = context_for(state)
    check(f"state `{state}` produces a directive", bool(ctx))
    nxt = hook._NEXT_STATE[state]
    check(
        f"state `{state}` is told to advance to `{nxt}`",
        f'"state":"{nxt}"' in ctx,
    )


print()
print("== 2. the hook stays silent when it must ==")

# The single most important negative: onboarding NEVER re-fires once done.
# Without this the agent would greet a month-old user as a stranger.
check("completed onboarding injects nothing", context_for("complete") == "")
rc, _ = run_hook(json.dumps({"state": "complete"}))
check("completed onboarding still exits 0", rc == 0, f"rc={rc}")

for label, content in [
    ("missing state file", None),
    ("corrupt JSON", "{not json at all"),
    ("empty file", ""),
    ("JSON that is not an object", "[1, 2, 3]"),
    ("object with no state key", '{"name": "Ada"}'),
    ("unknown state value", '{"state": "wat"}'),
    ("null state", '{"state": null}'),
]:
    rc, out = run_hook(content)
    check(f"{label}: injects nothing", out.strip() == "", out[:80])
    check(f"{label}: exits 0 (fail-open)", rc == 0, f"rc={rc}")

# A hook in front of every message must never be the reason a turn dies.
check(
    "hook resolves state via HERMES_HOME when SQUIRE_STATE_DIR is unset",
    context_for("greet", env_style="hermes_home") != "",
)


print()
print("== 3. the safety valve bounds the opposite failure ==")

# If the agent never writes the state file, an unbounded hook would re-inject
# the same directive forever — Groundhog Day, on a trial capped at 75 messages
# a day. Endless onboarding is as brand-damaging as none.
maxt = hook.MAX_INJECTED_TURNS
check("valve is documented as a module constant", isinstance(maxt, int) and maxt > 0)
# An absolute bound, not just a relative one. Every other assertion in this
# section is expressed in terms of MAX_INJECTED_TURNS, so raising the constant
# to a billion would "disable the valve" while keeping them all green. The flow
# is 8 states; anything past ~40 turns of nagging is not a safety valve.
check(
    "valve is set to a value that actually bounds nagging",
    len(hook._DIRECTIVES) <= maxt <= 40,
    f"MAX_INJECTED_TURNS={maxt}",
)
check(
    "just under the limit still injects",
    context_for("greet", counter=str(maxt - 1)) != "",
)
check(
    "at the limit the hook goes quiet",
    context_for("greet", counter=str(maxt)) == "",
)
check(
    "well past the limit stays quiet",
    context_for("greet", counter=str(maxt * 10)) == "",
)
# A corrupt counter must not disable onboarding.
check(
    "unreadable counter falls back to injecting",
    context_for("greet", counter="not-a-number") != "",
)


print()
print("== 4. the hook and the state machine cannot drift ==")

states = MACHINE["states"]
completion = MACHINE["completion_state"]
initial = MACHINE["initial"]

check("state machine parses as YAML", isinstance(states, dict) and bool(states))
check(f"initial state `{initial}` exists", initial in states)
check(f"completion state `{completion}` exists", completion in states)

yaml_drivable = {s for s in states if s != completion}
check(
    "hook has a directive for every non-terminal state",
    set(hook._DIRECTIVES) == yaml_drivable,
    f"hook={sorted(hook._DIRECTIVES)} yaml={sorted(yaml_drivable)}",
)

yaml_edges = {s: v.get("then") for s, v in states.items() if v.get("then")}
check(
    "hook transition table matches the YAML `then:` edges",
    hook._NEXT_STATE == yaml_edges,
    f"hook={hook._NEXT_STATE} yaml={yaml_edges}",
)
check("hook agrees on the completion state", hook.COMPLETION_STATE == completion)

# Every `then:` must land somewhere real, or the flow strands mid-onboarding.
for name, spec in states.items():
    nxt = spec.get("then")
    if nxt is not None:
        check(f"`{name}`.then -> `{nxt}` resolves", nxt in states)

# Reachability: walk from the initial state and confirm we touch everything and
# terminate. An unreachable state is dead copy someone will maintain forever; a
# non-terminating one is an onboarding loop.
seen, cur, terminated = set(), initial, False
for _ in range(len(states) + 2):
    if cur in seen:
        break
    seen.add(cur)
    if cur == completion:
        terminated = True
        break
    cur = states[cur].get("then")
    if cur is None:
        break
check("every state is reachable from the initial state", seen == set(states), f"unreached={sorted(set(states)-seen)}")
check("the flow terminates at the completion state", terminated)
check("the completion state is terminal", states[completion].get("then") is None)


print()
print("== 5. the greeting has actual substance ==")

greet = states["greet"]


def flat(text: str) -> str:
    """Collapse whitespace before substring matching.

    Both the YAML and the hook wrap their copy at ~80 columns, so a phrase like
    "their own AI account" is routinely split across a newline. Matching raw
    text here would produce assertions that pass or fail on line-wrapping,
    which is exactly the kind of test that gets deleted in six months.
    """
    return " ".join(text.split()).lower()


greet_blob = flat(json.dumps(greet))
greet_directive = flat(greet_ctx)
caps = {c["id"]: flat(c["say"]) for c in MACHINE["capabilities"]}
# What the agent will actually read on turn one: the greet script plus the
# capability lines it is told to draw from.
greet_effective = greet_blob + " " + " ".join(caps.values())

# The regression, stated directly. Suppressing capability talk is what produced
# a greeting with nothing in it — and the instruction must not survive even as
# a quotation, because a small model may read it as live guidance.
check(
    "no live instruction against explaining what the agent can do",
    "do not list what you can do" not in greet_blob,
)

# Who / what / what-next, in both the YAML script and the injected directive,
# because on turn one the model may act on either.
for label, blob in [
    ("state machine", greet_effective),
    ("injected directive", greet_directive),
]:
    check(f"greet ({label}) says what the agent IS", "personal assistant" in blob)
    check(f"greet ({label}) promises memory across conversations", "remember" in blob)
    check(
        f"greet ({label}) promises follow-through / scheduling",
        "schedule" in blob or "follow up" in blob,
    )
    check(
        f"greet ({label}) promises to check before dangerous actions",
        "check with them first" in blob or "before anything that deletes" in blob,
    )
    check(f"greet ({label}) tells them about the trial", "trial" in blob)
    check(
        f"greet ({label}) tells them what they must DO next",
        "own ai account" in blob,
    )
    check(f"greet ({label}) asks their name", "call" in blob)
    # /help is upstream's onboarding surface, not Squire's. It is not enough for
    # the greeting to omit it — the whole bug was a competing upstream note
    # pushing it, so our copy must actively rule it out.
    check(
        f"greet ({label}) explicitly forbids sending them to /help",
        "do not mention slash commands or /help" in blob,
    )

check("greet still asks exactly one question", greet.get("ask") is not None)
check(
    "greet quotes no prices (they belong in the facts block)",
    not re.search(r"\$\s*\d", greet_blob),
)

# Capability claims must be backed by the authoritative block, not invented.
check("capabilities block exists and is non-empty", bool(caps))
check(
    "greet draws on capability ids that actually exist",
    {"memory", "follow_through", "approvals"} <= set(caps),
    f"caps={sorted(caps)}",
)
for cap_id in ("memory", "follow_through", "approvals"):
    check(
        f"greet references the `{cap_id}` capability by id",
        cap_id in greet_blob,
    )
check(
    "every capability documents why it is honestly claimable",
    all(c.get("real_because") for c in MACHINE["capabilities"]),
)


print()
print("== 6. honesty rules survive the rewrite ==")

facts = MACHINE["facts"]
providers = {p["id"]: p for p in MACHINE["providers"]}

check("all four provider options are present", len(providers) == 4)
check(
    "the Claude Max option keeps its unsupported warning",
    "not supported by anthropic" in providers["claude_max_oauth"]["honest_label"].lower(),
)
check(
    "the Claude Max option keeps its API-key fallback",
    providers["claude_max_oauth"]["fallback"] == "anthropic_api_key",
)
check(
    "the Codex option is still labelled sanctioned",
    "sanction" in providers["openai_codex_oauth"]["honest_label"].lower(),
)

# The trial numbers the injected directives repeat must match the facts block —
# the hook is allowed to inline them, but not to invent them.
check("trial length agrees with the facts block", "3 days" in facts["trial_length"])
check(
    "the trial directive repeats the facts-block numbers",
    "3 days" in hook._DIRECTIVES["trial_explainer"]
    and "75 messages a day" in facts["trial_limits"]
    and "75 messages a day" in hook._DIRECTIVES["trial_explainer"]
    and "14 days" in hook._DIRECTIVES["trial_explainer"],
)
check(
    "no directive quotes a price (prices are answer-only, from facts)",
    not any(re.search(r"\$\s*\d", d) for d in hook._DIRECTIVES.values()),
    # NB: ${HERMES_HOME} / ${HERMES_SKILL_DIR} legitimately contain "$" —
    # this must match a currency amount, not any dollar sign.
)

# Regression on a real inaccuracy found while doing this work: cron is a
# Starter feature per prd.md §5.4; Pro adds capacity, not the feature.
check(
    "Pro pricing no longer claims to be what unlocks cron",
    "adds whatsapp, voice notes, extra personas and cron" not in facts["price_pro"].lower(),
    facts["price_pro"],
)
check("Starter is described as the full agent", "full agent" in facts["price_starter"].lower())

# The connected state must not announce a promise the platform cannot keep yet.
check(
    "the connected directive does not claim the trial key was revoked",
    "revoked" not in hook._DIRECTIVES["connected"].lower()
    or "do not say" in hook._DIRECTIVES["connected"].lower(),
)


print()
print("== 7. SOUL.md carries the mandate, and carries it FIRST ==")

check("SOUL.md still mentions the concierge skill", "concierge" in SOUL_TEXT)
check(
    "SOUL.md points at the state file",
    ".squire/concierge-state.json" in SOUL_TEXT,
)
check(
    "the mandate is imperative, not a bare conditional",
    "ONLY job this turn" in SOUL_TEXT,
)
check(
    "the mandate still exempts completed tenants",
    "complete" in SOUL_TEXT,
)

# Position is the point. Buried at line 79 of 93 it never fired.
body = SOUL_TEXT.split("-->", 1)[-1]          # drop the maintainer comment block
headings = [ln.strip() for ln in body.splitlines() if ln.startswith("## ")]
check("SOUL.md has section headings", bool(headings))
check(
    "the onboarding section is the FIRST section in the file",
    "onboarding" in headings[0].lower(),
    f"first heading = {headings[0] if headings else '<none>'}",
)

onboarding_pos = body.lower().index("onboarding")
check(
    "the mandate sits in the first quarter of the file",
    onboarding_pos < len(body) / 4,
    f"pos={onboarding_pos} of {len(body)}",
)
check(
    "the old buried 'The first conversation' section is gone",
    "## The first conversation" not in SOUL_TEXT,
)


print()
print("== 8. config wires the hook and silences the competing directive ==")

hooks_cfg = CONFIG_YAML.get("hooks") or {}
pre_llm = hooks_cfg.get("pre_llm_call") or []
check("a pre_llm_call hook is configured", len(pre_llm) == 1, str(hooks_cfg))
check(
    "the hook points at the concierge script",
    pre_llm and pre_llm[0]["command"].endswith("squire-concierge-hook.py"),
)
check("the hook has a timeout", bool(pre_llm and pre_llm[0].get("timeout")))

# The gateway has no TTY. Without auto-accept the hook silently never
# registers — a failure indistinguishable from the bug being fixed.
check(
    "shell hooks are auto-accepted (the gateway has no TTY to consent on)",
    CONFIG_YAML.get("hooks_auto_accept") is True,
)

# Upstream's profile-build directive is the strongest competitor for the same
# slot on message one.
check(
    "upstream's profile-build onboarding is turned off",
    (CONFIG_YAML.get("onboarding") or {}).get("profile_build") == "off",
)

# Guard the coordinator's separate fix so a careless merge cannot revert it.
check(
    "per-message reactions stay off (founder feedback, commit bfbacbc)",
    (CONFIG_YAML.get("telegram") or {}).get("reactions") is False,
)

check(
    "the trial model is still Haiku-class (directives are written for it)",
    "haiku" in str(CONFIG_YAML.get("model", "")).lower(),
)


print()
print("== 9. patch 005 suppresses upstream's competing note ==")

patch_text = PATCH_005.read_text(encoding="utf-8")
check("patch 005 exists", bool(patch_text))
check("patch 005 targets gateway/run.py", "b/gateway/run.py" in patch_text)
check(
    "patch 005 keys on the concierge state file",
    "concierge-state.json" in patch_text,
)
check(
    "patch 005 only suppresses while onboarding is unfinished",
    '!= "complete"' in patch_text,
)
check(
    "patch 005 fails open to upstream behaviour",
    "except Exception:" in patch_text and "_squire_concierge_active = False" in patch_text,
)

markers = (IMAGE_ROOT / "patches" / "markers.tsv").read_text(encoding="utf-8")
check("patch 005 has an upstream anchor marker", "patch-005-anchor" in markers)
check("patch 005 has a squire marker", "squire-005-concierge-suppress" in markers)
# The config knob is only honoured while upstream still calls
# profile_build_mode(). If upstream drops it, `profile_build: off` becomes a
# no-op that still LOOKS configured — the build must fail rather than quietly
# hand the first message back to upstream's directive.
check(
    "the profile_build config knob is build-checked against upstream",
    "upstream-onboarding-profile-build" in markers,
)
for row in markers.splitlines():
    if row.startswith(("patch-005", "squire-005", "upstream-onboarding")):
        check(f"marker row `{row.split(chr(9))[0]}` has 4 columns", len(row.split("\t")) == 4, row)


print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CONCIERGE ONBOARDING TESTS PASS")
