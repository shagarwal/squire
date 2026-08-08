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
# Run against a COPY of the script in an empty directory, not against the real
# patches/ dir. Once the founder's 001/002/003 patches are vendored, the real
# directory is no longer empty and this case would start trying to apply them to
# a two-file synthetic tree — a test that breaks the moment the thing it guards
# starts being used is worse than no test.
cp "$IMAGE_ROOT/patches/apply-patches.sh" "$tmp/patches/"
bash "$tmp/patches/apply-patches.sh" "$tmp/tree"

echo "--- case 6: apply-patches.sh rejects a patch that does not apply"
cat > "$tmp/patches/001-bogus.patch" <<'PATCH'
--- a/agent/ok.py
+++ b/agent/ok.py
@@ -1 +1 @@
-this context line does not exist in the target
+replacement
PATCH
if bash "$tmp/patches/apply-patches.sh" "$tmp/tree" >/dev/null 2>&1; then
  echo "BUG: apply-patches.sh accepted a patch that does not apply"; exit 1
fi
echo "correctly refused a non-applying patch"
rm -f "$tmp/patches/001-bogus.patch"

echo "ALL MARKER CHECKS PASS"
rm -rf "$tmp"
