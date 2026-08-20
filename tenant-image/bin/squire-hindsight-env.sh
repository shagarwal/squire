#!/usr/bin/env bash
#
# squire-hindsight-env.sh — derive Hindsight's LLM credentials, once, in one place.
#
# WHY THIS EXISTS
# ---------------
# Hindsight needs an LLM for extraction and consolidation. Which one changes
# during a tenant's life, and the change is invisible:
#
#   trial      -> our metered proxy key (ANTHROPIC_BASE_URL + trial key)
#   conversion -> the user's own key, written into .env by the connect flow,
#                 at which point the TRIAL KEY IS REVOKED.
#
# Before this script, the daemon was wired once at boot and never again. So the
# moment a user converted — the single most valuable event in the product — the
# daemon kept calling a revoked key, every extraction 401'd, and memory silently
# stopped accumulating for exactly the customers who had just paid. Nothing
# crashed; the agent simply, gradually, stopped remembering.
#
# So: the derivation lives here, both the entrypoint (boot) and
# squire-secrets-sync.sh (on every .env change) call it, and the daemon reads
# the result through squire-hindsight-run.sh. supervisord programs inherit
# supervisord's environment at start, so a plain `restart` would NOT pick up new
# values — the wrapper re-sourcing this file is what makes a restart effective.
#
# Usage: squire-hindsight-env.sh [--quiet]
# Exit:  0 unchanged · 10 changed (caller should restart the daemon) · 1 error

set -uo pipefail

TMPFS_DIR="${SQUIRE_SECRETS_TMPFS:-/dev/shm/squire}"
ENV_FILE="$TMPFS_DIR/.env"
OUT_FILE="${SQUIRE_HINDSIGHT_ENV_FILE:-$TMPFS_DIR/hindsight-llm.env}"

# ChatGPT (Codex OAuth) wiring. Since the 2026-08-16 connect fix, a ChatGPT
# subscription leaves NO key in .env at all: the OAuth tokens live only in
# hermes's auth.json envelope ($TMPFS_DIR/auth.json, sealed by secrets-sync).
# Before this script learned to read it, a ChatGPT-converted tenant looked
# "unconnected" here and fell through to the PROCESS env — i.e. the REVOKED
# trial key, 401 on every extraction: the exact silent failure this script
# exists to prevent, reintroduced for the one path both live conversions used.
#
# Hindsight consumes Codex credentials from $CODEX_HOME/auth.json in the
# upstream Codex-CLI format, which is deliberately NOT hermes's envelope
# format. So we derive a translated copy into tmpfs. THE LOAD-BEARING DETAIL:
# the derived file carries the access_token but NEVER the refresh_token.
# OAuth refresh tokens rotate on use; hermes's auth layer (locking,
# credential_pool sync) is the single owner of that rotation, and a second
# independent refresher racing it can invalidate the whole token family and
# log the user out of ChatGPT. Without a refresh_token hindsight runs in its
# documented "one-shot" degraded mode: it uses the access token until expiry,
# and freshness comes from the OUTSIDE loop — hermes refreshes its envelope,
# secrets-sync sees auth.json change and re-runs this script, the derived
# copy updates, exit 10 restarts the daemon.
CODEX_AUTH_FILE="${SQUIRE_CODEX_AUTH_FILE:-$TMPFS_DIR/auth.json}"
CODEX_HOME_DIR="${SQUIRE_CODEX_HOME_DIR:-$TMPFS_DIR/codex-home}"
PYBIN="${SQUIRE_PYTHON:-}"
if [ -z "$PYBIN" ]; then
    if [ -x /opt/squire/venv/bin/python ]; then
        PYBIN=/opt/squire/venv/bin/python
    else
        PYBIN="$(command -v python3 || true)"
    fi
fi

quiet=false
[ "${1:-}" = "--quiet" ] && quiet=true
log() { [ "$quiet" = true ] || printf '[squire-hindsight-env] %s\n' "$*"; }

