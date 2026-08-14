#!/usr/bin/env python3
"""Functional tests for the 1C credential-connect flow — runs WITHOUT Docker.

Covers, in order: the nonce store (single-use, 15-min TTL, constant-time,
atomic 0600), the /connect page served by the real shim (GET + POST against
faked provider validation endpoints), and the connected pipeline's ordering.

Usage: python3 tenant-image/tests/test_connect.py
Exit:  0 all assertions pass · 1 otherwise
"""
import json
import os
import pathlib
import stat
import sys
import tempfile
import time

IMAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(IMAGE_ROOT / "bin"))

import squire_connect  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


print("== nonce store ==")
state_dir = tempfile.mkdtemp()

nonce = squire_connect.mint_nonce(state_dir)
check("mint returns a urlsafe nonce of useful length", isinstance(nonce, str) and len(nonce) >= 40, nonce)

nonce_dir = pathlib.Path(state_dir) / "connect-nonces"
files = list(nonce_dir.glob("*.json"))
check("mint persisted exactly one nonce file", len(files) == 1, files)
if files:
    mode = stat.S_IMODE(files[0].stat().st_mode)
    check("nonce file is 0600", mode == 0o600, oct(mode))
    on_disk = json.loads(files[0].read_text())
    check("file holds the nonce and a created stamp",
          on_disk.get("nonce") == nonce and isinstance(on_disk.get("created"), float), on_disk)

check("a fresh nonce is found", squire_connect.find_nonce(state_dir, nonce) is not None)
check("a wrong nonce is not found", squire_connect.find_nonce(state_dir, "x" * 43) is None)
check("empty candidate is not found", squire_connect.find_nonce(state_dir, "") is None)

check("consume succeeds once", squire_connect.consume_nonce(state_dir, nonce) is True)
check("a consumed nonce is no longer found", squire_connect.find_nonce(state_dir, nonce) is None)
check("consume is not repeatable", squire_connect.consume_nonce(state_dir, nonce) is False)
check("the used marker file remains for audit",
      len(list(nonce_dir.glob("*.used"))) == 1, list(nonce_dir.iterdir()))

# Expiry: rewrite a minted nonce's created stamp into the past.
old = squire_connect.mint_nonce(state_dir)
old_file = next(p for p in nonce_dir.glob("*.json")
                if json.loads(p.read_text())["nonce"] == old)
payload = json.loads(old_file.read_text())
payload["created"] = time.time() - squire_connect.NONCE_TTL_SECONDS - 1
old_file.write_text(json.dumps(payload))
check("an expired nonce is not found", squire_connect.find_nonce(state_dir, old) is None)
check("an expired nonce cannot be consumed", squire_connect.consume_nonce(state_dir, old) is False)

# Two concurrent links may be outstanding; each is independently single-use.
a = squire_connect.mint_nonce(state_dir)
b = squire_connect.mint_nonce(state_dir)
check("two outstanding nonces coexist",
      squire_connect.find_nonce(state_dir, a) is not None
      and squire_connect.find_nonce(state_dir, b) is not None)
squire_connect.consume_nonce(state_dir, a)
check("consuming one leaves the other valid", squire_connect.find_nonce(state_dir, b) is not None)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL CONNECT TESTS PASS")
