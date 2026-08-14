# 1C — Credential connect: one-time key link + in-chat OAuth (design)

**Status:** approved by founder 2026-08-14 (brainstorm session). Pulled forward from Phase 1
after two live onboarding tests both hit walls at the LLM-connect step.

**Goal:** every tenant can connect its owner's own LLM account through flows that actually
exist, with credentials never transiting chat or shared infrastructure, and the trial key
revoked the moment a connection lands.

## Decisions (made with founder — do not re-litigate)

1. **3 connect paths ship**: OpenAI API key, ChatGPT subscription (any paid plan, via Codex
   device-code flow), Anthropic API key.
   **The Claude-subscription path is DROPPED** (decided 2026-08-14 after the OAuth spike):
   Anthropic's Consumer ToS now explicitly prohibits subscription-OAuth use in third-party
   products and enforces it server-side + with account bans (see
   `2026-08-14-spike-claude-oauth.md`). Anthropic = **API key only**. This work must also
   REMOVE the live "Claude Max subscription" option from the shipped concierge (handled by a
   separate v0.1.9 hotfix, then kept out here). The sanctioned Anthropic credential
   (`sk-ant-api03-…`) is the only Anthropic path.
   ChatGPT-subscription mechanism = **Codex device-code flow (RFC-8628-style, proprietary
   endpoints)**, GO-WITH-CAVEATS per `2026-08-14-spike-codex-device-flow.md`: device login is
   beta + off-by-default (onboarding must handle the 404-enable-retry path), and the
   "OpenAI sanctions this" claim is softened to "OpenAI's Codex team openly supports it, no
   formal written guarantee." Recommended: run real Codex in-tenant seeded by the flow rather
   than re-implementing backend calls.
2. **OAuth happens in chat; the page exists for API keys only.** Device-code sign-ins run
   on the provider's own site (user approves there; token is delivered provider → tenant
   via polling). The one-time link page's sole job is the secure hand-off of pasted API
   keys/tokens: browser → tenant container over TLS, touching neither Telegram nor shared
   services.
3. **Railway-generated domains for alpha** (`tenant-…up.railway.app`); custom per-tenant
   domains are Phase 1 polish, out of scope here.
4. **Approach A**: the connect page is served by the existing webhook shim's HTTP server
   (port 8080 — the port a Railway domain targets). No new processes.

## Architecture

### Tenant image

- **`tenant-image/bin/squire_connect.py`** (new module, imported by the shim):
  - Nonce store: single-use, 15-minute expiry, 32-byte urlsafe, constant-time comparison
    (mirror the `squire_autopair` bind-nonce discipline), persisted under `$HERMES_HOME`
    with 0600 atomic writes.
  - `GET /connect/<nonce>`: minimal, fully self-contained HTML (no external assets, no JS
    frameworks; inline CSS) offering the two API-key paths (OpenAI / Anthropic) plus a
    paste field for a Claude subscription `setup-token` output. Invalid/expired/used nonce
    renders a friendly "ask your agent for a fresh link" state — never an error dump.
  - `POST /connect/<nonce>`: validates the submitted key with a real provider call
    (cheap models-list/ping request), stores via `squire_secrets` (DEK-encrypted), marks
    the nonce used, triggers the connected pipeline (below), returns a "done — go back to
    Telegram" page. Invalid key → stated plainly, nothing stored, nonce still valid.
