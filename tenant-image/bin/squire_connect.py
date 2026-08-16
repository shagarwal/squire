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


def run_connected_pipeline(provider: str, hermes_home: str | None = None) -> None:
    """The conversion moment. Order matters: the model must point at the new
    provider BEFORE the gateway restarts (or it boots back onto the trial
    model), and the trial key is revoked only AFTER the tenant can serve
    without it."""
    switch_gateway_model(provider, hermes_home)
    restart_gateway()
    notify_control_api(provider)
