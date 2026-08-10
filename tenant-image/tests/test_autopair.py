#!/usr/bin/env python3
"""Trust-on-first-use owner binding — runs WITHOUT Docker.

This guards the first thirty seconds of every tenant's life. The failure it
exists to prevent already happened once in production: the founder tapped Start
on the first live tenant and got "I don't recognize you" plus a pairing code
instead of the concierge greeting.

Two properties carry all the weight, and they pull in opposite directions:

  1. The FIRST private message on an empty store must bind that sender as owner,
     so the greeting flows from that same message.
  2. Nothing else may EVER bind. Once an owner exists, every other unknown user
     must fall through to upstream's pairing gate — that gate is the per-tenant
     half of the "one trial per Telegram user ID" abuse anchor (prd.md §4) and
     the only thing stopping a stranger reaching someone else's agent.

So most of the assertions below are negative: they prove the door closes.

Usage: python3 tenant-image/tests/test_autopair.py
Exit:  0 all assertions pass · 1 otherwise
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import sys
import tempfile

IMAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(IMAGE_ROOT / "bin"))

from squire_autopair import (  # noqa: E402
    approved_path,
    extract_sender,
    maybe_bind_owner,
    pairing_dir,
)

failures: list[str] = []

# Sections 1-10 exercise the legacy (nonce-less) behaviour, so they must run in
# unauthenticated mode regardless of what the invoking shell has exported.
# Section 11 sets/clears the variable around each of its own assertions.
os.environ.pop("SQUIRE_BIND_NONCE", None)


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def dm(user_id: int, name: str = "alice", is_bot: bool = False, text: str = "/start") -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": user_id, "is_bot": is_bot, "first_name": name},
            "chat": {"id": user_id, "type": "private"},
            "text": text,
        },
    }


def group_msg(user_id: int) -> dict:
    u = dm(user_id)
    u["message"]["chat"] = {"id": -100123, "type": "supergroup"}
    return u


def fresh_home() -> str:
    return tempfile.mkdtemp()


def read_store(home: str) -> dict:
    p = approved_path(home)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
print("== 1. first contact on an empty store binds the owner ==")
home = fresh_home()
bound = maybe_bind_owner(home, dm(111))
check("returns the bound user id", bound == "111", f"got {bound!r}")

store = read_store(home)
check("store has exactly one approved user", list(store) == ["111"], f"got {list(store)}")
check("entry carries user_name", store.get("111", {}).get("user_name") == "alice")
check("entry carries approved_at", isinstance(store.get("111", {}).get("approved_at"), float))
check("entry records provenance",
      store.get("111", {}).get("approved_by") == "squire-autopair-first-contact")

# The gateway reads this exact path; if we write elsewhere the user still gets a
# pairing code and nothing explains why.
check("written to <home>/platforms/pairing/telegram-approved.json",
      approved_path(home) == pathlib.Path(home) / "platforms" / "pairing" / "telegram-approved.json",
      str(approved_path(home)))

mode = stat.S_IMODE(approved_path(home).stat().st_mode)
check("store is 0600", mode == 0o600, oct(mode))

# ---------------------------------------------------------------------------
print("== 2. a second, different user is NOT auto-approved ==")
second = maybe_bind_owner(home, dm(222, "mallory"))
check("second user returns None", second is None, f"got {second!r}")
store = read_store(home)
check("store still has only the owner", list(store) == ["111"], f"got {list(store)}")
check("intruder absent from store", "222" not in store)

# ---------------------------------------------------------------------------
print("== 3. restart does not re-open the gate ==")
# The store lives on the mounted volume, so a redeploy sees it already populated.
# Simulate by simply calling again — the module holds no state of its own.
again = maybe_bind_owner(home, dm(333, "eve"))
check("post-restart stranger returns None", again is None, f"got {again!r}")
check("owner unchanged after restart", list(read_store(home)) == ["111"])
# And the owner messaging again is a no-op rather than a rewrite.
owner_again = maybe_bind_owner(home, dm(111))
check("owner re-messaging is a no-op", owner_again is None, f"got {owner_again!r}")
check("approved_at not rewritten",
      read_store(home)["111"]["approved_at"] == store["111"]["approved_at"])

# ---------------------------------------------------------------------------
print("== 4. only real, private, human first contact may bind ==")
for label, update in (
    ("group message", group_msg(444)),
    ("bot sender", dm(555, is_bot=True)),
    ("channel post", {"update_id": 2, "channel_post": {"chat": {"type": "channel"}}}),
    ("callback query", {"update_id": 3, "callback_query": {"from": {"id": 666, "is_bot": False}}}),
    ("my_chat_member", {"update_id": 4, "my_chat_member": {"from": {"id": 777, "is_bot": False}}}),
    ("no from", {"update_id": 5, "message": {"chat": {"type": "private"}}}),
    ("non-int id", {"update_id": 6, "message": {"from": {"id": "x", "is_bot": False},
                                                "chat": {"type": "private"}}}),
    ("empty dict", {}),
    ("not a dict", []),
):
    h = fresh_home()
    got = maybe_bind_owner(h, update)
    check(f"{label} does not bind", got is None, f"got {got!r}")
    check(f"{label} writes no store", not approved_path(h).exists())

# edited_message is accepted — it is still the owner typing in their own DM.
h = fresh_home()
edited = {"update_id": 7, "edited_message": dm(888)["message"]}
check("edited_message binds", maybe_bind_owner(h, edited) == "888")

# ---------------------------------------------------------------------------
print("== 5. a corrupt or unreadable store must never be overwritten ==")
h = fresh_home()
p = approved_path(h)
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("{ this is not json")
got = maybe_bind_owner(h, dm(999))
check("corrupt store refuses to bind", got is None, f"got {got!r}")
check("corrupt store left untouched", p.read_text() == "{ this is not json")

# A store that is valid JSON but not a dict is treated as empty-but-unsafe.
h2 = fresh_home()
p2 = approved_path(h2)
p2.parent.mkdir(parents=True, exist_ok=True)
p2.write_text('["not", "a", "dict"]')
got2 = maybe_bind_owner(h2, dm(1000))
check("list-shaped store binds without clobbering others",
      got2 == "1000" and list(read_store(h2)) == ["1000"], f"got {got2!r}")

# ---------------------------------------------------------------------------
print("== 6. legacy pairing directory is honoured ==")
# get_hermes_dir prefers ~/.hermes/pairing when it exists AND is non-empty.
# Writing to the modern path while the gateway reads the legacy one is a silent
# failure — the user just keeps getting pairing codes.
h = fresh_home()
legacy = pathlib.Path(h) / "pairing"
legacy.mkdir(parents=True)
(legacy / "telegram-pending.json").write_text("{}")
check("non-empty legacy dir wins", pairing_dir(h) == legacy, str(pairing_dir(h)))

h = fresh_home()
(pathlib.Path(h) / "pairing").mkdir(parents=True)  # empty
check("empty legacy dir does not win",
      pairing_dir(h) == pathlib.Path(h) / "platforms" / "pairing", str(pairing_dir(h)))

# ---------------------------------------------------------------------------
print("== 7. extract_sender filtering (unit) ==")
check("private human message extracts", extract_sender(dm(1)) == ("1", "alice"))
check("username preferred over first_name",
      extract_sender({"message": {"from": {"id": 2, "is_bot": False, "username": "u",
                                           "first_name": "f"},
                                  "chat": {"type": "private"}}}) == ("2", "u"))
check("missing name yields empty string",
      extract_sender({"message": {"from": {"id": 3, "is_bot": False},
                                  "chat": {"type": "private"}}}) == ("3", ""))

# ---------------------------------------------------------------------------
print("== 8. an unwritable store degrades, it does not crash ==")
h = fresh_home()
d = pairing_dir(h)
d.mkdir(parents=True, exist_ok=True)
os.chmod(d, 0o500)  # read+execute, no write
try:
    got = maybe_bind_owner(h, dm(1234))
    check("unwritable dir returns None instead of raising", got is None, f"got {got!r}")
finally:
    os.chmod(d, 0o700)

print("== 9. /start is defanged so the greeting can fire ==")
# Authorization alone leaves the deep-link tap silent: upstream registers "start"
# as a command whose handler returns "" (run.py:14961) and empty responses are
# never sent. These assertions are the difference between "the owner is paired"
# and "the owner is paired AND heard something back".
from squire_autopair import defang_start_command  # noqa: E402

u = dm(1, text="/start")
check("plain /start becomes text", defang_start_command(u) is True)
check("text is 'start'", u["message"]["text"] == "start", u["message"]["text"])
check("no longer a slash command", not u["message"]["text"].startswith("/"))

u = dm(1, text="/start@SquireBot")
defang_start_command(u)
check("/start@Bot handled", u["message"]["text"] == "start", u["message"]["text"])

# SECURITY: the deep-link payload IS the bind nonce when ?start= auth is on, and
# the defanged text becomes a real user turn — transcript AND Hindsight memory.
# Keeping the payload would persist a live pairing credential in both. The click
# carried nothing the agent needs to see, so the payload is always dropped.
u = dm(1, text="/start ref_abc123")
defang_start_command(u)
check("deep-link payload stripped (nonce must not enter the transcript)",
      u["message"]["text"] == "start", u["message"]["text"])

# The bot_command entity must go with the slash, or adapters still see a command.
u = dm(1, text="/start")
u["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 6}]
defang_start_command(u)
check("bot_command entity removed", "entities" not in u["message"], u["message"].get("entities"))

u = dm(1, text="/start")
u["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 6},
                            {"type": "bold", "offset": 0, "length": 1}]
defang_start_command(u)
check("entity covering only the slash is dropped",
      "entities" not in u["message"], u["message"].get("entities"))

# With the payload stripped, "/start ref_abc" -> "start" deletes far more than
# the single leading slash (delta != 1), so the remap arithmetic cannot apply and
# entities are dropped rather than mis-shifted — same honest fallback as the
# "/start@Bot" case below.
u = dm(1, text="/start ref_abc")
u["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 6},
                            {"type": "code", "offset": 7, "length": 7}]
defang_start_command(u)
check("payload deletion drops entities rather than mis-shift",
      "entities" not in u["message"], u["message"].get("entities"))
check("payload deletion still yields bare 'start'",
      u["message"]["text"] == "start", u["message"]["text"])

# Surviving entities must SHIFT LEFT on the one edit that still qualifies for the
# remap: a bare "/start", where exactly the slash (delta == 1) was removed. An
# earlier version of this test asserted the UNshifted offsets and so encoded the
# off-by-one as correct.
u = dm(1, text="/start")
u["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 6},
                            {"type": "italic", "offset": 1, "length": 5}]
defang_start_command(u)
check("bare /start: later entities shift left by one",
      u["message"]["entities"] == [{"type": "italic", "offset": 0, "length": 5}],
      u["message"].get("entities"))
# The real assertion behind the arithmetic: the span still covers the same text.
txt = u["message"]["text"]
ent = u["message"]["entities"][0]
check("shifted entity still spans 'start'",
      txt[ent["offset"]:ent["offset"] + ent["length"]] == "start",
      repr(txt[ent["offset"]:ent["offset"] + ent["length"]]))

# An entity anchored at 0 covered the slash, so it shrinks rather than moves.
u = dm(1, text="/start")
u["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 6},
                            {"type": "bold", "offset": 0, "length": 3}]
defang_start_command(u)
check("entity covering the slash shrinks instead of moving",
      u["message"]["entities"] == [{"type": "bold", "offset": 0, "length": 2}],
      u["message"].get("entities"))

# /start@Bot removes MORE than one leading char (the @suffix is dropped too), so
# a single-constant shift would be wrong -> entities are dropped, not guessed.
u = dm(1, text="/start@SquireBot ref")
u["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 16},
                            {"type": "code", "offset": 17, "length": 3}]
defang_start_command(u)
check("/start@Bot drops entities rather than mis-shift",
      "entities" not in u["message"], u["message"].get("entities"))
check("/start@Bot text still defanged and payload-stripped",
      u["message"]["text"] == "start", u["message"]["text"])

# Malformed entities are dropped (documented), not passed through.
u = dm(1, text="/start")
u["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 6},
                            {"type": "code", "offset": "x", "length": 3},
                            "not-a-dict"]
defang_start_command(u)
check("malformed entities dropped", "entities" not in u["message"],
      u["message"].get("entities"))

# Everything else must be left strictly alone — this must not become a general
# command rewriter.
for label, text in (("ordinary text", "hello"), ("another command", "/help"),
                    ("start mid-sentence", "please /start it")):
    u = dm(1, text=text)
    check(f"{label} untouched", defang_start_command(u) is False and u["message"]["text"] == text,
          u["message"]["text"])

check("non-text message untouched",
      defang_start_command({"message": {"chat": {"type": "private"}}}) is False)
check("non-dict untouched", defang_start_command([]) is False)

print("== 10. concurrent first contacts: exactly ONE caller may be told it won ==")
# The shim is a ThreadingHTTPServer, so this genuinely runs concurrently. The
# store came out single-owner even before the lock — every writer was racing to
# write a set of size one — but the RETURN VALUE lied, and the caller uses it to
# decide whether to rewrite the user's message. N-1 users would have had their
# /start defanged and then been handed a pairing code.
import threading  # noqa: E402

for attempt in range(20):
    h = fresh_home()
    barrier = threading.Barrier(8)
    winners: list[str] = []
    lock = threading.Lock()

    def racer(uid: int) -> None:
        barrier.wait()
        got = maybe_bind_owner(h, dm(uid, f"u{uid}"))
        if got:
            with lock:
                winners.append(got)

    threads = [threading.Thread(target=racer, args=(2000 + i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    store = read_store(h)
    if len(winners) != 1 or list(store) != winners:
        check(f"exactly one winner (attempt {attempt})", False,
              f"winners={winners} store={list(store)}")
        break
else:
    check("exactly one winner across 20 x 8-thread races", True)
    check("store agrees with the reported winner", True)

print("== 11. deep-link bind nonce gates binding when configured ==")
# Pool bots are RECYCLED between tenants, and the previous owner keeps the bot
# chat forever. With trust-on-first-use alone, that previous owner can message
# the recycled bot the moment the next tenant's store is empty and silently
# become ITS owner. The nonce closes that: control-api provisions the tenant
# with SQUIRE_BIND_NONCE and hands the real user a t.me/<bot>?start=<nonce>
# link, so only the person holding the link can bind.
import contextlib  # noqa: E402
import hmac as _hmac  # noqa: E402
import io  # noqa: E402

import squire_autopair as _autopair  # noqa: E402
from squire_autopair import (  # noqa: E402
    BIND_NONCE_ENV,
    configured_bind_nonce,
    extract_start_payload,
)

NONCE = "tok_urlsafe_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"  # stand-in for token_urlsafe(32)

check("env var name is the interface contract", BIND_NONCE_ENV == "SQUIRE_BIND_NONCE")

# --- extract_start_payload is a pure parser -------------------------------
check("payload extracted from /start <p>",
      extract_start_payload(dm(1, text="/start abc")) == "abc")
check("payload extracted from /start@Bot <p>",
      extract_start_payload(dm(1, text="/start@SquireBot abc")) == "abc")
check("bare /start has no payload",
      extract_start_payload(dm(1, text="/start")) is None)
check("ordinary text has no payload",
      extract_start_payload(dm(1, text="hello")) is None)
check("other commands have no payload",
      extract_start_payload(dm(1, text="/help abc")) is None)
check("edited_message payload extracted",
      extract_start_payload({"edited_message": dm(1, text="/start abc")["message"]}) == "abc")
check("non-dict update has no payload", extract_start_payload([]) is None)

# --- configured_bind_nonce reads the environment --------------------------
os.environ.pop(BIND_NONCE_ENV, None)
check("unset env reads as no nonce", configured_bind_nonce() is None)
# An empty value is treated as UNSET, not as a nonce of "": failing closed on a
# misconfigured-empty variable would brick provisioning for that tenant.
os.environ[BIND_NONCE_ENV] = ""
check("empty env reads as no nonce", configured_bind_nonce() is None)
os.environ[BIND_NONCE_ENV] = NONCE
check("set env reads back the nonce", configured_bind_nonce() == NONCE)

# --- correct payload binds -------------------------------------------------
os.environ[BIND_NONCE_ENV] = NONCE
h = fresh_home()
got = maybe_bind_owner(h, dm(111, text=f"/start {NONCE}"))
check("correct nonce payload binds", got == "111", f"got {got!r}")
check("store written for correct nonce", list(read_store(h)) == ["111"])

# @Bot deep links produce "/start@Bot <payload>" in some clients; still valid.
h = fresh_home()
got = maybe_bind_owner(h, dm(112, text=f"/start@SquireBot {NONCE}"))
check("correct nonce via /start@Bot binds", got == "112", f"got {got!r}")

# --- everything else must NOT bind, and must say so loudly ----------------
for label, update in (
    ("wrong payload", dm(222, text="/start wrong-guess")),
    ("bare /start", dm(333, text="/start")),
    ("ordinary text", dm(444, text="hello")),
):
    h = fresh_home()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = maybe_bind_owner(h, update)
    check(f"{label} does not bind when a nonce is configured", got is None, f"got {got!r}")
    check(f"{label} writes no store", not approved_path(h).exists())
    out = buf.getvalue()
    check(f"{label} logs the refusal loudly", "nonce" in out.lower(), repr(out))
    check(f"{label} log never contains the nonce itself", NONCE not in out, repr(out))

# The sender-shape gate still runs FIRST: a group message carrying the correct
# nonce must not bind — otherwise pasting the link into a group hands ownership
# to whoever relays it.
h = fresh_home()
g = group_msg(555)
g["message"]["text"] = f"/start {NONCE}"
check("group message with correct nonce does not bind",
      maybe_bind_owner(h, g) is None)

# --- a wrong guess must not consume the one-shot ---------------------------
# The empty-store precondition is what makes binding at-most-once; a failed
# nonce attempt leaves the store empty, so the REAL owner's link still works.
h = fresh_home()
with contextlib.redirect_stdout(io.StringIO()):
    maybe_bind_owner(h, dm(666, text="/start wrong"))
got = maybe_bind_owner(h, dm(777, text=f"/start {NONCE}"))
check("owner still binds after an attacker's failed guess", got == "777", f"got {got!r}")

# --- the comparison is timing-safe ----------------------------------------
# Behavioural timing assertions are flaky by nature, so assert the mechanism:
# the accept decision must flow through hmac.compare_digest.
calls: list[tuple] = []
_real_compare = _hmac.compare_digest

def _spy_compare(a, b):
    calls.append((a, b))
    return _real_compare(a, b)

_autopair.hmac.compare_digest = _spy_compare
try:
    h = fresh_home()
    check("bind still works through compare_digest",
          maybe_bind_owner(h, dm(888, text=f"/start {NONCE}")) == "888")
    check("payload comparison goes through hmac.compare_digest", len(calls) > 0)
finally:
    _autopair.hmac.compare_digest = _real_compare

# --- unset nonce keeps today's behaviour exactly (open mode) ---------------
# Deliberately NOT fail-closed: if the control-api side of this contract lands
# out of order (old control-api, new tenant image), failing closed would brick
# provisioning for every new tenant. Unset means legacy trust-on-first-use,
# announced loudly so a fleet audit can find unauthenticated tenants.
del os.environ[BIND_NONCE_ENV]
h = fresh_home()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got = maybe_bind_owner(h, dm(999))
check("unset nonce still binds first contact (legacy)", got == "999", f"got {got!r}")
check("unset nonce logs unauthenticated mode",
      "unauthenticated mode" in buf.getvalue(), repr(buf.getvalue()))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL AUTOPAIR TESTS PASS")
