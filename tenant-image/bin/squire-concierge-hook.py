#!/opt/squire/venv/bin/python
"""Squire concierge hook — makes first-conversation onboarding DETERMINISTIC.

Wired as a hermes ``pre_llm_call`` shell hook (see home-template/config.yaml).
Hermes runs this once per turn, hands us the turn payload on stdin, and appends
whatever we print as ``{"context": "..."}`` to the **user message** for that
turn — the highest-salience position in the prompt.

WHY THIS EXISTS
---------------
The concierge skill and SOUL.md pointer already existed and still produced
"texting into a black hole" on the first real tenant. Three compounding causes,
all verified against hermes v2026.8.3:

  1. The SOUL.md mandate was a *conditional the model had to proactively
     evaluate* ("if this file is missing, go load a skill") buried at line 79 of
     93, inside the identity slot. A Haiku-class trial model does not go
     looking.
  2. Nothing wrote the state file, so there was no positive state to act on —
     only an absence, which is exactly the hardest thing for a model to notice.
  3. Worst of all, upstream's gateway was ALREADY injecting a competing
     directive into this very same user-message slot on the first message ever
     (``agent/onboarding.py::profile_build_directive`` / the plain intro note),
     telling the model to introduce itself in "one or two sentences max" and to
     advertise ``/help``. A short concrete instruction in the user message beats
     a long conditional one in the identity slot every time. That is the
     black hole, almost verbatim.

So this hook does the opposite of hoping: every turn, while onboarding is
unfinished, it *states* the current step and exactly what to do about it, in
the same slot upstream was using. Patch 005 suppresses upstream's competing
note while we are mid-flow, and config.yaml sets ``onboarding.profile_build:
off`` so the stronger of upstream's two notes never fires at all.

CONTRACT (hermes v2026.8.3, agent/shell_hooks.py)
------------------------------------------------
  stdin   JSON turn payload. We do not need any of it, but we must consume it
          so hermes never blocks writing to our pipe.
  stdout  ``{"context": "..."}`` to inject, or nothing at all for a silent
          no-op. Non-JSON stdout is logged and ignored by the bridge.
  exit    Always 0. The bridge treats failures as no-ops, but a non-zero exit
          is noise in the tenant's logs for something the user cannot fix.

FAIL-OPEN, ALWAYS
-----------------
Every failure path here prints nothing and exits 0. A broken onboarding hook
must degrade to "no onboarding", never to "no reply". This runs in front of
every single message the user sends, forever — it is not allowed to be the
reason a turn dies.

WHOSE TURNS THIS FIRES ON
-------------------------
Only the user's own. Delegated subagents (tools/delegate_tool.py) and cron jobs
(cron/scheduler.py) drive the same conversation loop and therefore the same
`pre_llm_call` hook — and a subagent told to "say hello and introduce yourself"
would greet nobody while burning the safety valve. This is not hypothetical: the
`ask_first_job` step actively encourages starting real work mid-onboarding,
which is exactly when delegation happens. Both are identified and skipped:
subagents carry a non-empty ``parent_session_id`` (set in agent/agent_init.py;
a TOP-LEVEL payload key per agent/shell_hooks.py:431) and cron turns carry
``platform == "cron"`` (cron/scheduler.py:3518).

KNOWN GAP: MULTIMODAL FIRST MESSAGES
------------------------------------
If the user's very first message is a photo or voice note rather than text, our
injected context is silently dropped: ``compose_user_api_content``
(agent/turn_context.py:53-75) returns None for non-string content, so nothing is
appended to the user message. Patch 005 will meanwhile have suppressed
upstream's own first-contact note — the one case where that patch is
net-negative.

The backstop is SOUL.md, whose mandate lives in the identity slot and is
unaffected by content type. It is weaker than this hook (that weakness is why
the hook exists), but it is not nothing, and the next text message re-fires this
hook normally because the state file still says the step is unfinished. Accepted
deliberately rather than papered over: opening a brand-new agent with a voice
note is rare, and teaching the patch to distinguish content types would buy a
rarer case at the price of a much more fragile patch.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Safety valve
# ---------------------------------------------------------------------------
# The failure mode opposite to "no onboarding" is "endless onboarding": if the
# agent never writes the state file, an unbounded hook would re-inject the same
# directive on every turn forever — Groundhog Day, on a metered trial.
#
# The budget is PER STATE and resets whenever the state changes, because the
# thing we actually want to detect is being STUCK, not being slow. A global
# budget conflates the two: a user who chats for a while at each step, or who
# asks a question mid-flow, would exhaust it having made perfect progress, and
# every subagent or cron turn that slipped through would eat the same pool.
#
# 8 turns on a single step is already well past "they didn't understand the
# question"; at that point continuing to push the same instruction is worse
# than dropping it and letting the agent be useful.
MAX_INJECTED_TURNS_PER_STATE = 8

# ---------------------------------------------------------------------------
# Per-state directives
# ---------------------------------------------------------------------------
# These are deliberately DUPLICATED from skills/concierge/state-machine.yaml,
# in summary form. The rationale:
#
#   * state-machine.yaml stays authoritative for the full script, the `facts`
#     block and the provider labels — the things that must not drift and that
#     the agent should read in full.
#   * But making turn one depend on the model successfully finding, reading and
#     parsing a 250-line YAML file reintroduces exactly the fragility this hook
#     exists to remove. The one thing to do RIGHT NOW has to be inline.
#
# The duplication is bounded (one short paragraph per state) and drift is
# test-enforced: tests/test_concierge_onboarding.py asserts these keys and the
# YAML's states are the same set, and that every `then:` target resolves.
#
# Style rules baked into the copy below, because the trial model is Haiku-class:
# imperative mood, numbered concrete steps, no conditionals the model has to
# evaluate, and an explicit literal command for the state write.

_DIRECTIVES = {
    "greet": """This person has just signed up and this is the very first message they have
