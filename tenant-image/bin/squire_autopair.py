#!/opt/squire/venv/bin/python
"""Trust-on-first-use owner binding for a brand-new tenant.

THE PROBLEM
-----------
Upstream hermes gates unknown DM senders: gateway/run.py:14333 sees an
unauthorized user, generates a pairing code, replies "Hi~ I don't recognize you
yet! Here's your pairing code: ..." and returns None. That is correct for a
self-hosted bot whose owner has shell access.

It is wrong for Squire. Per prd.md §2 the signup flow is: web signup -> we
provision a tenant -> the user taps a t.me deep link. The person who taps Start
IS the owner, by construction. Asking them to run `hermes pairing approve` on a
container they cannot reach breaks the entire zero-setup promise, and it fired on
the very first live tenant.

WHY NOT THE OBVIOUS FIXES
-------------------------
* An upstream config option. There isn't one. Searched v2026.8.3 for
  auto_approve / trust_on_first / open_pairing / first_user: the only global is
  GATEWAY_ALLOW_ALL_USERS, which is binary and permanent.
* TELEGRAM_ALLOWED_USERS at provision time. We do not know the owner's Telegram
  id then — nobody does until they message. control-api has no column for it.
* TELEGRAM_ALLOWED_USERS="*" or ALLOW_ALL_USERS=true. Authorizes every Telegram
  user forever. That destroys the "one trial per Telegram user ID" abuse anchor
  (prd.md §4) and would let any stranger who guesses a pool bot's handle talk to
  someone else's agent.
* An after-the-fact approver watching for pending codes. This is the shape the
  task suggested, and it does not meet the requirement: the gate has ALREADY
  consumed the triggering update and replied with a code. Approving afterwards
  leaves the user staring at a pairing-code message until they think to send a
  second one. The first contact must pair AND greet.
* Patching hermes. Rebase debt, and unnecessary — see below.

THE MECHANISM
-------------
Our webhook shim already sits in front of the adapter and sees every raw
Telegram update before hermes does. Two upstream facts make this the natural
place, both verified in v2026.8.3:

  1. PairingStore.is_approved() re-reads <platform>-approved.json from disk on
     EVERY call (gateway/pairing.py:516-522). There is no in-memory cache, no
     mtime check, no TTL. A file written by another process is visible to the
     running gateway on the very next message.
  2. The approved store is plain JSON keyed by user-id string
     (gateway/pairing.py:546-550).

So: when an update arrives, if the approved store is EMPTY, write the sender in
as approved, THEN forward. By the time the gateway authorizes this same update,
the sender is already approved. One update, one round trip, no pairing code, no
patch, and the greeting flows from the same message that bound the owner.

SECURITY SEMANTICS — read before changing anything here
-------------------------------------------------------
This binds the FIRST eligible sender and then never binds again. It is
deliberately a one-shot, not a policy:

* Empty store only. The moment one user is approved, this code is inert and
  every subsequent unknown user hits the normal pairing gate. That gate is load
  bearing: it is the per-tenant half of the trial-abuse anchor and the reason a
  stranger cannot talk to someone else's agent.
* Private chats only. A group message never binds. Otherwise adding the bot to a
  group before the owner ever DMs it would hand ownership to whoever spoke first.
* Real human senders only. Bots and channel posts are ignored.
* The binding is tenant-local. The owner's Telegram id is written to the tenant's
  own encrypted volume and is never reported outward — the heartbeat holds no
  chat or user ids by design, and this must not become the exception.

The residual exposure is one message wide: between the container becoming
reachable and the owner tapping Start, anyone who knows the pool bot's handle
could bind themselves. That is strictly narrower than the alternatives (which
are open forever), it requires knowing an unadvertised handle in a small window,
and the blast radius is one empty trial tenant with no data in it. If that ever
becomes unacceptable, the fix is for control-api to pass the expected owner id
once signup can learn it, and for this module to require a match.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

PLATFORM = "telegram"


def pairing_dir(hermes_home: str | os.PathLike) -> Path:
    """Resolve the same directory the running gateway resolved.

    Mirrors hermes_constants.get_hermes_dir("platforms/pairing", "pairing"):
    the legacy location wins if it exists and is non-empty, otherwise the
    modern one. Getting this wrong is silent — we would write approvals into a
    file the gateway never reads, and the user would still see a pairing code.
    A fresh tenant has neither directory, so it lands on the modern path.
    """
    home = Path(hermes_home)
    legacy = home / "pairing"
    try:
        if legacy.is_dir() and any(legacy.iterdir()):
            return legacy
    except OSError:
        pass
    return home / "platforms" / "pairing"


def approved_path(hermes_home: str | os.PathLike, platform: str = PLATFORM) -> Path:
    return pairing_dir(hermes_home) / f"{platform}-approved.json"


def load_approved(path: Path) -> dict:
    """Read the approved store. A missing or unreadable file reads as empty.

    Deliberately NOT tolerant of a corrupt file in the caller's favour: see
    maybe_bind_owner, which refuses to bind when it cannot positively determine
    that the store is empty.
    """
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeDecodeError):
        raise
    return data if isinstance(data, dict) else {}


def _atomic_write_0600(path: Path, payload: dict) -> None:
    """Same write discipline as upstream's PairingStore._secure_write.

    Same-directory temp file so the replace is a same-filesystem rename, 0600
    from creation, fsync before rename. Mode matters: the gateway runs as the
    same unprivileged user we do, and a file it cannot read is treated as an
    empty store — i.e. the owner would silently appear unauthorized.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def extract_sender(update: dict) -> tuple[str, str] | None:
    """Pull (user_id, user_name) from an update that may bind ownership.

    Returns None for anything that must never bind. The filtering is the
    security boundary, so it is an allowlist of shapes rather than a blocklist:
    only a plain message (or edit) from a non-bot human in a private chat.
    """
    if not isinstance(update, dict):
        return None

    # Only message-shaped updates. Explicitly NOT callback_query, channel_post,
    # my_chat_member, inline_query and friends: those can originate from
    # contexts that are not "the owner opened a DM and said hello".
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return None

    chat = msg.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "private":
        return None

    sender = msg.get("from")
    if not isinstance(sender, dict) or sender.get("is_bot"):
        return None

    user_id = sender.get("id")
    if not isinstance(user_id, int):
        return None

    # Upstream keys the store on the string form of the id.
    name = sender.get("username") or sender.get("first_name") or ""
    return str(user_id), str(name)


