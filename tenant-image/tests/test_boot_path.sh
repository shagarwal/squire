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

# assert_fatal <label> <output> <rc> <distinctive substring>
#
# A "refuses to boot" assertion has to prove the boot ACTUALLY DIED, not merely
# that a scary-looking string appeared in the log. Substring-only checks are
# mutation-blind: downgrading a `die` to a `log` leaves the message intact and
# the assertion still passes, which is exactly the bug this helper exists to
# close. So every fatality is checked three ways:
#
#   1. non-zero exit status  — the process really stopped
#   2. 'starting supervisord' ABSENT — it stopped BEFORE launching services
#   3. a distinctive substring — it stopped for the RIGHT reason, and the
#      operator is told what to do
#
# (2) is the negation of the "dev run still reaches supervisord" assertion and
# is what makes (3) unambiguous when a fatal path and a warning path share
# wording.
assert_fatal() {
    local label="$1" out="$2" rc="$3" needle="$4"
    check "$label: exits non-zero" \
        "$([ "$rc" -ne 0 ] && echo 1 || echo 0)" "rc=$rc"
    check "$label: never starts services" \
        "$([ "$(says "$out" 'starting supervisord')" = 0 ] && echo 1 || echo 0)"
    check "$label: says why" "$(says "$out" "$needle")"
}

# Stub supervisord: a successful boot must be able to exit 0, otherwise the exit
# status cannot distinguish "booted" from "refused" (the real binary does not
# exist outside the image, so a bare exec would fail on EVERY path).
STUB_SUPERVISORD="$ROOT/stub-supervisord"
cat > "$STUB_SUPERVISORD" <<'STUB'
#!/bin/sh
echo "[stub-supervisord] would supervise: $*"
exit 0
STUB
chmod +x "$STUB_SUPERVISORD"

# NOTE: extra environment is passed as trailing ARGUMENTS to boot(), not via a
# global. A global was tried and is subtly broken: boot() runs inside $( ), so
# any reset it performs happens in that subshell and the parent's value
# survives to leak into every later case.

# boot [webhook_url]
#
# HOME is deliberately set to $VOL: the entrypoint hard-asserts that HOME is the
# volume, because pg0 puts the PostgreSQL cluster under $HOME/.pg0 and a wrong
# value silently discards the tenant's memory on every redeploy.
#
# SQUIRE_ALLOW_EPHEMERAL_VOLUME=1 because a tmpdir is not a real mount point and
# TENANT_ID is set; that combination is a hard error in production by design.
# boot [webhook_url] [EXTRA=env ...]
boot() {
  local url="${1:-}" rc
  shift || true
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
      SQUIRE_SUPERVISORD="$STUB_SUPERVISORD" \
      SQUIRE_SUPERVISORD_CONF="$IMAGE_ROOT/supervisord.conf" \
      SQUIRE_DEK="${SQUIRE_DEK_OVERRIDE:-$DEK}" \
      TENANT_ID=t-boot-test \
      ${url:+TELEGRAM_WEBHOOK_URL=$url} \
      "$@" \
      bash "$BIN/squire-entrypoint.sh" 2>&1
  # Capture immediately: any command in between (even an assignment, which
  # returns 0) would overwrite the entrypoint's exit status.
  rc=$?
  return "$rc"
}

# raw_boot <env assignments...> — for cases that need to violate the defaults
# boot() enforces (a wrong HOME, a missing volume, no DEK at all).
raw_boot() {
  env -i PATH="$PATH" \
      SQUIRE_STATE_DIR="$VOL/.squire" \
      SQUIRE_SECRETS_TMPFS="$TMPFS" \
      SQUIRE_HOME_TEMPLATE="$IMAGE_ROOT/home-template" \
      SQUIRE_PYTHON="$(command -v python3)" \
      SQUIRE_BIN="$BIN" \
      SQUIRE_SUPERVISORD="$STUB_SUPERVISORD" \
      SQUIRE_SUPERVISORD_CONF="$IMAGE_ROOT/supervisord.conf" \
      "$@" \
      bash "$BIN/squire-entrypoint.sh" 2>&1
}

URL="https://ingress.example.com/webhook/telegram"

echo "== 1. first boot on an empty volume =="
out="$(boot "$URL")"; rc=$?
echo "$out" | sed 's/^/    | /' | grep -E 'durability ok|first boot|seeded|TELEGRAM_WEBHOOK_SECRET|telegram webhook|complete' || true