# PRECEDENCE IS PER-SOURCE, NOT PER-KEY. This is the whole correctness argument.
# -----------------------------------------------------------------------------
# The obvious implementation — "for each key, take .env if present, else the
# process env" — is wrong, and wrong in a way that silently breaks half the
# conversion paths:
#
#   Trial:      process env has ANTHROPIC_API_KEY (trial) + ANTHROPIC_BASE_URL
#               (our metering proxy). .env has no provider key.
#   User connects OpenAI (or ChatGPT/Codex — 2 of the 4 offered paths):
#               .env now holds OPENAI_API_KEY. The process env STILL holds the
#               trial ANTHROPIC_API_KEY until the next redeploy.
#
# Per-key fallback would then find an Anthropic key (from the stale process env),
# take the Anthropic branch, and configure Hindsight with the REVOKED trial key
# — the exact failure this script was written to fix, reintroduced through the
# back door. A per-key rule would also happily pair the user's own key with our
# proxy's base URL, routing their traffic back through our infrastructure after
# we told them it never would again.
#
# So: .env and the process env are treated as two whole, mutually exclusive
# configurations. If .env supplies ANY provider credential, the tenant has
# connected their own LLM and .env is the ONLY source consulted — keys and base
# URLs together. Otherwise we are still on the provisioned trial config.
file_get() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | tail -n 1; }

# CLAUDE_CODE_OAUTH_TOKEN counts as "the tenant has connected something" even
# though Hindsight cannot use it (see below). Without it in this list, a Claude
# Max tenant would fall through to the process env and land right back on the
# revoked trial key.
CONNECTED_MARKERS="ANTHROPIC_API_KEY OPENAI_API_KEY CLAUDE_CODE_OAUTH_TOKEN"

# Set only by the codex probe below; must exist on every path (set -u).
CODEX_SHA=""

tenant_connected=false
for marker in $CONNECTED_MARKERS; do
    if [ -n "$(file_get "$marker")" ]; then
        tenant_connected=true
        break
    fi
done

if [ "$tenant_connected" = true ]; then
    source_label=".env (tenant's own provider)"
    ANTHROPIC_KEY="$(file_get ANTHROPIC_API_KEY)"
    ANTHROPIC_BASE="$(file_get ANTHROPIC_BASE_URL)"
    OPENAI_KEY="$(file_get OPENAI_API_KEY)"
    OPENAI_BASE="$(file_get OPENAI_BASE_URL)"
    OAUTH_ONLY_TOKEN="$(file_get CLAUDE_CODE_OAUTH_TOKEN)"
else
    # No .env marker. Before concluding "still on trial", check for a ChatGPT
    # connection: it lives in auth.json only (see header). Precedence is
    # therefore .env keys > Codex auth.json > process env (trial) — the codex
    # probe MUST sit before the trial fallback or ChatGPT tenants get the
    # revoked trial key.
    CODEX_SHA=""
    if [ -f "$CODEX_AUTH_FILE" ] && [ -n "$PYBIN" ]; then
        # The python helper does the whole codex side in one parse: pick the
        # FRESHEST access token (largest JWT exp) across the providers
        # singleton AND hermes's credential_pool (hermes persists routine
        # refreshes into the pool, so the singleton alone can be stale), then
        # atomically (re)write the derived Codex-CLI-format file — WITHOUT the
        # refresh_token, per the header. Prints "ok <sha256>" or "none".
        CODEX_RESULT="$("$PYBIN" - "$CODEX_AUTH_FILE" "$CODEX_HOME_DIR" <<'PYEOF' 2>/dev/null
import base64, hashlib, json, os, sys, tempfile

auth_file, home_dir = sys.argv[1], sys.argv[2]

def jwt_exp(token):
    # Best-effort exp decode purely for "which token is freshest" ordering.
    # An unparseable token still counts as a candidate, just the oldest one.
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0.0

try:
    with open(auth_file) as f:
        doc = json.load(f)
