#!/usr/bin/env bash
#
# Boot-path regression test for squire-entrypoint.sh — runs WITHOUT Docker.
#
# Covers the secrets lifecycle end to end: first boot, restart, image upgrade
# over a customised SOUL.md, credential rotation, wrong-DEK refusal, missing-DEK
# fail-fast, the webhook-secret ownership contract, and the pg0 durability
# assertions. The most important assertion in this file is the negative one —
# that no plaintext secret ever appears anywhere on the volume — because that is
# the property the whole privacy claim rests on, and it fails silently if it
# regresses.
#
# The entrypoint ends with `exec supervisord`, which does not exist outside the
# image; that final exec is expected to fail and we assert on the state it left
# behind. Everything before the exec is the code under test.
#
# Usage: bash tenant-image/tests/test_boot_path.sh
# Exit:  0 all assertions pass · 1 otherwise
set -uo pipefail

IMAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$IMAGE_ROOT/bin"
ROOT="$(mktemp -d)"
VOL="$ROOT/data"
TMPFS="$ROOT/shm"
mkdir -p "$VOL" "$TMPFS"

DEK="$(python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())")"
DEK2="$(python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())")"

fails=0
check() { if [ "$2" = "1" ]; then echo "  PASS  $1"; else echo "  FAIL  $1 ${3:-}"; fails=$((fails+1)); fi; }
yn() { if "$@" >/dev/null 2>&1; then echo 1; else echo 0; fi; }
says() { echo "$1" | grep -q "$2" && echo 1 || echo 0; }

# Extra env for a single boot() call; reset by boot() after use.
EXTRA_ENV=()

# boot [webhook_url]
#
# HOME is deliberately set to $VOL: the entrypoint hard-asserts that HOME is the
# volume, because pg0 puts the PostgreSQL cluster under $HOME/.pg0 and a wrong
# value silently discards the tenant's memory on every redeploy.
#
# SQUIRE_ALLOW_EPHEMERAL_VOLUME=1 because a tmpdir is not a real mount point and
# TENANT_ID is set; that combination is a hard error in production by design.
boot() {
  local url="${1:-}"
  env -i PATH="$PATH" \
      HOME="$VOL" \
      HERMES_HOME="$VOL" \
      SQUIRE_VOLUME="$VOL" \
      SQUIRE_ALLOW_EPHEMERAL_VOLUME=1 \
      SQUIRE_STATE_DIR="$VOL/.squire" \
      SQUIRE_SECRETS_TMPFS="$TMPFS" \
      SQUIRE_HOME_TEMPLATE="$IMAGE_ROOT/home-template" \
      SQUIRE_PYTHON="$(command -v python3)" \
      SQUIRE_BIN="$BIN" \
      SQUIRE_DEK="${SQUIRE_DEK_OVERRIDE:-$DEK}" \
      TENANT_ID=t-boot-test \
      ${url:+TELEGRAM_WEBHOOK_URL=$url} \
      "${EXTRA_ENV[@]}" \
      bash "$BIN/squire-entrypoint.sh" 2>&1
  EXTRA_ENV=()
}

URL="https://ingress.example.com/webhook/telegram"

echo "== 1. first boot on an empty volume =="
out="$(boot "$URL")"
echo "$out" | sed 's/^/    | /' | grep -E 'durability ok|first boot|seeded|TELEGRAM_WEBHOOK_SECRET|telegram webhook|complete' || true

check "template SOUL.md seeded"      "$(yn test -f "$VOL/SOUL.md")"
check "template config.yaml seeded"  "$(yn test -f "$VOL/config.yaml")"
check "hindsight config seeded"      "$(yn test -f "$VOL/hindsight/config.json")"
check "concierge skill seeded"       "$(yn test -f "$VOL/skills/concierge/SKILL.md")"
check "state machine seeded"         "$(yn test -f "$VOL/skills/concierge/state-machine.yaml")"
check "init marker written"          "$(yn test -f "$VOL/.squire/initialized")"
check ".env is a symlink"            "$(yn test -L "$VOL/.env")"
check "auth.json is a symlink"       "$(yn test -L "$VOL/auth.json")"
check ".env resolves into tmpfs"     "$([ "$(readlink "$VOL/.env")" = "$TMPFS/.env" ] && echo 1 || echo 0)" "$(readlink "$VOL/.env")"
check "sealed .env on volume"        "$(yn test -f "$VOL/.squire/secrets/.env.enc")"
check "durability assertion ran"     "$(says "$out" 'durability ok')"
check "pg0 root reported on volume"  "$(says "$out" "$VOL/.pg0")"