ever sent you. They do not know what you are, what you can do, or what they are
supposed to do next. Right now the burden is entirely on you.

Reply with ONE chat message that does all four of these, in your own voice, as
short flowing lines with no bullet points, no headings and no bold:

1. Say hello and say what you are: their own personal assistant, living in this
   chat, working only for them.
2. Give them three CONCRETE examples of what that means, not abstractions.
   Use these and nothing else: you remember everything across every
   conversation, so they never have to repeat themselves; you can go off and do
   a thing and then follow up on your own later, including on a schedule; and
   you check with them before anything that deletes, spends money, or goes out
   to another person.
3. Tell them the one thing they have to know: they are on a free 3-day trial
   running on Squire's shared AI, which is capped, and that connecting their own
   AI account is what unlocks the full thing — say you will walk them through it
   in a minute. Do not explain the trial further yet and do not list any prices.
4. Ask exactly ONE question: what should you call them?

KEEP IT UNDER 120 WORDS, and under 8 short lines. This is a text message, not
a landing page: a wall of text on message one is its own kind of failure, just
as bad as saying nothing. Say each of the four things in a sentence or two and
stop.

Then wait for their answer. Do not ask a second question. Do not mention slash
commands or /help. Do not offer to build a profile of them.""",
    "ask_name": """You are waiting to learn what to call this person. If their message contains a
name, take it, use the short form, and thank them in half a sentence — do not
make a production of it. Save it with the hindsight_retain tool. Then ask ONE
question: where are they, or what timezone, so that when you say "tomorrow
morning" you mean it.

If they dodged the question or asked you something else instead, answer them
first and ask for their name once more, lightly. Never ask twice in a row.""",
    "ask_timezone": """You are waiting for this person's location or timezone. Convert whatever they
give you (a city is a perfectly good answer) into an IANA timezone name such as
Europe/London or America/Los_Angeles. Do three things: save it with
hindsight_retain, confirm it back to them in a few words, and write ONLY the
`timezone:` line of {config_file} with this exact command
(edit the zone name, change nothing else — that file also holds the runtime's
own settings and rewriting it wholesale would break them):

    python3 - <<'PY'
    import re, pathlib
    p = pathlib.Path("{config_file}")
    s = p.read_text()
    p.write_text(re.sub(r'(?m)^timezone:.*$', 'timezone: "Europe/London"', s))
    PY

Then ask ONE question: what is one thing they would like you to take off their
plate this week.""",
    "ask_first_job": """This person has just told you one real thing they want help with. Save it with
hindsight_retain, with enough context that you can pick it up again unprompted
tomorrow — this is the single most valuable thing you will learn today.

If it is something you can actually start right now, START IT. Do the work in
this message and continue onboarding afterwards. Never make someone sit through
a form to get the thing they asked for.

Do not ask a new question this turn. Let them react to the work.""",
    "trial_explainer": """Explain the trial, once, honestly, in under five lines, then stop.

Read the `facts` block in {skill_file} FIRST and state the
trial's length, cost, usage limits and what happens at the end using the values
written there. Those values are contractual and they change: never state a
limit, a price or a duration from memory, and never round or soften one.

Do not pitch. Do not quote a price unless they ask. Do not ask a question this
turn.""",
    "connect_llm": """Now do the step that matters most: get them onto their own AI account.