# The positive half of the exit-status contract: a healthy boot MUST exit 0 and
# MUST reach supervisord. Without this, "non-zero == refused" would be
# meaningless because every boot would be non-zero.
check "healthy boot exits 0"            "$([ "$rc" -eq 0 ] && echo 1 || echo 0)" "rc=$rc"
check "healthy boot reaches supervisord" "$(says "$out" 'starting supervisord')"

check "template SOUL.md seeded"      "$(yn test -f "$VOL/SOUL.md")"
check "template config.yaml seeded"  "$(yn test -f "$VOL/config.yaml")"
check "hindsight config seeded"      "$(yn test -f "$VOL/hindsight/config.json")"
check "concierge skill seeded"       "$(yn test -f "$VOL/skills/concierge/SKILL.md")"
check "state machine seeded"         "$(yn test -f "$VOL/skills/concierge/state-machine.yaml")"
check "init marker written"          "$(yn test -f "$VOL/.squire/initialized")"

# Onboarding state must EXIST after the first boot, and must say `greet`.
# The original design left this file absent and asked the agent to infer
# onboarding from its absence; that shipped, and the first live tenant got no
# onboarding at all. A present file IS the mechanism — squire-concierge-hook.py
# has nothing to inject without it.
check "concierge state seeded"       "$(yn test -f "$VOL/.squire/concierge-state.json")"
check "concierge state is greet" \
    "$(yn python3 -c "import json,sys; sys.exit(0 if json.load(open('$VOL/.squire/concierge-state.json'))['state']=='greet' else 1)")" \
    "$(cat "$VOL/.squire/concierge-state.json" 2>/dev/null)"
check "concierge state carries the documented keys" \
    "$(yn python3 -c "import json; d=json.load(open('$VOL/.squire/concierge-state.json')); assert {'state','name','timezone','llm','updated'} <= set(d)")"
check "concierge state seeding was logged" "$(says "$out" 'seeded concierge onboarding state')"
check "concierge state is 0600" \
    "$([ "$(stat -c %a "$VOL/.squire/concierge-state.json" 2>/dev/null)" = 600 ] && echo 1 || echo 0)" \
    "$(stat -c %a "$VOL/.squire/concierge-state.json" 2>/dev/null)"

check ".env is a symlink"            "$(yn test -L "$VOL/.env")"
check "auth.json is a symlink"       "$(yn test -L "$VOL/auth.json")"
check ".env resolves into tmpfs"     "$([ "$(readlink "$VOL/.env")" = "$TMPFS/.env" ] && echo 1 || echo 0)" "$(readlink "$VOL/.env")"
check "sealed .env on volume"        "$(yn test -f "$VOL/.squire/secrets/.env.enc")"
check "durability assertion ran"     "$(says "$out" 'durability ok')"
check "pg0 root reported on volume"  "$(says "$out" "$VOL/.pg0")"