- **`tenant-image/bin/squire-llm-connect`** (new CLI, the agent's only interface):
  - `mint-link` → nonce + `https://$RAILWAY_PUBLIC_DOMAIN/connect/<nonce>`.
  - `start openai-device` → runs the Codex RFC-8628 device flow: prints provider URL +
    user code (safe to show in chat), polls in the background, stores the token on grant.
  - `start claude-subscription` → same pattern via the headless OAuth spike (see spikes).
  - `status` → agent polls for "pending / connected / denied / timed-out".
  - The agent NEVER sees or handles credential material; the CLI prints only codes, URLs,
    and states.
- **Connected pipeline** (shared by page + CLI): store credential → switch gateway config
  from trial proxy to direct provider (model updated accordingly) → restart/reload
  gateway (lifecycle notices are already silenced) → call control-api
  `POST /internal/llm-connected {tenant_id, provider}` with the internal token.
- **Concierge rewrite**: connect states rebuilt around the CLI (mint link for key paths;
  relay code+URL for OAuth paths; poll and celebrate). Coming-soon labels from v0.1.8
  become live copy. Honest labels preserved; "Claude subscription (Pro or Max)" naming;
  chat-paste fallback retained with its unminimized disclosure.

### Security: public-domain exposure (ships in the same commit as the page)

Attaching a public domain exposes the shim's port to the internet. The shim gains
Host-based gating: requests whose `Host` is the public domain may reach `/connect/*`
ONLY; `/webhook/telegram` and `/health` answer only on private hostnames
(`*.railway.internal`). Public-Host requests to the webhook path → 403 + counter.
Adversarial tests required (fake-update injection via public Host must fail). Add basic
abuse limits on `/connect`: per-IP attempt counter with backoff; nonce misses are
constant-time.

### Control plane

- Provisioning: create a public Railway domain per tenant service (one extra Railway API
  mutation, idempotent on retry like the others); the domain reaches the tenant as env
  (`RAILWAY_PUBLIC_DOMAIN`).
- New `POST /internal/llm-connected`: internal-bearer auth; records connected provider on
  the tenant row (privacy check: provider name only, never credential material); revokes
  the trial LiteLLM key immediately.
- Heartbeat connected-marker becomes the reconciliation backstop (and is extended to see
  OAuth token artifacts, fixing the known `.env`-only `CONNECTED_MARKERS` gap). Worst
  case on a missed call: trial cap still bounds spend.

## Error handling

- Device flow denied / timed out → agent explains plainly, offers retry or the key path.
- Key fails provider validation → page says so; nothing stored; retry allowed.
- Credential stored but `/llm-connected` call fails → backstop reconciles on next beat.
- Link clicked while tenant sleeps → Railway wake (~15s) happens inside the browser
  request; page loads after wake. No special handling beyond a generous server timeout.
- Gateway restart mid-conversation is already silent (SQUIRE_SHUTDOWN_NOTICES=0); the
  agent's celebration message doubles as the "we're back" signal.

## Research spikes (run first; outcomes select fallbacks, not architecture)

1. **Codex device flow**: exact endpoints, client id, scopes, poll cadence, token shape
   (`auth.json`?) needed to use a ChatGPT subscription the way Codex CLI does. Fallback
   if infeasible headless: OpenAI key path + honest "subscription sign-in unavailable"
   copy (no dead ends — same rule as v0.1.8).
2. **Headless Claude subscription OAuth**: can the `claude setup-token` PKCE dance run
   in-container with the user completing it in their browser? Fallback: page instructs
   running `claude setup-token` locally and pasting the token into the key page.

## Testing

- Unit: nonce lifecycle; Host-gating (adversarial); page GET/POST paths incl. invalid
  key, reused nonce, expired nonce; both flow CLIs against faked provider endpoints;
  connected-pipeline ordering.
- Drift: rewritten concierge states (mint-link usage, code relay, celebration, naming).
- Contract: `/internal/llm-connected` payload pinned from tenant caller to control-api
  schema (same pattern as wake-typing).
- Exit criterion (PRD §8): all 4 paths connected on a live staging tenant; trial key
  verified revoked in LiteLLM; traffic verified direct-to-provider.

## Rollout

Image v0.2.0 + control-api release. Order: control-api DB change if any (tenant row
gains `connected_provider` — requires the manual ALTER TABLE, per [[deploy-ordering-gotcha]])
→ image published + `TENANT_IMAGE` bumped → control-api deployed → existing test tenants
reprovisioned (template-migration gap means in-place tenants don't get the new concierge).

## Out of scope

Custom domains; central web-app involvement in credentials (violates privacy promise);
provider webhooks; connecting non-Anthropic/OpenAI providers; the friendly error-state
messaging design (task #8, separate).