except Exception:
    print("none"); sys.exit(0)

candidates = []
entry = ((doc.get("providers") or {}).get("openai-codex") or {})
tok = entry.get("tokens") or {}
if tok.get("access_token"):
    candidates.append(tok)
for pool_entry in ((doc.get("credential_pool") or {}).get("openai-codex") or []):
    if not isinstance(pool_entry, dict):
        continue
    ptok = pool_entry.get("tokens") or pool_entry
    if isinstance(ptok, dict) and ptok.get("access_token"):
        candidates.append(ptok)

if not candidates:
    print("none"); sys.exit(0)

best = max(candidates, key=lambda t: jwt_exp(t.get("access_token", "")))
derived = {
    "auth_mode": "chatgpt",  # required verbatim by hindsight's CodexAuthManager
    "tokens": {
        # NO refresh_token, ever — hermes is the sole refresher (see header).
        "access_token": best["access_token"],
        "account_id": best.get("account_id") or "",
    },
}
blob = json.dumps(derived, indent=2).encode()

os.makedirs(home_dir, mode=0o700, exist_ok=True)
target = os.path.join(home_dir, "auth.json")
try:
    with open(target, "rb") as f:
        unchanged = f.read() == blob
except Exception:
    unchanged = False
if not unchanged:
    fd, tmp = tempfile.mkstemp(dir=home_dir)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
print("ok " + hashlib.sha256(blob).hexdigest())
PYEOF
)" || CODEX_RESULT="none"
        case "$CODEX_RESULT" in
            ok\ *) CODEX_SHA="${CODEX_RESULT#ok }" ;;
        esac
    fi

    if [ -n "$CODEX_SHA" ]; then
        source_label="auth.json (ChatGPT subscription via Codex OAuth)"
        ANTHROPIC_KEY=""; ANTHROPIC_BASE=""
        OPENAI_KEY=""; OPENAI_BASE=""
        OAUTH_ONLY_TOKEN=""
    else
        source_label="process env (provisioned trial)"
        ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-}"
        ANTHROPIC_BASE="${ANTHROPIC_BASE_URL:-}"
        OPENAI_KEY="${OPENAI_API_KEY:-}"
        OPENAI_BASE="${OPENAI_BASE_URL:-}"
        OAUTH_ONLY_TOKEN=""
    fi
fi

provider=""; model=""; key=""; base=""

if [ -n "$ANTHROPIC_KEY" ]; then
    provider="anthropic"
    # Haiku-class on purpose: this runs on EVERY retained turn in the
    # background. Using the conversation model here would multiply the tenant's
    # bill for work the user never sees.
    model="${HINDSIGHT_LLM_MODEL_ANTHROPIC:-claude-haiku-4-5}"
    key="$ANTHROPIC_KEY"
    base="$ANTHROPIC_BASE"
elif [ -n "$OPENAI_KEY" ]; then
    provider="openai"
    model="${HINDSIGHT_LLM_MODEL_OPENAI:-gpt-4.1-mini}"
    key="$OPENAI_KEY"
    base="$OPENAI_BASE"
elif [ -n "$CODEX_SHA" ]; then
    # ChatGPT subscription. Hindsight's openai-codex provider authenticates
    # from $CODEX_HOME/auth.json (the derived file we just wrote) — there is
    # no API key to hand it. Mini-class model for the same reason the other
    # branches pin Haiku-class: this runs on every retained turn.
    provider="openai-codex"
    model="${HINDSIGHT_LLM_MODEL_CODEX:-gpt-5.4-mini}"
    key=""
    base=""
fi

# A tenant that moved OFF codex (connected a plain key later) must not leave
# usable OAuth material lying around in tmpfs for a daemon that no longer
# reads it.
if [ "$provider" != "openai-codex" ]; then
    rm -f "$CODEX_HOME_DIR/auth.json" 2>/dev/null || true
fi

