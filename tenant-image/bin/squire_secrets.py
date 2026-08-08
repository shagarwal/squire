#!/opt/squire/venv/bin/python
"""DEK envelope crypto for the Squire tenant image.

Threat model this actually addresses (PRD §4, "Secrets: envelope encryption +
crypto-shredding"): the mounted volume is the durable, backed-up, potentially
platform-readable artifact. Nothing credential-bearing may be written to it in
plaintext. The per-tenant DEK arrives once per boot in the environment
(SQUIRE_DEK), never lands on disk, and deleting the wrapped copy held by the
control plane crypto-shreds every backup at once.

What it does NOT address, stated plainly so nobody over-claims it later: a root
process on the host can read the DEK out of the container's environment or its
memory. That is the PRD's published honesty boundary, not a gap in this file.

Layout on disk (the encrypted side, on the volume):

    <state>/secrets/<name>.enc     "SQENC1\\0" | 12-byte nonce | AES-256-GCM ct

`name` (e.g. ".env") is bound in as the AEAD associated data, so a `.env.enc`
cannot be renamed over `auth.json.enc` to make the agent load the wrong file.

Usage
-----
    squire_secrets.py seal   <plaintext-path> <enc-path> <aad-name>
    squire_secrets.py unseal <enc-path> <plaintext-path> <aad-name>
    squire_secrets.py check                 # DEK present and well-formed?
    squire_secrets.py selftest              # round-trip, no filesystem state

Exit codes: 0 ok · 2 bad/missing DEK · 3 corrupt or wrong-key ciphertext ·
1 anything else.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"SQENC1\0"
NONCE_LEN = 12  # AES-GCM standard; also what AESGCM.encrypt expects
KEY_LEN = 32  # AES-256

EXIT_BAD_DEK = 2
EXIT_BAD_CIPHERTEXT = 3


class DekError(Exception):
    """SQUIRE_DEK is missing, unparseable, or the wrong length."""


def load_dek(env: dict[str, str] | None = None) -> bytes:
    """Read and validate the 32-byte DEK from SQUIRE_DEK (base64).

    Accepts both standard and URL-safe base64, with or without padding —
    control-api's language/runtime should not be able to break boot with a
    padding preference.
    """
    env = os.environ if env is None else env
    raw = (env.get("SQUIRE_DEK") or "").strip()
    if not raw:
        raise DekError("SQUIRE_DEK is not set")

    padded = raw + "=" * (-len(raw) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            key = decoder(padded)
        except Exception:
            continue
        if len(key) == KEY_LEN:
            return key

    raise DekError(
        f"SQUIRE_DEK must decode to exactly {KEY_LEN} bytes of base64 "
        f"(got {len(raw)} base64 chars)"
    )


def seal_bytes(key: bytes, plaintext: bytes, aad: str) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad.encode("utf-8"))
    return MAGIC + nonce + ct


def unseal_bytes(key: bytes, blob: bytes, aad: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("not a Squire sealed file (bad magic)")
    body = blob[len(MAGIC):]
    if len(body) < NONCE_LEN + 16:  # nonce + GCM tag
        raise ValueError("sealed file is truncated")
    nonce, ct = body[:NONCE_LEN], body[NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, aad.encode("utf-8"))


def _write_private(path: str, data: bytes) -> None:
    """Write `data` to `path` atomically, mode 0600, never leaving a partial file.

    The temp file is created in the destination directory so os.replace() is a
    same-filesystem rename, and it is opened 0600 from the start — there is no
    window where the plaintext is world-readable.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".squire-tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Never leave a partial (possibly plaintext) file behind.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_seal(argv: list[str]) -> int:
    src, dst, aad = argv
    key = load_dek()
    with open(src, "rb") as fh:
        plaintext = fh.read()
    _write_private(dst, seal_bytes(key, plaintext, aad))
    return 0


def cmd_unseal(argv: list[str]) -> int:
    src, dst, aad = argv
    key = load_dek()
    with open(src, "rb") as fh:
        blob = fh.read()
    try:
        plaintext = unseal_bytes(key, blob, aad)
    except (InvalidTag, ValueError) as exc:
        # Do not fall back to "start empty" here. A tenant whose secrets cannot
        # be decrypted must fail loudly: silently booting with no credentials
        # would look like a working agent that has forgotten who the user is.
        print(
            f"squire_secrets: cannot decrypt {src} ({exc}). "
            "Wrong SQUIRE_DEK for this volume, or the file is corrupt.",
            file=sys.stderr,
        )
        return EXIT_BAD_CIPHERTEXT
    _write_private(dst, plaintext)
    return 0


def cmd_check(_argv: list[str]) -> int:
    load_dek()
    return 0


def cmd_selftest(_argv: list[str]) -> int:
    """Round-trip against a throwaway key. Used by CI; touches no tenant state."""
    key = os.urandom(KEY_LEN)
    payload = b"TELEGRAM_WEBHOOK_SECRET=deadbeef\nANTHROPIC_API_KEY=sk-test\n"

    blob = seal_bytes(key, payload, ".env")
    assert unseal_bytes(key, blob, ".env") == payload, "round-trip mismatch"

    # Wrong AAD must fail: this is the rename-attack guard.
    try:
        unseal_bytes(key, blob, "auth.json")
    except InvalidTag:
        pass
    else:  # pragma: no cover - only reached if AAD binding regressed
        raise AssertionError("AAD binding is not enforced")

    # Wrong key must fail.
    try:
        unseal_bytes(os.urandom(KEY_LEN), blob, ".env")
    except InvalidTag:
        pass
    else:  # pragma: no cover
        raise AssertionError("wrong key decrypted successfully")

    # Ciphertext must not contain the plaintext anywhere.
    assert b"sk-test" not in blob, "plaintext leaked into ciphertext"

    print("squire_secrets: selftest ok")
    return 0


COMMANDS = {
    "seal": (cmd_seal, 3),
    "unseal": (cmd_unseal, 3),
    "check": (cmd_check, 0),
    "selftest": (cmd_selftest, 0),
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 1
    handler, arity = COMMANDS[argv[1]]
    args = argv[2:]
    if len(args) != arity:
        print(f"squire_secrets: {argv[1]} takes {arity} argument(s)", file=sys.stderr)
        return 1
    try:
        return handler(args)
    except DekError as exc:
        print(f"squire_secrets: {exc}", file=sys.stderr)
        return EXIT_BAD_DEK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
