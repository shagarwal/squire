#!/usr/bin/env bash
#
# apply-patches.sh — apply Squire's hermes-agent patch overlay at image build.
#
# Productized from the founder's ~/.hermes/patches/apply-patches.sh, per
# docs/reference/hermes-update-plan-v2026.8.3.md §Phase 4. Differences from the
# VPS original, all deliberate:
#
#   * Uses `patch(1)`, not `git apply`. The upstream image's .dockerignore
#     excludes .git, so /opt/hermes is a source tree with no repository —
#     `git apply` has nothing to work against.
#   * Runs at BUILD time, once, against an immutable tree. There is no
#     "gateway is live, don't touch it" hazard to sequence around.
#   * Failure is fatal. On the VPS a failed patch meant "stop and think"; here
#     it means the image is wrong and must not be published.
#
# Ordering: patches are applied in lexical filename order, so the NNN- prefix
# is the sequence. Keep the file-disjoint rule from the update plan §Phase 8 —
# one patch per target file — so a patch that stops applying can be regenerated
# without untangling it from its neighbours.
#
# Usage: apply-patches.sh [INSTALL_DIR]     (default /opt/hermes)

set -euo pipefail

INSTALL_DIR="${1:-/opt/hermes}"
PATCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -d "$INSTALL_DIR" ]; then
    echo "apply-patches: install dir not found: $INSTALL_DIR" >&2
    exit 1
fi

# Nullglob so an empty patches/ directory yields an empty list rather than the
# literal string "*.patch".
shopt -s nullglob
patches=("$PATCH_DIR"/*.patch)
shopt -u nullglob

if [ ${#patches[@]} -eq 0 ]; then
    # NOT an error. The overlay is intentionally empty until the founder's
    # 001/002/003 patches are copied in from ~/.hermes/patches/ (they live on
    # the VPS, not in this repo). verify-markers.sh still runs after this and
    # still enforces the upstream markers we depend on.
    echo "apply-patches: no .patch files in $PATCH_DIR — overlay is empty (ok)"
    exit 0
fi

for patch_file in "${patches[@]}"; do
    name="$(basename "$patch_file")"

    # Dry run first so a bad patch produces a clear message instead of a
    # half-applied tree. --forward makes an already-applied patch a no-op
    # rather than an interactive "Reversed (or previously applied) patch
    # detected!" prompt, which keeps the script idempotent.
    if ! patch -p1 --batch --forward --dry-run \
            --directory="$INSTALL_DIR" --input="$patch_file" > /dev/null 2>&1; then

        # Distinguish "already applied" (fine, e.g. a rebuilt layer) from
        # "does not apply" (fatal, upstream moved under us).
        if patch -p1 --batch --reverse --dry-run \
                --directory="$INSTALL_DIR" --input="$patch_file" > /dev/null 2>&1; then
            echo "apply-patches: $name already applied — skipping"
            continue
        fi

        echo "apply-patches: FAILED — $name does not apply to $INSTALL_DIR" >&2
        echo "  This almost always means the pinned upstream tag moved and the" >&2
        echo "  patch needs regenerating. See docs/reference/hermes-update-plan-" >&2
        echo "  v2026.8.3.md §Phase 8 (regenerate any patch whose anchors changed)." >&2
        patch -p1 --batch --forward --dry-run \
            --directory="$INSTALL_DIR" --input="$patch_file" >&2 || true
        exit 1
    fi

    patch -p1 --batch --forward --directory="$INSTALL_DIR" --input="$patch_file"
    echo "apply-patches: applied $name"
done

echo "apply-patches: ${#patches[@]} patch(es) applied to $INSTALL_DIR"
