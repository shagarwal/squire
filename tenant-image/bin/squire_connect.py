#!/opt/squire/venv/bin/python
"""1C credential connect — nonce store, connect page, credential storage, pipeline.

Imported by squire-webhook-shim.py (serves GET/POST /connect/<nonce>) and by
the squire-llm-connect CLI (mints links, runs the device flow's storage side).

NONCE DISCIPLINE (mirrors squire_autopair's bind-nonce rules)
-------------------------------------------------------------
* 32 random bytes, urlsafe — unguessable, single-use, 15-minute expiry.
* Constant-time comparison (hmac.compare_digest on UTF-8 bytes) against every
  stored candidate, so a lookup's timing never narrows the search space.
* One FILE per nonce under $SQUIRE_STATE_DIR/connect-nonces/, written 0600 via
  the same same-directory-tempfile + fsync + os.replace discipline as
  squire_autopair._atomic_write_0600. Files, not one shared JSON store, because
  the minter (CLI process) and the consumer (shim process) are different
  processes: consumption is an atomic os.rename to a .used name, which cannot
  lose a mark to a concurrent mint the way a read-modify-write of a shared
  file could.
* NEVER log a nonce, a credential, or any part of either.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
import time

NONCE_TTL_SECONDS = 900  # 15 minutes, matching the design spec
NONCE_BYTES = 32


def _default_state_dir() -> str:
    state_dir = os.environ.get("SQUIRE_STATE_DIR")
    if not state_dir:
        home = os.environ.get("HERMES_HOME") or "/opt/data"
        state_dir = os.path.join(home, ".squire")
    return state_dir


def _nonce_dir(state_dir: str | None) -> str:
    return os.path.join(state_dir or _default_state_dir(), "connect-nonces")


def _atomic_write_0600(path: str, data: bytes) -> None:
    """Same discipline as squire_autopair: same-dir temp, 0600 from creation,
    fsync, atomic replace, no partial file left on any failure path."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".connect-tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mint_nonce(state_dir: str | None = None) -> str:
    """Mint a fresh single-use connect nonce and persist it. Returns the nonce.

    Also prunes: expired unused nonces are deleted (they can never be found
    again), and .used audit markers older than a day are cleaned up so the
    directory cannot grow without bound.
    """
    directory = _nonce_dir(state_dir)
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass

    now = time.time()
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            if name.endswith(".used"):
                if now - os.path.getmtime(path) > 86400:
                    os.unlink(path)
            elif name.endswith(".json"):
                created = json.loads(open(path, "rb").read()).get("created", 0)
                if now - float(created) > NONCE_TTL_SECONDS:
                    os.unlink(path)
        except (OSError, ValueError):
            continue  # pruning is best-effort; a stray file must not block minting

    nonce = secrets.token_urlsafe(NONCE_BYTES)
    filename = f"{time.time_ns()}-{secrets.token_hex(4)}.json"
    _atomic_write_0600(
        os.path.join(directory, filename),
        json.dumps({"nonce": nonce, "created": now}).encode("utf-8"),
    )
    return nonce


def find_nonce(state_dir: str | None, candidate: str) -> str | None:
    """Return the matching nonce file's path if `candidate` is live, else None.

    Scans EVERY stored nonce and compares each with hmac.compare_digest — the
    loop deliberately does not short-circuit shape checks per candidate, so a
    miss costs the same as a hit (constant-time discipline, same as autopair).
    Expired entries are treated as absent.
    """
    if not candidate:
        return None
    directory = _nonce_dir(state_dir)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    now = time.time()
    matched: str | None = None
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            record = json.loads(open(path, "rb").read())
            stored = str(record.get("nonce") or "")
            created = float(record.get("created") or 0)
        except (OSError, ValueError):
            continue
        fresh = (now - created) <= NONCE_TTL_SECONDS
        # Compare every candidate regardless of freshness so timing does not
        # reveal how many live nonces exist; only a FRESH match counts.
        if hmac.compare_digest(candidate.encode("utf-8"), stored.encode("utf-8")) and fresh:
            matched = path
    return matched


