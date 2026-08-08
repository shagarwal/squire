#!/usr/bin/env bash
#
# Regression test for the Hindsight LLM re-wiring — runs WITHOUT Docker.
#
# Guards the single most valuable moment in the product: the user connects their
# own LLM, control-api revokes our trial key, and the memory daemon must follow.
# Before this path existed the daemon was configured once at boot and kept
# calling the revoked key forever — every extraction 401'd and the agent quietly
# stopped forming memories for exactly the users who had just converted. Nothing
# crashed, so nothing would have caught it but a test like this.
#
# Usage: bash tenant-image/tests/test_hindsight_env.sh
set -uo pipefail

IMAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$IMAGE_ROOT/bin"
T="$(mktemp -d)"; mkdir -p "$T/shm"
export SQUIRE_SECRETS_TMPFS="$T/shm"
OUT="$T/shm/hindsight-llm.env"
fails=0
ck() { if [ "$2" = 1 ]; then echo "  PASS  $1"; else echo "  FAIL  $1 ${3:-}"; fails=1; fi; }

# --- trial: proxy base url + trial key ---
cat > "$T/shm/.env" <<'E'
ANTHROPIC_API_KEY=trial-key-123
ANTHROPIC_BASE_URL=https://proxy.squire.internal
E
bash "$BIN/squire-hindsight-env.sh" >/dev/null; rc=$?
ck "first derivation reports changed (rc=10)" "$([ $rc -eq 10 ] && echo 1 || echo 0)" "rc=$rc"
ck "provider=anthropic" "$(grep -q '^HINDSIGHT_API_LLM_PROVIDER=anthropic$' "$OUT" && echo 1 || echo 0)"
ck "trial key present" "$(grep -q '^HINDSIGHT_API_LLM_API_KEY=trial-key-123$' "$OUT" && echo 1 || echo 0)"
ck "proxy base url present" "$(grep -q '^HINDSIGHT_API_LLM_BASE_URL=https://proxy.squire.internal$' "$OUT" && echo 1 || echo 0)"
ck "haiku-class model" "$(grep -q 'claude-haiku' "$OUT" && echo 1 || echo 0)"

# --- idempotent: no change, no restart ---
bash "$BIN/squire-hindsight-env.sh" >/dev/null; rc=$?
ck "unchanged rerun reports 0 (no needless restart)" "$([ $rc -eq 0 ] && echo 1 || echo 0)" "rc=$rc"

# --- conversion: user connects their own key, trial revoked ---
cat > "$T/shm/.env" <<'E'
ANTHROPIC_API_KEY=sk-ant-user-own-key
E
bash "$BIN/squire-hindsight-env.sh" >/dev/null; rc=$?
ck "conversion reports changed (rc=10)" "$([ $rc -eq 10 ] && echo 1 || echo 0)" "rc=$rc"
ck "user key replaced trial key" "$(grep -q '^HINDSIGHT_API_LLM_API_KEY=sk-ant-user-own-key$' "$OUT" && echo 1 || echo 0)"
ck "revoked trial key is GONE" "$(grep -q 'trial-key-123' "$OUT" && echo 0 || echo 1)"
ck "proxy base url dropped with it" "$(grep -q 'proxy.squire.internal' "$OUT" && echo 0 || echo 1)"

# --- openai fallback ---
cat > "$T/shm/.env" <<'E'
OPENAI_API_KEY=sk-openai-user
E
bash "$BIN/squire-hindsight-env.sh" >/dev/null
ck "openai provider derived" "$(grep -q '^HINDSIGHT_API_LLM_PROVIDER=openai$' "$OUT" && echo 1 || echo 0)"

# --- no creds at all ---
: > "$T/shm/.env"
bash "$BIN/squire-hindsight-env.sh" >/dev/null
ck "no creds -> no LLM vars emitted" "$(grep -q 'HINDSIGHT_API_LLM_PROVIDER' "$OUT" && echo 0 || echo 1)"

# --- the key must never be logged ---
cat > "$T/shm/.env" <<'E'
ANTHROPIC_API_KEY=sk-ant-super-secret
E
outlog="$(bash "$BIN/squire-hindsight-env.sh" 2>&1)"
ck "key absent from stdout" "$(echo "$outlog" | grep -q 'sk-ant-super-secret' && echo 0 || echo 1)"

# --- file permissions ---
ck "env file is 0600" "$([ "$(stat -c %a "$OUT")" = 600 ] && echo 1 || echo 0)" "$(stat -c %a "$OUT")"

# --- the run wrapper actually exports them ---
cat > "$T/fake-hindsight" <<'F'
#!/bin/sh
echo "PROVIDER=$HINDSIGHT_API_LLM_PROVIDER KEY=$HINDSIGHT_API_LLM_API_KEY"
F
chmod +x "$T/fake-hindsight"
wrapped="$(SQUIRE_HINDSIGHT_BIN="$T/fake-hindsight" bash "$BIN/squire-hindsight-run.sh" 2>&1)"
ck "wrapper exports creds to the daemon" \
  "$(echo "$wrapped" | grep -q 'PROVIDER=anthropic KEY=sk-ant-super-secret' && echo 1 || echo 0)" "$wrapped"

rm -rf "$T"
[ $fails = 0 ] && echo "HINDSIGHT ENV TESTS PASS" || { echo "FAILURES"; exit 1; }
