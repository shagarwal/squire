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
        "model": "anthropic:claude-sonnet-5",
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
             counter: str | None = None, stdin: str | None = None,
             env_home: str | None = None) -> tuple[int, str]:
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
        if env_home is not None:
            # Point the directive's PATH templating at a real tree, so an
            # emitted command can be executed against actual files.
            env["HERMES_HOME"] = env_home

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
print("== 3b-bis. one injection per user turn, and one message per injection ==")

def flat(text: str) -> str:
    """Collapse whitespace before substring matching.

    Both the YAML and the hook wrap their copy at ~80 columns, so a phrase like
    "their own AI account" is routinely split across a newline. Matching raw
    text here would produce assertions that pass or fail on line-wrapping,
    which is exactly the kind of test that gets deleted in six months.
    """
    return " ".join(text.split()).lower()


# The injected context for every drivable state, reused by the sections below.
all_ctx = {st: context_for(st) for st in hook._NEXT_STATE}

# v0.1.2 landed the greeting and then sent a SECOND message, "Done — let me
# know what to call you.". Diagnosed before fixing: that was NOT a second
# injection. hermes calls pre_llm_call from build_turn_context
# (conversation_loop.py:1297), which sits BEFORE the API loop at :1402 under a
# comment reading "All once-per-turn setup ... the pre_llm_call plugin hook".
# One firing per user turn however many API round-trips it takes.
#
# The real cause was ONE turn emitting two assistant text blocks —
# greeting -> terminal tool call -> trailing "Done ..." — with the gateway
# delivering mid-turn prose as its own Telegram message
# (interim_assistant_messages, config_defaults.py:1193). The decisive tell: the
# second message re-asked for the NAME, whereas an ask_name re-injection would
# have asked for the TIMEZONE.
#
# So the fix is ordering + an explicit one-message rule, asserted here.
for st, ctx in all_ctx.items():
    body = flat(ctx)
    check(
        f"`{st}` orders the state write BEFORE any message",
        "step 1" in body and "before you say anything" in body,
    )
    check(
        f"`{st}` demands exactly one message",
        "send only that one message this turn" in body,
    )
    check(
        f"`{st}` forbids a trailing acknowledgement",
        'no "done"' in body and "no summary of what you just did" in body,
    )
    # The write command must come before the directive text in the injected
    # string, or "first" is only a claim.
    check(
        f"`{st}` puts the command ahead of the reply copy in the text",
        ctx.index("STEP 1") < ctx.index("STEP 2"),
    )

# Belt and braces: dedupe on turn id, so that if a future upstream ever moves
# the hook inside the API loop we degrade to one injection instead of
# re-greeting on every tool call.
TURN = json.dumps({"state": "greet"})
with tempfile.TemporaryDirectory() as td:
    home = pathlib.Path(td)
    sd = home / ".squire"; sd.mkdir()
    (sd / "concierge-state.json").write_text(TURN, encoding="utf-8")

    def call(turn_id: str, state_json: str | None = None) -> str:
        if state_json is not None:
            (sd / "concierge-state.json").write_text(state_json, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=payload(extra={"turn_id": turn_id}),
            env={"PATH": os.environ.get("PATH", ""), "SQUIRE_STATE_DIR": str(sd)},
            capture_output=True, text=True, timeout=30,
        )
        return proc.stdout

    first = call("turn-1")
    second = call("turn-1")          # same turn, e.g. a continuation call
    check("first call in a turn injects", first.strip() != "")
    check("a second call in the SAME turn injects nothing", second.strip() == "", second[:80])

    third = call("turn-2")           # next user turn, state unchanged
    check("the NEXT user turn injects again", third.strip() != "")

    # A state change between turns must still advance the copy.
    fourth = call("turn-3", json.dumps({"state": "ask_name"}))
    check("a state change between turns still advances", fourth.strip() != "")
    check(
        "the advanced turn carries the new state's directive",
        "`ask_name`" in json.loads(fourth)["context"],
    )

    # A missing turn id must never suppress onboarding.
    fifth = call("")
    check("an empty turn id does not suppress injection", fifth.strip() != "")


print()
print("== 3c. injected paths are ones the agent can actually open ==")

# ${HERMES_SKILL_DIR} is substituted ONLY while hermes loads skill *content*
# (skills/skill_commands.py, gated on skills.template_vars). It is not an OS
# env var, so in the shell the terminal tool runs it expands to nothing and
# "${HERMES_SKILL_DIR}/state-machine.yaml" becomes "/state-machine.yaml".
# Worst in trial_explainer, where that pointer IS the guard rail against
# inventing prices and limits.
for st, ctx in all_ctx.items():
    check(
        f"`{st}` directive contains no unexpanded template variable",
        "${" not in ctx and "{skill_file}" not in ctx,
        [tok for tok in ("${HERMES_SKILL_DIR}", "${HERMES_HOME}", "{skill_file}") if tok in ctx],
    )
