#!/usr/bin/env python3
"""Load a BotFather batch into the Squire bot pool.

BotFather allows ~20 bots per Telegram account, so the pool is topped up in manual
batches (PRD §4 "Telegram bot supply"). Paste the tokens BotFather gives you into a
newline-delimited file and run:

    export CONTROL_API_URL=https://control-api.squire.app
    export INTERNAL_API_TOKEN=...
    python infra/load_bots.py bots.txt

control-api validates every token against Telegram `getMe` before storing it, so a
typo can never be handed to a paying tenant. Registration is idempotent by bot id.

The token file is a secret: keep it out of git and shred it after loading.
Exit codes: 0 = all tokens accepted or already known, 1 = at least one rejected.
"""

from __future__ import annotations

import argparse
import sys

from _control_api_client import ControlAPI, ControlAPIError, fail


def read_tokens(path: str) -> list[str]:
    """Parse a newline-delimited token file. Blank lines and `#` comments ignored."""
    tokens: list[str] = []
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            head, sep, tail = line.partition(":")
            if not sep or not head.isdigit() or not tail:
                # Fail the whole file rather than silently loading a partial batch.
                raise ValueError(f"{path}:{lineno}: malformed bot token: {line}")
            tokens.append(line)
    if not tokens:
        raise ValueError(f"{path}: no tokens found")
    return tokens


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load Telegram bot tokens into the pool.")
    parser.add_argument("token_file", help="newline-delimited BotFather tokens")
    parser.add_argument("--control-api-url", default=None, help="overrides $CONTROL_API_URL")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        tokens = read_tokens(args.token_file)
    except (OSError, ValueError) as exc:
        return fail(str(exc))

    try:
        api = ControlAPI(base_url=args.control_api_url)
    except ControlAPIError as exc:
        return fail(str(exc))

    print(f"loading {len(tokens)} token(s) from {args.token_file}...")
    response = api.post("/internal/bots", json={"tokens": tokens})
    if response.status_code >= 400:
        return fail(f"control-api rejected the batch: {response.text[:300]}")

    body = response.json()

    # NOTE: only bot ids and usernames are ever printed -- never token material,
    # which would otherwise end up in shell history and CI logs.
    for bot in body.get("registered", []):
        print(f"  registered  {bot['bot_id']}  @{bot['username']}")
    for bot in body.get("skipped", []):
        print(f"  skipped     {bot['bot_id']}  ({bot['reason']})")
    for bot in body.get("failed", []):
        print(f"  FAILED      {bot.get('bot_id')}  {bot.get('error')}")

    print(
        f"\n{len(body.get('registered', []))} registered, "
        f"{len(body.get('skipped', []))} skipped, "
        f"{len(body.get('failed', []))} failed"
    )

    try:
        stats = api.get("/internal/bots").json()
        print(f"pool: {stats['available']} available / {stats['total']} total")
    except Exception:  # noqa: BLE001 -- stats are informational only
        pass

    return 1 if body.get("failed") else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
