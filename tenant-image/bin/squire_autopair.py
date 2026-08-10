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

* Authenticated requests only. The caller (squire-webhook-shim.py) binds only
  when the request carries the correct per-bot secret, which ingress re-stamps
  on every forward. Without that check, "can reach this tenant's port" would
  equal "is its permanent owner" — and the fleet-wide INTERNAL_API_TOKEN is
  readable by every trial tenant's own agent, so it must NOT be the credential
  that gates this. An unauthenticated update is still delivered; it just cannot
  bind, and degrades to the pairing code, which is visible and recoverable.

* Deep-link nonce, when provisioned. Pool bots are RECYCLED between tenants,
  and Telegram gives the previous owner the bot chat forever — so "first
  eligible sender" alone lets a recycled bot's previous owner bind themselves
  to the NEXT tenant, passing every check above (private chat, human, valid
  per-bot secret via real Telegram delivery). When SQUIRE_BIND_NONCE is set,
  binding additionally requires the update to be a "/start <nonce>" matching
  it (timing-safe), which only the holder of the t.me/<bot>?start=<nonce>
  link — the person control-api provisioned this tenant FOR — can send. When
  the variable is unset, legacy trust-on-first-use applies unchanged (see
  maybe_bind_owner for why unset must stay open rather than fail closed).

The residual exposure without a nonce is one message wide: between the
container becoming reachable and the owner tapping Start, an attacker who
holds a valid per-bot secret could bind themselves. That secret is known only
to Telegram, control-api and the tenant. That is strictly narrower than the
alternatives (which are open forever), and the blast radius is one empty trial
tenant with no data in it.

KNOWN LIMITS, stated so nobody discovers them the hard way
----------------------------------------------------------
* POLLING MODE HAS NO AUTOPAIR. This runs in the webhook shim, which only sees
  updates when the adapter is in webhook mode (TELEGRAM_WEBHOOK_URL set). A
  tenant running long polling — local `docker run`, or any future
  ingress-less deployment — never calls this and gets upstream's pairing code.
  That is correct for a hand-run dev container and wrong for production; if
  polling ever ships to customers this must move (a `pre_gateway_dispatch`
  plugin is the natural home).
* REVOKING THE LAST APPROVED USER RE-ARMS TRUST-ON-FIRST-USE. The trigger is
  "the approved store is empty", not "this tenant is new". So
  `hermes pairing revoke <platform> <id>` on the only owner returns the tenant
  to first-contact state, and the next authenticated message binds whoever sent
  it. For the intended flow (rebinding a tenant to a new owner) that is the
  useful behaviour; it is documented here because it is not the behaviour the
  phrase "first contact" implies on its own.
* REVOKE + NONCE: RE-ARMED, BUT ONLY FOR THE OLD LINK. When SQUIRE_BIND_NONCE
  is configured, revoking the sole owner re-arms binding as above — but the
  nonce is still required, control-api has no rotation path (redeploy does not
  re-run set_variables; only delete_tenant clears it), and the holder of the
  original ?start= link is, by construction, the owner who was just revoked.
  So a revoke can only ever re-bind the OLD owner; handing the tenant to a NEW
  owner requires delete + re-provision (which mints a fresh nonce). Strictly
  safer than the legacy open re-arm, where the next authenticated sender —
  whoever they were — won, but it means `pairing revoke` is not a rebinding
  tool on a nonce-provisioned tenant.