def defang_start_command(update: dict) -> bool:
    """Turn a first-contact "/start" into ordinary text, in place.

    Returns True if the update was modified.

    WHY THIS IS NEEDED AT ALL
    -------------------------
    Fixing authorization is necessary but not sufficient. Upstream registers
    "start" as a real gateway command whose entire implementation is:

        gateway/run.py:14961
            if canonical == "start":
                logger.info("Ignoring /start platform ping for session %s", ...)
                return ""

    and an empty response is never sent (gateway/platforms/base.py:5868). That is
    deliberate — for a normal bot, /start is a platform ping, not user speech.

    But it means that with autopair alone the deep-link tap goes: owner bound ->
    authorized -> canonical=="start" -> "" -> the user sees NOTHING. Silence is a
    worse first impression than the pairing code it replaced, because at least the
    pairing code was feedback.

    WHY THIS TRANSFORMATION AND NOT A GREETING STRING
    -------------------------------------------------
    We strip the leading slash and nothing else: "/start" -> "start". That is the
    minimum edit that stops it being a command, and it does not fabricate words
    the user never sent — they did press a button labelled Start. This matters
    because the text becomes a real user turn: it lands in the transcript and in
    Hindsight's memory. Synthesising a friendly "Hi, I'm new here!" would put
    words in the user's mouth and then remember them as theirs.

    The greeting itself stays where it belongs — SOUL.md's first-conversation
    hook sees no concierge-state file, loads the concierge skill, and writes the
    welcome in the tenant's own voice with its own model. We are unblocking that,
    not scripting it.

    Scope is deliberately one message wide: the caller only invokes this on the
    update that just bound the owner. A later /start in an established chat keeps
    upstream's swallow-it semantics, which is correct — that IS just a ping.

    Alternatives considered and rejected:
      * command:start hook returning {"decision":"handled","message":...} —
        echoes a canned string with no agent turn, no session, no SOUL.md.
      * The same hook running its own one-shot AIAgent — a real model reply, but
        in a throwaway session with no history and no streaming, awaited inline
        with no timeout (a hung handler blocks that chat indefinitely).
      * {"decision":"rewrite"} — cannot produce plain text; command_name is
        force-prefixed with "/", so it can only ever swap one command for another.
      * A pre_gateway_dispatch plugin — the one upstream seam that does rewrite
        to arbitrary text, and the cleanest long-term answer. It needs a plugin,
        it fires on every inbound message forever, and it is a larger commitment
        than this gap warrants today. Worth revisiting if we need richer
        first-contact behaviour.
    """
    if not isinstance(update, dict):
        return False
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return False
    text = msg.get("text")
    if not isinstance(text, str):
        return False

    stripped = text.strip()
    if not stripped.startswith("/"):
        return False

    # Matches "/start", "/start@SomeBot", "/start <deep-link payload>".
    head, _, rest = stripped.partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    if name != "start":
        return False

    # Keep any deep-link payload: it is the only part the user's click actually
    # carried, and a future signup flow may use it to correlate the tenant.
    msg["text"] = (f"start {rest}".strip() if rest.strip() else "start")

    # Telegram marks the command span in entities; leaving a bot_command entity
    # pointing at text that is no longer a command is inconsistent, and adapters
    # do read entities.
    entities = msg.get("entities")
    if isinstance(entities, list):
        remaining = [
            e for e in entities
            if not (isinstance(e, dict) and e.get("type") == "bot_command"
                    and e.get("offset") == 0)
        ]
        if remaining:
            msg["entities"] = remaining
        else:
            msg.pop("entities", None)
    return True