for st in ("ask_name", "connect_llm", "awaiting_credential"):
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
print("== 3c-bis. EVERY emitted command must run exactly as printed ==")

# The rule this section enforces, learned the hard way: an emitted command is
# only as good as its ability to run VERBATIM. The timezone step originally
# emitted an indented `python3 - <<'PY'` heredoc; a heredoc delimiter must sit
# at column 0, so it died with "here-document delimited by end-of-file" and an
# IndentationError, silently leaving config.yaml on UTC. It was missed because
# it was only ever substring-checked, while the state-write command was
# genuinely executed. So: check them all, structurally and by execution.

_CMD_STARTS = ("printf ", "python3 ", "python ", "sh ", "bash ", "sed ", "cat ", "echo ")


def emitted_commands(context: str) -> list[str]:
    """Every line in an injected directive that reads as a shell command."""
    return [
        ln.strip() for ln in context.splitlines() if ln.strip().startswith(_CMD_STARTS)
    ]


all_cmds = {st: emitted_commands(ctx) for st, ctx in all_ctx.items()}
check(
    "the states that promise a command actually emit one",
    all(all_cmds[st] for st in ("greet", "ask_timezone")),
    {k: len(v) for k, v in all_cmds.items()},
)

for st, cmds in all_cmds.items():
    # A multi-line construct cannot survive being emitted inside indented
    # prose. Ban the whole class rather than the one instance we hit.
    check(
        f"`{st}` emits no heredoc (cannot survive indentation)",
        "<<" not in all_ctx[st],
    )
    for i, cmd in enumerate(cmds):
        # bash -n parses without executing: catches unterminated quotes and
        # heredocs regardless of what the command would do.
        rc_syntax = subprocess.run(
            ["bash", "-n", "-c", cmd], capture_output=True, text=True
        ).returncode
        check(f"`{st}` command #{i} is syntactically valid shell", rc_syntax == 0, cmd[:90])
        check(
            f"`{st}` command #{i} is a single line",
            "\n" not in cmd and len(cmd.splitlines()) == 1,
        )

# Now actually RUN the timezone command, against a copy of the REAL template —
# the one that carries the hooks: block onboarding depends on.
with tempfile.TemporaryDirectory() as td:
    home = pathlib.Path(td)
    cfg = home / "config.yaml"
    cfg.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    before = cfg.read_text(encoding="utf-8")

    tz_ctx = context_for("ask_timezone", env_home=str(home))
    # Select by content, not position: the state-write command is now emitted
    # FIRST (STEP 1), so indexing would silently run the wrong one — which is
    # exactly what this test caught when the ordering changed.
    tz_cmds = [c for c in emitted_commands(tz_ctx) if "timezone:" in c]
    check("exactly one timezone command is emitted", len(tz_cmds) == 1, str(len(tz_cmds)))
    proc = subprocess.run(
        ["bash", "-c", tz_cmds[0]] if tz_cmds else ["false"],
        capture_output=True, text=True, timeout=30,
    )
    after = cfg.read_text(encoding="utf-8")
    try:
        parsed = yaml.safe_load(after)
    except Exception:
        parsed = None

