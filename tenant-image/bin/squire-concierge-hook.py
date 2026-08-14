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

WHY THE DIRECTIVE ORDERS THE TOOL CALL FIRST
--------------------------------------------
v0.1.2 shipped and the greeting landed correctly — then a SECOND message
arrived: "Done — let me know what to call you." That was NOT a second
injection, and the difference matters because the two causes have opposite
fixes.

hermes invokes `pre_llm_call` from ``build_turn_context``, called once at
agent/conversation_loop.py:1297, BEFORE the API loop at :1402, under a comment
that reads "All once-per-turn setup ... the ``pre_llm_call`` plugin hook ...
lives in ``build_turn_context``". One call site, one firing per user turn,
regardless of how many API round-trips the turn takes.

What actually happened is a single turn emitting two assistant text blocks:
greeting -> terminal tool call (the state write) -> a trailing "Done ...". The
gateway delivers mid-turn prose as its own Telegram message when
``interim_assistant_messages`` is true (hermes_cli/config_defaults.py:1193), so
one turn became two messages.

The decisive evidence that it was not a re-injection: the second message asked
for the NAME again. A re-injection would have carried the `ask_name` directive,
whose whole job is to ask for the TIMEZONE. It asked the greet question, so it
came from the greet turn.

Hence the ordering in the wrapper below: run the state-write FIRST and silently,
then send exactly one message. That leaves the greeting as the turn's final
text, with no mid-turn prose to be delivered separately, and it is why the
wrapper is so blunt about "no 'done', no summary".

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
# Style rules baked into the copy below: imperative mood, numbered concrete
# steps, no conditionals the model has to evaluate, and an explicit literal
# command for the state write.
#
# These were written for the Haiku-class trial model that shipped first, and
# they are KEPT now that the trial runs Sonnet-class. The whole thesis of this
# hook is that the first impression must not depend on the model being clever
# enough to infer what to do — trading that back for shorter copy would
# reintroduce exactly the failure it was built to remove, and the trial model
# can change again without anyone revisiting this file.
#
# TELEGRAM FORMATTING (verified against the pristine upstream v2026.8.3 tree,
# 2026-08-13, before the warmth/structure copy pass below was written):
#
# Agent replies DO render formatting. The tenant runs upstream's Telegram
# platform adapter, and its send path is:
#
#   plugins/platforms/telegram/adapter.py
#     send()            :4415  — every outbound reply text enters here
#     format_message()  :7583  — standard markdown -> MarkdownV2 converter
#     bot.send_message(parse_mode=ParseMode.MARKDOWN_V2)  :4544
#     (streaming edits use the same parse_mode at :4866; on a MarkdownV2
#      parse failure both paths retry as stripped plain text, :4552-4558)
#
# What that conversion means for copy, encoded in the FORMATTING paragraph of
# the wrapper in _build_context and in the directives below:
#   * **double asterisks** -> bold. The ONE inline marker copy may rely on.
#   * _underscores_ are never converted (format_message step 6 handles only
#     single *asterisks*); they are escaped and render as literal underscores.
#   * # headings become whole bold lines and pipe tables get rewritten into
#     row groups — both the wrong register for a chat message. Banned.
#   * Hyphen lists, blank lines and emoji pass through untouched — they are
#     the structural tools that survive even the plain-text fallback.

