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

# --- THE CROSS-PROVIDER TRAP -------------------------------------------------
# The tenant converts from our Anthropic trial to their own OpenAI key (or
# ChatGPT/Codex — 2 of the 4 offered paths). .env now holds only their OpenAI
# key, but the PROCESS env still carries the revoked trial ANTHROPIC_API_KEY
# until the next redeploy.
#
# A per-key "use .env, else process env" rule silently takes the Anthropic
# branch here and configures Hindsight with the revoked key. Precedence must be
# per-SOURCE: .env supplies a provider key, therefore .env is the only source.
#
# Note the harness deliberately EXPORTS the stale key — the earlier version of
# this suite passed only because it did not, which is exactly how the bug
# survived a green test run.
cat > "$T/shm/.env" <<'E'
OPENAI_API_KEY=sk-openai-user-own
E
export ANTHROPIC_API_KEY=REVOKED-TRIAL-KEY
export ANTHROPIC_BASE_URL=https://proxy.squire.internal
bash "$BIN/squire-hindsight-env.sh" >/dev/null
ck "stale process-env Anthropic key does NOT win" \
   "$(grep -q '^HINDSIGHT_API_LLM_PROVIDER=openai$' "$OUT" && echo 1 || echo 0)" \
   "got: $(grep '^HINDSIGHT_API_LLM_PROVIDER=' "$OUT")"
ck "revoked trial key absent from config" \
   "$(grep -q 'REVOKED-TRIAL-KEY' "$OUT" && echo 0 || echo 1)"
ck "user's own OpenAI key is used" \
   "$(grep -q '^HINDSIGHT_API_LLM_API_KEY=sk-openai-user-own$' "$OUT" && echo 1 || echo 0)"
ck "our proxy base URL not paired with the user's key" \
   "$(grep -q 'proxy.squire.internal' "$OUT" && echo 0 || echo 1)"

# --- Claude Max OAuth: no usable credential, but must NOT reuse the trial key --
cat > "$T/shm/.env" <<'E'
CLAUDE_CODE_OAUTH_TOKEN=oauth-token-not-an-api-key
E
outlog="$(bash "$BIN/squire-hindsight-env.sh" 2>&1)"
ck "Claude Max OAuth emits no LLM config" \
   "$(grep -q 'HINDSIGHT_API_LLM_PROVIDER' "$OUT" && echo 0 || echo 1)"
ck "Claude Max OAuth does not fall back to the trial key" \
   "$(grep -q 'REVOKED-TRIAL-KEY' "$OUT" && echo 0 || echo 1)"
ck "Claude Max OAuth idle state is explained in the log" \
   "$(echo "$outlog" | grep -qi 'idle' && echo 1 || echo 0)"

# --- trial still works when .env has nothing (process env is the source) ---
: > "$T/shm/.env"
bash "$BIN/squire-hindsight-env.sh" >/dev/null
ck "no .env creds -> provisioned trial key IS used" \
   "$(grep -q '^HINDSIGHT_API_LLM_API_KEY=REVOKED-TRIAL-KEY$' "$OUT" && echo 1 || echo 0)"
ck "trial proxy base URL used with the trial key" \
   "$(grep -q '^HINDSIGHT_API_LLM_BASE_URL=https://proxy.squire.internal$' "$OUT" && echo 1 || echo 0)"
unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL

# --- no creds at all ---
: > "$T/shm/.env"
bash "$BIN/squire-hindsight-env.sh" >/dev/null
ck "no creds -> no LLM vars emitted" "$(grep -q 'HINDSIGHT_API_LLM_PROVIDER' "$OUT" && echo 0 || echo 1)"

# --- ChatGPT (Codex OAuth): auth.json only, no .env marker -------------------
# Since the 2026-08-16 connect fix a ChatGPT tenant has NOTHING in .env; the
# tokens live in hermes's auth.json envelope. The script must take the codex
# branch, not fall through to the (revoked) trial key in the process env.
b64url() { printf '%s' "$1" | base64 | tr '+/' '-_' | tr -d '='; }
JWT_STALE="hdr.$(b64url '{"exp":1000000000}').sig"
JWT_FRESH="hdr.$(b64url '{"exp":2000000000}').sig"
: > "$T/shm/.env"
cat > "$T/shm/auth.json" <<E
{
  "version": 1,
  "active_provider": "openai-codex",
  "providers": {
    "openai-codex": {
      "auth_mode": "device_code",
      "tokens": {"access_token": "$JWT_STALE", "refresh_token": "rt-NEVER-COPY", "account_id": "acct-1"}
    }
  },
  "credential_pool": {
    "openai-codex": [
      {"source": "device_code", "tokens": {"access_token": "$JWT_FRESH", "refresh_token": "rt-NEVER-COPY-2", "account_id": "acct-1"}}
    ]
  }
}
E
export ANTHROPIC_API_KEY=REVOKED-TRIAL-KEY
export ANTHROPIC_BASE_URL=https://proxy.squire.internal
bash "$BIN/squire-hindsight-env.sh" >/dev/null; rc=$?
DERIVED="$T/shm/codex-home/auth.json"
ck "codex derivation reports changed (rc=10)" "$([ $rc -eq 10 ] && echo 1 || echo 0)" "rc=$rc"
ck "codex beats the revoked trial key" \
   "$(grep -q '^HINDSIGHT_API_LLM_PROVIDER=openai-codex$' "$OUT" && echo 1 || echo 0)" \
   "got: $(grep '^HINDSIGHT_API_LLM_PROVIDER=' "$OUT")"