def consume_nonce(state_dir: str | None, candidate: str) -> bool:
    """Atomically mark `candidate` used. True exactly once per nonce.

    os.rename is atomic on one filesystem, so of two racing consumers exactly
    one wins; the loser gets FileNotFoundError and reports False.
    """
    path = find_nonce(state_dir, candidate)
    if path is None:
        return False
    try:
        os.rename(path, path + ".used")
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Connect page (served by the webhook shim). Fully self-contained: inline CSS,
# no JS, no external assets — the page must render with the strictest CSP and
# without a single extra network fetch. Only the TWO API-key paths live here;
# the ChatGPT-subscription path is a device-code flow that runs entirely in
# chat + on the provider's own site (design decision 2), and there is NO
# Claude-subscription path at all (OAuth spike: NO-GO, prohibited + enforced).
# ---------------------------------------------------------------------------

_PAGE_STYLE = """
  body { font-family: system-ui, sans-serif; max-width: 26rem; margin: 8vh auto;
         padding: 0 1rem; color: #1a1a1a; background: #fafafa; }
  h1 { font-size: 1.3rem; } p { line-height: 1.5; }
  label { display: block; margin: 1rem 0 0.25rem; font-weight: 600; }
  input[type=password] { width: 100%; padding: 0.5rem; font-size: 1rem;
         border: 1px solid #bbb; border-radius: 6px; }
  .radio { margin: 0.35rem 0; font-weight: 400; }
  button { margin-top: 1.25rem; padding: 0.6rem 1.4rem; font-size: 1rem;
         border: 0; border-radius: 6px; background: #2456d6; color: #fff; }
  .note { font-size: 0.85rem; color: #555; margin-top: 1.5rem; }
  .error { color: #b00020; font-weight: 600; }
"""


def render_connect_page(nonce: str, error: str = "") -> str:
    """The one-time key hand-off form. `error` is OUR OWN fixed copy only —
    never provider response text and never user input (no reflection)."""
    error_html = f'<p class="error">{error}</p>' if error else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Connect your AI account</title>
<style>{_PAGE_STYLE}</style></head><body>
<h1>Connect your AI account</h1>
<p>This page is served by <strong>your own Squire runtime</strong> — the key you
paste goes straight to your agent's container over TLS and nowhere else. The
link is single-use and expires 15 minutes after it was minted.</p>
{error_html}
<form method="post" action="/connect/{nonce}" autocomplete="off">
  <label>Which key are you pasting?</label>
  <div class="radio"><label><input type="radio" name="provider" value="openai" checked>
    OpenAI API key (starts with <code>sk-</code>, from platform.openai.com)</label></div>
  <div class="radio"><label><input type="radio" name="provider" value="anthropic">
    Anthropic API key (starts with <code>sk-ant-api</code>, from console.anthropic.com)</label></div>
  <label for="api_key">Your API key</label>
  <input type="password" id="api_key" name="api_key" required minlength="20">
  <button type="submit">Connect</button>
</form>
<p class="note">We check the key with one cheap request to your provider before
saving it, store it encrypted on your agent's own volume, and your AI traffic
then goes directly to your provider.</p>
</body></html>"""


def render_invalid_page() -> str:
    """Friendly invalid/expired/used state — deliberately identical for all
    three causes so the page cannot be used to probe which nonces exist."""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Link expired</title>
<style>{_PAGE_STYLE}</style></head><body>
<h1>This link isn't live any more</h1>
<p>Connect links are single-use and expire after 15 minutes — this one has
either been used or timed out. Nothing is wrong with your account.</p>
<p><strong>Just ask your agent for a fresh link</strong> in your Telegram chat
and tap it straight away.</p>
</body></html>"""


def render_done_page(provider: str) -> str:
    label = {"openai": "OpenAI", "anthropic": "Anthropic"}.get(provider, provider)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Connected</title>
