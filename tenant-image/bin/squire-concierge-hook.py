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
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Safety valve
# ---------------------------------------------------------------------------
# The failure mode opposite to "no onboarding" is "endless onboarding": if the
# agent never writes the state file, an unbounded hook would re-inject the same
# directive on every turn forever — Groundhog Day, on a trial capped at 75
# messages a day. So we count how many turns we have injected and give up.
#
# 14 is comfortably more than the 6-state flow needs even with detours (the
# user asking about price, a failed credential, a real task done mid-flow), and
# far below the point where a stuck flow has burned the trial.
MAX_INJECTED_TURNS = 14

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
   you always check with them first before anything that deletes, spends money,
   or goes out to another person.
3. Tell them the one thing they have to know: they are on a free 3-day trial
   running on Squire's shared AI, which is capped, and that connecting their own
   AI account is what unlocks the full thing — say you will walk them through it
   in a minute. Do not explain the trial further yet and do not list any prices.
4. Ask exactly ONE question: what should you call them?

Then stop and wait for their answer. Do not ask a second question. Do not
mention slash commands or /help. Do not offer to build a profile of them.""",
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
hindsight_retain, write it into the `timezone:` key of ${HERMES_HOME}/config.yaml
with the terminal tool, and confirm it back to them in a few words.

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

Only these facts, exactly: it is free for 3 days (72 hours) from signup with no
card; it is capped at 75 messages a day; at 72 hours you stop answering and
reply only with a link to subscribe; their data is kept for 14 days after that
and then permanently destroyed unless they subscribe.

Do not pitch. Do not quote any price unless they ask — if they do ask, the only
numbers you may say are in ${HERMES_SKILL_DIR}/state-machine.yaml under `facts`.
Do not ask a question this turn.""",
    "connect_llm": """Now do the step that matters most: get them onto their own AI account.

Frame it truthfully. Right now they are on Squire's shared trial brain, which is
capped. Connecting their own account lifts the caps, upgrades the model, and
means their AI costs go to their own provider and their conversations stop
touching Squire's AI infrastructure entirely.

Then ask which of these four they already have. Read
${HERMES_SKILL_DIR}/state-machine.yaml under `providers` and use those honest
labels:

  - an OpenAI API key (fully supported)
  - a ChatGPT subscription, used via Codex (supported — OpenAI sanctions this)
  - an Anthropic API key (fully supported)
  - a Claude Max subscription (NOT supported by Anthropic — it works today but
    may break without warning, and you fall back to an API key if it does)

Never soften, bury or omit that last warning to make the option look better. If
they say "not now", accept it immediately without nagging and write the state as
"complete".""",
    "awaiting_credential": """This person is sending, or about to send, an AI account credential. Never let a
credential travel through chat if you can avoid it — follow
${HERMES_SKILL_DIR}/state-machine.yaml under `awaiting_credential`.

If they pasted an API key into the chat anyway: store it in ${HERMES_HOME}/.env
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


def _counter_path(state_file: Path) -> Path:
    """Sibling file holding the injected-turn count for the safety valve.

    Kept OUT of concierge-state.json on purpose: that file is written by the
    agent, and a hook that read-modify-writes the same file the agent is
    rewriting would race with it and could clobber a state transition. A
    separate counter can only ever lose a count, which is harmless.
    """
    return state_file.with_name("concierge-hook-turns")


def _bump_counter(path: Path) -> int:
    """Increment and return the injected-turn counter. Never raises."""
    count = 0
    try:
        count = int(path.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        count = 0
    count += 1
    try:
        path.write_text(str(count), encoding="utf-8")
    except Exception:
        # An unwritable counter must not stop onboarding — we just lose the
        # safety valve, which is the less bad of the two failures.
        pass
    return count


def _build_context(state: str, state_file: Path) -> str:
    """Assemble the injected directive for `state`."""
    directive = _DIRECTIVES[state]
    next_state = _NEXT_STATE.get(state, COMPLETION_STATE)

    # The literal write command matters. "Update the state file" is the kind of
    # soft instruction a small model silently skips; a copy-pasteable command
    # is not. Onboarding surviving a restart depends entirely on this landing.
    write_cmd = (
        "printf '%s' '{\"state\":\"" + next_state + "\"}' > " + str(state_file)
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
        "terminal tool, keeping any keys the file already has:\n"
        f"    {write_cmd}\n"
        "If you do not write that file, you will repeat this same step on the "
        "next message and the person will notice.\n\n"
        "If they ask you to stop onboarding, or just start giving you real work "
        "to do, then do the real work and write \"complete\" as the state "
        "instead. Never block someone at the front door.]"
    )


def main() -> int:
    # Consume stdin unconditionally. If we exit without reading, hermes can get
    # a broken pipe writing the payload — a hook that is supposed to be
    # invisible must not perturb the caller.
    try:
        sys.stdin.read()
    except Exception:
        pass

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

    if _bump_counter(_counter_path(state_file)) > MAX_INJECTED_TURNS:
        return 0

    json.dump({"context": _build_context(state, state_file)}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Absolute backstop. Nothing this script can do is worth a failed turn.
        sys.exit(0)