ck "trial key absent from codex config" "$(grep -q 'REVOKED-TRIAL-KEY' "$OUT" && echo 0 || echo 1)"
ck "codex emits no LLM_API_KEY line (auth is file-based)" \
   "$(grep -q '^HINDSIGHT_API_LLM_API_KEY=' "$OUT" && echo 0 || echo 1)"
ck "mini-class codex model" "$(grep -q '^HINDSIGHT_API_LLM_MODEL=gpt-5.4-mini$' "$OUT" && echo 1 || echo 0)"
ck "CODEX_HOME points at the derived dir" \
   "$(grep -q "^CODEX_HOME=$T/shm/codex-home$" "$OUT" && echo 1 || echo 0)"
ck "derived auth.json exists" "$([ -f "$DERIVED" ] && echo 1 || echo 0)"
ck "derived file is Codex-CLI format (auth_mode=chatgpt)" \
   "$(grep -q '"auth_mode": "chatgpt"' "$DERIVED" && echo 1 || echo 0)"
ck "FRESHEST access token wins (pool over stale singleton)" \
   "$(grep -q "$JWT_FRESH" "$DERIVED" && echo 1 || echo 0)"
ck "stale singleton token not used" "$(grep -q "$JWT_STALE" "$DERIVED" && echo 0 || echo 1)"
ck "refresh_token NEVER copied (hermes is the sole refresher)" \
   "$(grep -q 'rt-NEVER-COPY' "$DERIVED" && echo 0 || echo 1)"
ck "derived file is 0600" "$([ "$(stat -c %a "$DERIVED")" = 600 ] && echo 1 || echo 0)" "$(stat -c %a "$DERIVED")"
ck "codex tenants get NO embeddings vars (sub tokens auth but 429 insufficient_quota — verified live 2026-08-20)" \
   "$(grep -q 'HINDSIGHT_API_EMBEDDINGS' "$OUT" && echo 0 || echo 1)"

# --- codex idempotency + rotation ---
bash "$BIN/squire-hindsight-env.sh" >/dev/null; rc=$?
ck "unchanged codex rerun reports 0" "$([ $rc -eq 0 ] && echo 1 || echo 0)" "rc=$rc"
JWT_FRESHER="hdr.$(b64url '{"exp":3000000000}').sig"
sed -i "s|$JWT_FRESH|$JWT_FRESHER|" "$T/shm/auth.json"
bash "$BIN/squire-hindsight-env.sh" >/dev/null; rc=$?
ck "hermes token rotation reports changed (rc=10 -> restart)" "$([ $rc -eq 10 ] && echo 1 || echo 0)" "rc=$rc"
ck "derived file picked up the rotated token" "$(grep -q "$JWT_FRESHER" "$DERIVED" && echo 1 || echo 0)"

# --- .env key beats codex; leaving codex cleans up the derived file ---
cat > "$T/shm/.env" <<'E'
ANTHROPIC_API_KEY=sk-ant-user-own-key
E
bash "$BIN/squire-hindsight-env.sh" >/dev/null
ck ".env key beats codex auth.json" \
   "$(grep -q '^HINDSIGHT_API_LLM_PROVIDER=anthropic$' "$OUT" && echo 1 || echo 0)"
ck "derived codex auth.json removed when codex is not the provider" \
   "$([ -f "$DERIVED" ] && echo 0 || echo 1)"
ck "anthropic tenants get NO embeddings vars (no Anthropic embeddings API)" \
   "$(grep -q 'HINDSIGHT_API_EMBEDDINGS' "$OUT" && echo 0 || echo 1)"
rm -f "$T/shm/auth.json"
unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL

# --- embeddings on a plain OpenAI key ---
cat > "$T/shm/.env" <<'E'
OPENAI_API_KEY=sk-openai-embed-me
E
bash "$BIN/squire-hindsight-env.sh" >/dev/null
ck "plain OpenAI key: embeddings on the user's provider" \
   "$(grep -q '^HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai$' "$OUT" && echo 1 || echo 0)"
ck "embeddings use an EXPLICIT key (LLM-key fallback trap)" \
   "$(grep -q '^HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY=sk-openai-embed-me$' "$OUT" && echo 1 || echo 0)"
ck "openai embeddings dimensions pinned to 384" \
   "$(grep -q '^HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS=384$' "$OUT" && echo 1 || echo 0)"

# --- custom base URL: fail toward the local model, not runtime errors ---
cat > "$T/shm/.env" <<'E'
OPENAI_API_KEY=sk-openai-compat
OPENAI_BASE_URL=https://some-openai-compatible.example
E
bash "$BIN/squire-hindsight-env.sh" >/dev/null
ck "custom OPENAI_BASE_URL: no embeddings vars (endpoint may lack /v1/embeddings)" \
   "$(grep -q 'HINDSIGHT_API_EMBEDDINGS' "$OUT" && echo 0 || echo 1)"

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
