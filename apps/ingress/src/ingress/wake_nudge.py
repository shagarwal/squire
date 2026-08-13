"""
Typing-on-wake nudge.

When a forward fails and the update lands in the retry buffer, the tenant is
almost always asleep and the user is about to stare at a silent chat for the
~15-20s wake. Ingress cannot send the "typing..." chat action itself -- bot
tokens deliberately never live here (Gate G2) -- so instead it fire-and-forgets
a tiny POST to control-api's /internal/wake-typing endpoint, which owns the
token and keeps sendChatAction(chat_id, "typing") alive for the wake window.

PRIVACY: extracting chat_id requires parsing the update body, which app.py
otherwise treats as opaque bytes. The parse happens IN MEMORY ONLY and the
result flows exclusively into the outbound nudge request. Nothing parsed here
may ever reach log_event() -- its closed parameter list (see ingress/logging.py)
has no chat/body field, so a chat id structurally cannot be logged, and
tests/test_wake_nudge.py pins that the schema stays that way.

RELIABILITY: the nudge is best-effort UX, never a delivery precondition. It is
scheduled as a detached asyncio task with a sub-second timeout; every failure
is swallowed and counted (log_event("wake_nudge_failed", ...)) so the buffer
path -- the thing that actually delivers the message -- can never be delayed
or broken by it.
"""
from __future__ import annotations

import asyncio
import json
from typing import Dict, Optional, Set, Tuple

import httpx

from .config import Settings
from .logging import log_event


def extract_chat_id(body: bytes) -> Optional[int]:
    """Best-effort chat-id extraction from a raw Telegram update body.

    Handles the three update kinds ingress subscribes to (see control-api's
    set_webhook allowed_updates): message, edited_message, and callback_query
    (whose chat lives one level deeper, on the message the button was under).
    Returns None -- never raises -- for anything unparseable or chat-less:
    the caller simply skips the nudge and buffering proceeds untouched.
    """
    try:
        update = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(update, dict):
        return None

    candidates = [update.get("message"), update.get("edited_message")]
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        candidates.append(callback.get("message"))

    for message in candidates:
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict):
            continue
        chat_id = chat.get("id")
        # bool is an int subclass in Python; a `true` here is junk, not a chat.
        if isinstance(chat_id, int) and not isinstance(chat_id, bool):
            return chat_id
    return None


class WakeNudger:
    """Fires at most one wake-typing nudge per (bot_id, chat_id) per episode.

    An "episode" is one contiguous stretch of a tenant's buffer being
    non-empty: it starts when the buffer goes 0 -> 1 (the caller passes the
    pre-enqueue depth) and everything until the buffer drains counts as the
    same wake. control-api keeps the typing indicator alive for the whole
    window from a single nudge, so re-nudging on every buffered update would
    just be noise (and extra Telegram calls).
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        # tenant_id -> set of (bot_id, chat_id) already nudged this episode.
        # Keyed by tenant_id (NOT the attacker-controlled bot_id off the URL):
        # entries only exist for bot_ids control-api resolved to a real tenant,
        # so growth is bounded by the actual fleet size, and each set is
        # replaced wholesale when a new episode starts.
        self._nudged: Dict[str, Set[Tuple[int, int]]] = {}
        # Strong references to in-flight fire-and-forget tasks (asyncio only
        # keeps weak ones; without this a nudge task could be GC'd mid-flight).
        self._tasks: Set[asyncio.Task] = set()

    def on_buffered(self, *, bot_id: int, tenant_id: str, body: bytes,
                    queue_depth_before: int) -> None:
        """Called by app.py right after buffer.enqueue(). Must never raise."""
        try:
            if queue_depth_before == 0:
                # Buffer was empty -> this enqueue starts a NEW wake episode;
                # forget who we nudged last time so they get typing again.
                self._nudged.pop(tenant_id, None)

            chat_id = extract_chat_id(body)
            if chat_id is None:
                # No chat to notify -- fine, buffering already happened.
                log_event("wake_nudge_skipped", bot_id=bot_id, tenant_id=tenant_id)
                return

            pairs = self._nudged.setdefault(tenant_id, set())
            if (bot_id, chat_id) in pairs:
                return  # already nudged this chat this episode
            if len(pairs) >= self._settings.wake_nudge_max_chats_per_episode:
                return  # blunt memory/abuse cap; buffering is unaffected
            pairs.add((bot_id, chat_id))

            task = asyncio.create_task(self._send(bot_id, tenant_id, chat_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:
            # Belt and braces: the buffer path must survive any bug in here.
            log_event("wake_nudge_failed", bot_id=bot_id, tenant_id=tenant_id)

    async def _send(self, bot_id: int, tenant_id: str, chat_id: int) -> None:
        """POST the nudge to control-api. Swallow-and-count every failure."""
        url = f"{self._settings.control_api_url}/internal/wake-typing"
        try:
            resp = await self._client.post(
                url,
                json={"bot_id": bot_id, "chat_id": chat_id},
                headers={"Authorization": f"Bearer {self._settings.internal_api_token}"},
                # Sub-second budget: if control-api is slow, the typing nudge
                # has already missed its point -- give up rather than pile up.
                timeout=self._settings.wake_nudge_timeout,
            )
        except Exception:
            # Note: no chat_id in the log call -- log_event couldn't carry it anyway.
            log_event("wake_nudge_failed", bot_id=bot_id, tenant_id=tenant_id)
            return
        if 200 <= resp.status_code < 300:
            log_event("wake_nudge_sent", bot_id=bot_id, tenant_id=tenant_id,
                      status_code=resp.status_code)
        else:
            log_event("wake_nudge_failed", bot_id=bot_id, tenant_id=tenant_id,
                      status_code=resp.status_code)

    async def wait_idle(self) -> None:
        """Await all in-flight nudge tasks -- for deterministic tests only."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
