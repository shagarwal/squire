#!/opt/squire/venv/bin/python
"""1C credential connect — nonce store, connect page, credential storage, pipeline.

Imported by squire-webhook-shim.py (serves GET/POST /connect/<nonce>) and by
the squire-llm-connect CLI (mints links, runs the device flow's storage side).

NONCE DISCIPLINE (mirrors squire_autopair's bind-nonce rules)
-------------------------------------------------------------
* 32 random bytes, urlsafe — unguessable, single-use, 15-minute expiry.
* Constant-time comparison (hmac.compare_digest on UTF-8 bytes) against every
  stored candidate, so a lookup's timing never narrows the search space.
* One FILE per nonce under $SQUIRE_STATE_DIR/connect-nonces/, written 0600 via
  the same same-directory-tempfile + fsync + os.replace discipline as
  squire_autopair._atomic_write_0600. Files, not one shared JSON store, because
  the minter (CLI process) and the consumer (shim process) are different
  processes: consumption is an atomic os.rename to a .used name, which cannot
  lose a mark to a concurrent mint the way a read-modify-write of a shared
  file could.
* NEVER log a nonce, a credential, or any part of either.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
import time

NONCE_TTL_SECONDS = 900  # 15 minutes, matching the design spec
NONCE_BYTES = 32


def _default_state_dir() -> str:
    state_dir = os.environ.get("SQUIRE_STATE_DIR")
    if not state_dir:
        home = os.environ.get("HERMES_HOME") or "/opt/data"
        state_dir = os.path.join(home, ".squire")
    return state_dir


def _nonce_dir(state_dir: str | None) -> str:
    return os.path.join(state_dir or _default_state_dir(), "connect-nonces")


def _atomic_write_0600(path: str, data: bytes) -> None:
    """Same discipline as squire_autopair: same-dir temp, 0600 from creation,
    fsync, atomic replace, no partial file left on any failure path."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".connect-tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mint_nonce(state_dir: str | None = None) -> str:
    """Mint a fresh single-use connect nonce and persist it. Returns the nonce.

    Also prunes: expired unused nonces are deleted (they can never be found
    again), and .used audit markers older than a day are cleaned up so the
    directory cannot grow without bound.
    """
    directory = _nonce_dir(state_dir)
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass

    now = time.time()
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            if name.endswith(".used"):
                if now - os.path.getmtime(path) > 86400:
                    os.unlink(path)
            elif name.endswith(".json"):
                created = json.loads(open(path, "rb").read()).get("created", 0)
                if now - float(created) > NONCE_TTL_SECONDS:
                    os.unlink(path)
        except (OSError, ValueError):
            continue  # pruning is best-effort; a stray file must not block minting

    nonce = secrets.token_urlsafe(NONCE_BYTES)
    filename = f"{time.time_ns()}-{secrets.token_hex(4)}.json"
    _atomic_write_0600(
        os.path.join(directory, filename),
        json.dumps({"nonce": nonce, "created": now}).encode("utf-8"),
    )
    return nonce


def find_nonce(state_dir: str | None, candidate: str) -> str | None:
    """Return the matching nonce file's path if `candidate` is live, else None.

    Scans EVERY stored nonce and compares each with hmac.compare_digest — the
    loop deliberately does not short-circuit shape checks per candidate, so a
    miss costs the same as a hit (constant-time discipline, same as autopair).
    Expired entries are treated as absent.
    """
    if not candidate:
        return None
    directory = _nonce_dir(state_dir)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    now = time.time()
    matched: str | None = None
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            record = json.loads(open(path, "rb").read())
            stored = str(record.get("nonce") or "")
            created = float(record.get("created") or 0)
        except (OSError, ValueError):
            continue
        fresh = (now - created) <= NONCE_TTL_SECONDS
        # Compare every candidate regardless of freshness so timing does not
        # reveal how many live nonces exist; only a FRESH match counts.
        if hmac.compare_digest(candidate.encode("utf-8"), stored.encode("utf-8")) and fresh:
            matched = path
    return matched


def consume_nonce(state_dir: str | None, candidate: str) -> bool:
    """Atomically mark `candidate` used. True exactly once per nonce.

    os.rename is atomic on one filesystem, so of two racing consumers exactly
    one wins; the loser gets FileNotFoundError and reports False.
    """
    path = find_nonce(state_dir, candidate)
    if path is None:
        return False
    try:
        os.rename(path, path + ".used")
    except OSError:
        return False
    return True