<style>{_PAGE_STYLE}</style></head><body>
<h1>Done — {label} is connected 🎉</h1>
<p>Your key was verified and stored, encrypted, on your agent's own volume.</p>
<p><strong>Head back to Telegram</strong> — your agent will confirm there in a
moment.</p>
</body></html>"""


# ---------------------------------------------------------------------------
# Provider key validation — one cheap authenticated GET per provider.
# URLs are env-overridable ONLY so the Docker-free tests can fake the
# provider; production never sets these variables.
# ---------------------------------------------------------------------------
import urllib.error
import urllib.request

VALIDATE_TIMEOUT = 20.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every 3xx during validation.

    urllib's default opener transparently FOLLOWS redirects and re-sends the
    Authorization / x-api-key header to the target — potentially a different
    host. For a request whose whole purpose is to carry the user's secret key,
    that is a credential leak. Returning None from redirect_request makes urllib
    stop and raise the 3xx as an HTTPError instead of chasing it, so the key is
    never re-sent. A redirect is then treated as a reachable-failure.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


# One opener, reused: default handlers minus redirect-following.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _validate_urls() -> dict:
    return {
        "openai": os.environ.get(
            "SQUIRE_CONNECT_OPENAI_VALIDATE_URL", "https://api.openai.com/v1/models"
        ),
        "anthropic": os.environ.get(
            "SQUIRE_CONNECT_ANTHROPIC_VALIDATE_URL", "https://api.anthropic.com/v1/models"
        ),
    }


def validate_key(provider: str, key: str) -> tuple[bool, str]:
    """(ok, user_facing_message). The message is OUR fixed copy — provider
    response bodies are never reflected to the page (no injection surface)."""
    urls = _validate_urls()
    if provider not in urls:
        return False, "Unknown provider."
    if provider == "openai":
        headers = {"Authorization": f"Bearer {key}"}
    else:
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    request = urllib.request.Request(urls[provider], headers=headers, method="GET")
    try:
        # No-redirect opener: a 3xx must NEVER cause the key to be re-sent to a
        # redirect target (see _NoRedirect). It raises the 3xx as an HTTPError.
        with _NO_REDIRECT_OPENER.open(request, timeout=VALIDATE_TIMEOUT) as response:
            if 200 <= response.status < 300:
                return True, ""
            return False, "The provider didn't accept that key."
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "The provider didn't accept that key — check it and try again."
        # 3xx (redirect we refused) or any other unexpected status: reachable-failure.
        return False, "The provider couldn't be reached just now — try again in a minute."
    except (urllib.error.URLError, OSError):
        return False, "The provider couldn't be reached just now — try again in a minute."


# ---------------------------------------------------------------------------
# Credential storage. $HERMES_HOME/.env and auth.json are SYMLINKS into tmpfs
# (squire-entrypoint.sh §4): every write resolves the link first, because an
# atomic replace of the symlink path would swap the link for a plaintext file
# ON THE VOLUME — the one thing this architecture forbids. secrets-sync then
# seals the tmpfs change and re-wires Hindsight; nothing here duplicates that.
# ---------------------------------------------------------------------------


def _assert_tmpfs_target(env_path: str) -> None:
    """Fail closed unless the write will land on tmpfs, never the volume.

    $HERMES_HOME/.env is normally a SYMLINK the entrypoint points into tmpfs, so
    a credential write follows the link off the persistent volume. But if .env
    is a real file (or absent, so realpath returns the volume path), writing the
    key there would put PLAINTEXT ON THE VOLUME — the one outcome this
    architecture forbids. The entrypoint always makes the symlink first, so this
    is latent; this guard makes the code refuse to rely on that silently.

    Safe iff: env_path is itself a symlink (the tmpfs shape), OR its resolved
    target is OUTSIDE $HERMES_HOME (i.e. already on the tmpfs mount, not the
    volume). Anything else — a plain file resolving inside the volume — is
    refused with a clear error the caller turns into a friendly message.
    """
    if os.path.islink(env_path):
        return  # the expected tmpfs symlink shape
    hermes_home = os.environ.get("HERMES_HOME") or "/opt/data"
    target = os.path.realpath(env_path)
    home_real = os.path.realpath(hermes_home)
    try:
        inside_volume = os.path.commonpath([target, home_real]) == home_real
    except ValueError:
        # Different roots/drives => not under the volume => on some other mount.
        inside_volume = False
    if inside_volume:
        raise RuntimeError("refusing to write credential to non-tmpfs path")


def env_upsert(env_path: str, updates: dict) -> None:
    """Idempotent last-assignment-wins upsert, same semantics as the
    entrypoint's env_put, written to the RESOLVED target atomically (0600)."""
    _assert_tmpfs_target(env_path)  # fail closed before touching the target
    target = os.path.realpath(env_path)
    try:
        with open(target, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    kept = [
        line for line in lines
        if line.split("=", 1)[0] not in updates
    ]
    for key, value in updates.items():
        kept.append(f"{key}={value}")
    _atomic_write_0600(target, ("\n".join(kept) + "\n").encode("utf-8"))


def env_remove(env_path: str, keys) -> None:
    """Strip `keys` from the tenant's .env, leaving every other line untouched.

    The counterpart to env_upsert, and used by exactly one caller: the ChatGPT
    connect path, which until 2026-08-16 wrote OPENAI_API_KEY=<oauth token> and
    OPENAI_BASE_URL=<codex backend>. Those two made hermes route a ChatGPT
    subscription down the OpenAI API-key path; a tenant that connected under the
    old code still has them on disk, so fixing auth.json alone would not fix the
    tenant.

    No-ops when the file is absent or carries none of the keys — a rewrite that
    is not needed is a rewrite that can only go wrong.
    """
    keys = set(keys)
    target = os.path.realpath(env_path)
    try:
        with open(target, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return
    kept = [line for line in lines if line.split("=", 1)[0] not in keys]
    if kept == lines:
        return
    _assert_tmpfs_target(env_path)  # fail closed before touching the target
    _atomic_write_0600(target, ("\n".join(kept) + "\n").encode("utf-8"))


ANTHROPIC_DIRECT_BASE_URL = "https://api.anthropic.com"
# There is deliberately no CHATGPT_CODEX_BASE_URL any more. Pointing
# OPENAI_BASE_URL at the codex backend was one of the three wiring errors fixed
# on 2026-08-16: hermes selects that backend itself once auth.json names the
# openai-codex provider, and an explicit base URL only misroutes the gateway.


def store_api_key(provider: str, key: str, hermes_home: str | None = None) -> None:
    """Write the user's own key into the tenant's .env.

    Anthropic also gets an explicit direct base URL: the tenant's process env
    still carries the trial proxy's ANTHROPIC_BASE_URL, and .env must override
    it as a whole configuration (the exact per-source rule
    squire-hindsight-env.sh documents) so the user's key is never paired with
    our proxy.
    """
    home = hermes_home or os.environ.get("HERMES_HOME") or "/opt/data"
    env_path = os.path.join(home, ".env")
    if provider == "anthropic":
        env_upsert(env_path, {
            "ANTHROPIC_API_KEY": key,
            "ANTHROPIC_BASE_URL": ANTHROPIC_DIRECT_BASE_URL,
        })
    elif provider == "openai":
        env_upsert(env_path, {"OPENAI_API_KEY": key})
    else:
        raise ValueError(f"store_api_key: unknown provider {provider!r}")


# ---------------------------------------------------------------------------
# Connected pipeline (shared by the /connect page and the device-flow CLI):
#   store credential (caller already did) -> switch the gateway model ->
#   restart the gateway -> tell control-api so the trial key is revoked.
# Every step is independently non-fatal: a stored credential with a failed
# notify is reconciled by the heartbeat backstop; failing the whole pipeline
# would leave the USER's success page lying.
# ---------------------------------------------------------------------------
import re
import subprocess


#: Hermes's provider name for a ChatGPT subscription (see squire_codex_device).
CHATGPT_HERMES_PROVIDER = "openai-codex"


def _gateway_model_for(provider: str) -> str:
    """Env-overridable so a model rename never needs an image rebuild.

    anthropic keeps the trial's model id — same model, now direct + unmetered.
    chatgpt is a BARE SLUG, not a `provider:model` string: it goes into the
    `default:` key of the model mapping below, with the provider named
    separately. Valid slugs for a ChatGPT account today are gpt-5.4, gpt-5.5,
    gpt-5.4-mini and gpt-5.3-codex; upstream lists gpt-5.2-codex,
    gpt-5.1-codex-max and gpt-5.1-codex-mini as backend-REJECTED for ChatGPT
    accounts, so never default to one of those.
    """
    defaults = {
        "openai": "openai:gpt-4.1",
        "chatgpt": "gpt-5.4",
        "anthropic": "anthropic:claude-sonnet-5",
    }
    env_names = {
        "openai": "SQUIRE_CONNECT_MODEL_OPENAI",
        "chatgpt": "SQUIRE_CONNECT_MODEL_CHATGPT",
        "anthropic": "SQUIRE_CONNECT_MODEL_ANTHROPIC",
    }
    return os.environ.get(env_names.get(provider, ""), "") or defaults[provider]


def _model_yaml_block(provider: str) -> str:
    """The replacement for config.yaml's `model:` entry.

    ChatGPT subscriptions need a MAPPING, not a scalar. Live 2026-08-16: we
    wrote `model: "openai:gpt-5-codex"`, which hermes does not resolve to its
    openai-codex provider at all — it fell through to OpenRouter and every
    message came back "Missing Authentication header". Verified against a
    known-working hermes install.

    Deliberately NOT written here: `api_mode` (hermes forces codex_responses for
    this provider), `base_url` and `model.api_key` (the credential lives in
    auth.json), and `model.openai_runtime` (that opt-in needs a `codex` binary
    the image does not ship).
    """
    if provider == "chatgpt":
        return ('model:\n'
                f'  provider: "{CHATGPT_HERMES_PROVIDER}"\n'
                f'  default: "{_gateway_model_for(provider)}"')
    return f'model: "{_gateway_model_for(provider)}"'


#: The `model:` entry INCLUDING any indented mapping lines under it. Matching
#: the continuation lines matters in both directions: writing a scalar over the
#: first line of a mapping would strand `  provider:` / `  default:` under it,
#: which is invalid YAML and stops the gateway booting at all.
_MODEL_ENTRY_RE = re.compile(r'(?m)^model:[^\n]*(?:\n[ \t]+[^\n]*)*')


def switch_gateway_model(provider: str, hermes_home: str | None = None) -> bool:
    """Anchored rewrite of the `model:` entry ONLY — the same targeted
    discipline as the concierge hook's timezone command, and for the same
    reason: config.yaml also carries the hooks block that makes onboarding
    work, and a YAML round-trip is how that gets silently dropped."""
    home = hermes_home or os.environ.get("HERMES_HOME") or "/opt/data"
    config_path = os.path.join(home, "config.yaml")
    block = _model_yaml_block(provider)
    try:
        text = open(config_path, "r", encoding="utf-8").read()
        # A lambda replacement, not a string: a literal would let a backslash or
        # a \g in a model slug be read as a group reference.
        new_text = _MODEL_ENTRY_RE.sub(lambda _m: block, text, count=1)
        if new_text != text:
            _atomic_write_0600(os.path.realpath(config_path), new_text.encode("utf-8"))
        return True
    except OSError:
        print("[squire-connect] could not rewrite config.yaml model line", flush=True)
        return False


def restart_gateway() -> bool:
    """supervisorctl restart gateway. Shutdown notices are already silenced
    (SQUIRE_SHUTDOWN_NOTICES image default 0), so this is invisible in chat;
    the agent's celebration message doubles as the 'we're back' signal."""
    ctl = os.environ.get("SQUIRE_SUPERVISORCTL", "/opt/squire/venv/bin/supervisorctl")
    conf = os.environ.get("SQUIRE_SUPERVISORD_CONF", "/opt/squire/supervisord.conf")
    try:
        result = subprocess.run([ctl, "-c", conf, "restart", "gateway"],
                                capture_output=True, timeout=180, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        print("[squire-connect] gateway restart failed (will pick up on next boot)", flush=True)
        return False


def notify_control_api(provider: str) -> bool:
    """POST /internal/llm-connected {tenant_id, provider} with the internal
    bearer. Provider NAME only — never credential material. Non-fatal: the
    heartbeat's llm_connected marker is the reconciliation backstop."""
    base = (os.environ.get("CONTROL_API_URL") or "").rstrip("/")
    token = os.environ.get("INTERNAL_API_TOKEN") or ""
    tenant_id = os.environ.get("TENANT_ID") or ""
    if not (base and token and tenant_id):
        return False
    request = urllib.request.Request(
        f"{base}/internal/llm-connected",
        data=json.dumps({"tenant_id": tenant_id, "provider": provider}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError):
        print("[squire-connect] llm-connected call failed; heartbeat backstop will reconcile", flush=True)
        return False


# ---------------------------------------------------------------------------
# Proactive owner celebration.
#
# The connect happens OUTSIDE any chat turn — in the detached squire-llm-connect
# device-flow poller, or in the /connect POST handler's background thread. So
# the agent never gets a turn in which to notice "you're connected" and tell the
# user; before this, the user had to message the bot ("check again") to find out
# it had happened. The fix: the moment the credential lands, push the celebration
# straight to the owner's DM.
#
# MECHANISM — `hermes send` (NOT a raw Telegram call, NOT cron):
#   `hermes send --to "telegram:<chat_id>" "<text>"` sends as the bot to a
#   specific chat, runs NO LLM turn, and MIRRORS the text into the matching
#   gateway session as an ASSISTANT turn — so the agent remembers having said it
#   (no fake user message, no transcript pollution). The CLI is on PATH in the
#   tenant container. Confirmed upstream at tools/send_message_tool.py +
#   hermes_cli/send_cmd.py.
#
# The owner's chat_id, for a Telegram DM, IS the owner's Telegram user id — which
# squire_autopair wrote into the approved-owner store when the tenant was bound.
# We read it back through autopair's own helpers rather than hand-parsing.
# ---------------------------------------------------------------------------

#: Human-facing provider labels for the celebration copy. Anything not listed
#: falls back to the raw id (defensive; the three live paths are all covered).
_PROVIDER_LABELS = {"openai": "OpenAI", "anthropic": "Anthropic", "chatgpt": "ChatGPT"}


def _celebration_text(provider: str) -> str:
    """Warm 'you're connected' copy for `provider`, mirroring the concierge
    `connected` state's INTENT (state-machine.yaml + the hook's
    _DIRECTIVES["connected"]): confirm which provider is live, say plainly and
    truthfully that their key is stored encrypted on their own volume and their
    AI traffic now goes directly to their provider, that the built-in
    allowance's caps no longer apply — then ask exactly ONE question, their
    timezone/location.

    Style rules (formatting block of state-machine.yaml): short lines, **bold**
    only (hermes converts standard markdown -> MarkdownV2), NEVER underscores.
    No trial/pricing pitch and no operator jargon — this is the user's warm
    conversion moment, not an ops notice.
    """
    label = _PROVIDER_LABELS.get(provider, provider)
    return (
        f"**{label} is connected.** 🎉\n"
        "\n"
        "Your key is stored encrypted on your own private volume — only your "
        "agent can reach it.\n"
        "\n"
        "From now on your messages run straight through your own account, so "
        "the built-in allowance's caps no longer apply to you.\n"
        "\n"
        'Where are you (or just your timezone)? So I get "tomorrow morning" '
        "right."
    )


def _resolve_owner_chat_id(hermes_home: str | None) -> str | None:
    """The owner's Telegram chat_id from the autopair approved-owner store, or
    None when the tenant is somehow still unbound (shouldn't happen post-connect,
    but this must be safe rather than raise). For a Telegram DM the chat_id is
    the owner's user id, which is exactly the KEY autopair stores the record
    under — so the store's single key is the chat_id."""
    home = hermes_home or os.environ.get("HERMES_HOME") or "/opt/data"
    try:
        import squire_autopair  # bin/ is on sys.path via the shim/CLI importer
        approved = squire_autopair.load_approved(squire_autopair.approved_path(home))
    except Exception:
        return None
    if not isinstance(approved, dict) or not approved:
        return None
    # Autopair binds exactly one owner; take the first (only) key deterministically.
    return next(iter(approved))


def _concierge_state_path(hermes_home: str | None) -> str:
    """The concierge state file, resolved the SAME way squire-concierge-hook.py
    resolves it: SQUIRE_STATE_DIR if set, else <hermes_home>/.squire."""
    state_dir = os.environ.get("SQUIRE_STATE_DIR")
    if not state_dir:
        home = hermes_home or os.environ.get("HERMES_HOME") or "/opt/data"
        state_dir = os.path.join(home, ".squire")
    return os.path.join(state_dir, "concierge-state.json")


def _advance_concierge_after_connect(provider: str, hermes_home: str | None) -> None:
    """Advance the concierge flow past `connected` WITHOUT double-celebrating.

    The celebration already happened via `hermes send` above, so we set the
    stored step to the one that FOLLOWS `connected` in the state machine —
    `ask_timezone` (see state-machine.yaml flow order) — and, if the file
    already carries an `llm` key, point it at the provider that just connected.
    All other keys are preserved (merge, not overwrite). Atomic 0600 write, same
    discipline as every other write in this module. Absent/corrupt state file =>
    skip silently: this is non-fatal onboarding polish, never a hard dependency.
    """
    state_file = _concierge_state_path(hermes_home)
    try:
        raw = json.loads(open(state_file, "r", encoding="utf-8").read())
    except (OSError, ValueError):
        return  # no state file, unreadable, or not JSON — nothing to advance
    if not isinstance(raw, dict):
        return
    raw["state"] = "ask_timezone"  # the step after connected
    if "llm" in raw:
        raw["llm"] = provider
    try:
        _atomic_write_0600(state_file, json.dumps(raw).encode("utf-8"))
    except OSError:
        # Advancing the flow is best-effort: a failed write just means the hook
        # re-runs the `connected` directive, which is recoverable, not fatal.
        pass


#: Where the real hermes CLI lives. NOT bare "hermes", and NOT
#: /opt/hermes/bin/hermes: supervisord.conf spells out why for [program:gateway]
#: — /opt/hermes/bin is a docker-exec privilege-drop shim that goes first on
#: PATH, and a supervisord-spawned process inherits supervisord's own frozen
#: environment, which need not contain it at all. This function runs from the
#: DETACHED device-flow poller (a supervisord grandchild), so bare "hermes"
#: raised FileNotFoundError and the celebration silently never sent — live on
#: 2026-08-16. Use the venv binary supervisord itself uses.
HERMES_BIN_DEFAULT = "/opt/hermes/.venv/bin/hermes"


def _hermes_binary() -> str:
    """The venv hermes if it is there, else whatever PATH offers (dev boxes)."""
    if os.path.exists(HERMES_BIN_DEFAULT):
        return HERMES_BIN_DEFAULT
    import shutil
    return shutil.which("hermes") or HERMES_BIN_DEFAULT


def _env_file_values(hermes_home: str | None = None) -> dict:
    """Parse $HERMES_HOME/.env into a dict, following the tmpfs symlink.

    Same `KEY=VALUE`, last-assignment-wins shape env_upsert writes. Blank lines,
    `#` comments and lines without `=` are skipped; a single layer of matching
    surrounding quotes is stripped. Best-effort: a missing or unreadable file
    yields {} rather than raising, because every caller is on a non-fatal path.
    """
    home = hermes_home or os.environ.get("HERMES_HOME") or "/opt/data"
    target = os.path.realpath(os.path.join(home, ".env"))
    values: dict = {}
    try:
        with open(target, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _send_env(hermes_home: str | None = None) -> dict:
    """The environment `hermes send` needs, rebuilt from the CURRENT .env.

    WHY THIS EXISTS — the second half of the supervisord frozen-environment
    trap. HERMES_BIN_DEFAULT above fixed the binary NAME not resolving; this
    fixes the CREDENTIALS not resolving, and it bit live on 2026-08-19: the
    celebration failed with

        hermes send: Platform 'telegram' is not configured.

    while the very same command run by hand in the container worked fine.

    The reason is ordering. This code runs in a thread of the webhook shim
    ([program:webhook], priority 20), which supervisord starts with the frozen
    environment supervisord itself booted with — captured BEFORE secrets-sync
    (priority 25) seals the platform credentials into tmpfs. So the inherited
    env never contains them, no matter how long the tenant has been up.
    squire-hindsight-env.sh solves exactly this for Hindsight by re-sourcing
    .env on every restart; `hermes send` needs the same treatment, so we read
    .env at call time and let it WIN over the stale inherited values.

    HOME and HERMES_HOME are pinned last: `hermes send` documents its config as
    `~/.hermes/.env + ~/.hermes/config.yaml`, so a process that inherited some
    other HOME would look in the wrong place entirely.
    """
    home = hermes_home or os.environ.get("HERMES_HOME") or "/opt/data"
    env = os.environ.copy()
    env.update(_env_file_values(home))  # fresh creds beat the frozen ones
    env["HERMES_HOME"] = home
    env["HOME"] = home
    return env


def _record_celebration_failure(hermes_home: str | None, detail: str) -> None:
    """Leave a breadcrumb a human can actually find.

    The device-flow poller is detached with stdout/stderr on DEVNULL, so a
    print() here goes nowhere — which is exactly why the first live failure took
    a container autopsy to explain. Record the reason next to the connect status
    file instead. Best-effort: never raise, never block the pipeline.
    """
    try:
        state_dir = os.environ.get("SQUIRE_STATE_DIR") or os.path.join(
            hermes_home or os.environ.get("HERMES_HOME") or "/opt/data", ".squire"
        )
        os.makedirs(state_dir, exist_ok=True)
        _atomic_write_0600(
            os.path.join(state_dir, "celebration-error.json"),
            json.dumps({"error": detail[:500], "at": time.time()}).encode("utf-8"),
        )
    except Exception:  # noqa: BLE001 -- a breadcrumb must never break the flow
        pass
    print("[squire-connect] proactive celebration send failed; "
          "owner can still message the bot", flush=True)


def notify_owner_connected(provider: str, hermes_home: str | None = None) -> bool:
    """Proactively tell the owner "you're connected" and continue the flow.

    Runs OUTSIDE any chat turn (detached poller / connect handler), so it pushes
    the message itself via `hermes send`, which also mirrors the text into the
    gateway session as an assistant turn (the agent remembers saying it). Then it
    advances the concierge state so the flow does not re-celebrate.

    Returns True iff the send succeeded (rc == 0). Every failure path is
    non-fatal and returns False — a failed proactive send must never fail the
    connect pipeline; the user can still message the bot.

    NEVER logs the chat_id or any credential.
    """
    chat_id = _resolve_owner_chat_id(hermes_home)
    if not chat_id:
        # Unbound post-connect shouldn't happen, but is not worth failing over.
        print("[squire-connect] no bound owner; skipping proactive celebration", flush=True)
        return False

    text = _celebration_text(provider)
    hermes_bin = os.environ.get("SQUIRE_HERMES_BIN") or _hermes_binary()
    try:
        result = subprocess.run(
            [hermes_bin, "send", "--to", f"telegram:{chat_id}", text],
            capture_output=True, timeout=30, check=False,
            env=_send_env(hermes_home),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _record_celebration_failure(hermes_home, f"{type(exc).__name__}: {exc}")
        return False
    if result.returncode != 0:
        # stderr is OUR OWN send tool's diagnostics (never credential material):
        # the text we send is the celebration copy and the only argument is a
        # chat id, so recording a trimmed tail is safe and is the only way to
        # ever see why a detached-poller send failed.
        detail = (result.stderr or b"")[-300:].decode("utf-8", "replace").strip()
        _record_celebration_failure(hermes_home, f"rc={result.returncode} {detail}")
        return False

    # Only once the celebration is actually out do we advance the flow past it.
    _advance_concierge_after_connect(provider, hermes_home)
    return True


def run_connected_pipeline(provider: str, hermes_home: str | None = None) -> None:
    """The conversion moment. Order matters, and two constraints fix it:

      1. the model must point at the new provider BEFORE the gateway restarts
         (or it boots back onto the trial model), and
      2. the trial key is revoked only AFTER the tenant can serve without it —
         so notify_control_api stays behind restart_gateway.

    notify_owner_connected goes SECOND, ahead of the restart, and is non-fatal:
    the proactive DM is a nicety on top of an already-complete conversion, and a
    failed send must not fail the pipeline (a stored credential with a missed
    celebration is still a connected tenant).

    WHY SECOND, not last. `hermes send` needs no running gateway for a bot-token
    platform like Telegram — it uses the bot token directly. Sending it after
    restart_gateway() therefore bought nothing and cost ~45s of dead air:
    supervisorctl does not return until the new gateway clears startsecs=45, so
    the owner sat watching an idle chat for the most important 45 seconds of the
    product. Worse, it made the celebration hostage to gateway health — when the
    gateway went FATAL live on 2026-08-19 the message could never be sent at
    all. Sending first makes it land in ~2s and survive a restart that fails.

    The restart still happens right behind it, and the shim answers 503 while it
    runs, which ingress retries — so a reply typed into that window is delayed,
    never dropped."""
    switch_gateway_model(provider, hermes_home)
    notify_owner_connected(provider, hermes_home)
    restart_gateway()
    notify_control_api(provider)
