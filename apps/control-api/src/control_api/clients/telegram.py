"""Telegram Bot API client.

Four calls: validate a pool token (`getMe`), point a bot at ingress
(`setWebhook`), unhook it when a tenant is deleted (`deleteWebhook`), and keep
a chat looking alive while its sleeping tenant wakes (`sendChatAction`, used by
the /internal/wake-typing nudge).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from control_api.config import get_settings

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


def bot_id_from_token(token: str) -> int:
    """`"123456:ABC..."` -> `123456`.

    This is the interface contract shared with ingress: the numeric prefix of the
    bot token IS the `bot_id` in `/telegram/<bot_id>` and in the by-bot lookup.
    """
    raw = (token or "").strip()
    head, sep, tail = raw.partition(":")
    if not sep or not head.isdigit() or not tail:
        raise ValueError(f"malformed Telegram bot token: {raw[:12]!r}...")
    return int(head)


class TelegramClient:
    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.telegram_api_base_url).rstrip("/")
        self._client = client
        self._timeout = settings.telegram_timeout_seconds

    def _call(self, token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/bot{token}/{method}"
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            response = client.post(url, json=payload or {})
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method} request failed: {exc}") from exc
        finally:
            if self._client is None:
                client.close()

        try:
            body = response.json()
        except ValueError:
            raise TelegramError(f"{method} returned non-JSON (HTTP {response.status_code})")

        if not body.get("ok"):
            # Telegram puts the human-readable reason in `description`.
            raise TelegramError(
                f"{method} failed: {body.get('description', response.text[:200])}"
            )
        return body.get("result")

    def get_me(self, token: str) -> dict[str, Any]:
        """Validate a pool token and learn the bot's username (for the t.me link)."""
        return self._call(token, "getMe")

    def set_webhook(self, token: str, url: str, secret_token: str) -> bool:
        """Point the bot at ingress.

        `drop_pending_updates` matters: a recycled pool bot must not deliver a
        previous tenant's queued messages into a new tenant.
        """
        return bool(
            self._call(
                token,
                "setWebhook",
                {
                    "url": url,
                    "secret_token": secret_token,
                    "drop_pending_updates": True,
                    # Phase 0 only handles messages/callbacks; narrowing this cuts
                    # useless webhook traffic to ingress.
                    "allowed_updates": ["message", "edited_message", "callback_query"],
                },
            )
        )

    def delete_webhook(self, token: str) -> bool:
        """Unhook a bot so it can safely go back in the pool."""
        return bool(self._call(token, "deleteWebhook", {"drop_pending_updates": True}))

    def send_chat_action(self, token: str, chat_id: int, action: str = "typing") -> bool:
        """Show a chat action ("typing...") in the given chat.

        Used by the typing-on-wake nudge (/internal/wake-typing): a sleeping
        tenant takes ~15-20s to wake, and this is what keeps the chat looking
        alive in the meantime. Telegram displays the action for ~5s per call,
        so the caller repeats it on an interval.
        """
        return bool(self._call(token, "sendChatAction", {"chat_id": chat_id, "action": action}))