tmp="$(mktemp "${OUT_FILE}.XXXXXX")" || exit 1
chmod 0600 "$tmp"
{
    echo "# Generated by squire-hindsight-env.sh — do not edit."
    if [ -n "$provider" ]; then
        echo "HINDSIGHT_API_LLM_PROVIDER=$provider"
        echo "HINDSIGHT_API_LLM_MODEL=$model"
        # codex has no key: auth comes from $CODEX_HOME/auth.json instead.
        [ -n "$key" ] && echo "HINDSIGHT_API_LLM_API_KEY=$key"
        [ -n "$base" ] && echo "HINDSIGHT_API_LLM_BASE_URL=$base"
    fi
    if [ "$provider" = "openai-codex" ]; then
        echo "CODEX_HOME=$CODEX_HOME_DIR"
        # Not consumed by hindsight — this line exists so the cmp below sees
        # access-token rotations (the derived auth.json is a separate file)
        # and turns them into an exit-10 restart.
        echo "SQUIRE_CODEX_AUTH_SHA=$CODEX_SHA"
    fi
    # --- Embeddings on the tenant's own provider (Gate G1, 2026-08-20) ------
    # Principle: memory text may only be embedded by a service that ALREADY
    # sees the tenant's conversations — their connected LLM provider — never a
    # third party of ours. Anthropic has no embeddings API, so anthropic and
    # trial tenants keep the local model (RAM instead of disclosure).
    # Dimensions pinned to 384 = the local bge-small dimension: hindsight
    # refuses to boot on a dimension change over a non-empty vector table, so
    # 384 is what makes flipping providers mid-life safe. (text-embedding-3
    # supports truncation to 384.)
    if [ "$provider" = "openai-codex" ]; then
        echo "HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai-codex"
        echo "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS=384"
    elif [ "$provider" = "openai" ] && [ -z "$base" ]; then
        # Only for a PLAIN OpenAI key. A custom OPENAI_BASE_URL means an
        # OpenAI-compatible endpoint that may well not serve /v1/embeddings —
        # fail toward the local model, not toward runtime embedding errors.
        echo "HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai"
        echo "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY=$key"
        echo "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS=384"
    fi
} > "$tmp"

# Compare before replacing so an unchanged pass is a genuine no-op and cannot
# trigger a pointless daemon restart (which would discard in-flight extraction).
if [ -f "$OUT_FILE" ] && cmp -s "$tmp" "$OUT_FILE"; then
    rm -f "$tmp"
    exit 0
fi

mv "$tmp" "$OUT_FILE"
chmod 0600 "$OUT_FILE"

if [ -n "$provider" ]; then
    # Never log the key. Provider/model/base/source only — enough to debug a
    # misconfiguration, useless to anyone reading the log aggregator.
    log "LLM config changed: provider=$provider model=$model base=${base:-<default>} source=$source_label"
elif [ -n "$OAUTH_ONLY_TOKEN" ]; then
    # Known, honest limitation. Claude Max hands us an OAuth token rather than
    # an API key: CLAUDE_CODE_OAUTH_TOKEN is not an Anthropic API key and
    # hindsight-api has no OAuth path for it. (ChatGPT/Codex used to share
    # this fate but no longer does — since 2026-08-20 the codex probe above
    # derives a $CODEX_HOME/auth.json and takes the openai-codex branch.)
    #
    # The important part is what we do NOT do: fall back to the trial key. It is
    # revoked, so that would mean an hour of 401s instead of a clear idle state.
    # Extraction pauses, recall of existing memories keeps working, and the
    # concierge's fallback ladder can ask for an API key.
    log "tenant connected via Claude Max OAuth only — Hindsight has no usable credential."
    log "  Extraction is IDLE (recall of existing memories still works). Deliberate:"
    log "  falling back to the revoked trial key would 401 on every turn instead."
else
    log "no LLM credentials available ($source_label) — Hindsight extraction idle until one is connected"
fi
exit 10
