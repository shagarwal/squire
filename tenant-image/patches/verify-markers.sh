#!/usr/bin/env bash
#
# verify-markers.sh — enforce patches/markers.tsv against a hermes install tree.
#
# This is the "marker greps as a CI check" item from Task 0.2, and it runs in
# two places:
#
#   1. In the Dockerfile, right after apply-patches.sh. A missing required
#     marker fails the BUILD, so a bad image is never published or deployed.
#   2. In .github/workflows/tenant-image.yml, where the file itself is
#     lint-checked (well-formedness, referenced patches exist) without needing
#     a container.
#
# Usage: verify-markers.sh [INSTALL_DIR]     (default /opt/hermes)
# Exit:  0 all required markers present · 1 at least one missing

set -uo pipefail

INSTALL_DIR="${1:-/opt/hermes}"
MARKERS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/markers.tsv"

if [ ! -f "$MARKERS" ]; then
    echo "verify-markers: markers.tsv not found at $MARKERS" >&2
    exit 1
fi

fail=0
checked=0
warned=0

# IFS=$'\t' + read -r keeps tabs as the only separator, so patterns may contain
# spaces (e.g. `_MCP_TOOL_PREFIX = "mcp__"`). `|| [ -n "$id" ]` catches a final
# line with no trailing newline.
while IFS=$'\t' read -r id file pattern required || [ -n "${id:-}" ]; do
    # Skip comments and blank lines.
    case "${id:-}" in
        ''|'#'*) continue ;;
    esac

    if [ -z "${file:-}" ] || [ -z "${pattern:-}" ]; then
        echo "verify-markers: malformed row (need 4 tab-separated columns): $id" >&2
        fail=1
        continue
    fi

    required="${required:-required}"
    target="$INSTALL_DIR/$file"
    checked=$((checked + 1))

    if [ ! -f "$target" ]; then
        if [ "$required" = "optional" ]; then
            echo "verify-markers: [warn] $id — file missing: $file"
            warned=$((warned + 1))
        else
            echo "verify-markers: [FAIL] $id — file missing: $file" >&2
            fail=1
        fi
        continue
    fi

    # -F fixed string, -q quiet. Fixed strings only — see markers.tsv header.
    if grep -qF -- "$pattern" "$target"; then
        echo "verify-markers: [ok]   $id ($file)"
    elif [ "$required" = "optional" ]; then
        echo "verify-markers: [warn] $id — marker not found in $file (patch not vendored yet?)"
        warned=$((warned + 1))
    else
        echo "verify-markers: [FAIL] $id — marker not found in $file" >&2
        echo "                pattern: $pattern" >&2
        fail=1
    fi
done < "$MARKERS"

echo "verify-markers: $checked marker(s) checked, $warned warning(s)"

if [ "$fail" -ne 0 ]; then
    cat >&2 <<'EOF'

verify-markers: FAILED.

A required marker is missing from the pinned upstream tree. Do not paper over
this by deleting the row. Either:

  * upstream regressed a fix we depend on  -> stop, diagnose, do not bump; or
  * upstream refactored and moved the code -> re-verify the behaviour is still
    there, then update the marker in the same commit that bumps HERMES_VERSION.

See docs/reference/hermes-update-plan-v2026.8.3.md §7 (verification gates) and
docs/reference/hermes-400-extra-usage-fix.md for why the OAuth markers matter.
EOF
    exit 1
fi

exit 0