_DIRECTIVES = {
    "greet": """This person has just signed up and this is the very first message they have
ever sent you. They do not know what you are, what you can do, or what they are
supposed to do next. Right now the burden is entirely on you.

Reply with ONE chat message that does all four of these, in your own voice —
warm, human, and glad they showed up, never salesy. Shape it as short lines
with a blank line between thoughts, not one paragraph block, and bold the few
words that carry the message (the FORMATTING rules below say exactly what
renders):

1. Open with a warm hello and say what you are: their own personal assistant,
   living in this chat, working only for them.
2. Give them three CONCRETE examples of what that means, not abstractions —
   each on its own short line, so they scan rather than read. Use these and
   nothing else: you remember everything across every conversation, so they
   never have to repeat themselves; you can go off and do a thing and then
   follow up on your own later, including on a schedule; and you check with
   them before anything that deletes, spends money, or goes out to another
   person.
3. Say the first thing you will do together: connect their own AI account —
   OpenAI or Anthropic — and say you will walk them through it right after
   this. One sentence. Do not mention the trial, any allowance or cap, any
   prices, or paying for anything.
4. End on exactly ONE question, on its own line: what should you call them?

KEEP IT UNDER 120 WORDS, and under 8 short lines of text — blank lines
between them are free, and welcome. This is a text message, not a landing
page: a wall of text on message one is its own kind of failure, just as bad
as saying nothing.

Then wait for their answer. Do not ask a second question. Do not mention slash
commands or /help. Do not offer to build a profile of them.""",
    "ask_name": """You are waiting to learn what to call this person. If their message contains a
name, take it, use the short form, and thank them warmly in half a sentence —
do not make a production of it. Save it with the hindsight_retain tool.

Then open the setup step the greeting promised: getting them onto their own AI
account. This message is a friendly menu, not a settings page. Shape it as: a
short warm intro of two or three lines, a blank line, then the three options
ONE PER LINE — each a **bolded label** plus a one-line plain-language
description — and the question last, on its own line.

The intro, truthfully: out of the box they run on a small built-in allowance
Squire includes so day one just works, and it is capped; connecting their own
AI account lifts the caps and upgrades the model. Be explicit about whose bill
is whose: their AI usage is then billed by their own provider — it is not a
payment to Squire — and their conversations stop touching Squire's AI
infrastructure entirely.

Then ask ONE question: which of these three they already have. Read
{skill_file} under `providers` and keep these honest
labels intact:

  - **OpenAI API key** — fully supported
  - **ChatGPT subscription**, used via Codex — coming soon; OpenAI's Codex
    team openly supports third-party agents using ChatGPT sign-in (unlike
    Anthropic), though there's no formal written guarantee and OpenAI could
    change this — and the sign-in flow is not live in this alpha yet
  - **Anthropic API key** — fully supported

Never drop the "coming soon" flag from the ChatGPT-subscription line —
picking it is welcome, but nobody should pick it expecting to finish
connecting today — and never inflate the Codex team's open support into a
formal OpenAI endorsement: there is no written guarantee to promise.

If they ask about connecting a Claude subscription (earlier alphas offered
one, so they may have seen it): read the `claude_subscription` entry in the
`facts` block of {skill_file} and answer from it — it isn't available,
Anthropic's terms don't permit it for third-party apps, and an Anthropic API
key from console.anthropic.com is the way to run Claude models here. Answer
only if they ask — never volunteer it.
If they ask what the allowance is, or what Squire itself costs, read the
`facts` block in {skill_file} and answer with the values
written there — never from memory, and never rounded or softened — then come
back to the question. Do not volunteer prices.

If they dodged the name question or asked you something else instead, answer
them first and ask for their name once more, lightly. Never ask twice in a
row.""",
    "ask_timezone": """You are waiting for this person's location or timezone. Convert whatever they
give you (a city is a perfectly good answer) into an IANA timezone name such as
Europe/London or America/Los_Angeles. Do three things: save it with
hindsight_retain, confirm it back to them in a few words, and record it by
running this ONE command with the terminal tool, on a single line, changing
only Europe/London to their actual zone:

{tz_cmd}

Do not rewrite {config_file} any other way. That file also holds the runtime's
own settings, and replacing it wholesale breaks them.

Then ask ONE question: what is one thing they would like you to take off their
plate this week.""",
    "ask_first_job": """This person has just told you one real thing they want help with. Save it with
hindsight_retain, with enough context that you can pick it up again unprompted
tomorrow — this is the single most valuable thing you will learn today.

If it is something you can actually start right now, START IT. Do the work in
this message and continue onboarding afterwards. Never make someone sit through
a form to get the thing they asked for.

Do not ask a new question this turn. Let them react to the work.""",
    "connect_llm": """This person has just answered which AI account they have — or said they have
none, or asked something about the choice.

Whatever they picked, record it: in the STEP 1 command, set the "llm" value
to the picked option's exact id — "openai_api_key", "openai_codex_oauth" or
"anthropic_api_key". The next step reads that key to give them the right
hand-off. If they picked nothing, leave the value as it is.

If they picked the OpenAI API key or the Anthropic API key — the paths that
are live today — confirm it in one warm line and get the
credential moving. Read {skill_file} under `providers`
for that option's detail and its `fallback`. THE ONE-TIME LINK DOES NOT EXIST
YET — the tenant-served `/connect/<nonce>` endpoint ships in Phase 1C — so
never invent a URL: ask them to paste the credential here and say you will
secure it the moment it lands. Explain the missing link at most once in the
whole conversation — after this message, never repeat that explanation. Keep
the whole reply to a few short lines with a blank line between steps — this
is a quick hand-off, not a form.

If they picked the ChatGPT subscription (via Codex): that path is COMING
SOON and cannot be connected in this alpha yet. Send one warm message that
does exactly three things: confirm it is a good choice and it is coming —
their subscription will be usable the moment the sign-in flow ships; offer
the OpenAI API key path (a key from platform.openai.com) as what works
today; and promise nothing else. Never say a sign-in starts "in a moment",
never offer to kick off a flow that does not exist, and never ask them to
confirm they are ready for one.

If they asked about connecting a Claude subscription (earlier alphas offered
one): read the `claude_subscription` entry in the `facts` block of the same
file and answer from it — it isn't available, Anthropic's terms don't permit
it for third-party apps, and an Anthropic API key is the way to run Claude
models here. Answer only when asked — never volunteer it.

If they asked about the allowance, Squire's own pricing, or the difference,
read the `facts` block in the same file and answer with the values written
there — never from memory. Keep the two bills separate: AI usage on their own
account is their provider's bill; a Squire subscription pays for the assistant
itself, later, and is no part of this setup.

If they have none of these, or say "not now": accept it immediately without
nagging, say the built-in allowance keeps working meanwhile, and move on —
write the state as "ask_timezone" instead, and ask ONE question: where are
they, or what timezone, so that when you say "tomorrow morning" you mean
it.""",
    "awaiting_credential": """This person is sending, or about to send, an AI account credential.

Ask them to paste the key here, and handle it exactly as below. Never invent
a URL, and never re-explain why there is no one-time link — that explanation
was already given when they chose, and it is said at most once per
conversation. Read {skill_file} under `awaiting_credential` for
the detail.

When they paste an API key into the chat: store it in {env_file}
immediately, then DELETE their Telegram message right away without asking, then
tell them once, plainly and without scolding — two or three short lines, not a
security lecture — that the key worked, that you removed the message, and that
a pasted key did travel through Telegram and Squire's relay, so they can
rotate it if they want to be careful. State that disclosure straight and never
minimise it: qualifiers like "totally fine", "don't worry" and "perfectly
safe" are banned. The deletion and the rotation offer are the reassurance —
the words must not editorialise.

If the credential is rejected, say what the provider said in one line and offer
that provider's `fallback` from the same file. Never retry more than twice —
offer to come back to it later instead.

If they have gone quiet on it or changed the subject, drop it: answer what they
actually asked and write the state as "complete". You can raise it again when
the trial is nearly up.""",
    "connected": """Their own AI account is connected. This is good news — sound pleased about
it. Confirm which provider is live in one warm line, say their own key is in
use from now on so the built-in allowance's caps no longer apply to them, and
offer one concrete thing you can now do better. Short lines, not a paragraph.

Do NOT say the trial key has been revoked or that their traffic now goes
direct — neither is true yet.

Then ask ONE question: where are they, or what timezone, so that when you say
"tomorrow morning" you mean it.""",
}