SEC1="$(sed -n 's/^TELEGRAM_WEBHOOK_SECRET=//p' "$TMPFS/.env")"
check "dev-fallback secret generated"  "$(says "$out" 'dev fallback')"
check "webhook secret is 64 hex chars" "$([ "${#SEC1}" = 64 ] && echo 1 || echo 0)" "len=${#SEC1}"

echo "  -- no plaintext secret in captured LOG OUTPUT --"
# Everything these processes print goes to Railway's log aggregator, which sits
# outside the tenant's isolation boundary. A secret echoed into a log is just as
# exposed as one written to the volume — arguably more so, since logs are
# retained, indexed and readable by anyone with project access.
check "webhook secret absent from boot logs" \
    "$([ "$(says "$out" "$SEC1")" = 0 ] && echo 1 || echo 0)"
check "DEK absent from boot logs" \
    "$([ "$(says "$out" "$DEK")" = 0 ] && echo 1 || echo 0)"
# The ingress URL embeds the bot id, which is half a bot token.
check "full webhook URL absent from boot logs" \
    "$([ "$(says "$out" 'ingress.example.com')" = 0 ] && echo 1 || echo 0)"

echo "  -- no plaintext secret anywhere on the volume --"
# The tmpfs dir lives outside $VOL, so any hit here is a real leak.
leak="$(grep -rl "$SEC1" "$VOL" 2>/dev/null | grep -v '^$' || true)"
check "secret value not present in any volume file" "$([ -z "$leak" ] && echo 1 || echo 0)" "$leak"
check "sealed blob is not plaintext" "$(yn bash -c "! grep -q '$SEC1' '$VOL/.squire/secrets/.env.enc'")"
check "sealed blob has magic header" "$(yn bash -c "head -c 6 '$VOL/.squire/secrets/.env.enc' | grep -q SQENC1")"

# Change-detection digests are SHA-256 of credential file contents — material
# derived from a secret. On the volume they would let someone holding a backup
# confirm a guessed key offline, without ever needing the DEK. Grepping for the
# secret's literal value cannot catch this, so assert the location directly.
check "no secret-derived digests on the volume" \
    "$([ -z "$(find "$VOL" -name '*.sha256' 2>/dev/null)" ] && echo 1 || echo 0)" \
    "$(find "$VOL" -name '*.sha256' 2>/dev/null | tr '\n' ' ')"
check "digests live on tmpfs instead" \
    "$([ -n "$(find "$TMPFS" -name '*.sha256' 2>/dev/null)" ] && echo 1 || echo 0)"

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

echo "== 3b. onboarding state survives, and never restarts, across boots =="
# Onboarding that resets on redeploy would re-ask someone's name after they had
# already given it — the exact "this thing is fake" moment the concierge exists
# to prevent. And onboarding that re-fires after `complete` would greet a
# week-old user as a stranger, which is worse. Both directions are asserted.

# Mid-flow: the agent has banked a name and moved to ask_timezone.
printf '{"state":"ask_timezone","name":"Ada","timezone":null,"llm":"trial"}' \
    > "$VOL/.squire/concierge-state.json"
rm -rf "${TMPFS:?}"/*
out="$(boot "$URL")"
check "mid-flow state survives a restart" \
    "$(yn python3 -c "import json,sys; d=json.load(open('$VOL/.squire/concierge-state.json')); sys.exit(0 if d['state']=='ask_timezone' and d['name']=='Ada' else 1)")" \
    "$(cat "$VOL/.squire/concierge-state.json")"
check "restart did not log a re-seed" \
    "$([ "$(says "$out" 'seeded concierge onboarding state')" = 0 ] && echo 1 || echo 0)"

# Finished: a completed tenant must never be dragged back through onboarding.
printf '{"state":"complete","name":"Ada","timezone":"Europe/London","llm":"anthropic_api_key"}' \
    > "$VOL/.squire/concierge-state.json"
rm -rf "${TMPFS:?}"/*
out="$(boot "$URL")"
check "completed state is not re-seeded" \
    "$(yn python3 -c "import json,sys; sys.exit(0 if json.load(open('$VOL/.squire/concierge-state.json'))['state']=='complete' else 1)")" \
    "$(cat "$VOL/.squire/concierge-state.json")"

# The nastiest case: a restored-from-backup or rolled-back volume that lost its
# init marker. seed_template correctly treats that as a first boot — but the
# tenant is emphatically NOT new, and re-greeting them would be a visible bug.
# This is what the second `-f` guard in the entrypoint buys.
rm -f "$VOL/.squire/initialized"
rm -rf "${TMPFS:?}"/*
out="$(boot "$URL")"
check "lost init marker still does not reset a completed tenant" \
    "$(yn python3 -c "import json,sys; sys.exit(0 if json.load(open('$VOL/.squire/concierge-state.json'))['state']=='complete' else 1)")" \
    "$(cat "$VOL/.squire/concierge-state.json")"
check "first-boot path really did re-run (guard was load-bearing)" \
    "$(says "$out" 'first boot')"

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
out="$(boot "$URL" TELEGRAM_WEBHOOK_SECRET=supplied-by-control-api)"
check "supplied secret used verbatim" \
    "$([ "$(sed -n 's/^TELEGRAM_WEBHOOK_SECRET=//p' "$TMPFS/.env")" = "supplied-by-control-api" ] && echo 1 || echo 0)"
check "logged as coming from control-api" "$(says "$out" 'control-api')"
check "supplied secret overrode the dev fallback" \
    "$([ "$(sed -n 's/^TELEGRAM_WEBHOOK_SECRET=//p' "$TMPFS/.env")" != "$SEC1" ] && echo 1 || echo 0)"

# Rotation: a new value from control-api must propagate, not be masked by the
# sealed copy of the old one.
rm -rf "${TMPFS:?}"/*
out="$(boot "$URL" TELEGRAM_WEBHOOK_SECRET=rotated-value)"
check "rotated secret propagates" \
    "$([ "$(sed -n 's/^TELEGRAM_WEBHOOK_SECRET=//p' "$TMPFS/.env")" = "rotated-value" ] && echo 1 || echo 0)"
check "no duplicate assignments in .env" \
    "$([ "$(grep -c '^TELEGRAM_WEBHOOK_SECRET=' "$TMPFS/.env")" = 1 ] && echo 1 || echo 0)"
check "user credential survived the rotation" "$(yn grep -q 'sk-ant-user-supplied' "$TMPFS/.env")"

echo "== 6. wrong DEK must refuse to boot, not re-initialise =="
rm -rf "${TMPFS:?}"/*
# Assignment prefix INSIDE the substitution, so the override is scoped to this
# one boot and does not leak into every later case.
out="$(SQUIRE_DEK_OVERRIDE="$DEK2" boot "$URL")"; rc=$?
assert_fatal "wrong DEK" "$out" "$rc" 'could not decrypt'
check "sealed .env NOT destroyed"    "$(yn test -f "$VOL/.squire/secrets/.env.enc")"
rm -rf "${TMPFS:?}"/*
out="$(boot "$URL")"; rc=$?
check "recovers with the correct DEK" "$(yn grep -q 'sk-ant-user-supplied' "$TMPFS/.env")"
check "recovery boot exits 0"         "$([ "$rc" -eq 0 ] && echo 1 || echo 0)" "rc=$rc"

echo "== 7. missing DEK must fail fast =="
out="$(raw_boot HOME="$VOL" HERMES_HOME="$VOL" SQUIRE_VOLUME="$VOL" \
        SQUIRE_ALLOW_EPHEMERAL_VOLUME=1)"; rc=$?
assert_fatal "missing SQUIRE_DEK" "$out" "$rc" 'SQUIRE_DEK missing'

echo "== 8. durability assertions =="
# HOME pointing anywhere but the volume means pg0 writes the PostgreSQL cluster
# to ephemeral container storage, and every redeploy silently destroys the
# tenant's memory. Must be fatal, and must say why.
out="$(raw_boot HOME="$ROOT/elsewhere" HERMES_HOME="$VOL" SQUIRE_VOLUME="$VOL" \
        SQUIRE_ALLOW_EPHEMERAL_VOLUME=1 SQUIRE_DEK="$DEK" TENANT_ID=t-boot-test)"; rc=$?
assert_fatal "HOME != volume" "$out" "$rc" 'is not the mounted volume'
check "HOME != volume names the real risk" "$(says "$out" 'long-term memory')"

# A provisioned tenant (TENANT_ID set) on storage that is not a mount point.
# The needle is deliberately 'Attach the Railway volume' and NOT 'not a mount
# point': the latter also appears in the non-fatal dev-mode warning below, so
# grepping it would pass even with this whole branch deleted.
out="$(raw_boot HOME="$VOL" HERMES_HOME="$VOL" SQUIRE_VOLUME="$VOL" \
        SQUIRE_DEK="$DEK" TENANT_ID=t-boot-test)"; rc=$?
assert_fatal "provisioned tenant + unmounted volume" "$out" "$rc" 'Attach the Railway volume'

# No TENANT_ID == local dev. Must warn, not die.
rm -rf "${TMPFS:?}"/*
out="$(raw_boot HOME="$VOL" HERMES_HOME="$VOL" SQUIRE_VOLUME="$VOL" \
        SQUIRE_DEK="$DEK")"; rc=$?
check "dev run without a volume warns and continues" "$(says "$out" 'state is EPHEMERAL')"
check "dev run still reaches supervisord" "$(says "$out" 'starting supervisord')"
check "dev run exits 0"                   "$([ "$rc" -eq 0 ] && echo 1 || echo 0)" "rc=$rc"

# The override must actually work, or operators cannot unblock themselves.
out="$(raw_boot HOME="$VOL" HERMES_HOME="$VOL" SQUIRE_VOLUME="$VOL" \
        SQUIRE_ALLOW_EPHEMERAL_VOLUME=1 SQUIRE_DEK="$DEK" TENANT_ID=t-boot-test)"; rc=$?
check "SQUIRE_ALLOW_EPHEMERAL_VOLUME overrides the hard fail" \
    "$([ "$rc" -eq 0 ] && echo 1 || echo 0)" "rc=$rc"

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