"""

from __future__ import annotations

import hmac
import json
import os
import tempfile
import threading
import time
from pathlib import Path

PLATFORM = "telegram"

#: Deep-link bind nonce (interface contract with control-api's provisioning).
#: Pool bots are RECYCLED between tenants and the previous owner keeps the bot
#: chat forever — so "first eligible sender on an empty store" alone would let a
#: recycled bot's previous owner silently bind themselves as owner of the NEXT
#: tenant provisioned on it. When this variable is set, control-api has handed
#: the real user a https://t.me/<bot>?start=<nonce> link, and only the /start
#: that carries that nonce may bind. When it is UNSET we keep legacy
#: trust-on-first-use — deliberately NOT fail-closed, because a tenant image
#: that ships ahead of the control-api contract would otherwise brick
#: provisioning for every new tenant (see maybe_bind_owner).
BIND_NONCE_ENV = "SQUIRE_BIND_NONCE"

# Serialises read -> check-empty -> write in maybe_bind_owner.
#
# The shim is a ThreadingHTTPServer, so concurrent updates run this
# concurrently. Without the lock, the empty-store check and the write are not
# atomic and several callers can clear the check before any of them writes.
# Under an 8-thread burst the STORE still came out single-owner every time --
# every writer was racing to write a set of size one, and the write is a
# whole-file atomic replace -- but the RETURN VALUE lied: more than one caller
# was told "you bound the owner", and each of those then defanged its own
# /start. The durable state was fine; the contract was not.
#
# Per-process, which is sufficient: the shim is the only writer in this
# container. Upstream's PairingStore has the same scope (a threading.RLock).
_BIND_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """Same discipline as the shim's log(): line-oriented stdout, flushed.

    This module runs inside squire-webhook-shim.py, so output lands in the same
    container log stream. NEVER pass the nonce, a /start payload (it may BE the
    nonce), a Telegram user id, or any message text into this.
    """
    print(f"[squire-autopair] {msg}", flush=True)


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


def configured_bind_nonce() -> str | None:
    """The provisioning-time bind nonce, or None when running unauthenticated.

    Read per-call rather than at import so a redeploy that adds/rotates the
    variable takes effect without special handling. An empty or whitespace-only
    value reads as UNSET: that shape only occurs through misconfiguration, and
    treating "" as a real nonce would fail closed (no /start payload can ever
    equal it) and brick that tenant's onboarding.
    """
    value = os.environ.get(BIND_NONCE_ENV)
    if value is None:
        return None
    value = value.strip()
    return value or None


def extract_start_payload(update: dict) -> str | None:
    """The deep-link payload from a "/start <payload>" message, else None.

    Pure text parser — sender/chat filtering stays in extract_sender, which
    always runs first in maybe_bind_owner. Accepts the same command shapes as
    defang_start_command ("/start", "/start@Bot", with payload), returning the
    payload only when one is present.
    """
    if not isinstance(update, dict):
        return None
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return None
    text = msg.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, rest = stripped.partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    if name != "start":
        return None
    rest = rest.strip()
    return rest or None


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
    head = stripped.split(" ", 1)[0]
    name = head[1:].split("@", 1)[0].lower()
    if name != "start":
        return False

    # ALWAYS drop any deep-link payload. An earlier version kept it ("start
    # <payload>") on the theory a future signup flow might correlate on it —
    # but the payload is now the BIND NONCE, and this rewritten text becomes a
    # real user turn that lands in the transcript and in Hindsight's memory.
    # Keeping it would persist a live pairing credential in both, where the
    # tenant's own agent (and anything with memory access) could read it back.
    # The nonce has already done its one job by the time we get here.
    msg["text"] = "start"

    # Telegram entity offsets are absolute indices into the text, so removing
    # characters from the front invalidates every later entity. We only remap
    # them when the edit removed EXACTLY ONE leading character — a bare
    # "/start", where the sole change is the dropped slash (delta == 1). For
    # "/start@Bot" (the @suffix is dropped too), "/start <payload>" (the
    # payload is dropped too, see above), or leading / trailing whitespace,
    # more than the slash moved and the offsets no longer map by a single
    # constant. A WRONG offset highlights the wrong characters, which is worse
    # than none, so in those cases we drop entities entirely.
    #
    # In practice a /start message carries only its bot_command entity, so the
    # remap is defensive; the drop path is the honest fallback for the messy
    # cases rather than a guess at their arithmetic. Malformed entries (non-dict,
    # or non-int offset/length) are dropped for the same reason.
    entities = msg.get("entities")
    if isinstance(entities, list):
        delta = len(text) - len(msg["text"])
        if delta == 1 and text == text.strip():
            shifted = []
            for e in entities:
                if not isinstance(e, dict):
                    continue  # malformed entity dropped
                offset = e.get("offset")
                length = e.get("length")
                if not isinstance(offset, int) or not isinstance(length, int):
                    continue  # malformed entity dropped
                if offset == 0 and e.get("type") == "bot_command":
                    continue  # the command span itself
                moved = dict(e)
                if offset == 0:
                    # This entity covered the slash; it loses that character
                    # rather than moving, and is dropped if that empties it.
                    moved["length"] = length - 1
                    if moved["length"] <= 0:
                        continue
                else:
                    moved["offset"] = offset - 1
                shifted.append(moved)
            if shifted:
                msg["entities"] = shifted
            else:
                msg.pop("entities", None)
        else:
            # More than one leading char removed (@Bot suffix, surrounding
            # whitespace): offsets can't be remapped by a constant, so drop
            # rather than emit wrong ones.
            msg.pop("entities", None)
    return True


def strip_start_payload(update: dict) -> bool:
    """Remove the deep-link payload from a /start, in place, KEEPING the command.

    Returns True if the update was modified.

    The not-bound counterpart to defang_start_command. defang runs only on the
    single update that bound the owner; every other /start passing through the
    shim (the owner re-tapping the ?start= link once bound, SQUIRE_AUTOPAIR
    off, an unreadable store, an unauthenticated delivery) forwards as a
    command — upstream swallows "/start" as a platform ping, which is the
    correct behaviour for an established chat. But the payload on those
    messages may be the LIVE nonce, and a verbatim forward would write the
    pairing credential into the transcript and Hindsight memory. So: the
    command survives, the payload never does.

    "/start <p>" -> "/start"; "/start@Bot <p>" -> "/start@Bot" (minimal edit —
    the @Bot suffix is not secret and dropping it here would change command
    routing semantics that are not ours to change). A payload-less /start is
    left byte-for-byte untouched so the shim's verbatim-forward contract for
    ordinary traffic holds.
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
    head, _, rest = stripped.partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    if name != "start":
        return False
    if not rest.strip():
        return False  # no payload -> no edit

    msg["text"] = head

    # The kept text is the original's leading command token. When the original
    # had no leading whitespace, head is a strict PREFIX of it, so entities
    # wholly inside the prefix (the bot_command itself) keep their offsets and
    # only entities reaching into the deleted tail are dropped. With leading
    # whitespace the offsets no longer line up — drop them all rather than emit
    # wrong ones, the same honesty rule as defang's non-delta-1 cases.
    # Malformed entries are dropped for the same reason as in defang.
    entities = msg.get("entities")
    if isinstance(entities, list):
        kept = []
        if text.lstrip() == text:
            for e in entities:
                if not isinstance(e, dict):
                    continue  # malformed entity dropped
                offset = e.get("offset")
                length = e.get("length")
                if not isinstance(offset, int) or not isinstance(length, int):
                    continue  # malformed entity dropped
                if offset >= 0 and offset + length <= len(head):
                    kept.append(e)
        if kept:
            msg["entities"] = kept
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

        # Cheap unlocked pre-check so the overwhelmingly common case (an
        # established tenant, every message forever after the first) never
        # contends on the lock.
        try:
            if load_approved(path):
                return None
        except (OSError, ValueError, UnicodeDecodeError):
            return None

        sender = extract_sender(update)
        if sender is None:
            return None
        user_id, user_name = sender

        # Deep-link nonce gate — ADDITIVE on top of the existing boundaries
        # (extract_sender's shape allowlist above, and the caller's per-bot
        # secret check). Pool bots are recycled; the previous owner still has
        # the bot chat and can pass every legacy check the moment the next
        # tenant's store is empty. Only the holder of the ?start= link knows
        # the nonce, so when one is configured it is the ONLY way to bind.
        nonce = configured_bind_nonce()
        if nonce is not None:
            payload = extract_start_payload(update)
            # Compare as UTF-8 bytes: hmac.compare_digest raises TypeError on
            # non-ASCII str, and a crafted non-ASCII payload must be an
            # ordinary mismatch, not an exception swallowed by the outer try.
            if payload is None or not hmac.compare_digest(
                payload.encode("utf-8"), nonce.encode("utf-8")
            ):
                # Loud, because on an empty store this is either the real user
                # holding a wrong/mangled link or a recycled bot's previous
                # owner knocking. There is NO rotation path in control-api
                # (redeploy does not re-run set_variables; only delete_tenant
                # clears the nonce), so the operator remedies are: re-serve the
                # SAME link via GET /internal/tenants/{id}, or delete and
                # re-provision the tenant. Never log the payload or the sender
                # id — the payload may be a nonce for some other tenant, and
                # this stream reaches a shared log aggregator.
                _log(
                    "bind nonce configured but first contact did not present "
                    "it: NOT binding (falling back to upstream pairing). "
                    "Expected a /start via the tenant's ?start= deep link."
                )
                return None
            # A correct nonce is single-use in effect, not via a "used" marker:
            # the empty-store precondition below already makes binding
            # at-most-once per store lifetime, and control-api clears/rotates
            # the nonce on tenant delete/recycle. A tenant-side marker would
            # add a second piece of durable state that can disagree with the
            # store (e.g. marker written, bind lost the race) for no gain.
        else:
            # No nonce provisioned: keep legacy trust-on-first-use EXACTLY.
            # Deliberately open rather than fail-closed — if the control-api
            # side of this contract lands out of order, failing closed would
            # brick onboarding for every new tenant. Logged so a fleet audit
            # can find tenants still provisioned without deep-link auth.
            _log(
                f"{BIND_NONCE_ENV} unset: binding first contact in "
                "unauthenticated mode (legacy trust-on-first-use)"
            )

        # Everything that decides ownership happens under the lock: re-read,
        # confirm still empty, write, and verify. Doing the check outside it
        # lets two concurrent first contacts both believe they bound the owner.
        with _BIND_LOCK:
            try:
                approved = load_approved(path)
            except (OSError, ValueError, UnicodeDecodeError):
                # Unreadable or corrupt. Refuse to bind: we cannot prove the
                # store is empty, and overwriting it would revoke the real owner.
                return None
            if approved:
                return None

            _atomic_write_0600(path, {
                user_id: {
                    "user_name": user_name,
                    "approved_at": time.time(),
                    # Provenance, so an operator reading this file later can tell
                    # a trust-on-first-use binding from a deliberate `hermes
                    # pairing approve`. Upstream ignores unknown keys.
                    "approved_by": "squire-autopair-first-contact",
                }
            })

            # Read back before claiming success. The caller uses a truthy return
            # to decide whether to rewrite the user's message, so "I bound the
            # owner" must mean the durable store actually says so and says US.
            # A cross-process writer (the gateway approving a code at the same
            # instant) can still land last; if it did, we did not win and must
            # not report that we did.
            try:
                final = load_approved(path)
            except (OSError, ValueError, UnicodeDecodeError):
                return None
            if list(final) != [user_id]:
                return None

        return user_id
    except Exception:
        return None
