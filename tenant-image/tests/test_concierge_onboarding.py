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
DOCKERFILE = (IMAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")

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

def payload(**over) -> str:
    """A realistic pre_llm_call payload, shaped the way shell_hooks.py sends it.

    `parent_session_id` is a TOP-LEVEL key (_TOP_LEVEL_PAYLOAD_KEYS at
    agent/shell_hooks.py:431); the rest of the hook site's kwargs land in
    `extra`. Getting this shape right is the whole point of the subagent and
    cron assertions below.
    """
    extra = {
        "user_message": "hey",
        "is_first_turn": True,
        "model": "anthropic:claude-haiku-4-5",
        "platform": "telegram",
    }
    extra.update(over.pop("extra", {}))
    base = {
        "hook_event_name": "pre_llm_call",
        "session_id": "sess_test",
        "parent_session_id": "",
        "cwd": "/opt/data",
        "extra": extra,
    }
    base.update(over)
    return json.dumps(base)


PAYLOAD = payload()


def run_hook(state_file_content: str | None, *, env_style: str = "state_dir",
             counter: str | None = None, stdin: str | None = None) -> tuple[int, str]:
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
            input=PAYLOAD if stdin is None else stdin,
            env=env, capture_output=True, text=True, timeout=30,
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
# the same directive forever — Groundhog Day, on a metered trial. Endless
# onboarding is as brand-damaging as none.
maxt = hook.MAX_INJECTED_TURNS_PER_STATE
check("valve is documented as a module constant", isinstance(maxt, int) and maxt > 0)
# An absolute bound, not just a relative one. Every other assertion in this
# section is expressed in terms of the constant, so raising it to a billion
# would "disable the valve" while keeping them all green.
check(
    "valve is set to a value that actually bounds nagging",
    2 <= maxt <= 12,
    f"MAX_INJECTED_TURNS_PER_STATE={maxt}",
)
check(
    "just under the limit still injects",
    context_for("greet", counter=f"greet {maxt - 1}") != "",
)
check(
    "at the limit the hook goes quiet",
    context_for("greet", counter=f"greet {maxt}") == "",
)
check(
    "well past the limit stays quiet",
    context_for("greet", counter=f"greet {maxt * 10}") == "",
)

# The budget is PER STATE and resets on progress. A global budget would punish a
# user who simply chats a lot at each step, and would be drained by any subagent
# or cron turn that slipped through — penalising the flow for making progress is
# the wrong failure to engineer for.
check(
    "an exhausted budget for a DIFFERENT state does not silence this one",
    context_for("ask_timezone", counter=f"greet {maxt * 10}") != "",
)
check(
    "advancing the state resets the budget",
    context_for("ask_name", counter=f"greet {maxt}") != "",
)

# A corrupt or legacy (bare-integer) counter must not disable onboarding.
check(
    "unreadable counter falls back to injecting",
    context_for("greet", counter="not-a-number") != "",
)
check(
    "legacy bare-integer counter falls back to injecting",
    context_for("greet", counter="9") != "",
)


print()
print("== 3b. the hook fires for the USER only, not subagents or cron ==")

# Delegated subagents and cron jobs drive the same conversation loop and so the
# same hook. "Say hello and introduce yourself" injected into a subagent is a
# greeting nobody reads, and it burns the valve. ask_first_job actively
# encourages starting real work mid-onboarding, so delegation during onboarding
# is expected rather than hypothetical.
rc, out = run_hook(
    json.dumps({"state": "greet"}), stdin=payload(parent_session_id="sess_parent")
)
check("delegated subagent turn injects nothing", out.strip() == "", out[:80])
check("delegated subagent turn exits 0", rc == 0, f"rc={rc}")

rc, out = run_hook(
    json.dumps({"state": "greet"}), stdin=payload(extra={"platform": "cron"})
)
check("cron turn injects nothing", out.strip() == "", out[:80])
check("cron turn exits 0", rc == 0, f"rc={rc}")

# ...but a real user turn still fires, on any platform.
for plat in ("telegram", "whatsapp", ""):
    rc, out = run_hook(
        json.dumps({"state": "greet"}), stdin=payload(extra={"platform": plat})
    )
    check(f"user turn on platform {plat!r} still injects", out.strip() != "")

# The hook must survive a payload shape it did not expect rather than going
# silent — a stdin surprise must not cost someone their onboarding.
for label, blob in [
    ("empty stdin", ""),
    ("non-JSON stdin", "not json"),
    ("JSON array stdin", "[1,2]"),
    ("payload with no extra", json.dumps({"session_id": "s"})),
]:
    rc, out = run_hook(json.dumps({"state": "greet"}), stdin=blob)
    check(f"{label}: still injects (fail-open)", out.strip() != "", out[:60])
    check(f"{label}: exits 0", rc == 0, f"rc={rc}")


print()
print("== 3c. injected paths are ones the agent can actually open ==")

# ${HERMES_SKILL_DIR} is substituted ONLY while hermes loads skill *content*
# (skills/skill_commands.py, gated on skills.template_vars). It is not an OS
# env var, so in the shell the terminal tool runs it expands to nothing and
# "${HERMES_SKILL_DIR}/state-machine.yaml" becomes "/state-machine.yaml".
# Worst in trial_explainer, where that pointer IS the guard rail against
# inventing prices and limits.
all_ctx = {st: context_for(st) for st in hook._NEXT_STATE}
for st, ctx in all_ctx.items():
    check(
        f"`{st}` directive contains no unexpanded template variable",
        "${" not in ctx and "{skill_file}" not in ctx,
        [tok for tok in ("${HERMES_SKILL_DIR}", "${HERMES_HOME}", "{skill_file}") if tok in ctx],
    )
for st in ("trial_explainer", "connect_llm", "awaiting_credential"):
    check(
        f"`{st}` points at state-machine.yaml by absolute path",
        "/skills/concierge/state-machine.yaml" in all_ctx[st],
    )
check(
    "the timezone step points at config.yaml by absolute path",
    "/config.yaml" in all_ctx["ask_timezone"]
    and "${" not in all_ctx["ask_timezone"],
)
# HERMES_HOME is a real env var (Dockerfile) so paths resolve under it.
check(
    "paths resolve under HERMES_HOME",
    re.search(r"^ENV .*HERMES_HOME=/opt/data", DOCKERFILE, re.M) is not None
    or "HERMES_HOME=/opt/data" in DOCKERFILE,
)

# The timezone write must be surgical. config.yaml also carries the `hooks:`
# block that makes onboarding work at all — an agent rewriting the file
# wholesale to set one key could drop it and disable the mechanism.
check(
    "the timezone write targets only the timezone line",
    "^timezone:" in all_ctx["ask_timezone"],
    all_ctx["ask_timezone"][:200],
)

print()
print("== 3d. the state-write command is self-consistent ==")

# An earlier version told the model to run a truncating `printf >` AND to keep
# the file's existing keys — two contradictory instructions in exactly the place
# we are engineering against silent skips.
seeded = {"state": "greet", "name": None, "timezone": None, "llm": "trial",
          "updated": "2026-08-09T00:00:00Z"}
rc, out = run_hook(json.dumps(seeded))
ctx = json.loads(out)["context"]
check(
    "the write command carries every key the state file had",
    all(f'"{k}"' in ctx for k in seeded),
    [k for k in seeded if f'"{k}"' not in ctx],
)
check(
    "the write command no longer also demands manual key preservation",
    "keeping any keys" not in ctx,
)
# It must be runnable as-is: extract and execute it against a temp file.
def execute_write_command(context: str) -> dict | None:
    """Actually run the emitted command; None if it is not valid shell/JSON.

    Returns rather than raises so a badly-quoted command reports as a clean
    FAIL instead of aborting the suite before the later sections run.
    """
    try:
        cmd = next(
            l.strip() for l in context.splitlines() if l.strip().startswith("printf ")
        )
    except StopIteration:
        return None
    with tempfile.TemporaryDirectory() as td:
        target = pathlib.Path(td) / "concierge-state.json"
        try:
            subprocess.run(
                ["bash", "-c", cmd.rsplit(">", 1)[0] + "> " + str(target)],
                check=True, timeout=15, capture_output=True,
            )
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return None


written = execute_write_command(ctx)
check("the emitted command is valid shell and writes valid JSON", isinstance(written, dict))
check("running it advances the state", (written or {}).get("state") == "ask_name", str(written))
check(
    "running it preserves the other keys",
    written is not None
    and all(written.get(k) == v for k, v in seeded.items() if k != "state"),
    str(written),
)
# A name with an apostrophe must not break out of the shell quoting — this is
# the difference between shlex.quote and hand-rolled single quotes.
rc, out = run_hook(json.dumps({**seeded, "state": "ask_name", "name": "O'Brien"}))
w2 = execute_write_command(json.loads(out)["context"])
check(
    "a name containing an apostrophe survives shell quoting",
    (w2 or {}).get("name") == "O'Brien",
    str(w2),
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
trial_directive = context_for("trial_explainer")
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

# Removing the "don't list capabilities" brake made the OPPOSITE failure live:
# a wall of text as message one. The YAML's "fifteen seconds" note is the
# non-authoritative half — the hook directive is what the model actually reads.
check(
    "the greet directive caps its own length",
    "120 words" in greet_directive and "8 short lines" in greet_directive,
)
check(
    "the greet directive names the wall-of-text failure explicitly",
    "wall of text" in greet_directive,
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
# The message-count figure must live in ONE place. The trial model is moving
# Haiku -> Sonnet, which changes how far the $2 cap stretches, so this number is
# about to be edited — and a second copy inlined in the hook would silently keep
# telling users the old figure.
check(
    "the per-day message count lives in the facts block",
    "75 messages a day" in facts["trial_limits"],
)
check(
    "the trial directive does NOT inline the message count",
    not any(
        "75 message" in d or "75/day" in d for d in hook._DIRECTIVES.values()
    ),
)
check(
    "the trial directive points at the facts block by absolute path",
    "/skills/concierge/state-machine.yaml" in trial_directive
    and "`facts`" in trial_directive,
    trial_directive[:160],
)
check(
    "the trial directive forbids stating limits from memory",
    "from memory" in trial_directive.lower(),
)
# prd.md:118 lists the spend cap alongside the message count. Quoting only the
# message count implies 75/day is reachable when the $2 cap may end the trial
# far sooner — that is us misleading the user, not the user misusing it.
check(
    "the trial's spend cap is stated alongside the message count",
    "$2" in facts["trial_limits"],
    facts["trial_limits"],
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
# The tenant-served /connect/<nonce> endpoint ships in Phase 1C. Leading with
# "send the one-time link" invites the agent to invent a URL — a dead end for
# the user and a phishing-shaped habit to teach. The YAML notes said so, but
# notes are the third field down; the hook copy is what gets read.
_await = all_ctx["awaiting_credential"]
check(
    "the credential step says the one-time link does not exist yet",
    "does not exist yet" in _await.lower(),
)
check(
    "the credential step forbids inventing a URL",
    "never invent a url" in _await.lower(),
)
check(
    "the credential state's say[] no longer leads with sending the link",
    not str(states["awaiting_credential"]["say"][0]).lower().startswith("send the one-time link"),
    str(states["awaiting_credential"]["say"][0])[:80],
)

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
# A missing state file must mean SILENCE, not "onboard them". SOUL.md has no
# safety valve: on any tenant without the file, "this is your ONLY job" would
# assert on every turn forever. The hook is silent on a missing file for the
# same reason and these two must agree.
check(
    "SOUL.md no longer treats a missing state file as unfinished onboarding",
    "or the file is missing" not in SOUL_TEXT,
)
check(
    "SOUL.md says explicitly what a missing file means",
    "does not exist" in SOUL_TEXT,
)
# Prose that ships to the model every single turn should be instruction, not
# archaeology. Rationale belongs in HTML comments, which are still there for a
# maintainer but cost the model nothing to act on.
_visible = re.sub(r"<!--.*?-->", "", SOUL_TEXT, flags=re.S)
check(
    "SOUL.md's maintainer archaeology is in comments, not the prompt body",
    "it used to be" not in _visible.lower() and "never noticed it" not in _visible.lower(),
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

# The gateway has no TTY. Without an auto-accept opt-in the hook silently never
# registers — a failure indistinguishable from the bug being fixed.
#
# It must be the Dockerfile env var, NOT `hooks_auto_accept` in config.yaml:
# config.yaml is seeded once and then owned by the tenant (hermes rewrites it on
# migrations and via upstream mark_seen; the agent edits `timezone:`), so a key
# there only looks image-managed. HERMES_ACCEPT_HOOKS is honoured by
# _resolve_effective_accept (agent/shell_hooks.py:848).
check(
    "auto-accept is set in the image, via HERMES_ACCEPT_HOOKS",
    re.search(r"^ENV HERMES_ACCEPT_HOOKS=1", DOCKERFILE, re.M) is not None,
)
# The parsed key, not the raw text — the comment above it legitimately explains
# why hooks_auto_accept is NOT used here, and that prose should stay.
check(
    "auto-accept is not an active key in the tenant-writable config",
    CONFIG_YAML.get("hooks_auto_accept") is None,
    f"hooks_auto_accept={CONFIG_YAML.get('hooks_auto_accept')!r}",
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
# The state read must sit INSIDE the first-message guard: that block runs once
# per tenant lifetime, so onboarding costs no filesystem stat on any later turn.
check(
    "patch 005 reads state inside the first-message guard, not on every turn",
    patch_text.index("has_any_sessions") < patch_text.index("_sq_state_path"),
)
check(
    "patch 005 guards every append site (3 of them)",
    patch_text.count("(squire patch 005)") >= 3,
    f"guards={patch_text.count('(squire patch 005)')}",
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