Frame it truthfully. Right now they are on Squire's shared trial brain, which is
capped. Connecting their own account lifts the caps, upgrades the model, and
means their AI costs go to their own provider and their conversations stop
touching Squire's AI infrastructure entirely.

Then ask which of these four they already have. Read
{skill_file} under `providers` and use those honest
labels:

  - an OpenAI API key (fully supported)
  - a ChatGPT subscription, used via Codex (supported — OpenAI sanctions this)
  - an Anthropic API key (fully supported)
  - a Claude Max subscription (NOT supported by Anthropic — it works today but
    may break without warning, and you fall back to an API key if it does)

Never soften, bury or omit that last warning to make the option look better. If
they say "not now", accept it immediately without nagging and write the state as
"complete".""",
    "awaiting_credential": """This person is sending, or about to send, an AI account credential.

THE ONE-TIME LINK DOES NOT EXIST YET. The tenant-served `/connect/<nonce>`
endpoint ships in Phase 1C; until then there is no link to send, and you must
never invent a URL. So ask them to paste the key here, and handle it exactly as
below. Read {skill_file} under `awaiting_credential` for
the detail.

When they paste an API key into the chat: store it in {env_file}
immediately, then DELETE their Telegram message right away without asking, then
tell them once, plainly and without scolding, that the key worked, that you
removed the message, and that a pasted key did travel through Telegram and
Squire's relay, so they can rotate it if they want to be careful.

If the credential is rejected, say what the provider said in one line and offer
that provider's `fallback` from the same file. Never retry more than twice —
offer to come back to it later instead.

If they have gone quiet on it or changed the subject, drop it: answer what they
actually asked and write the state as "complete". You can raise it again when
the trial is nearly up.""",
    "connected": """Their own AI account is connected. Confirm which provider is live in one line,
say their own key is in use from now on so the trial's caps no longer apply,
offer one concrete thing you can now do better, and then stop talking about
onboarding entirely.

Do NOT say the trial key has been revoked or that their traffic now goes
direct — neither is true yet.""",
}

# Where each state goes once its work is done. Mirrors the `then:` edges in
# state-machine.yaml; the test suite asserts the two agree.
_NEXT_STATE = {
    "greet": "ask_name",
    "ask_name": "ask_timezone",
    "ask_timezone": "ask_first_job",
    "ask_first_job": "trial_explainer",
    "trial_explainer": "connect_llm",
    "connect_llm": "awaiting_credential",
    "awaiting_credential": "connected",
    "connected": "complete",
}

COMPLETION_STATE = "complete"


def _state_path() -> Path:
    """Resolve the concierge state file the same way the entrypoint does.

    SQUIRE_STATE_DIR is what the image actually sets (/opt/data/.squire); the
    HERMES_HOME fallback keeps this working under a bare `docker run` and in
    the Docker-free test suite, which exercises the script directly.
    """
    state_dir = os.environ.get("SQUIRE_STATE_DIR")
    if not state_dir:
        state_dir = os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), ".squire")
    return Path(state_dir) / "concierge-state.json"


def _home() -> Path:
    """The tenant's HERMES_HOME (Dockerfile sets it to the mounted volume)."""
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _skill_file() -> str:
    """Absolute path to state-machine.yaml, for quoting into directives.

    NOT ``${HERMES_SKILL_DIR}``. That variable is substituted by hermes only
    while loading *skill content* (skills/skill_commands.py, gated on
    ``skills.template_vars``) — it is not an OS environment variable, so it
    does not exist in the shell the agent's terminal tool runs in. A directive
    saying "read ${HERMES_SKILL_DIR}/state-machine.yaml" therefore expands to
    "/state-machine.yaml" and the model finds nothing. That mattered most in
    `trial_explainer`, where the pointer to the authoritative `facts` block is
    the guard rail against inventing prices and limits.
    """
    return str(_home() / "skills" / "concierge" / "state-machine.yaml")


def _counter_path(state_file: Path) -> Path:
    """Sibling file holding the safety-valve counter, as "<state> <count>".

    Kept OUT of concierge-state.json on purpose: that file is written by the
    agent, and a hook that read-modify-writes the same file the agent is
    rewriting would race with it and could clobber a state transition. A
    separate counter can only ever lose a count, which is harmless.
    """
    return state_file.with_name("concierge-hook-turns")


