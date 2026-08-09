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

u = dm(1, text="/start ref_abc123")
defang_start_command(u)
check("deep-link payload preserved",
      u["message"]["text"] == "start ref_abc123", u["message"]["text"])

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

# Surviving entities must SHIFT LEFT: we removed one character from the front of
# the text and entity offsets are absolute indices. An earlier version of this
# test asserted the UNshifted offsets and so encoded the off-by-one as correct.
u = dm(1, text="/start ref_abc")
u["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 6},
                            {"type": "code", "offset": 7, "length": 7}]
defang_start_command(u)
check("command entity dropped, later entities shift left by one",
      u["message"]["entities"] == [{"type": "code", "offset": 6, "length": 7}],
      u["message"].get("entities"))
# The real assertion behind the arithmetic: the span still covers the same text.
txt = u["message"]["text"]
ent = u["message"]["entities"][0]
check("shifted entity still spans 'ref_abc'",
      txt[ent["offset"]:ent["offset"] + ent["length"]] == "ref_abc",
      repr(txt[ent["offset"]:ent["offset"] + ent["length"]]))

# An entity anchored at 0 covered the slash, so it shrinks rather than moves.
u = dm(1, text="/start")
u["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": 6},
                            {"type": "bold", "offset": 0, "length": 3}]
defang_start_command(u)
check("entity covering the slash shrinks instead of moving",
      u["message"]["entities"] == [{"type": "bold", "offset": 0, "length": 2}],
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

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL AUTOPAIR TESTS PASS")
