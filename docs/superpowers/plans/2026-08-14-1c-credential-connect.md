# 1C — Credential Connect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every tenant can connect its owner's own LLM account through one of THREE live paths — OpenAI API key, ChatGPT subscription (Codex device-code flow), Anthropic API key — with credentials never transiting chat or shared infrastructure, and the trial LiteLLM key revoked the moment a connection lands.

**Architecture:** The tenant's existing webhook shim (stdlib `ThreadingHTTPServer` on `$PORT`) grows a Host-gated `/connect/<nonce>` page for the two API-key paths; a new `squire-llm-connect` CLI is the agent's only interface (mint link, run the Codex device flow, poll status) and never prints credential material; a shared connected-pipeline stores the credential in the tmpfs `.env`/`auth.json` (sealed by the existing secrets-sync loop), switches the gateway model, restarts the gateway, and calls a new control-api `POST /internal/llm-connected` which records the provider and revokes the trial key. Provisioning gains a `serviceDomainCreate` step so each tenant has a public Railway domain, and the heartbeat gains a boolean connected-marker as the revocation backstop.

**Tech Stack:** Python 3 stdlib only on the tenant side (http.server, urllib, hmac, secrets, json — matching the shim/autopair style); FastAPI + SQLModel + httpx/respx on control-api; Railway GraphQL (`serviceDomainCreate`); no new dependencies anywhere.

**There is NO Claude-subscription path.** The Claude OAuth spike verdict is NO-GO (`docs/superpowers/specs/2026-08-14-spike-claude-oauth.md`): Anthropic's Consumer ToS prohibits subscription OAuth in third-party products and it is enforced server-side. Anthropic = API key only. A separate branch (`fix/drop-claude-sub-option`, v0.1.9 hotfix) removes the live `claude_max_oauth` concierge option; **Task 10 of this plan assumes that branch has already landed** and must not re-do or duplicate its work.

**Specs this plan implements:**
- `docs/superpowers/specs/2026-08-14-1c-credential-connect-design.md` (approved design; Decisions section = 3 paths)
- `docs/superpowers/specs/2026-08-14-spike-codex-device-flow.md` (GO-WITH-CAVEATS; all device-flow constants come from its table)

**Branch setup (before Task 1):**

```bash
git fetch origin main
git checkout -b feat/1c-credential-connect origin/main
# Task 10 requires the Claude-drop hotfix. If it has merged, this is a no-op;
# if it has not, merge its branch first and resolve before starting Task 10:
git merge --no-edit origin/fix/drop-claude-sub-option || true
```

---

## Key design decisions (locked in — do not re-decide during execution)

1. **Nonce store = one file per nonce** under `$SQUIRE_STATE_DIR/connect-nonces/`, because the shim (consumer) and the CLI (minter) are *different processes*: consumption is an atomic `os.rename(... -> .used)`, which cannot race a concurrent mint the way a single shared JSON file could. Lookup always scans every candidate with `hmac.compare_digest` (constant-time per the autopair discipline). TTL 15 minutes.
2. **Credential writes go to the resolved symlink target.** `$HERMES_HOME/.env` and `$HERMES_HOME/auth.json` are symlinks into tmpfs (`squire-entrypoint.sh` §4). An atomic `os.replace()` on the symlink *path* would replace the symlink with a plaintext file **on the volume** — the exact thing the architecture forbids. Every write helper therefore writes to `os.path.realpath(path)`. The existing `squire-secrets-sync.sh` loop then seals the change and re-wires Hindsight; we never duplicate that machinery.
3. **Host gating is fail-closed**: when a public domain is configured (`RAILWAY_PUBLIC_DOMAIN` or `SQUIRE_PUBLIC_DOMAIN` env), any request whose `Host` is **not** recognisably private (`*.railway.internal`, an IP literal, `localhost`, empty, or Railway's `healthcheck.railway.app`) may reach `/connect/*` ONLY; everything else answers 403. With no public domain configured (dev, every existing test) behaviour is unchanged. The shim today never reads `Host` — this is net-new via `self.headers.get("Host")`.
4. **Device flow: re-implement the 4 auth calls; do NOT bake the codex binary.** The spike gives exact request/response shapes for all four HTTP calls, they are plain JSON/form POSTs, and baking a Rust CLI into the image for *login only* is heavy. The spike's "run real Codex" recommendation concerns *serving* traffic (headers/attestation drift); serving compatibility is verified in the Task 12 rollout checks, with the OpenAI-API-key path as the no-dead-end fallback if the ChatGPT backend rejects hermes-shaped requests.
5. **Provider wire names** (used by the page, the CLI, the pipeline, control-api, and the concierge hand-off): `"openai"` (API key), `"anthropic"` (API key), `"chatgpt"` (Codex device flow). Concierge menu ids stay `openai_api_key` / `openai_codex_oauth` / `anthropic_api_key`.
6. **Public domain reaches the container two ways (belt and braces).** Railway documents `RAILWAY_PUBLIC_DOMAIN` as an auto-injected runtime variable once a service domain exists, injected *at deploy time*. Our new `CREATE_DOMAIN` provisioning step runs before `DEPLOY`, so the first deploy should already carry it — but Railway docs were unreachable from the planning sandbox, so we do not bet the feature on injection: `_step_set_variables` also writes the domain explicitly as `SQUIRE_PUBLIC_DOMAIN` (read back from the Railway API, names→values never persisted in the DB). Tenant code prefers `RAILWAY_PUBLIC_DOMAIN`, falls back to `SQUIRE_PUBLIC_DOMAIN`. Verify the injection claim on staging during Task 12 and record the answer in the deploy notes.
7. **DB changes are manual ALTER TABLE** (no migrations exist). Per the deploy-ordering gotcha: a `git push` to main auto-deploys control-api, so both ALTERs (Task 7 + Task 9) run against the live DB **before** the branch merges to main. Exact SQL and ordering in Task 12.

---

## File Structure

**Created (tenant image):**
- `tenant-image/bin/squire_connect.py` — nonce store (mint/find/consume), connect-page HTML rendering, provider key validation, `.env`/`auth.json` credential writes, connected pipeline (model switch → gateway restart → control-api call). Imported by the shim and the CLI.
- `tenant-image/bin/squire_codex_device.py` — the 4 Codex device-flow HTTP calls (usercode, poll, exchange, refresh), JWT claim decoding, plan gating, `auth.json` shape, refresh policy. Pure functions, injectable opener.
- `tenant-image/bin/squire-llm-connect` — agent-facing CLI: `mint-link`, `status`, `start openai-device`, `refresh openai-device`, hidden `_poll` daemon. Prints only URLs/codes/states, never credentials.
- `tenant-image/tests/test_connect.py` — nonce lifecycle, page GET/POST through the real shim (faked provider endpoints), pipeline ordering. Own harness (no pytest), mirrors `test_webhook_shim.py`.
- `tenant-image/tests/test_llm_connect_cli.py` — device flow against a fake issuer (404-not-enabled, pending→grant, plan gate, exchange shape, refresh rotation), CLI output hygiene.

**Modified (tenant image):**
- `tenant-image/bin/squire-webhook-shim.py` — Host gating; `/connect/<nonce>` GET/POST routing; per-IP attempt backoff.
- `tenant-image/bin/squire-heartbeat.py` — `llm_connected` collector (env-marker names + auth.json artifact) added to the payload whitelist.
- `tenant-image/tests/test_webhook_shim.py` — adversarial Host-gating section.
- `tenant-image/tests/test_heartbeat.py` — `llm_connected` field expectations.
- `tenant-image/home-template/skills/concierge/state-machine.yaml` — 3 live paths, CLI-driven connect states, celebration.
- `tenant-image/bin/squire-concierge-hook.py` — rewritten connect directives; `_COMING_SOON_PROVIDERS` emptied.
- `tenant-image/tests/test_concierge_onboarding.py` — drift assertions for the rewritten flow.
- `.github/workflows/tenant-image.yml` — register the two new test files.

**Modified (control-api):**
- `apps/control-api/src/control_api/models.py` — `Tenant.connected_provider`, `Heartbeat.llm_connected`, `ProvisionStep.CREATE_DOMAIN`.
- `apps/control-api/src/control_api/schemas.py` — `LlmConnectedRequest/Response`, `HeartbeatRequest.llm_connected`.
- `apps/control-api/src/control_api/provisioning.py` — `record_llm_connected`, `_step_create_domain`, `SQUIRE_PUBLIC_DOMAIN` variable, heartbeat reconciliation.
- `apps/control-api/src/control_api/routers/internal.py` — `POST /internal/llm-connected`.
- `apps/control-api/src/control_api/clients/railway.py` — `get_service_domain`, `create_service_domain`.
- `apps/control-api/tests/railway_fake.py` — `domains` query + `serviceDomainCreate` handlers.
- `apps/control-api/tests/test_privacy_schema.py` — whitelist additions (`connected_provider`, `llm_connected`).
- `apps/control-api/tests/test_internal_api.py` — endpoint tests.
- `apps/control-api/tests/test_state_machine.py` — domain-step tests + step-order pin updates.
- `apps/control-api/tests/test_cross_service_contracts.py` — llm-connected contract pin.

---

### Task 1: Nonce store + mint (`squire_connect.py`)

**Files:**
- Create: `tenant-image/bin/squire_connect.py`
- Create: `tenant-image/tests/test_connect.py`

- [ ] **Step 1: Write the failing tests (nonce lifecycle section)**

Create `tenant-image/tests/test_connect.py` (mode 755, shebang — the CI exec-bit check requires it):

```python
#!/usr/bin/env python3
"""Functional tests for the 1C credential-connect flow — runs WITHOUT Docker.

Covers, in order: the nonce store (single-use, 15-min TTL, constant-time,
atomic 0600), the /connect page served by the real shim (GET + POST against
faked provider validation endpoints), and the connected pipeline's ordering.

Usage: python3 tenant-image/tests/test_connect.py
Exit:  0 all assertions pass · 1 otherwise
"""
import json
import os
import pathlib
import stat
import sys
import tempfile
import time

IMAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(IMAGE_ROOT / "bin"))

import squire_connect  # noqa: E402

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


print("== nonce store ==")
state_dir = tempfile.mkdtemp()

nonce = squire_connect.mint_nonce(state_dir)
check("mint returns a urlsafe nonce of useful length", isinstance(nonce, str) and len(nonce) >= 40, nonce)

nonce_dir = pathlib.Path(state_dir) / "connect-nonces"
files = list(nonce_dir.glob("*.json"))
check("mint persisted exactly one nonce file", len(files) == 1, files)
if files:
    mode = stat.S_IMODE(files[0].stat().st_mode)
    check("nonce file is 0600", mode == 0o600, oct(mode))
    on_disk = json.loads(files[0].read_text())
    check("file holds the nonce and a created stamp",
          on_disk.get("nonce") == nonce and isinstance(on_disk.get("created"), float), on_disk)

check("a fresh nonce is found", squire_connect.find_nonce(state_dir, nonce) is not None)
check("a wrong nonce is not found", squire_connect.find_nonce(state_dir, "x" * 43) is None)
check("empty candidate is not found", squire_connect.find_nonce(state_dir, "") is None)

check("consume succeeds once", squire_connect.consume_nonce(state_dir, nonce) is True)
check("a consumed nonce is no longer found", squire_connect.find_nonce(state_dir, nonce) is None)
check("consume is not repeatable", squire_connect.consume_nonce(state_dir, nonce) is False)
check("the used marker file remains for audit",
      len(list(nonce_dir.glob("*.used"))) == 1, list(nonce_dir.iterdir()))

# Expiry: rewrite a minted nonce's created stamp into the past.
old = squire_connect.mint_nonce(state_dir)
old_file = next(p for p in nonce_dir.glob("*.json")
                if json.loads(p.read_text())["nonce"] == old)
payload = json.loads(old_file.read_text())
payload["created"] = time.time() - squire_connect.NONCE_TTL_SECONDS - 1
old_file.write_text(json.dumps(payload))
check("an expired nonce is not found", squire_connect.find_nonce(state_dir, old) is None)
check("an expired nonce cannot be consumed", squire_connect.consume_nonce(state_dir, old) is False)

# Two concurrent links may be outstanding; each is independently single-use.
a = squire_connect.mint_nonce(state_dir)
b = squire_connect.mint_nonce(state_dir)
check("two outstanding nonces coexist",
      squire_connect.find_nonce(state_dir, a) is not None
      and squire_connect.find_nonce(state_dir, b) is not None)
squire_connect.consume_nonce(state_dir, a)
check("consuming one leaves the other valid", squire_connect.find_nonce(state_dir, b) is not None)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL CONNECT TESTS PASS")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 tenant-image/tests/test_connect.py`
Expected: `ModuleNotFoundError: No module named 'squire_connect'`

- [ ] **Step 3: Write the nonce store**

Create `tenant-image/bin/squire_connect.py` (mode 755):

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 tenant-image/tests/test_connect.py`
Expected: every `PASS` line, exit 0, `ALL CONNECT TESTS PASS`

- [ ] **Step 5: Record exec bits and commit**

```bash
git update-index --add --chmod=+x tenant-image/bin/squire_connect.py tenant-image/tests/test_connect.py
git add tenant-image/bin/squire_connect.py tenant-image/tests/test_connect.py
git commit -m "1C: single-use connect-nonce store (mint/find/consume, 15-min TTL)"
```

---

### Task 2: Host gating in the shim (lands before the page)

**Files:**
- Modify: `tenant-image/bin/squire-webhook-shim.py`
- Modify: `tenant-image/tests/test_webhook_shim.py`

The shim today reads only `self.path` — `Host` is never consulted. Attaching a public Railway domain points internet traffic at this port, so the webhook and health paths must refuse any request that did not arrive over the private network. Gating is keyed on the presence of a configured public domain, so every existing dev/test flow (no domain) is untouched.

- [ ] **Step 1: Write the failing adversarial tests**

Append to `tenant-image/tests/test_webhook_shim.py`, immediately BEFORE the final `print()` / `if failures:` block at the end of the file:

```python
print("== Host gating: a public domain must not expose the webhook ==")
# Attaching a Railway public domain (1C connect page) points the INTERNET at
# this port. A forged Telegram update delivered via the public domain is the
# account-takeover / message-injection path, so when a public domain is
# configured, /webhook/telegram and /health answer ONLY for private-looking
# Hosts (railway.internal, IP literals, localhost). /connect/* is the single
# public surface. With no public domain configured (every section above),
# nothing changes.
import http.client  # noqa: E402

PUB = "tenant-test.up.railway.app"


def raw_request(method, path, body=None, host=None, extra=None):
    """http.client so we control the Host header exactly (urllib rewrites it)."""
    conn = http.client.HTTPConnection("127.0.0.1", SHIM_PORT, timeout=10)
    headers = {"Content-Type": "application/json"}
    if host is not None:
        headers["Host"] = host
    headers.update(extra or {})
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Length"] = str(len(data))
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, payload

adapter6 = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), FakeAdapter)
adapter6.daemon_threads = True
threading.Thread(target=adapter6.serve_forever, daemon=True).start()
received.clear()
proc = run({"SQUIRE_TELEGRAM_UPSTREAM_PATH": UPSTREAM_PATH,
            "SQUIRE_PUBLIC_DOMAIN": PUB})