SEC1="$(sed -n 's/^TELEGRAM_WEBHOOK_SECRET=//p' "$TMPFS/.env")"
check "dev-fallback secret generated"  "$(says "$out" 'dev fallback')"
check "webhook secret is 64 hex chars" "$([ "${#SEC1}" = 64 ] && echo 1 || echo 0)" "len=${#SEC1}"

echo "  -- no plaintext secret anywhere on the volume --"
# The tmpfs dir lives outside $VOL, so any hit here is a real leak.
leak="$(grep -rl "$SEC1" "$VOL" 2>/dev/null | grep -v '^$' || true)"
check "secret value not present in any volume file" "$([ -z "$leak" ] && echo 1 || echo 0)" "$leak"
check "sealed blob is not plaintext" "$(yn bash -c "! grep -q '$SEC1' '$VOL/.squire/secrets/.env.enc'")"
check "sealed blob has magic header" "$(yn bash -c "head -c 6 '$VOL/.squire/secrets/.env.enc' | grep -q SQENC1")"

echo "== 2. restart: same DEK, secrets survive =="
rm -rf "${TMPFS:?}"/*                      # tmpfs is volatile — simulate a redeploy
out="$(boot "$URL")"
SEC2="$(sed -n 's/^TELEGRAM_WEBHOOK_SECRET=//p' "$TMPFS/.env")"
check "webhook secret unchanged across restart" "$([ "$SEC1" = "$SEC2" ] && echo 1 || echo 0)"
check "restart did not re-seed (no 'first boot')" "$([ "$(says "$out" 'first boot')" = 0 ] && echo 1 || echo 0)"
check ".env still a symlink after restart" "$(yn test -L "$VOL/.env")"

echo "== 3. tenant customises SOUL.md; upgrade must not clobber it =="
echo "MY CUSTOM PERSONA" > "$VOL/SOUL.md"
rm -f "$VOL/skills/concierge/SKILL.md"     # simulate a newly-baked file in image vN+1
rm -rf "${TMPFS:?}"/*
out="$(boot "$URL")"
check "customised SOUL.md preserved" "$(yn grep -q 'MY CUSTOM PERSONA' "$VOL/SOUL.md")"
check "missing baked file re-seeded"  "$(yn test -f "$VOL/skills/concierge/SKILL.md")"

echo "== 4. agent writes a credential; sync re-seals it =="
echo 'ANTHROPIC_API_KEY=sk-ant-user-supplied' >> "$TMPFS/.env"
env -i PATH="$PATH" HERMES_HOME="$VOL" SQUIRE_STATE_DIR="$VOL/.squire" \
    SQUIRE_SECRETS_TMPFS="$TMPFS" SQUIRE_PYTHON="$(command -v python3)" \
    SQUIRE_BIN="$BIN" SQUIRE_DEK="$DEK" \
    bash "$BIN/squire-secrets-sync.sh" --once >/dev/null 2>&1
check "new key not plaintext on volume" "$(yn bash -c "! grep -rq 'sk-ant-user-supplied' '$VOL'")"
rm -rf "${TMPFS:?}"/*
out="$(boot "$URL")"
check "new key survives a redeploy" "$(yn grep -q 'sk-ant-user-supplied' "$TMPFS/.env")"

echo "== 5. control-api owns TELEGRAM_WEBHOOK_SECRET =="
# The ownership contract: when control-api supplies the secret, the tenant uses
# it verbatim. Both sides then register identical values with Telegram, so the
# two setWebhook calls are idempotent instead of fighting.
rm -rf "${TMPFS:?}"/*
EXTRA_ENV=(TELEGRAM_WEBHOOK_SECRET=supplied-by-control-api)
out="$(boot "$URL")"
check "supplied secret used verbatim" \
    "$([ "$(sed -n 's/^TELEGRAM_WEBHOOK_SECRET=//p' "$TMPFS/.env")" = "supplied-by-control-api" ] && echo 1 || echo 0)"
check "logged as coming from control-api" "$(says "$out" 'control-api')"
check "supplied secret overrode the dev fallback" \
    "$([ "$(sed -n 's/^TELEGRAM_WEBHOOK_SECRET=//p' "$TMPFS/.env")" != "$SEC1" ] && echo 1 || echo 0)"

# Rotation: a new value from control-api must propagate, not be masked by the
# sealed copy of the old one.
rm -rf "${TMPFS:?}"/*
EXTRA_ENV=(TELEGRAM_WEBHOOK_SECRET=rotated-value)
out="$(boot "$URL")"
check "rotated secret propagates" \
    "$([ "$(sed -n 's/^TELEGRAM_WEBHOOK_SECRET=//p' "$TMPFS/.env")" = "rotated-value" ] && echo 1 || echo 0)"
check "no duplicate assignments in .env" \
    "$([ "$(grep -c '^TELEGRAM_WEBHOOK_SECRET=' "$TMPFS/.env")" = 1 ] && echo 1 || echo 0)"
check "user credential survived the rotation" "$(yn grep -q 'sk-ant-user-supplied' "$TMPFS/.env")"

echo "== 6. wrong DEK must refuse to boot, not re-initialise =="
rm -rf "${TMPFS:?}"/*
# Assignment prefix INSIDE the substitution, so the override is scoped to this
# one boot and does not leak into every later case.
out="$(SQUIRE_DEK_OVERRIDE="$DEK2" boot "$URL")"
check "refuses with a clear message" "$(says "$out" 'could not decrypt')"
check "sealed .env NOT destroyed"    "$(yn test -f "$VOL/.squire/secrets/.env.enc")"
rm -rf "${TMPFS:?}"/*
out="$(boot "$URL")"
check "recovers with the correct DEK" "$(yn grep -q 'sk-ant-user-supplied' "$TMPFS/.env")"

echo "== 7. missing DEK must fail fast =="
out="$(env -i PATH="$PATH" HOME="$VOL" HERMES_HOME="$VOL" SQUIRE_VOLUME="$VOL" \
    SQUIRE_ALLOW_EPHEMERAL_VOLUME=1 SQUIRE_STATE_DIR="$VOL/.squire" \
    SQUIRE_SECRETS_TMPFS="$TMPFS" SQUIRE_HOME_TEMPLATE="$IMAGE_ROOT/home-template" \
    SQUIRE_PYTHON="$(command -v python3)" SQUIRE_BIN="$BIN" \
    bash "$BIN/squire-entrypoint.sh" 2>&1)"
check "refuses without SQUIRE_DEK" "$(says "$out" 'SQUIRE_DEK missing')"

echo "== 8. durability assertions =="
# HOME pointing anywhere but the volume means pg0 writes the PostgreSQL cluster
# to ephemeral container storage. Must be fatal, and must say why.
out="$(env -i PATH="$PATH" HOME="$ROOT/elsewhere" HERMES_HOME="$VOL" SQUIRE_VOLUME="$VOL" \
    SQUIRE_ALLOW_EPHEMERAL_VOLUME=1 SQUIRE_STATE_DIR="$VOL/.squire" \
    SQUIRE_SECRETS_TMPFS="$TMPFS" SQUIRE_HOME_TEMPLATE="$IMAGE_ROOT/home-template" \
    SQUIRE_PYTHON="$(command -v python3)" SQUIRE_BIN="$BIN" SQUIRE_DEK="$DEK" \
    TENANT_ID=t-boot-test \
    bash "$BIN/squire-entrypoint.sh" 2>&1)"
check "HOME != volume is fatal" "$(says "$out" 'is not the mounted volume')"
check "error names the real risk" "$(says "$out" 'long-term memory')"

out="$(env -i PATH="$PATH" HOME="$VOL" HERMES_HOME="$VOL" SQUIRE_VOLUME="$VOL" \
    SQUIRE_STATE_DIR="$VOL/.squire" \
    SQUIRE_SECRETS_TMPFS="$TMPFS" SQUIRE_HOME_TEMPLATE="$IMAGE_ROOT/home-template" \
    SQUIRE_PYTHON="$(command -v python3)" SQUIRE_BIN="$BIN" SQUIRE_DEK="$DEK" \
    TENANT_ID=t-boot-test \
    bash "$BIN/squire-entrypoint.sh" 2>&1)"
check "provisioned tenant + unmounted volume is fatal" "$(says "$out" 'not a mount point')"

# No TENANT_ID == local dev. Must warn, not die.
rm -rf "${TMPFS:?}"/*
out="$(env -i PATH="$PATH" HOME="$VOL" HERMES_HOME="$VOL" SQUIRE_VOLUME="$VOL" \
    SQUIRE_STATE_DIR="$VOL/.squire" \
    SQUIRE_SECRETS_TMPFS="$TMPFS" SQUIRE_HOME_TEMPLATE="$IMAGE_ROOT/home-template" \
    SQUIRE_PYTHON="$(command -v python3)" SQUIRE_BIN="$BIN" SQUIRE_DEK="$DEK" \
    bash "$BIN/squire-entrypoint.sh" 2>&1)"
check "dev run without a volume warns and continues" "$(says "$out" 'state is EPHEMERAL')"
check "dev run still reaches supervisord" "$(says "$out" 'starting supervisord')"

echo "== 9. polling mode when no webhook URL is supplied =="
rm -rf "${TMPFS:?}"/*
out="$(boot)"
check "falls back to long polling" "$(says "$out" 'long polling')"

echo "== 10. shutdown durability: SIGTERM must interrupt the sleep =="
# A credential written seconds before a redeploy must still be sealed. That
# requires the SIGTERM handler to run IMMEDIATELY — bash will not run a trap
# while a foreground `sleep` is executing, so the loop uses `sleep & wait`.
# With a 300s interval, a regression here shows up as a ~300s hang that blows
# through supervisord's stopwaitsecs and loses the write.
SYNCDIR="$ROOT/sync"; SVOL="$SYNCDIR/data"; STMP="$SYNCDIR/shm"
mkdir -p "$SVOL/.squire" "$STMP"
printf 'INITIAL=1\n' > "$STMP/.env"
env -i PATH="$PATH" HERMES_HOME="$SVOL" SQUIRE_STATE_DIR="$SVOL/.squire" \
    SQUIRE_SECRETS_TMPFS="$STMP" SQUIRE_PYTHON="$(command -v python3)" \
    SQUIRE_BIN="$BIN" SQUIRE_DEK="$DEK" SQUIRE_SECRETS_SYNC_INTERVAL=300 \
    bash "$BIN/squire-secrets-sync.sh" > "$SYNCDIR/log" 2>&1 &
syncpid=$!
sleep 2                                    # let the first pass finish, then it sleeps 300s
printf 'ANTHROPIC_API_KEY=sk-written-just-before-stop\n' >> "$STMP/.env"
started=$(date +%s)
kill -TERM "$syncpid" 2>/dev/null
wait "$syncpid" 2>/dev/null
elapsed=$(( $(date +%s) - started ))

check "SIGTERM interrupts the sleep (${elapsed}s, interval 300s)" \
    "$([ "$elapsed" -le 5 ] && echo 1 || echo 0)" "took ${elapsed}s"
check "final sync ran on shutdown" "$(yn grep -q 'final sync' "$SYNCDIR/log")"
SQUIRE_DEK="$DEK" python3 "$BIN/squire_secrets.py" \
    unseal "$SVOL/.squire/secrets/.env.enc" "$SYNCDIR/out" ".env" >/dev/null 2>&1
check "late credential was re-sealed" "$(yn grep -q 'sk-written-just-before-stop' "$SYNCDIR/out")"
check "late credential never plaintext on volume" \
    "$(yn bash -c "! grep -rq 'sk-written-just-before-stop' '$SVOL'")"

echo
rm -rf "$ROOT"
if [ "$fails" -gt 0 ]; then echo "$fails FAILURE(S)"; exit 1; fi
echo "ALL BOOT-PATH TESTS PASS"