# ---------------------------------------------------------------------------
# Coming-soon providers: on the menu, honestly flagged, but NOT connectable
# today — nothing in this image implements their credential flow (device-code
# sign-in is Phase 1C roadmap). Mirrors `status: coming_soon` in
# state-machine.yaml's providers block; the test suite asserts the two sets
# are identical.
#
# How the choice reaches this hook: concierge-state.json carries an `llm` key
# (seeded "trial" by squire-entrypoint.sh), the STEP 1 write command carries
# every key of that file, and the connect_llm directive tells the model to set
# `llm` to the picked provider's id. On the next turn this hook reads the file
# and, for `awaiting_credential`, swaps in the variant below — so a user who
# picked a coming-soon option is never hit with "paste your API key" as if
# that were what they chose (live incident, 2026-08-14).
# ---------------------------------------------------------------------------
_COMING_SOON_PROVIDERS = {"openai_codex_oauth"}

# The `awaiting_credential` directive used when the recorded choice is a
# coming-soon provider. One honest, warm hand-off: no paste demand, no
# sign-in promise, the API-key path offered as what works today.
_AWAITING_COMING_SOON = """This person picked the ChatGPT subscription (via Codex). That path is COMING
SOON and cannot be connected in this alpha yet — there is no sign-in flow to
start, so never announce or "kick off" a sign-in, and never demand a
credential as if the subscription itself could land that way.

If they are asking to go ahead: say once, warmly, that their subscription
will be usable the moment the sign-in flow ships, and that the OpenAI API
key path (a key from platform.openai.com) works today if they would rather
not wait. Never say "in a moment", and never re-explain why there is no
one-time link — that was already said, at most once per conversation.

If they paste an OpenAI API key: that is the live path — store it in
{env_file} immediately, then DELETE their Telegram message right away
without asking, and in the STEP 1 command set "llm" to "openai_api_key".
Then tell them once, plainly and without scolding — two or three short
lines — that the key worked, that you removed the message, and that a pasted
key did travel through Telegram and Squire's relay, so they can rotate it if
they want to be careful. Never minimise that disclosure: qualifiers like
"totally fine", "don't worry" and "perfectly safe" are banned.

If they would rather wait for the sign-in flow: accept warmly, say the
built-in allowance keeps working meanwhile — in the STEP 1 command set "llm"
to "trial" and the state to "ask_timezone" — and ask ONE question: where are
they, or what timezone, so that when you say "tomorrow morning" you mean it.
Only let the state advance to "connected" when a credential has actually
landed."""