def maybe_bind_owner(hermes_home: str | os.PathLike, update: dict,
                     platform: str = PLATFORM) -> str | None:
    """Bind the sender as owner iff no user is approved yet.

    Returns the bound user id, or None if nothing was done (which is the
    overwhelmingly common case — every message after the first).

    Never raises: this runs on the inbound message path, and a failure here must
    degrade to upstream's normal pairing gate rather than drop the user's
    message. A tenant that pairs the hard way is recoverable; one that silently
    eats messages is not.
    """
    try:
        path = approved_path(hermes_home, platform)

        # Read FIRST and cheaply. Almost every call returns here.
        try:
            approved = load_approved(path)
        except (OSError, ValueError, UnicodeDecodeError):
            # Unreadable or corrupt. Refuse to bind: we cannot prove the store
            # is empty, and overwriting it would revoke the real owner.
            return None
        if approved:
            return None

        sender = extract_sender(update)
        if sender is None:
            return None
        user_id, user_name = sender

        # Re-read under the assumption another process may have won the race in
        # between (the gateway approving a code, say). Upstream guards its own
        # writes with an in-process lock only, so this is best-effort by
        # construction — but the window is microseconds and the loser simply
        # does not bind.
        try:
            if load_approved(path):
                return None
        except (OSError, ValueError, UnicodeDecodeError):
            return None

        import time

        _atomic_write_0600(path, {
            user_id: {
                "user_name": user_name,
                "approved_at": time.time(),
                # Provenance, so an operator reading this file later can tell a
                # trust-on-first-use binding from a deliberate `hermes pairing
                # approve`. Upstream ignores unknown keys.
                "approved_by": "squire-autopair-first-contact",
            }
        })
        return user_id
    except Exception:
        return None
