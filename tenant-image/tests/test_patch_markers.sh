#!/usr/bin/env bash
#
# Behavioural test for the patch-overlay guard — runs WITHOUT Docker.
#
# verify-markers.sh is the thing standing between an upstream regression and a
# broken tenant, so it needs a test that proves it FAILS when it should. A
# marker checker that can only pass is decoration.
#
# Usage: bash tenant-image/tests/test_patch_markers.sh
set -euo pipefail

IMAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
mkdir -p "$tmp/tree/agent" "$tmp/patches"
printf 'def _to_oauth_wire_name(): pass\n' > "$tmp/tree/agent/ok.py"
cp "$IMAGE_ROOT/patches/verify-markers.sh" "$tmp/patches/"

printf 'ok\tagent/ok.py\t_to_oauth_wire_name\trequired\n' > "$tmp/patches/markers.tsv"
echo "--- case 1: marker present (expect pass)"
bash "$tmp/patches/verify-markers.sh" "$tmp/tree"

printf 'missing\tagent/ok.py\tthis_string_is_not_present\trequired\n' > "$tmp/patches/markers.tsv"
echo "--- case 2: required marker missing (expect FAIL)"
if bash "$tmp/patches/verify-markers.sh" "$tmp/tree" >/dev/null 2>&1; then
  echo "BUG: verify-markers.sh passed a tree with a missing required marker"; exit 1
fi
echo "correctly failed closed"

printf 'soft\tagent/ok.py\tthis_string_is_not_present\toptional\n' > "$tmp/patches/markers.tsv"
echo "--- case 3: optional marker missing (expect pass with warning)"
bash "$tmp/patches/verify-markers.sh" "$tmp/tree"

echo "--- case 4: pattern containing spaces"
printf 'spaces\tagent/ok.py\tdef _to_oauth_wire_name(): pass\trequired\n' > "$tmp/patches/markers.tsv"
bash "$tmp/patches/verify-markers.sh" "$tmp/tree"

echo "--- case 5: apply-patches.sh with an empty overlay (expect no-op pass)"
bash "$IMAGE_ROOT/patches/apply-patches.sh" "$tmp/tree"

echo "ALL MARKER CHECKS PASS"
rm -rf "$tmp"
