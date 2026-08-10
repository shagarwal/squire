"""Secret generation.

The only cryptographic thing control-api does in v0 is *mint* secrets and forget
them. Nothing here ever reaches the database — with one deliberate exception:
the deep-link bind nonce is stored on the tenant row (see `generate_bind_nonce`
and `models.Tenant.bind_nonce` for why that is safe and necessary).
"""

from __future__ import annotations

import base64
import secrets

#: PRD §4 envelope encryption: one 256-bit data-encryption key per tenant.
DEK_BYTES = 32


def generate_dek() -> str:
    """32 random bytes, base64-encoded, for the tenant's `SQUIRE_DEK` variable.

    Handed straight to Railway as a service variable and then dropped on the floor.
    The control DB gets a `dek_set` boolean and nothing else -- so a control-plane
    compromise yields no tenant key material, and deleting the Railway service is a
    crypto-shred.

    (Gate G2 in Phase 1 replaces Railway-variable delivery with a one-time sealed
    token + KMS decrypt in-tenant. Until then, our Railway API token can read it --
    documented as an accepted alpha risk in implementation-plan.md Task 0.2.)
    """
    return base64.b64encode(secrets.token_bytes(DEK_BYTES)).decode("ascii")


def generate_webhook_secret() -> str:
    """A Telegram `secret_token`.

    Telegram allows 1-256 chars of `A-Z a-z 0-9 _ -`, which is exactly the
    URL-safe base64 alphabet, so `token_urlsafe` is always valid.
    """
    return secrets.token_urlsafe(32)


def generate_bind_nonce() -> str:
    """The tenant's one-time owner-binding nonce (`SQUIRE_BIND_NONCE`).

    Travels twice: to the tenant as a Railway variable, and to the user inside
    the `https://t.me/<bot>?start=<nonce>` deep link. The tenant's autopair
    refuses to bind an owner unless the first `/start` carries this value, which
    is what stops the previous owner of a RECYCLED pool bot from binding
    themselves to the bot's next tenant.

    Telegram start payloads allow up to 64 chars of `A-Z a-z 0-9 _ -` — exactly
    the URL-safe base64 alphabet, so `token_urlsafe(32)` (43 chars) is always a
    valid payload.
    """
    return secrets.token_urlsafe(32)


def generate_tenant_id() -> str:
    """Short, URL-safe, DNS-safe tenant id.

    It becomes part of the Railway service name (`tenant-<id>`) and therefore part
    of the private hostname, so it must be lowercase hex with no separators.
    16 hex chars = 64 bits; collisions are not a practical concern.
    """
    return secrets.token_hex(8)


def generate_job_id() -> str:
    return secrets.token_hex(8)