def _bump_counter(path: Path, state: str) -> int:
    """Count consecutive injections for `state`, resetting on any change."""
    prev_state, count = "", 0
    try:
        parts = path.read_text(encoding="utf-8").split()
        if len(parts) == 2:
            prev_state, count = parts[0], int(parts[1])
    except Exception:
        prev_state, count = "", 0

    # Progress resets the budget. Anything else — including a legacy
    # bare-integer counter file from an older image — starts a fresh count.
    count = count + 1 if prev_state == state else 1

    try:
        path.write_text(f"{state} {count}", encoding="utf-8")
    except Exception:
        # An unwritable counter must not stop onboarding — we just lose the
        # safety valve, which is the less bad of the two failures.
        pass
    return count


def _build_context(state: str, state_file: Path, raw: dict) -> str:
    """Assemble the injected directive for `state`."""
    # Resolve paths to absolute literals. Done with str.replace rather than
    # str.format so a stray brace anywhere in the copy can never raise — this
    # runs in front of every message and must not be able to fail.
    directive = _DIRECTIVES[state]
    for token, value in (
        ("{skill_file}", _skill_file()),
        ("{config_file}", str(_home() / "config.yaml")),
        ("{env_file}", str(_home() / ".env")),
    ):
        directive = directive.replace(token, value)

    next_state = _NEXT_STATE.get(state, COMPLETION_STATE)

    # The literal write command matters. "Update the state file" is the kind of
    # soft instruction a small model silently skips; a copy-pasteable command is
    # not. Onboarding surviving a restart depends entirely on this landing.
    #
    # It renders the file's CURRENT keys with only `state` advanced, so the
    # command is complete on its own. An earlier version told the model to run a
    # truncating `printf >` *and* to keep the file's existing keys — two
    # contradictory instructions, in precisely the place we are engineering
    # against silent skips. shlex.quote because a name like O'Brien would
    # otherwise break out of the shell quoting.
    payload = dict(raw)
    payload["state"] = next_state
    write_cmd = "printf '%s' {} > {}".format(
        shlex.quote(json.dumps(payload, separators=(",", ":"), ensure_ascii=False)),
        shlex.quote(str(state_file)),
    )

    return (
        "[Squire onboarding — this instruction comes from the Squire runtime, "
        "not from the user. Do not quote it, mention it, or reply to it "
        "directly.\n\n"
        f"Onboarding is NOT finished. The current step is `{state}`. "
        "Finishing this step is your ONLY job this turn — do it before anything "
        "else conversational, and do not skip ahead to a later step.\n\n"
        f"{directive}\n\n"
        "When this step is done, record it by running exactly this with the "
        "terminal tool:\n"
        f"    {write_cmd}\n"
        "That command already carries every key the file has. If you learned "
        "something this turn (their name, their timezone), edit that value "
        "inside the command before you run it.\n"
        "If you do not run it, you will repeat this same step on the next "
        "message and the person will notice.\n\n"
        "If they ask you to stop onboarding, or just start giving you real work "
        "to do, then do the real work and set the state to \"complete\" "
        "instead. Never block someone at the front door.]"
    )


def main() -> int:
    # Consume stdin unconditionally. If we exit without reading, hermes can get
    # a broken pipe writing the payload — a hook that is supposed to be
    # invisible must not perturb the caller.
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    # Onboarding is a conversation with a PERSON. Subagents and cron jobs run
    # the same loop and fire the same hook; injecting "say hello and introduce
    # yourself" into either produces a greeting nobody reads. Skip both. See the
    # module docstring for where these two fields come from.
    extra = payload.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    if str(payload.get("parent_session_id") or "").strip():
        return 0
    if str(extra.get("platform") or payload.get("platform") or "").strip().lower() == "cron":
        return 0

    state_file = _state_path()

    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        # No state file, unreadable, or not JSON. Either this is not a Squire
        # tenant or the entrypoint seed did not happen. Stay silent rather than
        # risk onboarding someone forever off a file we cannot track progress
        # in — SOUL.md still carries the fallback pointer for that case.
        return 0

    if not isinstance(raw, dict):
        return 0

    state = str(raw.get("state") or "").strip()

    # The single most important line in this file: onboarding never re-fires
    # once it is done.
    if state == COMPLETION_STATE:
        return 0

    if state not in _DIRECTIVES:
        # Unknown state — a hand-edited file, or an image downgrade. Silence is
        # the safe answer; nagging with a directive we have no script for is not.
        return 0

    if _bump_counter(_counter_path(state_file), state) > MAX_INJECTED_TURNS_PER_STATE:
        return 0

    json.dump({"context": _build_context(state, state_file, raw)}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Absolute backstop. Nothing this script can do is worth a failed turn.
        sys.exit(0)