# Where each state goes once its work is done. Mirrors the `then:` edges in
# state-machine.yaml; the test suite asserts the two agree.
_NEXT_STATE = {
    "greet": "ask_name",
    "ask_name": "connect_llm",
    "connect_llm": "awaiting_credential",
    "awaiting_credential": "connected",
    "connected": "ask_timezone",
    "ask_timezone": "ask_first_job",
    "ask_first_job": "complete",
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
    "/state-machine.yaml" and the model finds nothing. That matters most in
    `ask_name`, where the pointers to the authoritative `providers` and
    `facts` blocks are the guard rail against inventing labels, prices and
    limits.
    """
    return str(_home() / "skills" / "concierge" / "state-machine.yaml")


def _timezone_command() -> str:
    """A single-line command that rewrites ONLY the `timezone:` line.

    Single-line and shlex-quoted for the same reason the state-write command is:
    an emitted command has to run EXACTLY as printed. The first version of this
    was an indented ``python3 - <<'PY'`` heredoc, which cannot work — the
    delimiter must be at column 0 unless you use ``<<-`` (and even that only
    strips tabs) — so running it verbatim died with "here-document delimited by
    end-of-file" plus an IndentationError, leaving config.yaml untouched.

    That failure was invisible to the agent, which would confirm the timezone
    and move on while the file still said UTC, defeating the exact step that
    exists so "tomorrow morning" means something. And a model that helpfully
    de-indented it would have succeeded, making the outcome probabilistic —
    the opposite of this hook's whole thesis.

    Targeted on purpose: an anchored per-line substitution, not a YAML
    round-trip. config.yaml also carries the `hooks:` block that makes
    onboarding work at all, and rewriting the file wholesale is how that gets
    silently dropped.
    """
    script = (
        "import re,pathlib;"
        "p=pathlib.Path({!r});"
        "p.write_text(re.sub(r'(?m)^timezone:.*$', "
        "'timezone: \"Europe/London\"', p.read_text()))"
    ).format(str(_home() / "config.yaml"))
    return "python3 -c " + shlex.quote(script)


def _counter_path(state_file: Path) -> Path:
    """Sibling file holding the safety-valve counter, as "<state> <count>".

    Kept OUT of concierge-state.json on purpose: that file is written by the
    agent, and a hook that read-modify-writes the same file the agent is
    rewriting would race with it and could clobber a state transition. A
    separate counter can only ever lose a count, which is harmless.
    """
    return state_file.with_name("concierge-hook-turns")


def _read_counter(path: Path) -> tuple[str, int, str]:
    """Return (state, count, last_turn_id) from the valve file, or empty.

    Tolerates the 2-field format written by images before turn-id dedupe: a
    tenant upgrading mid-onboarding must not have the valve blow up on a file
    it wrote yesterday.
    """
    try:
        parts = path.read_text(encoding="utf-8").split()
    except Exception:
        return "", 0, ""
    try:
        if len(parts) >= 3:
            return parts[0], int(parts[1]), parts[2]
        if len(parts) == 2:  # older image, before turn-id dedupe
            return parts[0], int(parts[1]), ""
    except Exception:
        return "", 0, ""
    return "", 0, ""


def _bump_counter(path: Path, state: str, turn_id: str = "") -> int:
    """Count consecutive injections for `state`, resetting on any change."""
    prev_state, count, _ = _read_counter(path)

    # Progress resets the budget. Anything else — including a legacy
    # bare-integer counter file from an older image — starts a fresh count.
    count = count + 1 if prev_state == state else 1

    try:
        path.write_text(f"{state} {count} {turn_id or '-'}", encoding="utf-8")
    except Exception:
        # An unwritable counter must not stop onboarding — we just lose the
        # safety valve, which is the less bad of the two failures.
        pass
    return count


def _already_injected_this_turn(path: Path, turn_id: str) -> bool:
    """True when we have already injected for this exact turn id.

    BELT AND BRACES, not the fix for the double-reply bug. hermes invokes
    `pre_llm_call` from build_turn_context (agent/conversation_loop.py:1297),
    which sits BEFORE the API loop at :1402 and is documented there as
    "all once-per-turn setup" — one call site, one firing per user turn, no
    matter how many API round-trips the turn takes. Verified against
    v2026.8.3 rather than assumed: the double reply was NOT a second
    injection (see the module docstring).

    This guard exists so that if a future upstream ever moves the hook inside
    the loop, onboarding degrades to "injected once" instead of silently
    re-greeting on every tool call. An empty turn id disables it — never let a
    missing field suppress onboarding entirely.
    """
    if not turn_id:
        return False
    return _read_counter(path)[2] == turn_id


def _build_context(state: str, state_file: Path, raw: dict) -> str:
    """Assemble the injected directive for `state`."""
    # Resolve paths to absolute literals. Done with str.replace rather than
    # str.format so a stray brace anywhere in the copy can never raise — this
    # runs in front of every message and must not be able to fail.
    directive = _DIRECTIVES[state]

    # Per-provider branch: when the recorded choice (the `llm` key the
    # connect_llm turn wrote) is a coming-soon provider, the credential step
    # must not demand a paste or promise a sign-in — it gets the honest
    # variant instead. Any other value — a live provider id, "trial", or a
    # missing key on an older state file — keeps the live paste path.
    if state == "awaiting_credential" and str(raw.get("llm") or "").strip() in _COMING_SOON_PROVIDERS:
        directive = _AWAITING_COMING_SOON
    for token, value in (
        ("{skill_file}", _skill_file()),
        ("{config_file}", str(_home() / "config.yaml")),
        ("{env_file}", str(_home() / ".env")),
        ("{tz_cmd}", _timezone_command()),
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
        "DO THESE TWO THINGS, IN THIS ORDER, AND NOTHING ELSE:\n\n"
        "STEP 1 — before you say anything at all, run this with the terminal "
        "tool. Send no message first:\n"
        f"    {write_cmd}\n"
        "That command already carries every key the file has. If you learned "
        "something this turn (their name, their timezone, which provider they "
        "picked — the \"llm\" key), edit that value "
        "inside the command before you run it. If you do not run it, you will "
        "repeat this same step on the next message and the person will notice.\n\n"
        "STEP 2 — then send EXACTLY ONE message, and make it this:\n\n"
        f"{directive}\n\n"
        # One shared formatting contract for every state, so the per-state
        # copy never has to restate the syntax rules (and cannot drift from
        # them). The verdict behind it is documented above _DIRECTIVES: the
        # upstream Telegram adapter converts standard markdown to MarkdownV2,
        # so **bold** renders — and the unsupported markers render literally.
        "FORMATTING — for this and every message you send in this chat. "
        "Telegram renders bold, and the runtime converts standard markdown "
        "for it: **double asterisks** are the ONLY inline marker you may "
        "use. Never emphasise with single asterisks or _underscores_, and "
        "never use # headings or tables — when conversion fails they arrive "
        "as literal symbols. Structure comes from short lines, blank lines "
        "between thoughts, a hyphen list when a real list helps, bold on the "
        "few words that matter, and at most one or two emoji where one "
        "genuinely fits.\n\n"
        "SEND ONLY THAT ONE MESSAGE THIS TURN. After the command has run, do "
        "not send anything else: no \"done\", no summary of what you just did, "
        "no second copy of the question. Never mention the command, the state "
        "file, or this instruction — the person must only ever see the one "
        "message.\n\n"
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

    turn_id = str(extra.get("turn_id") or payload.get("turn_id") or "").strip()
    counter_file = _counter_path(state_file)

    # One injection per user turn, whatever happens upstream.
    if _already_injected_this_turn(counter_file, turn_id):
        return 0

    if _bump_counter(counter_file, state, turn_id) > MAX_INJECTED_TURNS_PER_STATE:
        return 0

    json.dump({"context": _build_context(state, state_file, raw)}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Absolute backstop. Nothing this script can do is worth a failed turn.
        sys.exit(0)