try:
    update = {"update_id": 5000, "message": {"text": "forged"}}

    # The attack: a Telegram-shaped POST arriving with the public domain's Host.
    status, _ = raw_request("POST", "/webhook/telegram", update, host=PUB)
    check("public Host: webhook POST -> 403", status == 403, status)
    check("public Host: nothing was forwarded upstream", len(received) == 0, received)

    # Even WITH valid delivery credentials: the public edge is not ingress.
    status, _ = raw_request("POST", "/webhook/telegram", update, host=PUB,
                            extra={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    check("public Host + valid secret: still 403", status == 403, status)
    check("still nothing forwarded", len(received) == 0, received)

    # Port suffix and case must not dodge the gate.
    status, _ = raw_request("POST", "/webhook/telegram", update, host=PUB.upper() + ":443")
    check("public Host with port/case: still 403", status == 403, status)

    # An unknown public-looking Host fails closed too.
    status, _ = raw_request("POST", "/webhook/telegram", update, host="evil.example.com")
    check("unknown non-private Host: 403 (fail closed)", status == 403, status)

    # /health follows the webhook: private only, once a public domain exists.
    status, _ = raw_request("GET", "/health", host=PUB)
    check("public Host: /health -> 403", status == 403, status)

    # The gate counts rejections so the fleet can see probing.
    _, metrics = get("/metrics")
    check("public-Host webhook posts counted as rejected",
          metrics.get("updates_rejected", 0) >= 3, metrics)

    # Private-looking Hosts keep working: this is how ingress and Railway's
    # private network actually address the shim.
    status, _ = raw_request("POST", "/webhook/telegram",
                            {"update_id": 5001, "message": {"text": "hello"}},
                            host="tenant-abc.railway.internal:8080")
    check("railway.internal Host: webhook delivers", status == 200, status)
    status, _ = raw_request("GET", "/health", host="tenant-abc.railway.internal")
    check("railway.internal Host: /health 200", status == 200, status)
    status, _ = raw_request("GET", "/health", host="127.0.0.1:18080")
    check("IP-literal Host: /health 200", status == 200, status)
    status, _ = raw_request("GET", "/health", host="healthcheck.railway.app")
    check("Railway healthcheck Host: /health 200", status == 200, status)
finally:
    proc.terminate()
    proc.wait(timeout=10)
    adapter6.shutdown()
    adapter6.server_close()
```

- [ ] **Step 2: Run to verify the new section fails**

Run: `python3 tenant-image/tests/test_webhook_shim.py`
Expected: earlier sections PASS; the new section FAILs on `public Host: webhook POST -> 403` (currently 200 — the shim ignores Host).

- [ ] **Step 3: Implement Host gating**

In `tenant-image/bin/squire-webhook-shim.py`, add after the `REQUIRE_AUTH = ...` block (module level):

```python
# --- Public-domain Host gating (1C) -----------------------------------------
# Attaching a Railway public domain (for the /connect page) exposes this port
# to the internet. Everything except /connect/* must then answer ONLY for
# requests that addressed the shim by a private name. Railway injects
# RAILWAY_PUBLIC_DOMAIN once a domain exists; provisioning also writes
# SQUIRE_PUBLIC_DOMAIN explicitly so this gate cannot depend on platform
# injection timing. Unset (dev, plain docker run) => no gating at all.
PUBLIC_DOMAIN = (
    os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("SQUIRE_PUBLIC_DOMAIN") or ""
).strip().lower()


def _request_host(headers) -> str:
    """The Host header, lowercased, port stripped. '[::1]:80' handled too."""
    host = (headers.get("Host") or "").strip().lower()
    if host.startswith("["):  # bracketed IPv6
        return host.split("]", 1)[0].lstrip("[")
    return host.rsplit(":", 1)[0] if ":" in host else host


def _host_is_private(headers) -> bool:
    """True when the request addressed us by a name only the private network,
    the container itself, or Railway's own health prober would use.

    Fail-closed: with a public domain configured, an unrecognised Host is
    treated as public. An attacker reaching the public edge cannot make the
    edge route an arbitrary Host to this container, but this code must not
    rely on that — 'not provably private' is the safe reading.
    """
    host = _request_host(headers)
    if host in ("", "localhost", "healthcheck.railway.app"):
        return True
    if host.endswith(".railway.internal"):
        return True
    try:
        ipaddress.ip_address(host)
        return True  # IP literal: private-network or loopback addressing
    except ValueError:
        return False
```

In `do_GET`, insert as the FIRST lines of the method (before the `/health` check):

```python
        # Public-domain gate: with a domain attached, only /connect/* is public.
        if PUBLIC_DOMAIN and not _host_is_private(self.headers):
            if not self.path.split("?", 1)[0].startswith("/connect/"):
                log("rejected public-Host GET outside /connect")
                self._respond(403, {"error": "forbidden"})
                return
```

In `do_POST`, insert as the FIRST lines of the method (before `path != WEBHOOK_PATH`):

```python
        path = self.path.split("?", 1)[0]

        # Public-domain gate (see do_GET). A forged Telegram update arriving
        # via the public domain — even with a stolen delivery secret — is the
        # injection path this exists to close. Counted so probing is visible.
        if PUBLIC_DOMAIN and not _host_is_private(self.headers):
            if not path.startswith("/connect/"):
                log("rejected public-Host POST outside /connect")
                count("updates_rejected")
                self._respond(403, {"error": "forbidden"})
                return
```

…and delete the now-duplicate `path = self.path.split("?", 1)[0]` line that previously opened `do_POST`.

- [ ] **Step 4: Run the full shim suite**

Run: `python3 tenant-image/tests/test_webhook_shim.py`
Expected: `ALL SHIM TESTS PASS` (all prior sections unaffected — they run without a public domain).

- [ ] **Step 5: Commit**

```bash
git add tenant-image/bin/squire-webhook-shim.py tenant-image/tests/test_webhook_shim.py
git commit -m "1C: Host-gate the shim — public domain reaches /connect only"
```

---

### Task 3: GET /connect/<nonce> — the one-time page

**Files:**
- Modify: `tenant-image/bin/squire_connect.py` (page rendering)
- Modify: `tenant-image/bin/squire-webhook-shim.py` (route)
- Modify: `tenant-image/tests/test_connect.py` (shim-served page section)

- [ ] **Step 1: Write the failing tests**

Append to `tenant-image/tests/test_connect.py`, before the final `if failures:` block:

```python
print("== GET /connect/<nonce> through the real shim ==")
import http.client      # noqa: E402
import subprocess       # noqa: E402
import urllib.request   # noqa: E402

SHIM = str(IMAGE_ROOT / "bin" / "squire-webhook-shim.py")
SHIM_PORT = 18081
PUB = "tenant-test.up.railway.app"


def shim_get(path, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", SHIM_PORT, timeout=10)
    headers = {"Host": host} if host else {}
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    ctype = resp.getheader("Content-Type") or ""
    conn.close()
    return resp.status, ctype, body


def start_shim(home):
    env = dict(os.environ)
    env.update({
        "PORT": str(SHIM_PORT),
        "HERMES_HOME": home,
        "SQUIRE_STATE_DIR": os.path.join(home, ".squire"),
        "SQUIRE_PUBLIC_DOMAIN": PUB,
        "TELEGRAM_WEBHOOK_SECRET": "s3cr3t",
        "TENANT_ID": "t-connect-test",
    })
    proc = subprocess.Popen([sys.executable, SHIM], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{SHIM_PORT}/health", timeout=2)
            return proc
        except Exception:
            time.sleep(0.25)
    raise AssertionError("shim never became ready")


home = tempfile.mkdtemp()
page_state = os.path.join(home, ".squire")
live = squire_connect.mint_nonce(page_state)
proc = start_shim(home)
try:
    status, ctype, body = shim_get(f"/connect/{live}", host=PUB)
    check("live nonce -> 200 HTML page", status == 200 and "text/html" in ctype, (status, ctype))
    check("page offers the OpenAI key path", "OpenAI" in body, body[:400])
    check("page offers the Anthropic key path", "Anthropic" in body, body[:400])
    check("page never offers a Claude subscription path",
          "subscription" not in body.lower() and "setup-token" not in body.lower(), body[:400])
    check("page posts back to the same nonce", f"/connect/{live}" in body, body[:400])
    check("page is self-contained (no external assets)",
          "http://" not in body and "https://" not in body
          and "<script src" not in body and "<link" not in body, body[:400])

    status, _, body = shim_get("/connect/definitely-not-a-nonce", host=PUB)
    check("invalid nonce -> 200 friendly page, not an error dump",
          status == 200 and "ask your" in body.lower(), (status, body[:300]))
    check("invalid page mentions a fresh link", "fresh link" in body.lower(), body[:300])

    # GET must NOT consume — the user may reload before submitting.
    status, _, _ = shim_get(f"/connect/{live}", host=PUB)
    check("reloading the page keeps the nonce live", status == 200
          and squire_connect.find_nonce(page_state, live) is not None, status)

    # The page is reachable from the private side too (wake path goes via edge,
    # but nothing about the page should REQUIRE the public Host).
    status, _, _ = shim_get(f"/connect/{live}")
    check("connect page also answers without a public Host", status == 200, status)
finally:
    proc.terminate()
    proc.wait(timeout=10)
    out = proc.stdout.read() if proc.stdout else ""
    check("nonce never appears in shim logs", live not in out, out[-300:])
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tenant-image/tests/test_connect.py`
Expected: nonce section PASSes; the new section fails with `live nonce -> 200 HTML page` (shim answers 404 — no route yet).

- [ ] **Step 3: Add page rendering to `squire_connect.py`**

Append to `tenant-image/bin/squire_connect.py`:

```python
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
```

- [ ] **Step 4: Route it in the shim**

In `tenant-image/bin/squire-webhook-shim.py`, extend the by-path import block (where `squire_autopair` is imported) with a second guarded import:

```python
try:
    import squire_connect
except ImportError:  # pragma: no cover - defensive; page 404s, webhook unaffected
    squire_connect = None
```

Add next to `_respond` in `Handler`:

```python
    def _respond_html(self, status: int, html: str):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)
```

In `do_GET`, after the public-domain gate and before the `/health` check:

```python
        path = self.path.split("?", 1)[0]
        if path.startswith("/connect/"):
            # One-time credential hand-off page (1C). GET never consumes the
            # nonce — the user may reload before submitting.
            if squire_connect is None:
                self._respond(404, {"error": "not found"})
                return
            candidate = path[len("/connect/"):]
            if squire_connect.find_nonce(None, candidate) is None:
                # Same friendly page for invalid/expired/used — never an error
                # dump, and never an oracle for which nonces exist.
                self._respond_html(200, squire_connect.render_invalid_page())
                return
            self._respond_html(200, squire_connect.render_connect_page(candidate))
            return
```

(Then change the two existing `self.path.split("?", 1)[0]` comparisons in `do_GET` to use the new `path` local.)

- [ ] **Step 5: Run both suites**

Run: `python3 tenant-image/tests/test_connect.py && python3 tenant-image/tests/test_webhook_shim.py`
Expected: both end with their ALL-PASS line.

- [ ] **Step 6: Commit**

```bash
git add tenant-image/bin/squire_connect.py tenant-image/bin/squire-webhook-shim.py tenant-image/tests/test_connect.py
git commit -m "1C: one-time GET /connect/<nonce> page (OpenAI + Anthropic keys only)"
```

### Task 4: POST /connect/<nonce> — validate, store, consume

**Files:**
- Modify: `tenant-image/bin/squire_connect.py` (key validation + credential storage)
- Modify: `tenant-image/bin/squire-webhook-shim.py` (POST route + per-IP backoff)
- Modify: `tenant-image/tests/test_connect.py` (POST section, faked providers)

Provider validation URLs are env-overridable (`SQUIRE_CONNECT_OPENAI_VALIDATE_URL`, `SQUIRE_CONNECT_ANTHROPIC_VALIDATE_URL`) purely so the subprocess-driven tests can point them at a local fake — defaults are the real endpoints and the variables are never set in production.

- [ ] **Step 1: Write the failing tests**

Append to `tenant-image/tests/test_connect.py` before the final `if failures:` block:

```python
print("== POST /connect/<nonce>: validate -> store -> consume ==")
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
import threading            # noqa: E402
import urllib.parse         # noqa: E402

FAKE_PROVIDER_PORT = 18082
provider_calls = []


class FakeProvider(BaseHTTPRequestHandler):
    """Answers both providers' models-list validation endpoints."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def do_GET(self):
        provider_calls.append({
            "path": self.path,
            "bearer": self.headers.get("Authorization"),
            "x_api_key": self.headers.get("x-api-key"),
            "version": self.headers.get("anthropic-version"),
        })
        # /openai/ok and /anthropic/ok accept; /openai/bad rejects with 401.
        status = 401 if "/bad" in self.path else 200
        body = b'{"data": []}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


fake_provider = ThreadingHTTPServer(("127.0.0.1", FAKE_PROVIDER_PORT), FakeProvider)
fake_provider.daemon_threads = True
threading.Thread(target=fake_provider.serve_forever, daemon=True).start()


def shim_post(nonce, provider, key, host=PUB, port=SHIM_PORT):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    form = urllib.parse.urlencode({"provider": provider, "api_key": key}).encode()
    conn.request("POST", f"/connect/{nonce}", body=form, headers={
        "Host": host,
        "Content-Type": "application/x-www-form-urlencoded",
        "Content-Length": str(len(form)),
    })
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp.status, body


home2 = tempfile.mkdtemp()
state2 = os.path.join(home2, ".squire")
# The real container's .env is a SYMLINK into tmpfs. Reproduce that shape so
# the test proves credential writes follow the link instead of replacing it
# with a plaintext file on the "volume".
tmpfs = tempfile.mkdtemp()
open(os.path.join(tmpfs, ".env"), "w").close()
os.symlink(os.path.join(tmpfs, ".env"), os.path.join(home2, ".env"))


def start_shim2(**overrides):
    env = dict(os.environ)
    env.update({
        "PORT": str(SHIM_PORT),
        "HERMES_HOME": home2,
        "SQUIRE_STATE_DIR": state2,
        "SQUIRE_PUBLIC_DOMAIN": PUB,
        "TELEGRAM_WEBHOOK_SECRET": "s3cr3t",
        "TENANT_ID": "t-connect-test",
        "SQUIRE_CONNECT_OPENAI_VALIDATE_URL":
            f"http://127.0.0.1:{FAKE_PROVIDER_PORT}/openai/ok",
        "SQUIRE_CONNECT_ANTHROPIC_VALIDATE_URL":
            f"http://127.0.0.1:{FAKE_PROVIDER_PORT}/anthropic/ok",
        # Task 6 wires the full pipeline; these keep restart/notify inert here.
        "SQUIRE_SUPERVISORCTL": "/bin/true",
        "CONTROL_API_URL": "",
    })
    env.update(overrides)
    proc = subprocess.Popen([sys.executable, SHIM], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{SHIM_PORT}/health", timeout=2)
            return proc
        except Exception:
            time.sleep(0.25)
    raise AssertionError("shim never became ready")


OPENAI_KEY = "sk-test-openai-key-0123456789abcdef0123"
ANTHROPIC_KEY = "sk-ant-api03-test-key-0123456789abcdef"

proc = start_shim2()
try:
    # --- happy path: OpenAI key -------------------------------------------
    n1 = squire_connect.mint_nonce(state2)
    status, body = shim_post(n1, "openai", OPENAI_KEY)
    check("valid OpenAI key -> done page", status == 200 and "back to Telegram" in body,
          (status, body[:300]))
    check("provider was actually called to validate",
          any(c["path"] == "/openai/ok" and c["bearer"] == f"Bearer {OPENAI_KEY}"
              for c in provider_calls), provider_calls)
    env_text = open(os.path.join(tmpfs, ".env")).read()
    check("key stored in .env", f"OPENAI_API_KEY={OPENAI_KEY}" in env_text, env_text)
    check(".env is still a symlink (write followed it to tmpfs)",
          os.path.islink(os.path.join(home2, ".env")), "symlink was replaced")
    check("nonce consumed by success", squire_connect.find_nonce(state2, n1) is None)

    # A reused nonce gets the friendly invalid page and stores nothing.
    before = env_text
    status, body = shim_post(n1, "openai", "sk-test-second-key-000000000000000000")
    check("reused nonce -> invalid page", status == 200 and "ask your" in body.lower(),
          (status, body[:200]))
    check("reused nonce stored nothing",
          open(os.path.join(tmpfs, ".env")).read() == before, "env changed")

    # --- invalid key: stated plainly, nothing stored, nonce STILL valid ---
    # The fake provider 401s on the /bad path, so restart the shim pointing
    # the OpenAI validation URL at it.
    provider_calls.clear()
    n2 = squire_connect.mint_nonce(state2)
    proc.terminate(); proc.wait(timeout=10)
    proc = start_shim2(SQUIRE_CONNECT_OPENAI_VALIDATE_URL=
                       f"http://127.0.0.1:{FAKE_PROVIDER_PORT}/openai/bad")
    status, body = shim_post(n2, "openai", "sk-test-a-key-the-provider-rejects-000")
    check("rejected key -> plain statement on the form page",
          status == 200 and "didn't accept" in body, (status, body[:300]))
    check("rejected key stored nothing",
          "rejects" not in open(os.path.join(tmpfs, ".env")).read(), "stored a bad key")
    check("nonce still valid after a rejected key",
          squire_connect.find_nonce(state2, n2) is not None)

    # --- happy path: Anthropic key (fresh shim, back on the OK endpoint) --
    proc.terminate(); proc.wait(timeout=10)
    proc = start_shim2()
    status, body = shim_post(n2, "anthropic", ANTHROPIC_KEY)
    check("valid Anthropic key -> done page", status == 200 and "Anthropic" in body,
          (status, body[:300]))
    env_text = open(os.path.join(tmpfs, ".env")).read()
    check("Anthropic key stored", f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}" in env_text, env_text)
    check("direct base URL stored with it (overrides the trial proxy)",
          "ANTHROPIC_BASE_URL=https://api.anthropic.com" in env_text, env_text)
    check("anthropic validation sent x-api-key + version header",
          any(c["path"] == "/anthropic/ok" and c["x_api_key"] == ANTHROPIC_KEY
              and c["version"] for c in provider_calls), provider_calls)

    # --- abuse limit: repeated misses back off ----------------------------
    got_429 = False
    for i in range(8):
        status, _ = shim_post("not-a-nonce", "openai", f"sk-test-guess-{i:030d}")
        if status == 429:
            got_429 = True
            break
    check("repeated nonce misses from one IP hit a 429 backoff", got_429, status)
finally:
    proc.terminate()
    proc.wait(timeout=10)
    out = proc.stdout.read() if proc.stdout else ""
    check("no credential material in shim logs",
          OPENAI_KEY not in out and ANTHROPIC_KEY not in out, out[-300:])
    fake_provider.shutdown()
    fake_provider.server_close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tenant-image/tests/test_connect.py`
Expected: earlier sections PASS; this section fails at `valid OpenAI key -> done page` (POST /connect answers 404).

- [ ] **Step 3: Add validation + storage to `squire_connect.py`**

Append to `tenant-image/bin/squire_connect.py`:

```python
# ---------------------------------------------------------------------------
# Provider key validation — one cheap authenticated GET per provider.
# URLs are env-overridable ONLY so the Docker-free tests can fake the
# provider; production never sets these variables.
# ---------------------------------------------------------------------------
import urllib.error
import urllib.request

VALIDATE_TIMEOUT = 20.0


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
        with urllib.request.urlopen(request, timeout=VALIDATE_TIMEOUT) as response:
            if 200 <= response.status < 300:
                return True, ""
            return False, "The provider didn't accept that key."
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "The provider didn't accept that key — check it and try again."
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


def env_upsert(env_path: str, updates: dict) -> None:
    """Idempotent last-assignment-wins upsert, same semantics as the
    entrypoint's env_put, written to the RESOLVED target atomically (0600)."""
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


ANTHROPIC_DIRECT_BASE_URL = "https://api.anthropic.com"
CHATGPT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


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
```

- [ ] **Step 4: Add `do_POST` routing + backoff to the shim**

In `tenant-image/bin/squire-webhook-shim.py`, add at module level (near `_COUNTERS`):

```python
# --- /connect abuse limiting -------------------------------------------------
# Per-IP failure backoff for the connect page. In-memory (resets on restart),
# which matches the threat: online guessing of a live nonce. Nonce lookups are
# already constant-time; this bounds the request RATE.
_CONNECT_FAILS: dict = {}
_CONNECT_FAILS_LOCK = threading.Lock()
CONNECT_MAX_FAILS = 5
CONNECT_LOCKOUT_SECONDS = 60.0


def _connect_throttled(ip: str) -> bool:
    import time as _time
    with _CONNECT_FAILS_LOCK:
        count_, until = _CONNECT_FAILS.get(ip, (0, 0.0))
        return count_ >= CONNECT_MAX_FAILS and _time.monotonic() < until


def _connect_record_failure(ip: str) -> None:
    import time as _time
    with _CONNECT_FAILS_LOCK:
        count_, _until = _CONNECT_FAILS.get(ip, (0, 0.0))
        _CONNECT_FAILS[ip] = (count_ + 1, _time.monotonic() + CONNECT_LOCKOUT_SECONDS)


def _connect_clear(ip: str) -> None:
    with _CONNECT_FAILS_LOCK:
        _CONNECT_FAILS.pop(ip, None)
```

In `Handler.do_POST`, directly after the public-domain gate block (so `/connect` POSTs never fall through to the webhook logic):

```python
        if path.startswith("/connect/"):
            self._handle_connect_post(path)
            return
```

And add the handler method to `Handler`:

```python
    def _handle_connect_post(self, path: str) -> None:
        """POST /connect/<nonce>: validate the pasted key with the provider,
        store it, consume the nonce, kick the connected pipeline. An invalid
        key is stated plainly, stores nothing, and leaves the nonce live."""
        if squire_connect is None:
            self._respond(404, {"error": "not found"})
            return

        ip = (self.client_address or ("",))[0]
        if _connect_throttled(ip):
            log("throttled /connect attempt")
            self._respond(429, {"error": "too many attempts"},
                          extra_headers=(("Retry-After", "60"),))
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, {"error": "bad Content-Length"})
            return
        if length <= 0 or length > 64 * 1024:
            self._respond(413, {"error": "body missing or too large"})
            return
        try:
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._respond(400, {"error": "bad form body"})
            return
        provider = (form.get("provider") or [""])[0]
        api_key = (form.get("api_key") or [""])[0].strip()

        candidate = path[len("/connect/"):]
        if squire_connect.find_nonce(None, candidate) is None:
            _connect_record_failure(ip)
            self._respond_html(200, squire_connect.render_invalid_page())
            return
        if provider not in ("openai", "anthropic") or len(api_key) < 20:
            _connect_record_failure(ip)
            self._respond_html(200, squire_connect.render_connect_page(
                candidate, error="That didn't look like a key — pick a provider and paste the whole key."))
            return

        ok, message = squire_connect.validate_key(provider, api_key)
        if not ok:
            # Stated plainly, nothing stored, nonce still valid (spec).
            _connect_record_failure(ip)
            self._respond_html(200, squire_connect.render_connect_page(candidate, error=message))
            return

        # Store FIRST, then consume: a consume that lost a race after a store
        # is harmless (same credential), while the reverse order could burn
        # the nonce with nothing saved.
        squire_connect.store_api_key(provider, api_key)
        squire_connect.consume_nonce(None, candidate)
        _connect_clear(ip)
        # Never log the provider *response* or the key; the provider NAME is fine.
        log(f"connect: stored {provider} credential; nonce consumed")

        # Task 6 wires squire_connect.run_connected_pipeline here (background
        # thread: a gateway restart takes ~45s and must not hold the browser).
        self._respond_html(200, squire_connect.render_done_page(provider))
```

Also add `import urllib.parse` to the shim's imports.

- [ ] **Step 5: Run both suites**

Run: `python3 tenant-image/tests/test_connect.py && python3 tenant-image/tests/test_webhook_shim.py`
Expected: both ALL-PASS. (The pipeline comment is not dead code smell — Task 6's test drives the wiring.)

- [ ] **Step 6: Commit**

```bash
git add tenant-image/bin/squire_connect.py tenant-image/bin/squire-webhook-shim.py tenant-image/tests/test_connect.py
git commit -m "1C: POST /connect — provider-validated key hand-off, single-use nonce, per-IP backoff"
```

---

### Task 5: `squire-llm-connect` CLI + Codex device flow

**Files:**
- Create: `tenant-image/bin/squire_codex_device.py`
- Create: `tenant-image/bin/squire-llm-connect`
- Create: `tenant-image/tests/test_llm_connect_cli.py`

All constants come from the Codex spike table verbatim. The four calls are re-implemented (decision 4); the issuer is env-overridable (`SQUIRE_CODEX_ISSUER`) only for tests.

- [ ] **Step 1: Write the failing device-flow unit tests**

Create `tenant-image/tests/test_llm_connect_cli.py` (mode 755):

```python
#!/usr/bin/env python3
"""Codex device-flow + squire-llm-connect CLI tests — runs WITHOUT Docker.

The four device-flow HTTP calls are exercised in-process against an injected
transport (no sockets), then the CLI is run as a subprocess against a local
fake issuer for the output-hygiene and 404-not-enabled contracts.

Usage: python3 tenant-image/tests/test_llm_connect_cli.py
Exit:  0 all assertions pass · 1 otherwise
"""
import base64
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IMAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(IMAGE_ROOT / "bin"))

import squire_codex_device as dev  # noqa: E402

CLI = str(IMAGE_ROOT / "bin" / "squire-llm-connect")

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def fake_jwt(claims: dict) -> str:
    def seg(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=")
        return raw.decode()
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


print("== spike constants are pinned ==")
cfg = dev.DeviceFlowConfig()
check("client_id from the spike", cfg.client_id == "app_EMoamEEZ73f0CkXaXp7hrann", cfg.client_id)
check("issuer default", cfg.issuer == "https://auth.openai.com", cfg.issuer)
check("verification URL", cfg.verification_url == "https://auth.openai.com/codex/device")
check("redirect_uri for the exchange",
      cfg.redirect_uri == "https://auth.openai.com/deviceauth/callback", cfg.redirect_uri)

print("== the four calls, against an injected transport ==")
calls = []


def transport_ok(url, body, headers):
    calls.append({"url": url, "body": body, "headers": headers})
    if url.endswith("/api/accounts/deviceauth/usercode"):
        return 200, json.dumps({"device_auth_id": "da-1", "user_code": "ABCD-EFGHI",
                                "interval": "0"}).encode()
    if url.endswith("/api/accounts/deviceauth/token"):
        # first poll pending (403), second grants
        n = sum(1 for c in calls if c["url"].endswith("/deviceauth/token"))
        if n == 1:
            return 403, b"{}"
        return 200, json.dumps({"authorization_code": "ac-1",
                                "code_challenge": "cc-1", "code_verifier": "cv-1"}).encode()
    if url.endswith("/oauth/token"):
        return 200, json.dumps({
            "id_token": fake_jwt({"chatgpt_plan_type": "plus", "chatgpt_account_id": "acct-9",
                                  "exp": int(time.time()) + 3600}),
            "access_token": "at-secret-1", "refresh_token": "rt-secret-1",
        }).encode()
    raise AssertionError(f"unexpected url {url}")


start = dev.request_user_code(cfg, transport=transport_ok)
check("usercode call returns code + id", start == {"device_auth_id": "da-1",
      "user_code": "ABCD-EFGHI", "interval": "0"}, start)
check("usercode call sent the client_id",
      json.loads(calls[0]["body"]) == {"client_id": cfg.client_id}, calls[0])

check("interval clamps to >=5 (serde default 0 busy-polls)",
      dev.poll_interval_seconds(start) == 5, dev.poll_interval_seconds(start))
check("interval respects server value above the floor",
      dev.poll_interval_seconds({"interval": "9"}) == 9)

pending = dev.poll_once(cfg, "da-1", "ABCD-EFGHI", transport=transport_ok)
check("403 reads as pending (None)", pending is None, pending)
grant = dev.poll_once(cfg, "da-1", "ABCD-EFGHI", transport=transport_ok)
check("grant returns the server PKCE triple",
      grant == {"authorization_code": "ac-1", "code_challenge": "cc-1",
                "code_verifier": "cv-1"}, grant)

tokens = dev.exchange_code(cfg, grant, transport=transport_ok)
check("exchange returned tokens", tokens.get("access_token") == "at-secret-1", tokens)
exchange = calls[-1]
check("exchange is a FORM post with the spike's exact fields",
      exchange["headers"].get("Content-Type") == "application/x-www-form-urlencoded"
      and b"grant_type=authorization_code" in exchange["body"]
      and b"code=ac-1" in exchange["body"]
      and b"code_verifier=cv-1" in exchange["body"]
      and b"redirect_uri=" in exchange["body"]
      and cfg.client_id.encode() in exchange["body"], exchange)

claims = dev.jwt_claims(tokens["id_token"])
check("id_token claims decode without verification", claims.get("chatgpt_plan_type") == "plus")
check("plan gate: plus passes", dev.plan_allowed(claims) is True)
check("plan gate: free is rejected", dev.plan_allowed({"chatgpt_plan_type": "free"}) is False)
check("plan gate: unknown/missing does not hard-fail",
      dev.plan_allowed({}) is True)

auth = dev.build_auth_json(tokens)
check("auth.json has the spike's shape",
      auth["auth_mode"] == "chatgpt"
      and set(auth["tokens"]) == {"id_token", "access_token", "refresh_token", "account_id"}
      and auth["tokens"]["account_id"] == "acct-9"
      and isinstance(auth["last_refresh"], str), auth)

print("== refresh policy ==")
fresh = dict(auth)
check("fresh tokens do not need refresh", dev.needs_refresh(fresh, now=time.time()) is False)
soon = dev.build_auth_json({**tokens, "id_token": tokens["id_token"]})
soon["access_token_exp"] = time.time() + 60  # within the 5-minute window
check("exp within 5 min triggers refresh", dev.needs_refresh(soon, now=time.time()) is True)
stale = dict(auth)
stale["last_refresh"] = dev.iso_utc(time.time() - 9 * 86400)  # > 8 days
check("last_refresh older than 8 days triggers refresh",
      dev.needs_refresh(stale, now=time.time()) is True)


def transport_refresh(url, body, headers):
    calls.append({"url": url, "body": body, "headers": headers})
    return 200, json.dumps({"id_token": tokens["id_token"],
                            "access_token": "at-secret-2",
                            "refresh_token": "rt-secret-2"}).encode()


new_tokens = dev.refresh_tokens(cfg, "rt-secret-1", transport=transport_refresh)
refresh_call = calls[-1]
check("refresh is a JSON post with grant_type refresh_token",
      refresh_call["headers"].get("Content-Type") == "application/json"
      and json.loads(refresh_call["body"]) == {
          "client_id": cfg.client_id, "grant_type": "refresh_token",
          "refresh_token": "rt-secret-1"}, refresh_call)
check("refresh returns rotated tokens", new_tokens["refresh_token"] == "rt-secret-2")

print("== 404 => device login not enabled (the beta caveat) ==")


def transport_404(url, body, headers):
    return 404, b"not found"


try:
    dev.request_user_code(cfg, transport=transport_404)
    check("404 raises DeviceLoginNotEnabled", False, "no exception")
except dev.DeviceLoginNotEnabled:
    check("404 raises DeviceLoginNotEnabled", True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL DEVICE-FLOW TESTS PASS")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tenant-image/tests/test_llm_connect_cli.py`
Expected: `ModuleNotFoundError: No module named 'squire_codex_device'`

- [ ] **Step 3: Implement `squire_codex_device.py`**

Create `tenant-image/bin/squire_codex_device.py` (mode 755):

```python
#!/opt/squire/venv/bin/python
"""Codex device-code sign-in — the 4 proprietary calls, per the 2026-08-14 spike.

UX-equivalent to RFC 8628 but NOT RFC 8628: OpenAI-proprietary endpoints,
server-generated PKCE, and 403/404-as-pending. Every constant below comes from
the spike table (openai/codex, codex-rs/login) and must not be "corrected"
toward the RFC.

The transport is injectable ((url, body_bytes, headers) -> (status, body_bytes))
so the whole flow tests without sockets. NEVER log tokens, codes, or bodies.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


class DeviceLoginNotEnabled(Exception):
    """First usercode call 404'd: 'device code authorization' is off for this
    ChatGPT account (beta, off by default). Onboarding must walk the user
    through enabling it in ChatGPT Settings -> Security, then retry."""


class DeviceFlowError(Exception):
    """Terminal failure (denied, expired, malformed response)."""


@dataclass(frozen=True)
class DeviceFlowConfig:
    client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    issuer: str = field(
        default_factory=lambda: os.environ.get("SQUIRE_CODEX_ISSUER", "https://auth.openai.com")
    )
    usercode_path: str = "/api/accounts/deviceauth/usercode"
    token_path: str = "/api/accounts/deviceauth/token"
    oauth_token_path: str = "/oauth/token"
    redirect_uri: str = "https://auth.openai.com/deviceauth/callback"
    verification_url: str = "https://auth.openai.com/codex/device"
    min_poll_seconds: int = 5
    code_lifetime_seconds: int = 900  # user code expires in 15 minutes


def _default_transport(url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


def request_user_code(cfg: DeviceFlowConfig, transport=_default_transport) -> dict:
    """Call 1: mint the device code. 404 => device login not enabled (beta)."""
    status, body = transport(
        cfg.issuer + cfg.usercode_path,
        json.dumps({"client_id": cfg.client_id}).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    if status == 404:
        raise DeviceLoginNotEnabled()
    if status != 200:
        raise DeviceFlowError(f"usercode endpoint answered HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise DeviceFlowError("usercode endpoint returned non-JSON") from exc
    if not data.get("device_auth_id") or not data.get("user_code"):
        raise DeviceFlowError("usercode response missing device_auth_id/user_code")
    return data


def poll_interval_seconds(usercode_response: dict) -> int:
    """Server sends interval as STRING seconds; serde-defaults to 0, which
    would busy-poll — clamp to >= 5 (spike)."""
    try:
        interval = int(str(usercode_response.get("interval", "5")))
    except ValueError:
        interval = 5
    return max(5, interval)


def poll_once(cfg: DeviceFlowConfig, device_auth_id: str, user_code: str,
              transport=_default_transport) -> dict | None:
    """Call 2: poll for approval. 403/404 mean PENDING (proprietary quirk);
    200 returns the server-generated PKCE triple."""
    status, body = transport(
        cfg.issuer + cfg.token_path,
        json.dumps({"device_auth_id": device_auth_id, "user_code": user_code}).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    if status in (403, 404):
        return None
    if status != 200:
        raise DeviceFlowError(f"device poll answered HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise DeviceFlowError("device poll returned non-JSON") from exc
    if not data.get("authorization_code"):
        raise DeviceFlowError("grant response missing authorization_code")
    return data


def exchange_code(cfg: DeviceFlowConfig, grant: dict, transport=_default_transport) -> dict:
    """Call 3: authorization_code -> tokens. FORM-encoded, spike-exact fields,
    including the server-generated code_verifier."""
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": grant["authorization_code"],
        "redirect_uri": cfg.redirect_uri,
        "client_id": cfg.client_id,
        "code_verifier": grant["code_verifier"],
    }).encode("utf-8")
    status, body = transport(
        cfg.issuer + cfg.oauth_token_path, form,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    if status != 200:
        raise DeviceFlowError(f"token exchange answered HTTP {status}")
    tokens = json.loads(body)
    if not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise DeviceFlowError("token exchange missing access/refresh token")
    return tokens


def refresh_tokens(cfg: DeviceFlowConfig, refresh_token: str,
                   transport=_default_transport) -> dict:
    """Call 4: refresh. JSON body (unlike the exchange). Refresh tokens are
    one-time-use/rotating: the CALLER must persist the new pair atomically
    BEFORE using the new access token (see squire-llm-connect refresh)."""
    status, body = transport(
        cfg.issuer + cfg.oauth_token_path,
        json.dumps({"client_id": cfg.client_id, "grant_type": "refresh_token",
                    "refresh_token": refresh_token}).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    if status in (400, 401):
        # Reused/expired/revoked refresh token: terminal — re-link in chat.
        raise DeviceFlowError("refresh token rejected (re-link required)")
    if status != 200:
        raise DeviceFlowError(f"refresh answered HTTP {status}")
    tokens = json.loads(body)
    if not tokens.get("access_token"):
        raise DeviceFlowError("refresh response missing access_token")
    return tokens


def jwt_claims(id_token: str) -> dict:
    """Decode the id_token payload WITHOUT signature verification — it arrived
    over TLS directly from the issuer and is used only for plan gating and the
    account id, never as an authentication decision."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def plan_allowed(claims: dict) -> bool:
    """Reject only an explicit free plan (spike: claim chatgpt_plan_type,
    values free/plus/pro/business). Missing claim => allow with the provider
    as the final arbiter — hard-failing on an absent beta claim would strand
    paying users."""
    return str(claims.get("chatgpt_plan_type", "")).lower() != "free"


def iso_utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def build_auth_json(tokens: dict) -> dict:
    """The $CODEX_HOME/auth.json shape from the spike, stored at
    $HERMES_HOME/auth.json (already a sealed name — secrets-sync encrypts it
    onto the volume with AAD 'auth.json')."""
    claims = jwt_claims(tokens.get("id_token", ""))
    account_id = str(
        claims.get("chatgpt_account_id")
        or (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id", "")
    )
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": tokens.get("id_token", ""),
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
            "account_id": account_id,
        },
        "last_refresh": iso_utc(time.time()),
    }


def needs_refresh(auth: dict, now: float) -> bool:
    """Spike policy: refresh when the access token expires within 5 minutes or
    last_refresh is more than 8 days old."""
    exp = auth.get("access_token_exp")
    if exp is None:
        exp = jwt_claims((auth.get("tokens") or {}).get("id_token", "")).get("exp")
    if isinstance(exp, (int, float)) and exp - now < 300:
        return True
    last = auth.get("last_refresh", "")
    try:
        parsed = time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (ValueError, TypeError):
        return True  # unknown age: refreshing is the safe default
    return (now - parsed) > 8 * 86400
```

- [ ] **Step 4: Run the unit tests**

Run: `python3 tenant-image/tests/test_llm_connect_cli.py`
Expected: all sections up to and including the 404 section PASS.

- [ ] **Step 5: Write the failing CLI tests**

Append to `tenant-image/tests/test_llm_connect_cli.py` before the final `if failures:` block:

```python
print("== squire-llm-connect CLI ==")
ISSUER_PORT = 18083
issuer_mode = {"enabled": True}


class FakeIssuer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path == "/api/accounts/deviceauth/usercode":
            if not issuer_mode["enabled"]:
                self._send(404, {})
                return
            self._send(200, {"device_auth_id": "da-cli", "user_code": "WXYZ-ABCDE",
                             "interval": "0"})
        elif self.path == "/api/accounts/deviceauth/token":
            self._send(200, {"authorization_code": "ac-cli",
                             "code_challenge": "cc", "code_verifier": "cv"})
        elif self.path == "/oauth/token":
            self._send(200, {"id_token": fake_jwt({"chatgpt_plan_type": "pro",
                                                   "chatgpt_account_id": "acct-cli",
                                                   "exp": int(time.time()) + 3600}),
                             "access_token": "at-cli-secret",
                             "refresh_token": "rt-cli-secret"})
        else:
            self._send(500, {"error": self.path})


issuer = ThreadingHTTPServer(("127.0.0.1", ISSUER_PORT), FakeIssuer)
issuer.daemon_threads = True
threading.Thread(target=issuer.serve_forever, daemon=True).start()

home = tempfile.mkdtemp()
os.makedirs(os.path.join(home, ".squire"), exist_ok=True)
open(os.path.join(home, ".env"), "w").close()
CLI_ENV = dict(os.environ)
CLI_ENV.update({
    "HERMES_HOME": home,
    "SQUIRE_STATE_DIR": os.path.join(home, ".squire"),
    "SQUIRE_CODEX_ISSUER": f"http://127.0.0.1:{ISSUER_PORT}",
    "SQUIRE_PUBLIC_DOMAIN": "tenant-cli.up.railway.app",
    "SQUIRE_SUPERVISORCTL": "/bin/true",   # pipeline (Task 6) must not touch a real supervisor
    "CONTROL_API_URL": "",
    "TENANT_ID": "t-cli-test",
})


def cli(*args, env=None):
    out = subprocess.run([sys.executable, CLI, *args], env=env or CLI_ENV,
                         capture_output=True, text=True, timeout=60)
    return out.returncode, out.stdout.strip(), out.stderr


rc, out, err = cli("mint-link")
check("mint-link exits 0", rc == 0, (rc, err))
link = json.loads(out)
check("mint-link prints a JSON url on the public domain",
      link.get("url", "").startswith("https://tenant-cli.up.railway.app/connect/"), out)
check("mint-link states the expiry", link.get("expires_in_minutes") == 15, out)

no_domain_env = dict(CLI_ENV)
no_domain_env.pop("SQUIRE_PUBLIC_DOMAIN", None)
no_domain_env.pop("RAILWAY_PUBLIC_DOMAIN", None)
rc, out, _ = cli("mint-link", env=no_domain_env)
check("mint-link without a public domain fails with a clear state",
      rc != 0 and json.loads(out).get("state") == "no_public_domain", (rc, out))

rc, out, _ = cli("status")
check("status with no flow yet", json.loads(out).get("state") == "none", out)

# --- the 404-not-enabled onboarding branch ------------------------------
issuer_mode["enabled"] = False
rc, out, _ = cli("start", "openai-device")
check("device login disabled: exit 3", rc == 3, (rc, out))
info = json.loads(out)
check("disabled: state says not_enabled", info.get("state") == "not_enabled", out)
check("disabled: instructions mention ChatGPT Settings -> Security",
      "Security" in info.get("instructions", "") and "device code" in info.get("instructions", "").lower(), out)
issuer_mode["enabled"] = True

# --- the full grant ------------------------------------------------------
rc, out, _ = cli("start", "openai-device")
check("start exits 0", rc == 0, (rc, out))
started = json.loads(out)
check("start prints ONLY the code and URL (chat-safe)",
      started.get("user_code") == "WXYZ-ABCDE"
      and started.get("verification_url") == "https://auth.openai.com/codex/device"
      and "at-cli-secret" not in out and "rt-cli-secret" not in out, out)

deadline = time.time() + 30
final = {}
while time.time() < deadline:
    _, sout, _ = cli("status")
    final = json.loads(sout)
    if final.get("state") in ("connected", "denied", "error", "timed_out"):
        break
    time.sleep(0.5)
check("background poll reached connected", final.get("state") == "connected", final)
check("status names the provider, not the tokens",
      final.get("provider") == "chatgpt" and "at-cli-secret" not in json.dumps(final), final)

auth_path = os.path.join(home, "auth.json")
check("auth.json written", os.path.exists(auth_path), auth_path)
auth = json.loads(open(auth_path).read())
check("auth.json carries the granted tokens",
      auth["tokens"]["access_token"] == "at-cli-secret"
      and auth["tokens"]["account_id"] == "acct-cli"
      and auth["auth_mode"] == "chatgpt", auth)
env_text = open(os.path.join(home, ".env")).read()
check("gateway env got the access token + codex base url",
      "OPENAI_API_KEY=at-cli-secret" in env_text
      and "OPENAI_BASE_URL=https://chatgpt.com/backend-api/codex" in env_text, env_text)
```

- [ ] **Step 6: Run to verify the CLI section fails**

Run: `python3 tenant-image/tests/test_llm_connect_cli.py`
Expected: device-flow sections PASS; CLI section fails (`No such file or directory: .../squire-llm-connect`).

- [ ] **Step 7: Implement the CLI**

Create `tenant-image/bin/squire-llm-connect` (mode 755):

```python
#!/opt/squire/venv/bin/python
"""squire-llm-connect — the agent's ONLY interface to credential connection.

Prints machine-readable JSON lines carrying only URLs, short codes, and
states. NO code path here prints, logs, or returns credential material — the
agent relays this output into a Telegram chat.

Commands
--------
  mint-link                one-time https://<domain>/connect/<nonce> URL
  status                   {"state": none|pending|connected|denied|timed_out|error|not_enabled, ...}
  start openai-device      begin the Codex device flow; prints code + URL,
                           polls in the background, stores tokens on grant
  refresh openai-device    rotate the ChatGPT tokens if the spike policy says so
  _poll <id> <code> <interval>   internal: the detached poll daemon
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import squire_codex_device as dev  # noqa: E402
import squire_connect  # noqa: E402


def _state_dir() -> str:
    state_dir = os.environ.get("SQUIRE_STATE_DIR")
    if not state_dir:
        home = os.environ.get("HERMES_HOME") or "/opt/data"
        state_dir = os.path.join(home, ".squire")
    return state_dir


def _status_path() -> str:
    return os.path.join(_state_dir(), "llm-connect-status.json")


def write_status(state: str, **extra) -> None:
    payload = {"state": state, "updated": dev.iso_utc(time.time()), **extra}
    squire_connect._atomic_write_0600(
        _status_path(), json.dumps(payload).encode("utf-8")
    )


def read_status() -> dict:
    try:
        return json.loads(open(_status_path(), "rb").read())
    except (OSError, ValueError):
        return {"state": "none"}


def public_domain() -> str:
    return (
        os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        or os.environ.get("SQUIRE_PUBLIC_DOMAIN")
        or ""
    ).strip()


NOT_ENABLED_INSTRUCTIONS = (
    "Device sign-in is switched off on this ChatGPT account (it is a beta "
    "feature, off by default). To enable it: open chatgpt.com, go to Settings "
    "-> Security, and turn ON 'device code authorization'. On a Team or "
    "Enterprise workspace an admin has to enable it. Then ask me to try again."
)


def cmd_mint_link() -> int:
    domain = public_domain()
    if not domain:
        print(json.dumps({"state": "no_public_domain",
                          "detail": "no RAILWAY_PUBLIC_DOMAIN/SQUIRE_PUBLIC_DOMAIN set"}))
        return 1
    nonce = squire_connect.mint_nonce(None)
    print(json.dumps({"url": f"https://{domain}/connect/{nonce}",
                      "expires_in_minutes": squire_connect.NONCE_TTL_SECONDS // 60}))
    return 0


def cmd_status() -> int:
    print(json.dumps(read_status()))
    return 0


def store_chatgpt_tokens(tokens: dict) -> None:
    """auth.json (sealed name — secrets-sync encrypts it) + gateway env.
    Both writes resolve symlinks (see squire_connect.env_upsert's rationale)."""
    home = os.environ.get("HERMES_HOME") or "/opt/data"
    auth = dev.build_auth_json(tokens)
    auth_path = os.path.realpath(os.path.join(home, "auth.json"))
    squire_connect._atomic_write_0600(
        auth_path, json.dumps(auth, indent=2).encode("utf-8")
    )
    squire_connect.env_upsert(os.path.join(home, ".env"), {
        "OPENAI_API_KEY": auth["tokens"]["access_token"],
        "OPENAI_BASE_URL": squire_connect.CHATGPT_CODEX_BASE_URL,
    })


def cmd_start_openai_device() -> int:
    cfg = dev.DeviceFlowConfig()
    try:
        started = dev.request_user_code(cfg)
    except dev.DeviceLoginNotEnabled:
        # The beta/off-by-default caveat: a REAL onboarding branch, not an
        # error. The agent relays these instructions and retries afterwards.
        write_status("not_enabled", provider="chatgpt")
        print(json.dumps({"state": "not_enabled",
                          "instructions": NOT_ENABLED_INSTRUCTIONS}))
        return 3
    except dev.DeviceFlowError as exc:
        write_status("error", provider="chatgpt", detail=str(exc))
        print(json.dumps({"state": "error", "detail": str(exc)}))
        return 1

    write_status("pending", provider="chatgpt")
    interval = dev.poll_interval_seconds(started)
    # Detach the poller so the agent's terminal command returns immediately;
    # the agent then polls `status`. start_new_session survives our exit.
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "_poll",
         started["device_auth_id"], started["user_code"], str(interval)],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(json.dumps({
        "state": "pending",
        "verification_url": cfg.verification_url,
        "user_code": started["user_code"],
        "expires_in_minutes": cfg.code_lifetime_seconds // 60,
    }))
    return 0


def cmd_poll(device_auth_id: str, user_code: str, interval: str) -> int:
    cfg = dev.DeviceFlowConfig()
    delay = max(cfg.min_poll_seconds, int(interval))
    deadline = time.monotonic() + cfg.code_lifetime_seconds
    grant = None
    while time.monotonic() < deadline:
        try:
            grant = dev.poll_once(cfg, device_auth_id, user_code)
        except dev.DeviceFlowError as exc:
            write_status("denied", provider="chatgpt", detail=str(exc))
            return 1
        if grant is not None:
            break
        time.sleep(delay)
    if grant is None:
        write_status("timed_out", provider="chatgpt")
        return 1

    try:
        tokens = dev.exchange_code(cfg, grant)
    except dev.DeviceFlowError as exc:
        write_status("error", provider="chatgpt", detail=str(exc))
        return 1

    if not dev.plan_allowed(dev.jwt_claims(tokens.get("id_token", ""))):
        # Free plan cannot serve Codex traffic — honest denial + the key path.
        write_status("denied", provider="chatgpt",
                     detail="This ChatGPT account is on the free plan; a paid "
                            "plan is required. The OpenAI API-key path works today.")
        return 1

    store_chatgpt_tokens(tokens)
    # Task 6 wires squire_connect.run_connected_pipeline("chatgpt") here.
    write_status("connected", provider="chatgpt")
    return 0


def cmd_refresh_openai_device() -> int:
    home = os.environ.get("HERMES_HOME") or "/opt/data"
    auth_path = os.path.realpath(os.path.join(home, "auth.json"))
    try:
        auth = json.loads(open(auth_path, "rb").read())
    except (OSError, ValueError):
        print(json.dumps({"state": "error", "detail": "no auth.json to refresh"}))
        return 1
    if not dev.needs_refresh(auth, now=time.time()):
        print(json.dumps({"state": "fresh"}))
        return 0
    cfg = dev.DeviceFlowConfig()
    try:
        tokens = dev.refresh_tokens(cfg, auth["tokens"]["refresh_token"])
    except dev.DeviceFlowError:
        # Terminal (reused/expired/revoked): the agent must offer a re-link.
        write_status("error", provider="chatgpt", detail="refresh rejected; re-link needed")
        print(json.dumps({"state": "relink_required"}))
        return 1
    # Rotating refresh tokens: PERSIST the new pair before anything uses it —
    # crash after use-before-persist would strand us with a burned token.
    store_chatgpt_tokens(tokens)
    print(json.dumps({"state": "refreshed"}))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "mint-link":
        return cmd_mint_link()
    if len(argv) >= 2 and argv[1] == "status":
        return cmd_status()
    if len(argv) >= 3 and argv[1] == "start" and argv[2] == "openai-device":
        return cmd_start_openai_device()
    if len(argv) >= 3 and argv[1] == "refresh" and argv[2] == "openai-device":
        return cmd_refresh_openai_device()
    if len(argv) >= 5 and argv[1] == "_poll":
        return cmd_poll(argv[2], argv[3], argv[4])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] **Step 8: Run the full suite**

Run: `python3 tenant-image/tests/test_llm_connect_cli.py`
Expected: `ALL DEVICE-FLOW TESTS PASS`

- [ ] **Step 9: Record exec bits and commit**

```bash
git update-index --add --chmod=+x tenant-image/bin/squire_codex_device.py tenant-image/bin/squire-llm-connect tenant-image/tests/test_llm_connect_cli.py
git add tenant-image/bin/squire_codex_device.py tenant-image/bin/squire-llm-connect tenant-image/tests/test_llm_connect_cli.py
git commit -m "1C: squire-llm-connect CLI + Codex device flow (4 spike calls, 404-enable branch, plan gate, refresh)"
```

---

### Task 6: The connected pipeline (switch → reload → notify)

**Files:**
- Modify: `tenant-image/bin/squire_connect.py` (pipeline)
- Modify: `tenant-image/bin/squire-webhook-shim.py` (wire into POST)
- Modify: `tenant-image/bin/squire-llm-connect` (wire into `_poll`)
- Modify: `tenant-image/tests/test_connect.py` (ordering + wiring tests)

- [ ] **Step 1: Write the failing pipeline tests**

Append to `tenant-image/tests/test_connect.py` before the final `if failures:` block:

```python
print("== connected pipeline: switch model -> restart gateway -> notify ==")
home3 = tempfile.mkdtemp()
# A config.yaml with the trial model line plus other keys that MUST survive.
config_path = os.path.join(home3, "config.yaml")
open(config_path, "w").write(
    'model: "anthropic:claude-sonnet-5"\n'
    "hooks:\n  pre_llm_call: something-load-bearing\n"
    "timezone: UTC\n"
)
open(os.path.join(home3, ".env"), "w").close()

# Fake supervisorctl: records its argv so ordering and arguments are provable.
bindir = tempfile.mkdtemp()
ctl_log = os.path.join(bindir, "ctl.log")
ctl = os.path.join(bindir, "supervisorctl")
open(ctl, "w").write(f"#!/bin/sh\necho \"$@\" >> {ctl_log}\n")
os.chmod(ctl, 0o755)

# Fake control-api capturing /internal/llm-connected.
notified = []


class FakeControl(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        notified.append({"path": self.path,
                         "auth": self.headers.get("Authorization"),
                         "payload": json.loads(self.rfile.read(n) or b"{}")})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


CONTROL_PORT = 18084
control = ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), FakeControl)
control.daemon_threads = True
threading.Thread(target=control.serve_forever, daemon=True).start()

os.environ["SQUIRE_SUPERVISORCTL"] = ctl
os.environ["SQUIRE_SUPERVISORD_CONF"] = "/tmp/fake-supervisord.conf"
os.environ["CONTROL_API_URL"] = f"http://127.0.0.1:{CONTROL_PORT}"
os.environ["INTERNAL_API_TOKEN"] = "internal-tok-test"
os.environ["TENANT_ID"] = "t-pipeline-test"

order = []
real_switch = squire_connect.switch_gateway_model
real_restart = squire_connect.restart_gateway
real_notify = squire_connect.notify_control_api
squire_connect.switch_gateway_model = lambda p, h=None: (order.append("switch"), real_switch(p, h))[1]
squire_connect.restart_gateway = lambda: (order.append("restart"), real_restart())[1]
squire_connect.notify_control_api = lambda p: (order.append("notify"), real_notify(p))[1]
try:
    squire_connect.run_connected_pipeline("openai", hermes_home=home3)
finally:
    squire_connect.switch_gateway_model = real_switch
    squire_connect.restart_gateway = real_restart
    squire_connect.notify_control_api = real_notify

check("pipeline order is switch -> restart -> notify",
      order == ["switch", "restart", "notify"], order)

config_text = open(config_path).read()
check("model line rewritten to the OpenAI default",
      'model: "openai:gpt-4.1"' in config_text, config_text)
check("the rest of config.yaml survived (targeted line edit, no YAML round-trip)",
      "pre_llm_call: something-load-bearing" in config_text
      and "timezone: UTC" in config_text, config_text)
check("gateway restarted via supervisorctl",
      os.path.exists(ctl_log) and "restart gateway" in open(ctl_log).read(),
      open(ctl_log).read() if os.path.exists(ctl_log) else "no ctl call")
check("control-api notified on the pinned route",
      notified and notified[0]["path"] == "/internal/llm-connected", notified)
check("notify payload is exactly {tenant_id, provider}",
      notified and notified[0]["payload"] == {"tenant_id": "t-pipeline-test",
                                              "provider": "openai"}, notified)
check("notify carries the internal bearer",
      notified and notified[0]["auth"] == "Bearer internal-tok-test", notified)

# Model choice per provider (env-overridable, defaults locked here).
open(config_path, "w").write('model: "anthropic:claude-sonnet-5"\n')
squire_connect.switch_gateway_model("chatgpt", home3)
check("chatgpt switches to the codex model",
      'model: "openai:gpt-5-codex"' in open(config_path).read(), open(config_path).read())
open(config_path, "w").write('model: "anthropic:claude-sonnet-5"\n')
squire_connect.switch_gateway_model("anthropic", home3)
check("anthropic keeps the sonnet model (direct, no proxy)",
      'model: "anthropic:claude-sonnet-5"' in open(config_path).read())

# A notify failure must not blow up the pipeline (heartbeat backstop covers it).
control.shutdown()
control.server_close()
notified.clear()
try:
    squire_connect.run_connected_pipeline("openai", hermes_home=home3)
    check("pipeline survives an unreachable control-api", True)
except Exception as exc:  # noqa: BLE001
    check("pipeline survives an unreachable control-api", False, repr(exc))
for var in ("SQUIRE_SUPERVISORCTL", "SQUIRE_SUPERVISORD_CONF",
            "CONTROL_API_URL", "INTERNAL_API_TOKEN", "TENANT_ID"):
    os.environ.pop(var, None)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tenant-image/tests/test_connect.py`
Expected: fails with `AttributeError: module 'squire_connect' has no attribute 'switch_gateway_model'`.

- [ ] **Step 3: Implement the pipeline**

Append to `tenant-image/bin/squire_connect.py`:

```python
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


def _gateway_model_for(provider: str) -> str:
    """Env-overridable so a model rename never needs an image rebuild.
    anthropic keeps the trial's model id — same model, now direct + unmetered."""
    defaults = {
        "openai": "openai:gpt-4.1",
        "chatgpt": "openai:gpt-5-codex",
        "anthropic": "anthropic:claude-sonnet-5",
    }
    env_names = {
        "openai": "SQUIRE_CONNECT_MODEL_OPENAI",
        "chatgpt": "SQUIRE_CONNECT_MODEL_CHATGPT",
        "anthropic": "SQUIRE_CONNECT_MODEL_ANTHROPIC",
    }
    return os.environ.get(env_names.get(provider, ""), "") or defaults[provider]


def switch_gateway_model(provider: str, hermes_home: str | None = None) -> bool:
    """Anchored per-line rewrite of the `model:` line ONLY — the same targeted
    discipline as the concierge hook's timezone command, and for the same
    reason: config.yaml also carries the hooks block that makes onboarding
    work, and a YAML round-trip is how that gets silently dropped."""
    home = hermes_home or os.environ.get("HERMES_HOME") or "/opt/data"
    config_path = os.path.join(home, "config.yaml")
    model = _gateway_model_for(provider)
    try:
        text = open(config_path, "r", encoding="utf-8").read()
        new_text = re.sub(r'(?m)^model:.*$', f'model: "{model}"', text, count=1)
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
```

- [ ] **Step 4: Wire the two call sites**

In `tenant-image/bin/squire-webhook-shim.py`, in `_handle_connect_post`, replace the comment line `# Task 6 wires squire_connect.run_connected_pipeline here ...` with:

```python
        # Background thread: a supervisord gateway restart can take ~45s and
        # must not hold the browser's response open that long.
        threading.Thread(
            target=squire_connect.run_connected_pipeline, args=(provider,),
            daemon=True,
        ).start()
```

In `tenant-image/bin/squire-llm-connect`, in `cmd_poll`, replace the comment line `# Task 6 wires squire_connect.run_connected_pipeline("chatgpt") here.` with:

```python
    squire_connect.run_connected_pipeline("chatgpt")
```

(The CLI poller is already detached — synchronous is correct there.)

- [ ] **Step 5: Run all three tenant suites**

Run: `python3 tenant-image/tests/test_connect.py && python3 tenant-image/tests/test_llm_connect_cli.py && python3 tenant-image/tests/test_webhook_shim.py`
Expected: three ALL-PASS lines. (`test_connect`'s Task 4 section set `SQUIRE_SUPERVISORCTL=/bin/true` and `CONTROL_API_URL=` so the newly wired pipeline stays inert there.)

- [ ] **Step 6: Commit**

```bash
git add tenant-image/bin/squire_connect.py tenant-image/bin/squire-webhook-shim.py tenant-image/bin/squire-llm-connect tenant-image/tests/test_connect.py
git commit -m "1C: connected pipeline — model switch, silent gateway restart, llm-connected notify"
```

---

### Task 7: control-api — `connected_provider` + POST /internal/llm-connected

**Files:**
- Modify: `apps/control-api/src/control_api/models.py`
- Modify: `apps/control-api/src/control_api/schemas.py`
- Modify: `apps/control-api/src/control_api/provisioning.py`
- Modify: `apps/control-api/src/control_api/routers/internal.py`
- Modify: `apps/control-api/tests/test_privacy_schema.py`
- Modify: `apps/control-api/tests/test_internal_api.py`
- Modify: `apps/control-api/tests/test_cross_service_contracts.py`

**DB note:** SQLModel maps `str | None` to VARCHAR; there are no migrations, so production needs `ALTER TABLE tenant ADD COLUMN connected_provider VARCHAR;` run manually BEFORE the control-api deploy (Task 12 sequences this; the classifier blocks live DDL, so the operator must `!`-approve it).

- [ ] **Step 1: Write the failing endpoint tests**

Append to `apps/control-api/tests/test_internal_api.py` (the file already imports `httpx`, `respx`, `db`, `Tenant` and `TenantStatus` at module scope — reuse them):

```python
# ---------------------------------------------------------------------------
# POST /internal/llm-connected (1C): the tenant reports its owner connected an
# LLM; control-api records the provider NAME and revokes the trial key.
# ---------------------------------------------------------------------------


class TestLlmConnected:
    def _tenant_with_trial_key(self, session):
        tenant = Tenant(
            id="t-conn-1",
            email="conn@example.com",
            status=TenantStatus.RUNNING,
            trial_key_alias="squire-trial-t-conn-1",
            trial_key_active=True,
        )
        session.add(tenant)
        session.commit()
        return tenant

    @respx.mock
    def test_records_provider_and_revokes_trial_key(self, client, auth, session):
        self._tenant_with_trial_key(session)
        delete = respx.post("https://trial-proxy.squire.test/key/delete").mock(
            return_value=httpx.Response(200, json={"deleted_keys": 1})
        )
        response = client.post(
            "/internal/llm-connected",
            json={"tenant_id": "t-conn-1", "provider": "openai"},
            headers=auth,
        )
        assert response.status_code == 200
        assert response.json() == {
            "tenant_id": "t-conn-1",
            "provider": "openai",
            "connected_provider_recorded": True,
            "trial_key_revoked": True,
        }
        assert delete.called

        with db.session_scope() as s:
            tenant = s.get(Tenant, "t-conn-1")
            assert tenant.connected_provider == "openai"
            assert tenant.trial_key_active is False

    def test_idempotent_when_trial_key_already_gone(self, client, auth, session):
        session.add(Tenant(id="t-conn-2", email="conn2@example.com",
                           status=TenantStatus.RUNNING, trial_key_active=False))
        session.commit()
        response = client.post(
            "/internal/llm-connected",
            json={"tenant_id": "t-conn-2", "provider": "chatgpt"},
            headers=auth,
        )
        assert response.status_code == 200
        assert response.json()["trial_key_revoked"] is False
        assert response.json()["connected_provider_recorded"] is True

    def test_unknown_tenant_404(self, client, auth):
        response = client.post(
            "/internal/llm-connected",
            json={"tenant_id": "t-none", "provider": "openai"},
            headers=auth,
        )
        assert response.status_code == 404

    def test_requires_internal_token(self, client):
        assert client.post(
            "/internal/llm-connected",
            json={"tenant_id": "t", "provider": "openai"},
        ).status_code == 401

    def test_provider_vocabulary_is_closed(self, client, auth, session):
        self._tenant_with_trial_key(session)
        # Not a provider name -> 422. This is the privacy guard: the field can
        # never carry key material because only three literals fit through it.
        response = client.post(
            "/internal/llm-connected",
            json={"tenant_id": "t-conn-1", "provider": "sk-ant-api03-oops"},
            headers=auth,
        )
        assert response.status_code == 422

    def test_extra_fields_are_rejected(self, client, auth, session):
        self._tenant_with_trial_key(session)
        response = client.post(
            "/internal/llm-connected",
            json={"tenant_id": "t-conn-1", "provider": "openai",
                  "api_key": "sk-should-never-fit"},
            headers=auth,
        )
        assert response.status_code == 422
```

Append to `apps/control-api/tests/test_cross_service_contracts.py`:

```python
# ---------------------------------------------------------------------------
# tenant -> control-api: the llm-connected conversion call (1C). Same pattern
# as the wake-typing pins: route + payload keys agreed between
# tenant-image/bin/squire_connect.py and control_api with only this test
# connecting them, and the tenant caller swallows failures by design.
# ---------------------------------------------------------------------------

TENANT_CONNECT = (
    Path(__file__).resolve().parents[3] / "tenant-image" / "bin" / "squire_connect.py"
)


@pytest.fixture(scope="module")
def tenant_connect_source() -> str:
    if not TENANT_CONNECT.is_file():  # pragma: no cover - not in this checkout
        pytest.skip(f"tenant image not present at {TENANT_CONNECT}")
    return TENANT_CONNECT.read_text(encoding="utf-8")


def test_tenant_calls_the_route_control_api_serves(client, tenant_connect_source):
    assert "/internal/llm-connected" in tenant_connect_source, (
        "squire_connect no longer targets /internal/llm-connected; if the path "
        "moved, move control-api's route with it (and vice versa)."
    )
    # Unauthenticated probe: 401 proves the route exists AND is behind auth.
    assert client.post("/internal/llm-connected", json={}).status_code == 401


def test_tenant_payload_keys_match_the_request_schema(tenant_connect_source):
    from control_api.schemas import LlmConnectedRequest

    assert 'json.dumps({"tenant_id": tenant_id, "provider": provider})' in tenant_connect_source, (
        "squire_connect's notify payload literal changed; update this pin and "
        "confirm the keys still match LlmConnectedRequest."
    )
    assert set(LlmConnectedRequest.model_fields) == {"tenant_id", "provider"}
```

In `apps/control-api/tests/test_privacy_schema.py`, add to `EXPECTED_COLUMNS["tenant"]` (after `"bind_nonce",`):

```python
        # 1C: which provider the owner connected. The NAME only — "openai",
        # "anthropic" or "chatgpt" — pinned to a closed Literal in
        # schemas.LlmConnectedRequest, so this column is structurally unable
        # to hold key material.
        "connected_provider",
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/control-api && python -m pytest tests/test_internal_api.py::TestLlmConnected tests/test_privacy_schema.py tests/test_cross_service_contracts.py -q`
Expected: llm-connected tests 404 (no route); privacy test fails (column whitelisted but absent).

- [ ] **Step 3: Implement**

`apps/control-api/src/control_api/models.py` — in `Tenant`, after the `bind_nonce` field:

```python
    # 1C: which provider the owner connected ("openai" / "anthropic" /
    # "chatgpt"). The NAME only, never credential material — the ingest schema
    # pins it to a closed Literal, and test_privacy_schema whitelists it.
    connected_provider: str | None = None
```

`apps/control-api/src/control_api/schemas.py` — add (near `RevokeTrialKeyResponse`; `Literal` comes from `typing`):

```python
class LlmConnectedRequest(BaseModel):
    """POST /internal/llm-connected -- a tenant reports its owner connected an LLM.

    `provider` is a CLOSED vocabulary on purpose: three literals fit through
    this field and nothing else, so it is structurally unable to smuggle key
    material into the control plane. `extra="forbid"` for the same reason as
    every other tenant-writable schema here.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    provider: Literal["openai", "anthropic", "chatgpt"]


class LlmConnectedResponse(BaseModel):
    tenant_id: str
    provider: str
    connected_provider_recorded: bool
    trial_key_revoked: bool
```

`apps/control-api/src/control_api/provisioning.py` — add after `revoke_trial_key`:

```python
def record_llm_connected(
    session: Session, tenant_id: str, provider: str,
    clients: ProvisionClients | None = None,
) -> bool:
    """The conversion moment, control-plane side: record the provider NAME and
    revoke the trial key immediately (PRD §2: their traffic never touches our
    infrastructure again). Returns whether a key was actually revoked --
    idempotent, because the tenant retries and the heartbeat backstop can race.
    """
    tenant = get_tenant(session, tenant_id)
    tenant.connected_provider = provider
    _touch(session, tenant)
    return revoke_trial_key(session, tenant_id, clients=clients)
```

`apps/control-api/src/control_api/routers/internal.py` — add after the `revoke_trial_key` route, and extend the schemas import with `LlmConnectedRequest, LlmConnectedResponse`:

```python
@router.post("/llm-connected", response_model=LlmConnectedResponse)
def llm_connected(
    payload: LlmConnectedRequest, session: Session = Depends(get_session)
) -> LlmConnectedResponse:
    """Called by the tenant's connected pipeline (squire_connect.py) the moment
    its owner's credential lands. Records the provider name (values are pinned
    to a closed Literal -- never key material) and revokes the trial key."""
    try:
        revoked = provisioning.record_llm_connected(
            session, payload.tenant_id, payload.provider
        )
    except provisioning.TenantNotFound as exc:
        raise HTTPException(status_code=404, detail="tenant not found") from exc
    return LlmConnectedResponse(
        tenant_id=payload.tenant_id,
        provider=payload.provider,
        connected_provider_recorded=True,
        trial_key_revoked=revoked,
    )
```

- [ ] **Step 4: Run the control-api suite**

Run: `cd apps/control-api && python -m pytest -q`
Expected: all green (the privacy whitelist, the new endpoint tests, and the contract pins all pass; nothing else touched the schema).

- [ ] **Step 5: Commit**

```bash
git add apps/control-api/src/control_api/models.py apps/control-api/src/control_api/schemas.py apps/control-api/src/control_api/provisioning.py apps/control-api/src/control_api/routers/internal.py apps/control-api/tests/test_privacy_schema.py apps/control-api/tests/test_internal_api.py apps/control-api/tests/test_cross_service_contracts.py
git commit -m "1C: /internal/llm-connected — record connected_provider, revoke trial key (ALTER TABLE required, see rollout)"
```

### Task 8: Provisioning — public domain per tenant (`serviceDomainCreate`)

**Files:**
- Modify: `apps/control-api/src/control_api/clients/railway.py`
- Modify: `apps/control-api/src/control_api/models.py` (new step)
- Modify: `apps/control-api/src/control_api/provisioning.py`
- Modify: `apps/control-api/tests/railway_fake.py`
- Modify: `apps/control-api/tests/test_state_machine.py`

**RAILWAY_PUBLIC_DOMAIN — investigation finding (documented for the record):** Railway lists `RAILWAY_PUBLIC_DOMAIN` among its auto-provided runtime variables — it appears in a service's environment once the service has a public domain, and (like all Railway-provided variables) it is stamped at deploy time. Our `CREATE_DOMAIN` step is ordered *before* `DEPLOY` precisely so the tenant's first deploy already carries it. This claim could NOT be re-verified against live docs from the planning sandbox (docs endpoint unreachable), so the plan does not bet on it: `_step_set_variables` also writes the domain explicitly as `SQUIRE_PUBLIC_DOMAIN` (read back from the Railway API by name; nothing is persisted in the control DB), and all tenant code prefers `RAILWAY_PUBLIC_DOMAIN` with `SQUIRE_PUBLIC_DOMAIN` as fallback. **Task 12 verifies on staging whether Railway injection actually occurred and records the answer.**

- [ ] **Step 1: Teach the fake Railway the two new operations**

`FakeRailway.handler` raises `AssertionError` on unknown operations, so every provisioning test will fail loudly until this lands — that IS the failing-test step. In `apps/control-api/tests/railway_fake.py`, add a field to the dataclass:

```python
    #: service_id -> public domain. serviceDomainCreate populates it; the
    #: `domains` query probe reads it. Pre-seed to simulate a pre-existing
    #: domain (the probe-first idempotency case).
    service_domains: dict[str, str] = field(default_factory=dict)
```

and two branches in `handler` (before the final `raise AssertionError`):

```python
        if op == "domains":
            domain = self.service_domains.get(variables["serviceId"])
            service_domains = [{"domain": domain}] if domain else []
            return self._ok({"domains": {"serviceDomains": service_domains}})

        if op == "serviceDomainCreate":
            sid = variables["input"]["serviceId"]
            domain = self.service_domains.setdefault(sid, f"{sid}-test.up.railway.app")
            return self._ok({"serviceDomainCreate": {"id": f"dom-{sid}", "domain": domain}})
```

- [ ] **Step 2: Write the failing state-machine tests**

Append to `apps/control-api/tests/test_state_machine.py`:

```python
# ---------------------------------------------------------------------------
# 1C: public domain per tenant (the /connect page's front door)
# ---------------------------------------------------------------------------


@respx.mock
def test_domain_is_created_before_deploy_and_reaches_the_variables(pool_bot, fake_railway):
    mock_all(fake_railway)
    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id)
        assert job.status == JobStatus.DONE, job.last_error

    ops = fake_railway.operations()
    assert "serviceDomainCreate" in ops
    # Deploy-time injection of RAILWAY_PUBLIC_DOMAIN only works if the domain
    # exists before the deploy — the ordering IS the feature.
    assert ops.index("serviceDomainCreate") < ops.index("serviceInstanceDeploy")

    sent = fake_railway.variables_for("variableCollectionUpsert")["input"]["variables"]
    domain = next(iter(fake_railway.service_domains.values()))
    # Belt and braces: the domain is ALSO an explicit variable, so the tenant
    # never depends on Railway's injection timing.
    assert sent["SQUIRE_PUBLIC_DOMAIN"] == domain


@respx.mock
def test_domain_step_is_probe_first_idempotent(pool_bot, fake_railway):
    """serviceDomainCreate's idempotency is UNVERIFIED against the live API, so
    the step probes first — the same discipline attach_volume earned the hard
    way (volumeCreate silently duplicates)."""
    mock_all(fake_railway)
    with db.session_scope() as s:
        tenant, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
        # Simulate a previous attempt that created service + domain, then died.
        name = provisioning.service_name_for(tenant.id)
    fake_railway.existing_services[name] = "svc-pre"
    fake_railway.service_domains["svc-pre"] = "pre-existing.up.railway.app"

    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id)
        assert job.status == JobStatus.DONE, job.last_error

    assert "serviceDomainCreate" not in fake_railway.operations(), (
        "a pre-existing domain must be adopted, not duplicated"
    )
    sent = fake_railway.variables_for("variableCollectionUpsert")["input"]["variables"]
    assert sent["SQUIRE_PUBLIC_DOMAIN"] == "pre-existing.up.railway.app"


@respx.mock
def test_domain_probe_failure_is_retryable(pool_bot, fake_railway):
    mock_all(fake_railway)
    fake_railway.fail_on.add("domains")
    with db.session_scope() as s:
        _, job = provisioning.create_tenant(s, email="alpha@squire.test")
        job_id = job.id
    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id)
        assert job.status == JobStatus.PENDING
        assert job.step == ProvisionStep.CREATE_DOMAIN

    fake_railway.fail_on.discard("domains")
    with db.session_scope() as s:
        job = provisioning.advance_job(s, job_id, force=True)
        assert job.status == JobStatus.DONE, job.last_error
```

- [ ] **Step 3: Run to verify failure**

Run: `cd apps/control-api && python -m pytest tests/test_state_machine.py -q`
Expected: the three new tests fail (`ProvisionStep` has no `CREATE_DOMAIN`; `SQUIRE_PUBLIC_DOMAIN` absent).

- [ ] **Step 4: Implement**

`apps/control-api/src/control_api/clients/railway.py` — add after `get_variable_names`:

```python
    # -- public domains (1C connect page) ----------------------------------
    # !! UNVERIFIED against the live API (unlike everything above — see the
    # module header). Shapes follow Railway's public schema:
    # serviceDomainCreate(input:{environmentId, serviceId}) and the `domains`
    # query. Verify live during the 1C staging rollout and update this note.

    def get_service_domain(self, service_id: str) -> str | None:
        """The service's generated public domain, or None if none exists yet.

        The probe half of create_service_domain's idempotency: domain creation
        has not been proven idempotent, so (exactly like attach_volume) we
        never mutate without first establishing absence.
        """
        query = """
        query domains($projectId: String!, $environmentId: String!, $serviceId: String!) {
          domains(
            projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId
          ) {
            serviceDomains { domain }
          }
        }
        """
        data = self._gql(
            query,
            {
                "projectId": self.project_id,
                "environmentId": self.environment_id,
                "serviceId": service_id,
            },
        )
        for entry in ((data.get("domains") or {}).get("serviceDomains")) or []:
            domain = (entry or {}).get("domain")
            if domain:
                return domain
        return None

    def create_service_domain(self, service_id: str) -> str:
        """Generate a Railway public domain (`tenant-….up.railway.app`) for the
        service. Callers must probe with get_service_domain first."""
        mutation = """
        mutation serviceDomainCreate($input: ServiceDomainCreateInput!) {
          serviceDomainCreate(input: $input) { id domain }
        }
        """
        data = self._gql(
            mutation,
            {"input": {"environmentId": self.environment_id, "serviceId": service_id}},
        )
        domain = (data.get("serviceDomainCreate") or {}).get("domain")
        if not domain:
            raise RailwayError(f"serviceDomainCreate returned no domain: {data}")
        return domain
```

`apps/control-api/src/control_api/models.py` — in `ProvisionStep`, after `ATTACH_VOLUME`:

```python
    CREATE_DOMAIN = "create_domain"
```

and in `PROVISION_STEP_ORDER`, insert `ProvisionStep.CREATE_DOMAIN` between `ATTACH_VOLUME` and `CREATE_TRIAL_KEY`.

`apps/control-api/src/control_api/provisioning.py` — add after `_step_attach_volume`:

```python
def _step_create_domain(session: Session, tenant: Tenant, clients: ProvisionClients) -> None:
    """Public Railway domain for the 1C /connect page (design decision 3:
    Railway-generated domains for alpha; custom domains are Phase 1 polish).

    Probe-first, like attach_volume: serviceDomainCreate's idempotency is
    unverified, and a retry must adopt rather than duplicate. The domain is
    deliberately NOT persisted on the tenant row — _step_set_variables reads
    it back from Railway by name, and no control-plane query path needs it.
    Ordering before DEPLOY is load-bearing: Railway stamps
    RAILWAY_PUBLIC_DOMAIN into the container at deploy time, so the domain
    must exist before the first deploy runs.
    """
    if clients.railway.get_service_domain(tenant.railway_service_id):
        return
    clients.railway.create_service_domain(tenant.railway_service_id)
    _touch(session, tenant)
```

register it in `_STEP_HANDLERS`:

```python
    ProvisionStep.CREATE_DOMAIN: _step_create_domain,
```

and in `_step_set_variables`, add to the `variables` dict (after the `SQUIRE_HEARTBEAT_INTERVAL` entry):

```python
        # 1C: the tenant's public domain, written explicitly so the connect
        # CLI never depends on Railway's deploy-time RAILWAY_PUBLIC_DOMAIN
        # injection actually happening. Read back by name from Railway (the
        # domain is not stored in the control DB). Tenant code prefers
        # RAILWAY_PUBLIC_DOMAIN and falls back to this.
        "SQUIRE_PUBLIC_DOMAIN": clients.railway.get_service_domain(
            tenant.railway_service_id
        ) or "",
```

- [ ] **Step 5: Run the whole control-api suite and fix step-order pins**

Run: `cd apps/control-api && python -m pytest -q`

Every failure will be one of two mechanical kinds — fix exactly these, nothing else:
1. A test whose FakeRailway sees the new `domains`/`serviceDomainCreate` operations in an operations-list assertion (add them to the expected list in provisioning order: after the volume operations, before `variableCollectionUpsert`).
2. A test pinning the step sequence around `ATTACH_VOLUME` → `CREATE_TRIAL_KEY` (the known pins are the `job.step == ProvisionStep...` assertions at `tests/test_state_machine.py` lines ~351, ~570, ~859, ~1017, ~1070, and any step list in `tests/test_infra_cli.py`): where a test drove the machine to a step *after* `ATTACH_VOLUME` by counting advances, one more advance (or the new step name) is now expected.

Expected after fixes: full suite green.

- [ ] **Step 6: Commit**

```bash
git add apps/control-api/src/control_api/clients/railway.py apps/control-api/src/control_api/models.py apps/control-api/src/control_api/provisioning.py apps/control-api/tests/railway_fake.py apps/control-api/tests/test_state_machine.py apps/control-api/tests/test_infra_cli.py
git commit -m "1C: CREATE_DOMAIN provisioning step — public domain before deploy, SQUIRE_PUBLIC_DOMAIN variable"
```

---

### Task 9: Heartbeat connected-marker — the reconciliation backstop

**Files:**
- Modify: `tenant-image/bin/squire-heartbeat.py`
- Modify: `tenant-image/tests/test_heartbeat.py`
- Modify: `apps/control-api/src/control_api/schemas.py`
- Modify: `apps/control-api/src/control_api/models.py`
- Modify: `apps/control-api/src/control_api/provisioning.py`
- Modify: `apps/control-api/tests/test_privacy_schema.py`
- Modify: `apps/control-api/tests/test_internal_api.py`

If the pipeline's `/internal/llm-connected` call is lost (control-api down, container killed mid-pipeline), the trial key stays active for a converted tenant. The heartbeat closes that: one boolean, derived from the same markers `squire-hindsight-env.sh` uses **plus the auth.json OAuth artifact** (fixing the known `.env`-only CONNECTED_MARKERS gap the design spec names) — key NAMES and value *presence* only, never values, and only a boolean leaves the container.

- [ ] **Step 1: Write the failing tenant-side tests**

In `tenant-image/tests/test_heartbeat.py`:
(a) add `"llm_connected",` to the `ALLOWED_FIELDS` set at the top;
(b) append before the final failure-summary block (the file already imports `importlib.util` for its `load_emitter` helper at line ~285 — if not, add `import importlib.util`):

```python
print("== llm_connected marker (1C reconciliation backstop) ==")
# One boolean, derived from credential ARTIFACTS (env key names + auth.json
# token presence). No value ever leaves the container; the whitelist test
# above still pins the full payload.
import importlib.util as _ilu  # noqa: E402


def _load_hb(tag, home):
    saved = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = home
    try:
        spec = _ilu.spec_from_file_location(tag, EMITTER)
        module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = saved


home_none = tempfile.mkdtemp()
hb = _load_hb("hb_llm_none", home_none)
check("no artifacts -> llm_connected False", hb.llm_connected() is False)
check("llm_connected is whitelisted for the payload",
      "llm_connected" in hb.PAYLOAD_FIELDS, hb.PAYLOAD_FIELDS)

home_env = tempfile.mkdtemp()
with open(os.path.join(home_env, ".env"), "w") as fh:
    fh.write("SOMETHING_ELSE=1\nOPENAI_API_KEY=sk-test-connected-000\n")
hb = _load_hb("hb_llm_env", home_env)
check(".env provider key -> llm_connected True", hb.llm_connected() is True)

home_empty = tempfile.mkdtemp()
with open(os.path.join(home_empty, ".env"), "w") as fh:
    fh.write("OPENAI_API_KEY=\n")  # present but EMPTY: not connected
hb = _load_hb("hb_llm_empty", home_empty)
check("empty marker value -> not connected", hb.llm_connected() is False)

home_auth = tempfile.mkdtemp()
with open(os.path.join(home_auth, "auth.json"), "w") as fh:
    json.dump({"auth_mode": "chatgpt",
               "tokens": {"access_token": "at-x", "refresh_token": "rt-x",
                          "id_token": "", "account_id": "a"}}, fh)
hb = _load_hb("hb_llm_auth", home_auth)
check("auth.json OAuth artifact -> llm_connected True (the CONNECTED_MARKERS gap)",
      hb.llm_connected() is True)

home_corrupt = tempfile.mkdtemp()
with open(os.path.join(home_corrupt, "auth.json"), "w") as fh:
    fh.write("{ not json")
hb = _load_hb("hb_llm_corrupt", home_corrupt)
check("corrupt auth.json reads as not connected (collector never raises)",
      hb.llm_connected() is False)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 tenant-image/tests/test_heartbeat.py`
Expected: `AttributeError: ... has no attribute 'llm_connected'`.

- [ ] **Step 3: Implement the collector**

In `tenant-image/bin/squire-heartbeat.py`:
(a) add `"llm_connected",` to `PAYLOAD_FIELDS`;
(b) add a collector after `backup_age_seconds()`:

```python
def llm_connected() -> bool:
    """True once the tenant holds its owner's own LLM credential (1C).

    The RECONCILIATION BACKSTOP for the connect flow: if the pipeline's
    /internal/llm-connected call was lost, control-api sees this flag against
    a still-active trial key on the next beat and revokes it then.

    Privacy discipline: this reads credential FILES, which nothing else in
    this program does — so what it may extract is pinned hard. It checks key
    NAMES against the same marker set squire-hindsight-env.sh uses, plus the
    auth.json token artifact the .env-only markers famously miss, and emits
    ONE BOOLEAN. No value, length, or prefix ever leaves this function.
    """
    markers = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    try:
        with open(os.path.join(HERMES_HOME, ".env"), encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.partition("=")
                if sep and key.strip() in markers and value.strip():
                    return True
    except OSError:
        pass
    try:
        with open(os.path.join(HERMES_HOME, "auth.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        tokens = data.get("tokens") if isinstance(data, dict) else None
        if isinstance(tokens, dict) and tokens.get("access_token"):
            return True
    except (OSError, ValueError):
        pass
    return False
```

(c) in `build_payload()`, next to the two liveness booleans:

```python
        "llm_connected": llm_connected(),
```

- [ ] **Step 4: Run the tenant suite**

Run: `python3 tenant-image/tests/test_heartbeat.py`
Expected: ALL PASS (whitelist updated in step 1a, so the main payload sections accept the new field).

- [ ] **Step 5: Write the failing control-api reconciliation test**

Append to `apps/control-api/tests/test_internal_api.py` (inside or alongside `TestLlmConnected`):

```python
class TestHeartbeatReconciliation:
    @respx.mock
    def test_connected_beat_with_live_trial_key_revokes_it(self, client, auth, session):
        session.add(Tenant(id="t-beat-1", email="beat@example.com",
                           status=TenantStatus.RUNNING,
                           trial_key_alias="squire-trial-t-beat-1",
                           trial_key_active=True))
        session.commit()

        delete = respx.post("https://trial-proxy.squire.test/key/delete").mock(
            return_value=httpx.Response(200, json={"deleted_keys": 1})
        )
        response = client.post(
            "/internal/heartbeat",
            json={"tenant_id": "t-beat-1", "uptime_seconds": 10,
                  "gateway_up": True, "hindsight_up": True,
                  "llm_connected": True},
            headers=auth,
        )
        assert response.status_code == 200
        assert delete.called, "backstop must revoke the orphaned trial key"

        with db.session_scope() as s:
            assert s.get(Tenant, "t-beat-1").trial_key_active is False

    @respx.mock
    def test_unconnected_beat_leaves_the_trial_key_alone(self, client, auth, session):
        session.add(Tenant(id="t-beat-2", email="beat2@example.com",
                           status=TenantStatus.RUNNING,
                           trial_key_alias="squire-trial-t-beat-2",
                           trial_key_active=True))
        session.commit()

        delete = respx.post("https://trial-proxy.squire.test/key/delete").mock(
            return_value=httpx.Response(200, json={"deleted_keys": 1})
        )
        response = client.post(
            "/internal/heartbeat",
            json={"tenant_id": "t-beat-2", "uptime_seconds": 10,
                  "gateway_up": True, "hindsight_up": True,
                  "llm_connected": False},
            headers=auth,
        )
        assert response.status_code == 200
        assert not delete.called
```

Also add `"llm_connected",` to `EXPECTED_COLUMNS["heartbeat"]` in `apps/control-api/tests/test_privacy_schema.py` with this comment:

```python
        # 1C backstop: ONE tenant-reported boolean ("my owner connected an
        # LLM"). Not derived from anything a user said; carries no credential.
        "llm_connected",
```

- [ ] **Step 6: Run to verify failure**

Run: `cd apps/control-api && python -m pytest tests/test_internal_api.py tests/test_privacy_schema.py -q`
Expected: 422 (unknown field `llm_connected` — `extra="forbid"`), and the privacy whitelist mismatch.

- [ ] **Step 7: Implement the control-api half**

`apps/control-api/src/control_api/schemas.py` — in `HeartbeatRequest`, next to the other optional fields:

```python
    #: 1C reconciliation backstop: the tenant's own "owner has connected an
    #: LLM" flag, derived from credential artifacts on the tenant side. A
    #: True against a still-active trial key triggers revocation.
    llm_connected: bool | None = None
```

`apps/control-api/src/control_api/models.py` — in `Heartbeat`, next to the liveness booleans:

```python
    # 1C: tenant-reported connected flag (see schemas.HeartbeatRequest).
    llm_connected: bool | None = None
```

`apps/control-api/src/control_api/provisioning.py` — in `record_heartbeat`, change the second line to capture the tenant, and append the reconciliation after `session.refresh(row)`:

```python
    tenant = get_tenant(session, tenant_id)  # raises TenantNotFound -> 404
```

```python
    # 1C reconciliation backstop: a converted tenant whose
    # /internal/llm-connected call was lost still gets its trial key revoked
    # on the next beat. Idempotent (revoke_trial_key no-ops once inactive);
    # worst case on a missed beat, the trial cap still bounds spend.
    if fields.get("llm_connected") and tenant.trial_key_active:
        log.info("heartbeat reconciliation: tenant %s connected an LLM but the "
                 "trial key is still active — revoking", tenant_id)
        revoke_trial_key(session, tenant_id)
    return row
```

(remove the old bare `return row`).

- [ ] **Step 8: Run both suites**

Run: `cd apps/control-api && python -m pytest -q && cd ../.. && python3 tenant-image/tests/test_heartbeat.py`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add tenant-image/bin/squire-heartbeat.py tenant-image/tests/test_heartbeat.py apps/control-api/src/control_api/schemas.py apps/control-api/src/control_api/models.py apps/control-api/src/control_api/provisioning.py apps/control-api/tests/test_privacy_schema.py apps/control-api/tests/test_internal_api.py
git commit -m "1C: llm_connected heartbeat marker + trial-key reconciliation backstop (ALTER TABLE required, see rollout)"
```

---

### Task 10: Concierge rewrite — three live paths

**Files:**
- Modify: `tenant-image/home-template/skills/concierge/state-machine.yaml`
- Modify: `tenant-image/bin/squire-concierge-hook.py`
- Modify: `tenant-image/tests/test_concierge_onboarding.py`

**Precondition:** `fix/drop-claude-sub-option` has landed (Branch setup, top of this plan): `claude_max_oauth` is already gone from the providers block, the hook, and the drift suite, which now expect THREE providers. This task does not touch anything Claude-related beyond that assumption. If `grep -n claude_max_oauth tenant-image/ -r` still matches, STOP and merge that branch first.

- [ ] **Step 1: Write the failing drift tests**

In `tenant-image/tests/test_concierge_onboarding.py`, **replace** the coming-soon block (the checks between the comment `# -- the coming-soon truth lives in the providers block, machine-readably ----` at ~line 1061 and the `claude setup-token`-adjacent checks — i.e. every check that asserts `openai_codex_oauth` is `coming_soon`, that its label says "coming soon", or that its honest_label says "not connectable") with:

```python
# -- 1C: all three paths are LIVE; the connect flow runs through the CLI ----
check(
    "no provider is marked coming_soon any more (1C shipped)",
    [p["id"] for p in MACHINE["providers"] if p.get("status") == "coming_soon"] == [],
)
check(
    "exactly the three 1C providers exist",
    set(providers) == {"openai_api_key", "openai_codex_oauth", "anthropic_api_key"},
    set(providers),
)
check(
    "the hook's coming-soon set is empty to match",
    getattr(hook, "_COMING_SOON_PROVIDERS", None) == set(),
    getattr(hook, "_COMING_SOON_PROVIDERS", None),
)
check(
    "the codex option no longer claims 'coming soon' anywhere",
    "coming soon" not in flat(providers["openai_codex_oauth"]["label"]).lower()
    and "coming soon" not in flat(providers["openai_codex_oauth"]["honest_label"]).lower(),
)
check(
    "the codex honest label uses the spike's softened wording, not 'sanctions'",
    "openly supports" in flat(providers["openai_codex_oauth"]["honest_label"])
    and "no formal written guarantee" in flat(providers["openai_codex_oauth"]["honest_label"])
    and "sanction" not in flat(providers["openai_codex_oauth"]["honest_label"]).lower(),
    providers["openai_codex_oauth"]["honest_label"],
)
check(
    "the codex option teaches the enable-device-login caveat",
    "device code authorization" in flat(providers["openai_codex_oauth"]["notes"]),
    providers["openai_codex_oauth"].get("notes"),
)
check(
    "codex fallback is still the OpenAI key path",
    providers["openai_codex_oauth"]["fallback"] == "openai_api_key",
)

# The hook's connect directives drive the CLI, never invented URLs.
check(
    "connect_llm directive mints links via the CLI",
    "squire-llm-connect mint-link" in all_ctx["connect_llm"],
)
check(
    "awaiting_credential drives status polling via the CLI",
    "squire-llm-connect status" in all_ctx["awaiting_credential"],
)
check(
    "the device-flow directive starts the real flow",
    "start openai-device" in getattr(hook, "_AWAITING_DEVICE_FLOW", ""),
)
check(
    "the device-flow directive handles the not_enabled branch",
    "not_enabled" in getattr(hook, "_AWAITING_DEVICE_FLOW", "")
    and "Security" in getattr(hook, "_AWAITING_DEVICE_FLOW", ""),
)
check(
    "the connected directive celebrates and may now state revocation truthfully",
    "revoked" in all_ctx["connected"] and "pleased" in all_ctx["connected"],
)
check(
    "no directive still claims the one-time link does not exist",
    all("DOES NOT EXIST" not in ctx for ctx in all_ctx.values()),
)
```

Also DELETE the now-false assertions elsewhere in the file that pin the pre-1C world; find each with these greps and remove exactly the checks they hit (each currently asserts copy this task removes):
- `grep -n "DOES NOT EXIST\|does not exist yet\|never invent a URL" tenant-image/tests/test_concierge_onboarding.py`
- `grep -n "coming soon\|coming_soon\|_AWAITING_COMING_SOON" tenant-image/tests/test_concierge_onboarding.py`
- `grep -n "has been revoked\|now goes direct" tenant-image/tests/test_concierge_onboarding.py` (the `connected` state's old "do NOT say revoked" pins)

One removal is a REPLACEMENT, not a deletion: the line (~1230)
`_dirs["awaiting_credential (coming-soon variant)"] = hook._AWAITING_COMING_SOON`
feeds the shared style checks that follow it — change it to
`_dirs["awaiting_credential (device-flow variant)"] = hook._AWAITING_DEVICE_FLOW`
so the new variant directive stays under the same style/formatting assertions.

- [ ] **Step 2: Run to verify failure**

Run: `python3 tenant-image/tests/test_concierge_onboarding.py`
Expected: the new checks FAIL (`coming_soon` still set; `_AWAITING_DEVICE_FLOW` missing).

- [ ] **Step 3: Rewrite the YAML providers block and connect states**

In `tenant-image/home-template/skills/concierge/state-machine.yaml`:

**(a)** Replace the `openai_codex_oauth` provider entry with:

```yaml
  - id: openai_codex_oauth
    label: OpenAI ChatGPT subscription (via Codex)
    blurb: >
      Use your existing paid ChatGPT plan instead of metered API billing. You
      approve a short code on OpenAI's own site — no key to paste, and your
      sign-in never touches Squire's servers.
    honest_label: >
      OpenAI's Codex team openly supports third-party agents using ChatGPT
      sign-in (unlike Anthropic); there's no formal written guarantee, and
      OpenAI could change this.
    credential: oauth_device_code
    env: OPENAI_API_KEY
    fallback: openai_api_key
    notes: >
      Device sign-in is a beta ChatGPT feature and OFF by default. If the
      start command reports it is not enabled, walk them through it exactly
      once: chatgpt.com -> Settings -> Security -> turn on "device code
      authorization" (a Team/Enterprise workspace needs an admin), then start
      again. A free ChatGPT plan cannot connect — say so plainly and offer
      the API-key path. Never paste or ask for tokens in chat: the flow
      delivers them provider -> container directly.
```

**(b)** Replace the `connect_llm` state's `say` block with:

```yaml
    say:
      - >-
        If they picked an API-key path (OpenAI or Anthropic): confirm in one
        warm line, then run `/opt/squire/bin/squire-llm-connect mint-link`
        with the terminal tool and send them the URL it prints. Tell them it
        is served by their own container, single-use, and expires in 15
        minutes — the key goes browser -> their agent and never through
        Telegram or Squire's shared services. If the command reports
        no_public_domain, fall back to the paste-in-chat path with its full
        disclosure (see awaiting_credential).
      - >-
        If they picked the ChatGPT subscription: run
        `/opt/squire/bin/squire-llm-connect start openai-device` with the
        terminal tool. Relay exactly what it prints — the verification URL
        and the short code (both are safe for chat; they expire in 15
        minutes) — and tell them to open the URL, sign in to ChatGPT, and
        type the code. If it prints not_enabled instead, give the
        enable-device-login walkthrough from the provider notes, once, and
        offer to start again after they flip it.
      - >-
        If they have none of the three, or say "not now": accept it
        immediately, say the built-in allowance keeps working meanwhile, set
        the state to ask_timezone, and move on. Never nag.
    ask: null
    then: awaiting_credential
```

**(c)** Replace the `awaiting_credential` state with:

```yaml
  awaiting_credential:
    goal: Land the chosen credential — link for keys, device code for ChatGPT.
    say:
      - >-
        For a key path: they have the one-time link. While they use it, poll
        `/opt/squire/bin/squire-llm-connect status` with the terminal tool
        when they say they're done (or after a natural pause). "connected"
        means move to the connected step. If the link expired, mint a fresh
        one without ceremony.
      - >-
        For the ChatGPT device flow: they have the URL and code. Poll the
        same status command. On "denied" for a free plan, say a paid plan is
        required and offer the OpenAI API-key path. On "timed_out", offer to
        start again. On "not_enabled", give the Settings -> Security
        walkthrough (once) and restart the flow when they say it's on.
      - >-
        If they paste an API key into the chat anyway, that still works:
        handle it exactly as `on_paste_in_chat` says — store it, delete
        their message, and give the transit disclosure, unminimised.
    ask: null
    then: connected
    on_paste_in_chat:
      - Store the key, then delete their Telegram message immediately.
      - Tell them once, plainly, in a couple of short lines rather than a
        security lecture, that a pasted key did transit Telegram and our
        relay, and that they can rotate it if they want to be careful.
      - >-
        Never minimise that disclosure: qualifiers like "totally fine",
        "don't worry" and "perfectly safe" are banned. The deletion and the
        rotation offer are the reassurance; the words must not editorialise.
    on_failure:
      - If the credential is rejected, say what the provider said, in one line.
      - Offer the next-best path for that provider (see providers[].fallback).
      - Never loop more than twice; offer to come back to it later instead.
    notes: >
      The link and the device code are minted by the tenant runtime
      (squire-llm-connect), never invented by the agent. The hook branches
      its directive on the `llm` key connect_llm recorded, so the device-flow
      provider gets the code-relay directive rather than a link.
```

**(d)** In the `connected` state, replace the `notes` block with:

```yaml
    notes: >
      Since 1C shipped, the full pipeline is real: the credential is stored
      encrypted on their own volume, the gateway now talks directly to their
      provider, and control-api revokes our trial key the moment the
      connection lands. It is now TRUE — and good news worth saying — that
      their traffic no longer touches Squire's AI infrastructure and the
      trial key is revoked. Sound pleased; this is the conversion moment.
```

- [ ] **Step 4: Rewrite the hook directives**

In `tenant-image/bin/squire-concierge-hook.py`:

**(a)** Replace the `"connect_llm"` directive value in `_DIRECTIVES` with:

```python
    "connect_llm": """This person has just answered which AI account they have — or said they have
none, or asked something about the choice.

Whatever they picked, record it: in the STEP 1 command, set the "llm" value
to the picked option's exact id — "openai_api_key", "openai_codex_oauth" or
"anthropic_api_key". The next step reads that key to give them the right
hand-off. If they picked nothing, leave the value as it is.

If they picked the OpenAI API key or the Anthropic API key: confirm it in
one warm line, then run this with the terminal tool:

    /opt/squire/bin/squire-llm-connect mint-link

and send them the "url" it prints. Tell them, in two short lines: the link
is served by their own private container, it is single-use and expires in
15 minutes, and the key they paste there goes straight to their agent —
never through Telegram or Squire's shared servers. If the command prints
no_public_domain, they can paste the key here instead — handle a paste
exactly as the next step's rules say, including the transit disclosure.

If they picked the ChatGPT subscription (via Codex): run this with the
terminal tool:

    /opt/squire/bin/squire-llm-connect start openai-device

If it prints a user_code and verification_url, relay BOTH exactly — they
are safe for chat — and tell them: open that URL, sign in to their ChatGPT
account, type the code, done; the code expires in 15 minutes. If it prints
"not_enabled" instead, relay its instructions once (chatgpt.com ->
Settings -> Security -> turn on "device code authorization"; a Team or
Enterprise workspace needs an admin) and offer to start again once they
have flipped it. Never paste tokens in chat and never ask them to.

If they asked about the allowance, Squire's own pricing, or the difference,
read the `facts` block in {skill_file} and answer with the values written
there — never from memory. Keep the two bills separate.

If they have none of the three, or say "not now": accept it immediately
without nagging, say the built-in allowance keeps working meanwhile, and
move on — write the state as "ask_timezone" instead, and ask ONE question:
where are they, or what timezone, so that when you say "tomorrow morning"
you mean it.""",
```

**(b)** Replace the `"awaiting_credential"` directive value with:

```python
    "awaiting_credential": """This person is mid-connect on an API-key path: they have a one-time link
served by their own container.

When they say they've submitted the key (or after a natural pause), run this
with the terminal tool and read the "state" it prints:

    /opt/squire/bin/squire-llm-connect status

"connected": move on — in the STEP 1 command set the state to "connected".
"none" or still pending: they likely haven't finished; offer help, and if
the link expired, run `/opt/squire/bin/squire-llm-connect mint-link` and
send the fresh URL without ceremony.

If they paste an API key into the chat instead, that still works: store it
in {env_file} immediately, then DELETE their Telegram message right away
without asking, then tell them once, plainly and without scolding — two or
three short lines — that the key worked, that you removed the message, and
that a pasted key did travel through Telegram and Squire's relay, so they
can rotate it if they want to be careful. Never minimise that disclosure:
qualifiers like "totally fine", "don't worry" and "perfectly safe" are
banned.

If the page rejected their key, say what it said in one line and offer that
provider's `fallback` from {skill_file}. Never retry more than twice —
offer to come back to it later instead.

If they have gone quiet on it or changed the subject, drop it: answer what
they actually asked and write the state as "complete". You can raise it
again when the trial is nearly up.""",
```

**(c)** Replace the `"connected"` directive value with:

```python
    "connected": """Their own AI account is connected — this is the conversion moment, and it is
good news: sound genuinely pleased about it.

In a few short lines: confirm which provider is live, and say truthfully
that their key is stored encrypted on their own volume, their AI traffic
now goes directly to their provider (it no longer touches Squire's AI
infrastructure), and Squire's built-in trial key for them has been revoked
— their own account is the engine now.

Offer one concrete thing you can now do better. Short lines, not a
paragraph.

Then ask ONE question: where are they, or what timezone, so that when you
say "tomorrow morning" you mean it.""",
```

**(d)** Replace the `_COMING_SOON_PROVIDERS` set, the `_AWAITING_COMING_SOON` string, and the branch in `_build_context` with:

```python
# 1C: nothing is coming-soon any more — all three paths are live. The set is
# kept (empty) because the drift suite pins hook-set == YAML-status-set.
_COMING_SOON_PROVIDERS: set = set()

# Providers whose credential lands via the in-chat device flow rather than the
# one-time link. The awaiting_credential directive branches on this.
_DEVICE_FLOW_PROVIDERS = {"openai_codex_oauth"}

# The `awaiting_credential` directive when the recorded choice is the ChatGPT
# device flow: relay/poll the flow, never demand a paste.
_AWAITING_DEVICE_FLOW = """This person is mid-connect on the ChatGPT device flow: they have (or need)
a short code and the verification URL.

If the flow has not started or they want a fresh code, run this with the
terminal tool and relay the user_code and verification_url it prints —
both are safe for chat:

    /opt/squire/bin/squire-llm-connect start openai-device

If that prints "not_enabled": relay its instructions once — device sign-in
is a beta ChatGPT feature, off by default; they enable it at chatgpt.com ->
Settings -> Security -> "device code authorization" (a Team/Enterprise
workspace needs an admin) — then offer to start again.

While they approve it in the browser, poll with the terminal tool:

    /opt/squire/bin/squire-llm-connect status

"connected": move on — in the STEP 1 command set the state to "connected".
"denied" mentioning the free plan: say plainly that a paid ChatGPT plan is
required, and offer the OpenAI API-key path (in the STEP 1 command set
"llm" to "openai_api_key"). "timed_out": the code expired — offer to start
again. Never ask them to paste any token into the chat.

If they would rather switch to an API key or come back later, accept
warmly — the built-in allowance keeps working meanwhile; for later, write
the state as "ask_timezone" and ask ONE question: where are they, or what
timezone."""
```

and in `_build_context`, replace the coming-soon branch with:

```python
    # Per-provider branch: the ChatGPT device flow lands its credential in
    # chat + on the provider's site, so its awaiting step relays/polls the
    # flow instead of pointing at the one-time link.
    if state == "awaiting_credential" and str(raw.get("llm") or "").strip() in _DEVICE_FLOW_PROVIDERS:
        directive = _AWAITING_DEVICE_FLOW
```

- [ ] **Step 5: Run the drift suite and reconcile stragglers**

Run: `python3 tenant-image/tests/test_concierge_onboarding.py`

The rewritten copy will trip some remaining pre-1C copy pins (the suite has ~360 checks). For each failure, the rule is: if the check asserts pre-1C reality (no link exists, coming soon, do-not-say-revoked), DELETE it; if it asserts an honesty/billing/formatting invariant, FIX THE COPY, not the check. The invariant checks (billing separation, no minimisers, one question per message, `facts`-only pricing) must all still pass unmodified.

Expected: exit 0, all checks pass.

- [ ] **Step 6: Commit**

```bash
git add tenant-image/home-template/skills/concierge/state-machine.yaml tenant-image/bin/squire-concierge-hook.py tenant-image/tests/test_concierge_onboarding.py
git commit -m "1C: concierge rewrite — three live paths via squire-llm-connect (mint-link, device flow, celebration)"
```

---

### Task 11: CI registration

**Files:**
- Modify: `.github/workflows/tenant-image.yml`

New tenant test files must be registered individually (the workflow runs each suite as its own step) and be mode 100755 with a shebang (the "Executable bits are recorded in git" step fails otherwise — already handled at each task's commit).

- [ ] **Step 1: Verify the exec bits are recorded**

Run: `git ls-files -s tenant-image/bin/squire_connect.py tenant-image/bin/squire_codex_device.py tenant-image/bin/squire-llm-connect tenant-image/tests/test_connect.py tenant-image/tests/test_llm_connect_cli.py`
Expected: every line starts with `100755`. If any says `100644`: `git update-index --chmod=+x <file>` and amend the owning commit.

- [ ] **Step 2: Register the two suites**

In `.github/workflows/tenant-image.yml`, after the `- name: Webhook shim contract` step, add:

```yaml
      - name: Credential connect flow (1C)
        if: steps.present.outputs.exists == 'true'
        # Nonce lifecycle (single-use, 15-min TTL, constant-time), the
        # /connect page GET/POST through the real shim against faked provider
        # validation, credential writes resolving the tmpfs symlinks, and the
        # connected pipeline's switch -> restart -> notify ordering.
        run: python3 tenant-image/tests/test_connect.py

      - name: Codex device flow + squire-llm-connect CLI (1C)
        if: steps.present.outputs.exists == 'true'
        # The four spike-pinned device-flow calls against a fake issuer, the
        # 404-not-enabled onboarding branch, free-plan gating, rotating-token
        # refresh discipline, and the CLI's no-credentials-in-output contract.
        run: python3 tenant-image/tests/test_llm_connect_cli.py
```

- [ ] **Step 3: Sanity-run everything the workflow runs**

Run: `python3 -m compileall -q tenant-image/bin tenant-image/tests && python3 tenant-image/tests/test_connect.py && python3 tenant-image/tests/test_llm_connect_cli.py && python3 tenant-image/tests/test_webhook_shim.py && python3 tenant-image/tests/test_heartbeat.py && python3 tenant-image/tests/test_concierge_onboarding.py && python3 tenant-image/tests/test_autopair.py`
Expected: all suites pass; compileall silent.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/tenant-image.yml
git commit -m "1C: register connect + device-flow test suites in tenant-image CI"
```

---

### Task 12: Rollout runbook (image v0.2.0 + control-api)

This task is OPERATIONS, not code — execute it top to bottom after review/merge approval of the branch. Ordering is load-bearing throughout (deploy-ordering gotcha: a push to main auto-deploys control-api, and the tenant-image tag push publishes the image).

- [ ] **Step 1: Full local verification**

```bash
cd apps/control-api && python -m pytest -q && cd ../..
for t in test_connect test_llm_connect_cli test_webhook_shim test_heartbeat test_concierge_onboarding test_autopair; do python3 tenant-image/tests/$t.py || exit 1; done
```

- [ ] **Step 2: ALTER TABLE — staging control-db FIRST, before anything deploys**

The permission classifier blocks live DDL; the operator must `!`-approve these. Run against **squire-staging**'s control-db (via `railway connect` psql on the linked control-db service):

```sql
ALTER TABLE tenant ADD COLUMN connected_provider VARCHAR;
ALTER TABLE heartbeat ADD COLUMN llm_connected BOOLEAN;
```

Both are additive and nullable: the currently-deployed control-api ignores them, so there is no window of breakage. (Rollback, if ever needed, is `ALTER TABLE tenant DROP COLUMN connected_provider;` / `ALTER TABLE heartbeat DROP COLUMN llm_connected;` — also classifier-blocked, also `!`-gated.)

- [ ] **Step 3: Merge and publish the image**

```bash
git checkout main && git merge --no-ff feat/1c-credential-connect
git tag tenant-image-v0.2.0
git push origin tenant-image-v0.2.0   # CI builds + pushes ghcr.io/shagarwal/squire/hermes-tenant:0.2.0
```

Do NOT push main yet — the tag ref alone triggers the image build without deploying control-api. Wait for the `tenant-image` workflow to go green (`gh run watch`).

- [ ] **Step 4: Bump TENANT_IMAGE, then deploy control-api**

On the **staging** control-api service, set the variable (this is what new provisions use):

```bash
railway variables --service control-api --set "TENANT_IMAGE=ghcr.io/shagarwal/squire/hermes-tenant:0.2.0"
git push origin main    # auto-deploys control-api (DB already ALTERed in step 2)
```

- [ ] **Step 5: Reprovision the staging test tenant**

The template-migration gap means in-place tenants never get the new concierge/home-template — reprovision, don't redeploy:

```bash
curl -sf -X DELETE -H "Authorization: Bearer $INTERNAL_API_TOKEN" "$CONTROL_API_URL/internal/tenants/<staging-tenant-id>"
curl -sf -X POST -H "Authorization: Bearer $INTERNAL_API_TOKEN" -H "Content-Type: application/json" -d '{"email": "founder+1c@squire.test"}' "$CONTROL_API_URL/internal/tenants"
```

Confirm the provision job reaches DONE (`GET /internal/provision-jobs/<id>`) — this exercises `CREATE_DOMAIN` live for the first time; if `serviceDomainCreate`/`domains` shapes were wrong, it fails HERE, loudly, before any customer sees it. Then check:

```bash
railway variables --service tenant-<id> | grep -E "RAILWAY_PUBLIC_DOMAIN|SQUIRE_PUBLIC_DOMAIN"
```

**Record the RAILWAY_PUBLIC_DOMAIN finding** (did Railway inject it? was the fallback needed?) in `docs/superpowers/specs/2026-08-14-1c-credential-connect-design.md` under Rollout.

- [ ] **Step 6: PRD §8 switchover verification — all THREE paths, live on staging**

For each path, on a freshly bound staging tenant:

1. **OpenAI API key:** in Telegram, pick the OpenAI key option; open the minted link; paste a real key; verify: done page; agent celebration; `Host`-gate spot-check (`curl -s -o /dev/null -w '%{http_code}' https://<tenant-domain>/webhook/telegram -d '{}'` → 403); gateway answers with the OpenAI model; LiteLLM shows the trial key GONE (`curl -s -H "Authorization: Bearer $LITELLM_MASTER_KEY" "$LITELLM_BASE_URL/key/info?key_alias=squire-trial-<tenant-id>"` → not found); `GET /internal/tenants/<id>` … `connected_provider: "openai"`; OpenAI usage dashboard shows the traffic (direct-to-provider proof).
2. **Anthropic API key:** same sequence with an `sk-ant-api03-…` key; verify `.env` carries `ANTHROPIC_BASE_URL=https://api.anthropic.com` (not the trial proxy) via the agent, and Anthropic console shows usage.
3. **ChatGPT (device flow):** pick the subscription option; if the account has device login off, verify the agent delivers the enable-in-Settings walkthrough (the 404 branch) and recovers after enabling; approve the code at auth.openai.com/codex/device; verify status reaches connected, auth.json exists (sealed on the volume as `auth.json.enc`), trial key revoked, `connected_provider: "chatgpt"`. **Then send a real message and confirm the gateway's requests against `chatgpt.com/backend-api/codex` are accepted.** If the backend rejects hermes-shaped requests (missing `ChatGPT-Account-ID`/`originator` headers — the spike's drift risk), file the follow-up immediately and have the concierge steer ChatGPT users to the key path until it lands; the flow's token acquisition remains valid either way.

Also verify the backstop once: with a connected tenant, manually re-activate its trial flag in staging DB (`UPDATE tenant SET trial_key_active = true WHERE id = '<id>';` — `!`-gated), wait one heartbeat, confirm it flips back to false and the log line `heartbeat reconciliation` appears.

- [ ] **Step 7: Production**

Repeat steps 2, 4 (prod control-db ALTERs → prod TENANT_IMAGE bump → control-api deploy — the main push already happened, so trigger the prod deploy per the environment's flow) and reprovision/verify one prod canary tenant before opening signups.

- [ ] **Step 8: Tag and close out**

```bash
git tag v0.2.0 -m "1C credential connect: 3 live paths, trial revocation on connect"
git push origin v0.2.0
```

---

## Self-review: spec coverage map

| Spec requirement (2026-08-14 design, as amended to 3 paths) | Task |
|---|---|
| 3 connect paths, Claude-subscription DROPPED everywhere | 3/4 (page: keys only), 5 (device flow), 10 (concierge; assumes drop-branch), whole plan (no Claude task exists) |
| Nonce store: single-use, 15-min, 32-byte urlsafe, constant-time, 0600 atomic, autopair discipline | 1 |
| `GET /connect/<nonce>` self-contained HTML, two key paths, friendly invalid/expired | 3 |
| `POST /connect/<nonce>`: real provider validation, squire_secrets-backed storage (tmpfs + sealed by secrets-sync), nonce consumed, invalid key → plain + nonce survives | 4 |
| `squire-llm-connect` mint-link / `start openai-device` / `status`; agent never sees credentials | 5 |
| Codex spike constants: client_id, 4 calls, poll clamp ≥5s, auth.json shape, refresh rules, plan gate, 404-enable-retry as onboarding branch | 5 |
| Connected pipeline: store → gateway switch (model + direct base URL) → silent reload → `POST /internal/llm-connected` | 4 (store), 6 (switch/reload/notify) |
| Host gating ships with/before the page; public Host → /connect only; adversarial fake-update tests; per-IP backoff; constant-time misses | 2 (gate), 4 (backoff), 1 (constant-time) |
| Provisioning: idempotent public-domain mutation; domain reaches container as env | 8 |
| `POST /internal/llm-connected`: internal bearer, provider name only (closed Literal), revoke trial key immediately | 7 |
| Heartbeat connected-marker backstop incl. OAuth-artifact (auth.json) gap fix | 9 |
| Concierge rewrite: CLI-driven states, honest labels (spike's softened OpenAI wording), chat-paste fallback + unminimised disclosure, celebration | 10 |
| Error handling: denied/timed-out → retry or key path; invalid key plain; lost notify → backstop; sleep-wake inside browser request (no special code — Railway wake happens before the request reaches the shim; generous validate timeout); silent gateway restart | 5, 4, 9, 3, 6 |
| Drift tests for concierge; contract test pinned like wake-typing; unit tests for nonce/host/page/CLI/pipeline | 10, 7, 1–6 |
| Exit criterion (PRD §8, now 3 paths): live staging connect, trial key verified revoked, traffic direct | 12 |
| Rollout order: ALTER TABLE → image v0.2.0 → TENANT_IMAGE bump → control-api deploy → reprovision | 12 |
| CI: new test files registered individually, exec bits 100755 | 11 (+ per-task `git update-index`) |

**Placeholder scan:** no TBDs; every test/impl step carries its code; the two deliberately-bounded steps (Task 8 step 5, Task 10 step 5) name the exact anchors and the decision rule instead of code, because their content is *the current suite's output* — anything more specific would be fabricated line numbers.

**Type consistency:** `mint_nonce/find_nonce/consume_nonce(state_dir, candidate)` used identically in Tasks 1/3/4/5; provider wire literals `"openai" | "anthropic" | "chatgpt"` identical across `store_api_key`, `run_connected_pipeline`, `LlmConnectedRequest`, and the CLI; `run_connected_pipeline(provider, hermes_home=None)` matches both call sites; `DeviceFlowConfig` fields match every `dev.*` call; CLI env contract (`RAILWAY_PUBLIC_DOMAIN`/`SQUIRE_PUBLIC_DOMAIN`, `SQUIRE_SUPERVISORCTL`, `CONTROL_API_URL`, `INTERNAL_API_TOKEN`, `TENANT_ID`) consistent across Tasks 4/5/6/8.

**Confirmed:** there is no Claude-subscription task anywhere in this plan.