check("the timezone command exits 0", proc.returncode == 0, proc.stderr[:200])
check("config.yaml still parses as YAML afterwards", isinstance(parsed, dict))
check(
    "the timezone actually changed",
    isinstance(parsed, dict) and parsed.get("timezone") == "Europe/London",
    str((parsed or {}).get("timezone")),
)
# The regression that would disable onboarding entirely.
check(
    "the hooks: block survives the timezone write",
    isinstance(parsed, dict)
    and (parsed.get("hooks") or {}).get("pre_llm_call")
    and parsed["hooks"]["pre_llm_call"][0]["command"].endswith("squire-concierge-hook.py"),
    str((parsed or {}).get("hooks")),
)
check(
    "the timezone write changes exactly one line",
    sum(1 for a, b in zip(before.splitlines(), after.splitlines()) if a != b) == 1,
)
check(
    "no other config key is disturbed",
    isinstance(parsed, dict)
    and parsed.get("onboarding", {}).get("profile_build") == "off"
    and (parsed.get("telegram") or {}).get("reactions") is False,
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

# Founder feedback 2026-08-13: connect-first. The ORDER is the product decision
# now, so pin it exactly — a reorder that slides a trial pitch back in front of
# the connect step must fail this build, not slip through as "still 8 states".
EXPECTED_ORDER = [
    "greet", "ask_name", "connect_llm", "awaiting_credential",
    "connected", "ask_timezone", "ask_first_job", "complete",
]
_walk, _cur = [], initial
for _ in range(len(states) + 2):
    _walk.append(_cur)
    if _cur == completion:
        break
    _cur = states[_cur].get("then")
check("the connect-first order is pinned exactly", _walk == EXPECTED_ORDER, str(_walk))
check(
    "LLM connection is the first substantive step after the name exchange",
    hook._NEXT_STATE.get("ask_name") == "connect_llm",
)
check(
    "the standalone trial-explainer state is gone from both sides",
    "trial_explainer" not in states and "trial_explainer" not in hook._DIRECTIVES,
)
check(
    "connecting resumes onboarding rather than ending it",
    hook._NEXT_STATE.get("connected") == "ask_timezone",
)


print()
print("== 5. the greeting has actual substance ==")

greet = states["greet"]


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
    # Founder feedback 2026-08-13: the greeting must not open with the trial,
    # pricing, or payment — that mixing is what made message one read like a
    # bill. The ban must be a LIVE instruction in the copy, not just an
    # absence: an absence is one careless rewrite away from silently coming
    # back, an instruction has to be deleted on purpose.
    check(
        f"greet ({label}) explicitly forbids mentioning the trial",
        "do not mention the trial" in blob,
    )
    check(
        f"greet ({label}) extends the ban to prices and payment",
        "prices, or paying for anything" in blob,
    )
    check(
        f"greet ({label}) carries no affirmative trial pitch",
        "3-day trial" not in blob and "squire's shared ai" not in blob,
    )
    # Founder feedback 2026-08-19, reversing 2026-08-13's connect promise:
    # message one is "what I am + three capabilities + your name?" and NOTHING
    # else — connecting the AI account debuts in message two (ask_name's
    # reply), where its allowance framing lives anyway. Same live-instruction
    # rule as the trial ban above: the copy must actively forbid the
    # pre-announcement, not merely omit it.
    check(
        f"greet ({label}) explicitly forbids pre-announcing the connect step",
        "do not pre-announce" in blob,
    )
    check(
        f"greet ({label}) does not name the providers (that is ask_name's job)",
        "openai" not in blob and "anthropic" not in blob,
    )
    # Anthropic is API-key only since 2026-08-14 (Consumer ToS change,
    # server-side enforced): the greeting must not promise connecting a
    # "Claude account" the flow can no longer accept.
    check(
        f"greet ({label}) no longer promises a Claude account",
        "claude" not in blob,
    )
    check(f"greet ({label}) asks their name", "call" in blob)
    # /help is upstream's onboarding surface, not Squire's. It is not enough for
    # the greeting to omit it — the whole bug was a competing upstream note
    # pushing it, so our copy must actively rule it out.
    check(
        f"greet ({label}) explicitly forbids sending them to /help",
        "do not mention slash commands or /help" in blob,
    )

print()
print("== 5b. ask_timezone hands the user a menu, not homework ==")

# Founder feedback 2026-08-19: after name/account/timezone, the flow used to
# end on "what's one thing you'd want me to take off your plate?" — a generic
# open question a brand-new user cannot answer, because they do not yet know
# what the agent can and cannot do. The message must instead offer concrete
# example requests to steal, plus the honest tool-connection line. Both
# mirrors, same as greet: the model may act on either.
tz_state_blob = flat(json.dumps(states["ask_timezone"]))
tz_directive = flat(context_for("ask_timezone", env_home=str(home)))
for label, blob in [
    ("state machine", tz_state_blob),
    ("injected directive", tz_directive),
]:
    check(
        f"ask_timezone ({label}) no longer ends on the generic plate question",
        "take off" not in blob,
    )
    check(
        f"ask_timezone ({label}) offers concrete examples to steal",
        "example" in blob and ("remind" in blob or "recurring" in blob),
    )
    # 2026-08-19, corrected the same day it was written: the first version of
    # this section asserted "no connectors exist". WRONG — the upstream skill
    # library ships in the tenant image and is seeded onto every tenant volume
    # (github/*, google-workspace, himalaya, notion, airtable; verified
    # in-container). The honest claim is: real, GUIDED connections — never
    # "one tap".
    check(
        f"ask_timezone ({label}) names the flagship connections",
        "github" in blob and "google" in blob and "notion" in blob,
    )
    check(
        f"ask_timezone ({label}) keeps the guided-not-one-tap honesty label",
        "guided" in blob or "walk them through" in blob or "walk you through" in blob,
    )

# The honesty label must live in the capabilities contract too, so a future
# copy pass cannot quietly upgrade "guided setup" into "instant connections".
tool_cap = next(
    (c for c in MACHINE["capabilities"] if c["id"] == "tool_connections"), None
)
check("capabilities carries a tool_connections entry", tool_cap is not None)
tool_cap_blob = flat(tool_cap["say"]) if tool_cap else ""
check(
    "tool_connections names GitHub and Google concretely",
    "github" in tool_cap_blob and "google" in tool_cap_blob,
)
check(
    "tool_connections keeps its guided-setup honesty label",
    "guided" in tool_cap_blob and "one tap" in tool_cap_blob,
)
check(
    "tool_connections forbids promising unverifiable tools",
    "do not promise a tool" in tool_cap_blob,
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
print("== 5b. the formatting verdict is encoded, and the copy is structured ==")

# Founder feedback 2026-08-13 (live v0.1.6 test): the intro and the connect
# pitch were "just a block of text" — warmth and structure were requested,
# including bold. Before touching copy, what actually renders was verified
# against the pristine upstream v2026.8.3 tree rather than assumed:
#
#   Tenant replies go through upstream's Telegram platform adapter. send()
#   (plugins/platforms/telegram/adapter.py:4415) runs format_message()
#   (:7583) — a standard-markdown -> MarkdownV2 converter — then calls
#   bot.send_message(parse_mode=ParseMode.MARKDOWN_V2) (:4544; streaming
#   edits :4866), falling back to stripped plain text when MarkdownV2
#   parsing fails (:4552-4558).
#
# Net verdict, which these pins keep the copy honest about: **double
# asterisks** render as bold and are the ONE marker to rely on; underscore
# emphasis is never converted (it renders as literal underscores), and
# headings/tables convert into blocks too heavy for a chat message. A copy
# rewrite that starts using unsupported syntax — or bans bold again — must
# fail this build.
for st, ctx in all_ctx.items():
    body = flat(ctx)
    check(f"`{st}` names the one working bold syntax (**...**)", "**" in ctx)
    check(f"`{st}` bans underscore emphasis by name", "_underscores_" in ctx)
    check(
        f"`{st}` bans headings and tables in chat copy",
        "headings" in body and "tables" in body,
    )

# The greet must now ASK for structure, not merely stop banning it.
greet_flat = flat(greet_ctx)
check(
    "greet no longer forbids bold outright",
    "no bold" not in greet_flat and "no bold" not in flat(json.dumps(states["greet"])),
)
check(
    "greet asks for blank-line spacing instead of a paragraph block",
    "blank line" in greet_flat and "paragraph block" in greet_flat,
)
check(
    "greet puts each capability example on its own line",
    "its own short line" in greet_flat,
)
check(
    "greet keeps emoji sparing, not confetti",
    "one or two emoji" in greet_flat,
)

# The connect pitch: a friendly menu, not a settings page — one option per
# line, label bolded, description in plain language.
_pitch_flat = flat(all_ctx["ask_name"])
check(
    "the connect pitch is framed as a friendly menu, not a settings page",
    "friendly menu" in _pitch_flat and "settings page" in _pitch_flat,
)
check("the connect pitch puts one option per line", "one per line" in _pitch_flat)
for lbl in (
    "**openai api key**",
    "**chatgpt subscription**",
    "**anthropic api key**",
):
    check(f"the connect pitch bolds the {lbl} label", lbl in _pitch_flat)
# The Claude-subscription option was removed 2026-08-14 (prohibited by
# Anthropic's Consumer ToS). The menu must not offer it in any form.
check(
    "the connect pitch offers no Claude-subscription option",
    "**claude max" not in _pitch_flat and "**claude subscription" not in _pitch_flat,
)

# The YAML mirror carries the same verdict WITH its evidence, so the next
# copy pass starts from the finding instead of re-deriving or guessing it.
_fmt = MACHINE.get("formatting") or {}
check("state-machine.yaml documents the formatting verdict", bool(_fmt))
check(
    "the verdict cites the adapter evidence by file and line",
    "plugins/platforms/telegram/adapter.py" in str(_fmt) and "4544" in str(_fmt),
)
check(
    "the hook documents the same verdict for maintainers",
    "MarkdownV2" in HOOK.read_text(encoding="utf-8"),
)


print()
print("== 6. honesty rules survive the rewrite ==")

facts = MACHINE["facts"]
providers = {p["id"]: p for p in MACHINE["providers"]}

# Founder decision 2026-08-14 (spike-backed): Anthropic's Consumer ToS now
# prohibits third-party apps using Claude subscriptions (Feb 2026 change,
# server-side enforced — account bans), so the claude_max_oauth option was
# REMOVED entirely. Anthropic is API-key only. Three options remain.
check(
    "exactly the three surviving provider options are present",
    set(providers) == {"openai_api_key", "openai_codex_oauth", "anthropic_api_key"},
    str(sorted(providers)),
)
# The OpenAI honesty label used to say OpenAI "explicitly sanctions" the
# ChatGPT-subscription path — an overstatement of OpenAI's actual stance.
# The accurate claim: the Codex team openly supports it, with no formal
# written guarantee.
_codex_hl = flat(providers["openai_codex_oauth"]["honest_label"])
check(
    "the Codex label claims open Codex-team support, not formal endorsement",
    "openai's codex team openly supports third-party agents using chatgpt sign-in"
    in _codex_hl,
    _codex_hl,
)
check(
    "the Codex label admits the support could change",
    "no formal written guarantee" in _codex_hl and "openai could change this" in _codex_hl,
)

# The trial numbers the injected directives repeat must match the facts block —
# the hook is allowed to inline them, but not to invent them.
check("trial length agrees with the facts block", "3 days" in facts["trial_length"])
# The message-count figure must live in ONE place. This design paid for itself
# within a day: the trial moved Haiku/$2 -> Sonnet/$10 and facts.trial_limits
# was the only line of copy that had to change. A second copy inlined in the
# hook would have gone on telling users the old figure.
check(
    "the per-day message count lives in the facts block",
    "75 messages a day" in facts["trial_limits"],
)
check(
    "no directive inlines the per-day message count",
    not any(
        "75 message" in d or "75/day" in d for d in hook._DIRECTIVES.values()
    ),
)
# trial_explainer is gone; the allowance framing at the provider-options step
# (ask_name) is now the place trial mechanics surface, and it inherits the
# same guard rails: facts by absolute path, nothing from memory, answer-only.
_pitch = all_ctx["ask_name"]
check(
    "the allowance pitch points at the facts block by absolute path",
    "/skills/concierge/state-machine.yaml" in _pitch and "`facts`" in _pitch,
    _pitch[:160],
)
check(
    "the pitch forbids stating limits or prices from memory",
    "from memory" in _pitch.lower(),
)
check(
    "the pitch keeps prices answer-only",
    "do not volunteer prices" in flat(_pitch),
)
# The founder's actual complaint: LLM usage costs and paying for Squire were
# presented as one thing. The separation must now be explicit in the copy.
check(
    "the pitch separates provider billing from paying Squire",
    "billed by their own provider" in flat(_pitch)
    and "not a payment to squire" in flat(_pitch),
)
check(
    "the three honest labels are presented where the options are",
    "fully supported" in flat(_pitch)
    and "openly supports third-party agents" in flat(_pitch)
    and "no formal written guarantee" in flat(_pitch),
)
check(
    "declining the connect step continues onboarding instead of ending it",
    '"ask_timezone"' in all_ctx["connect_llm"],
)
# prd.md:118 lists the spend cap alongside the message count. Quoting only the
# message count implies 75/day is reachable when the $2 cap may end the trial
# far sooner — that is us misleading the user, not the user misusing it.
check(
    "the trial's spend cap is stated alongside the message count",
    "$10" in facts["trial_limits"],
    facts["trial_limits"],
)
# The two numbers must stay mutually consistent. 75/day x 3 days ~= 225 turns;
# at Sonnet ~$0.04/turn that is ~$9, so $10 is reachable. If the model is
# upgraded again without raising the cap, the advertised allowance silently
# stops being achievable and the copy starts misleading people.
check(
    "no stale $2 cap survives anywhere in the copy",
    "$2 " not in facts["trial_limits"] and not any("$2" in d for d in hook._DIRECTIVES.values()),
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

# 1C: the one-time link is real now (squire-llm-connect mint-link), so the
# pre-1C pins that the link "does not exist yet", that directives must "never
# invent a URL", and that `connected` must NOT say the trial key was revoked
# have all been removed — their premises are false since 1C shipped. Section 6b
# pins the live CLI-driven behaviour that replaced them.

# Dropping the standalone trial explainer must not drop the honest disclosure
# itself: the end-of-trial mechanics stay in facts for whenever they are asked
# about, and `complete` still names the one moment they get said unprompted.
check(
    "trial end behaviour stays in facts, answer-only",
    "72 hours" in facts["trial_end_behaviour"]
    and "14 days" in facts["trial_end_behaviour"],
)
check(
    "complete still re-raises the connect step near trial expiry",
    "12 hours" in flat(json.dumps(states["complete"])),
)
check(
    "complete states the end-of-trial mechanics when it does re-raise",
    "trial_end_behaviour" in json.dumps(states["complete"]),
)


print()
print("== 6b. the connect flow is honest per provider (live incident 2026-08-14) ==")

# The incident this section exists to prevent, screenshot-verified in the
# founder's live test: the user picked "ChatGPT subscription" from the menu,
# the agent promised a device-code sign-in ("just confirm you're ready and
# I'll kick off the sign-in flow"), then next turn demanded an API key
# instead, repeated the "no one-click link yet" explanation in consecutive
# messages, and called the key's transit through Telegram "totally fine".
# Nothing in this image implements device-code or OAuth sign-in — 1C is the
# roadmap item — so the only honest menu is one that says which options are
# connectable TODAY, and choosing a coming-soon one must get one warm honest
# message, not a bait-and-switch.

# -- 1C: all three paths are LIVE; the connect flow runs through the CLI ----
check(
    "no provider is marked coming_soon any more (1C shipped)",
    [p["id"] for p in MACHINE["providers"] if p.get("status") == "coming_soon"] == [],
)
check(
    "exactly the three 1C providers exist",
    set(providers) == {"openai_api_key", "openai_codex_oauth", "anthropic_api_key"},
    set(providers),
)
check(
    "the hook's coming-soon set is empty to match",
    getattr(hook, "_COMING_SOON_PROVIDERS", None) == set(),
    getattr(hook, "_COMING_SOON_PROVIDERS", None),
)
check(
    "the codex option no longer claims 'coming soon' anywhere",
    "coming soon" not in flat(providers["openai_codex_oauth"]["label"]).lower()
    and "coming soon" not in flat(providers["openai_codex_oauth"]["honest_label"]).lower(),
)
check(
    "the codex honest label uses the spike's softened wording, not 'sanctions'",
    "openly supports" in flat(providers["openai_codex_oauth"]["honest_label"])
    and "no formal written guarantee" in flat(providers["openai_codex_oauth"]["honest_label"])
    and "sanction" not in flat(providers["openai_codex_oauth"]["honest_label"]).lower(),
    providers["openai_codex_oauth"]["honest_label"],
)
check(
    "the codex option teaches the enable-device-login caveat",
    "device code authorization" in flat(providers["openai_codex_oauth"]["notes"]),
    providers["openai_codex_oauth"].get("notes"),
)
check(
    "codex fallback is still the OpenAI key path",
    providers["openai_codex_oauth"]["fallback"] == "openai_api_key",
)

# The hook's connect directives drive the CLI, never invented URLs.
check(
    "connect_llm directive mints links via the CLI",
    "squire-llm-connect mint-link" in all_ctx["connect_llm"],
)
check(
    "awaiting_credential drives status polling via the CLI",
    "squire-llm-connect status" in all_ctx["awaiting_credential"],
)
check(
    "the device-flow directive starts the real flow",
    "start openai-device" in getattr(hook, "_AWAITING_DEVICE_FLOW", ""),
)
check(
    "the device-flow directive handles the not_enabled branch",
    "not_enabled" in getattr(hook, "_AWAITING_DEVICE_FLOW", "")
    and "Security" in getattr(hook, "_AWAITING_DEVICE_FLOW", ""),
)
check(
    "the connected directive celebrates and may now state revocation truthfully",
    "revoked" in all_ctx["connected"] and "pleased" in all_ctx["connected"],
)
check(
    "no directive still claims the one-time link does not exist",
    all("DOES NOT EXIST" not in ctx for ctx in all_ctx.values()),
)

# -- choosing the ChatGPT subscription routes to the device flow (1C) --------
_choice = flat(all_ctx["connect_llm"])
check(
    "connect_llm has an explicit ChatGPT-subscription branch",
    "if they picked the chatgpt subscription" in _choice,
)

# -- the choice is recorded where the next turn can read it ------------------
# Mechanism (existing, not invented here): concierge-state.json is seeded by
# squire-entrypoint.sh with an `llm` key ("trial"), the STEP 1 write command
# carries every key of that file, and the directive tells the model to set
# `llm` to the picked provider id. The hook reads the file on the next turn
# and picks the credential-step directive from it.
check(
    "connect_llm tells the model to record the pick in the llm key",
    '"llm"' in all_ctx["connect_llm"]
    and all(f'"{pid}"' in all_ctx["connect_llm"] for pid in providers),
    [pid for pid in providers if f'"{pid}"' not in all_ctx["connect_llm"]],
)


def ctx_with_llm(state: str, llm: str | None) -> str:
    """Injected context for `state` with a recorded provider choice."""
    blob: dict = {"state": state}
    if llm is not None:
        blob["llm"] = llm
    rc_, out_ = run_hook(json.dumps(blob))
    return json.loads(out_)["context"] if out_.strip() else ""


_cs = ctx_with_llm("awaiting_credential", "openai_codex_oauth")
_cs_flat = flat(_cs)
check(
    "the ChatGPT choice gets its own device-flow credential-step directive",
    bool(_cs) and _cs != all_ctx["awaiting_credential"],
)
check(
    "the device-flow variant offers the OpenAI key path as its fallback",
    '"openai_api_key"' in _cs,
)
# The variant is still a normal injection: silent state write first, exactly
# one message, no unexpanded templating.
check(
    "the device-flow variant keeps the one-message wrapper",
    "step 1" in _cs_flat and "send only that one message this turn" in _cs_flat,
)
check(
    "the device-flow variant has no unexpanded template variable",
    "${" not in _cs and "{env_file}" not in _cs and "{skill_file}" not in _cs,
)

# 1C: the "missing-link said at most once" cohort is gone — the one-time link
# is real now (squire-llm-connect mint-link), so there is no missing-link
# explanation to cap or forbid repeating. The live CLI-driven copy replaced it.

# -- the security disclosure is plain, and minimising it is banned -----------
# The incident rendering was "briefly passes through Telegram and our relay —
# totally fine". The fact must be stated straight: the key DID transit
# Telegram and the relay, the message is deleted, rotation is offered — and
# the qualifiers that soften it are banned by name, because that is the form
# the drift actually took.
_yaml_paste = flat(" ".join(str(s) for s in states["awaiting_credential"]["on_paste_in_chat"]))
# 1C: the transit disclosure is pinned only on the paths that actually take a
# pasted key. The device-flow variant never asks for a paste (it delivers the
# token provider -> container), so the disclosure does not apply to it.
for _label, _blob in [
    ("YAML on_paste_in_chat", _yaml_paste),
    ("hook credential directive", flat(hook._DIRECTIVES["awaiting_credential"])),
]:
    check(
        f"{_label} states the transit fact plainly",
        "telegram" in _blob and "relay" in _blob,
    )
    check(
        f"{_label} bans minimising qualifiers by name",
        '"totally fine"' in _blob
        and '"don\'t worry"' in _blob
        and '"perfectly safe"' in _blob,
    )
check(
    "the deletion is still promised in the credential directive",
    "delete their telegram message" in flat(hook._DIRECTIVES["awaiting_credential"]),
)
check(
    "rotation advice survives in both mirrors",
    "rotate" in flat(hook._DIRECTIVES["awaiting_credential"]) and "rotate" in _yaml_paste,
)


print()
print("== 6c. the Claude-subscription option is GONE (Anthropic ToS, 2026-08-14) ==")

# Founder decision 2026-08-14, backed by the research spikes in
# docs/superpowers/specs/2026-08-14-spike-*.md: Anthropic's Consumer ToS now
# prohibits third-party apps from using Claude subscriptions (Feb 2026 change,
# server-side enforced — accounts get banned). The claude_max_oauth option that
# shipped live in v0.1.8 — menu line, providers entry, setup-token hand-off,
# CLAUDE_CODE_OAUTH_TOKEN path — must be gone from every surface the agent
# reads. Anthropic is API-key only.

_yaml_raw = flat(STATE_MACHINE.read_text(encoding="utf-8"))
_all_directives = list(hook._DIRECTIVES.values()) + [hook._AWAITING_DEVICE_FLOW]
_rendered = flat(" ".join(all_ctx.values()))

check(
    "no claude_max provider survives anywhere in the state machine",
    "claude_max" not in _yaml_raw and "claude max" not in _yaml_raw,
)
check(
    "no CLAUDE_CODE_OAUTH_TOKEN path survives in the state machine",
    "claude_code_oauth" not in _yaml_raw,
)
check(
    "no setup-token hand-off survives in the state machine",
    "setup-token" not in _yaml_raw,
)
check(
    "no provider option mentions Claude at all",
    all("claude" not in flat(json.dumps(p)) for p in MACHINE["providers"]),
)
check(
    "the removed provider id is never offered for the llm key",
    all("claude_max_oauth" not in d for d in _all_directives)
    and "claude_max_oauth" not in _rendered,
)
check(
    "no directive hands out the setup-token mechanism",
    all("setup-token" not in flat(d) for d in _all_directives),
)
check(
    "the overstated 'sanctions' wording is gone from every surface",
    "sanction" not in _yaml_raw
    and all("sanction" not in flat(d) for d in _all_directives),
)
check(
    "SKILL.md no longer advertises the Claude-subscription path",
    "claude max" not in flat((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")),
)

# A user who saw the option in an earlier alpha may ask where it went. The
# honest answer lives in the facts block — answer-only, like every fact — and
# the connect-adjacent states point at it by name rather than paraphrasing.
_claude_fact = flat(facts.get("claude_subscription", ""))
check(
    "facts carries the Claude-subscription answer",
    "isn't available" in _claude_fact
    and "don't permit it for third-party apps" in _claude_fact,
    _claude_fact,
)
check(
    "the answer redirects to an Anthropic API key, concretely",
    "**api key**" in _claude_fact and "console.anthropic.com" in _claude_fact,
)
# 1C: the streamlined connect_llm copy no longer carries the Claude-subscription
# answer — the menu (ask_name) is where that option was advertised and where the
# question naturally arises, so the answer-only handling is pinned there.
for _st in ("ask_name",):
    check(
        f"`{_st}` points at facts.claude_subscription for the if-asked answer",
        "claude_subscription" in flat(all_ctx[_st]),
    )
    check(
        f"`{_st}` keeps that answer answer-only (never volunteered)",
        "never volunteer" in flat(all_ctx[_st]),
    )


print()
print("== 6d. ask_name menu: the ChatGPT path is LIVE, not 'coming soon' (Task 10 gap) ==")

# Task 10 made all three connect paths live and rewrote connect_llm /
# awaiting_credential / connected accordingly, but left the ask_name provider
# MENU still advertising the ChatGPT path as "coming soon" / "not live in this
# alpha yet". That is an internal contradiction — the menu calls a path
# unavailable one turn before the flow connects it live — and it is the exact
# honesty defect (advertising a state that isn't true) this whole feature
# exists to remove.
_ask = flat(all_ctx["ask_name"])
check(
    "the ask_name ChatGPT option is NOT advertised as coming soon",
    "coming soon" not in _ask,
)
check(
    "the ask_name ChatGPT option is NOT labelled unavailable / not-live",
    "not live" not in _ask,
)
check(
    "the ask_name ChatGPT option keeps the softened honesty, never 'sanctions'",
    "openly supports" in _ask
    and "no formal written guarantee" in _ask
    and "sanction" not in _ask,
    _ask[:200],
)

# Routing negative-assertion, re-added after Task 10's reconciliation dropped
# it: ONLY the recorded codex provider gets the in-chat device-flow directive
# in awaiting_credential. Every other recorded pick (both API keys, the seeded
# "trial" placeholder) and an unset llm key must get the API-key/link directive,
# never the device-flow one — otherwise the model would demand a device code
# from someone who chose to paste a key.
for _llm in ("openai_api_key", "anthropic_api_key", "trial", None):
    _ac = ctx_with_llm("awaiting_credential", _llm)
    check(
        f"awaiting_credential with llm={_llm!r} does NOT get the device-flow directive",
        "device flow" not in flat(_ac) and hook._AWAITING_DEVICE_FLOW not in _ac,
    )
check(
    "awaiting_credential WITH the codex provider DOES get the device-flow directive",
    "device flow" in flat(ctx_with_llm("awaiting_credential", "openai_codex_oauth")),
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

# The model and the advertised budget are one decision, changed together on
# 2026-08-09. Pinning them in the same assertion is what stops a future model
# bump from quietly leaving the copy describing the old economics.
_model = str(CONFIG_YAML.get("model", ""))
check(
    "the trial model is the Sonnet-class default the budget was sized for",
    "sonnet" in _model.lower(),
    _model,
)
check(
    "the model string carries its provider prefix (hermes sends it verbatim)",
    _model.startswith("anthropic:"),
    _model,
)
# The exact string must be exposed by the trial proxy or every trial message
# 400s — observed live 2026-08-09. apps/trial-proxy/tests/test_config.py owns
# this contract; assert it from the tenant side too, since this file is the
# half that changes.
_proxy_cfg = IMAGE_ROOT.parent / "apps" / "trial-proxy" / "config.yaml"
if _proxy_cfg.is_file():
    _names = {m["model_name"] for m in yaml.safe_load(_proxy_cfg.read_text())["model_list"]}
    check(
        "the tenant's model string is exposed verbatim by the trial proxy",
        _model in _names,
        f"{_model!r} not in {sorted(_names)}",
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

# --- the /sethome prompt: set the home channel instead of asking ------------
# Upstream prompts "No home channel is set for Telegram ... type /sethome" on
# the first message of a fresh session, so it arrived AHEAD of the greeting and
# pointed at a surface Squire does not sell. A Squire tenant is a single-user
# DM: the chat the message arrived in IS the home channel, and there is nothing
# to choose. Setting it also makes cron briefings work with no user action.
check(
    "patch 005 adopts the DM as the home channel",
    "persist_home_channel" in patch_text and "HomeChannel" in patch_text,
)
check(
    "it uses upstream's own persistence API rather than hand-writing config",
    "_sq_persist_home(_sq_home)" in patch_text,
)
check(
    "adoption is gated on this being a Squire tenant",
    patch_text.count("concierge-state.json") >= 2,
)
check(
    "the adopted channel is the chat the message arrived in",
    "chat_id=str(source.chat_id)" in patch_text,
)
# On-disk only would leave the running gateway still believing no home is set,
# so the prompt could reappear later in the same process.
check(
    "the live in-memory config is updated too, not just the file",
    "_sq_pc.home_channel = _sq_home" in patch_text,
)
check(
    "setting home suppresses the prompt by satisfying upstream's own check",
    'home_env = "set"' in patch_text,
)
check(
    "adoption failure falls back to upstream rather than breaking the turn",
    "squire home-channel adoption failed" in patch_text,
)
# The prompt must be pre-empted, never reworded — upstream's own branch stays.
check(
    "upstream's /sethome branch is left intact for non-Squire deployments",
    "sethome_cmd" not in patch_text or "-" not in patch_text.split("sethome_cmd")[0][-2:],
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
# If upstream reworks the /sethome block, our insertion point vanishes and the
# prompt silently comes back — invisible without an anchor row.
check(
    "the /sethome prompt has an upstream anchor marker",
    "upstream-sethome-prompt" in markers,
)
check(
    "home-channel adoption has a squire marker",
    "squire-005-home-channel" in markers,
)
for row in markers.splitlines():
    if row.startswith(("patch-005", "squire-005", "upstream-onboarding", "upstream-sethome")):
        check(f"marker row `{row.split(chr(9))[0]}` has 4 columns", len(row.split("\t")) == 4, row)


print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CONCIERGE ONBOARDING TESTS PASS")
