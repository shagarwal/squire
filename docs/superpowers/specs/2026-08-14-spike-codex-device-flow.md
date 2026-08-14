# Spike: Codex ChatGPT-subscription sign-in from a headless container — VERDICT: GO-WITH-CAVEATS

**Question:** can a headless tenant container drive the ChatGPT-subscription sign-in the way
Codex CLI does — no browser, no localhost, no inbound redirect?

**Verdict: GO-WITH-CAVEATS.** Codex's **device-code flow** (`codex login --device-auth`) does
exactly this: print URL + short code into Telegram, poll HTTPS, receive tokens. The tenant's
public domain is NOT needed for auth. It is UX-equivalent to RFC 8628 but uses OpenAI-
proprietary endpoints (custom `/api/accounts/deviceauth/*`, server-generated PKCE, 403/404-as-
pending), so the spec must say "Codex device-code flow (RFC-8628-style, proprietary
endpoints)", not "RFC-8628".

## Caveats (must be handled in 1C)

1. **Device-code login is beta and OFF by default.** First `usercode` call returns 404 until
   the user enables "device code authorization" in ChatGPT Settings → Security (personal
   accounts self-serve; Team/Enterprise needs a workspace admin). Onboarding must detect the
   404 and walk the user through enabling it, then retry — not dead-end.
2. **"OpenAI sanctions this path" is overstated.** Basis: Codex is Apache-2.0; Codex lead
   publicly endorsed third-party harnesses on ChatGPT sign-in (Jan 2026), explicitly
   contrasting with Anthropic's ban. BUT there is no formal written ToS clause blessing it,
   the shared client_id reuse question is unanswered, and Codex has `x-oai-attestation`
   plumbing (harness-attestation infra of the kind Anthropic used before enforcing). Correct
   user-facing wording: **"OpenAI's Codex team openly supports third-party agents using
   ChatGPT sign-in (unlike Anthropic); there's no formal written guarantee, and OpenAI could
   change this."** (This wording also needs fixing in the LIVE v0.1.8 concierge copy.)
3. **Recommended integration: run actual Codex** (`codex exec` / app-server / SDK) inside the
   tenant, seeded via the device flow, rather than re-implementing raw calls to the ChatGPT
   backend — so token refresh, headers, and attestation drift are OpenAI's problem. Direct
   `chatgpt.com/backend-api/codex/responses` calls work today but carry drift/attestation
   risk. (Squire already runs the real gateway; the analogous choice applies.)

## Concrete constants (openai/codex, Rust `codex-rs/login`)

| Item | Value |
|---|---|
| client_id | `app_EMoamEEZ73f0CkXaXp7hrann` |
| Issuer | `https://auth.openai.com` |
| Device usercode | `POST {issuer}/api/accounts/deviceauth/usercode` body `{"client_id":…}` → `{device_auth_id,user_code,interval}` (404 = device login not enabled) |
| Device poll | `POST {issuer}/api/accounts/deviceauth/token` body `{device_auth_id,user_code}`; 403/404 = pending; success → `{authorization_code,code_challenge,code_verifier}` (server-generated PKCE) |
| User verification URL | `https://auth.openai.com/codex/device` (code like `ABCD-EFGHI`, expires 15 min) |
| Token exchange | `POST {issuer}/oauth/token` form: `grant_type=authorization_code, code, redirect_uri=https://auth.openai.com/deviceauth/callback, client_id, code_verifier` → `{id_token,access_token,refresh_token}` |
| Refresh | `POST https://auth.openai.com/oauth/token` JSON `{client_id,grant_type:"refresh_token",refresh_token}`; proactive when access-token `exp` within 5 min or `last_refresh` > 8 days; refresh tokens are one-time-use/rotating — persist atomically before use; terminal errors (reused/expired/revoked) → prompt re-link in chat |
| Poll interval | server `interval` (string seconds; clamp to ≥5s — serde default 0 busy-polls) |
| Credential file | `$CODEX_HOME/auth.json`: `{auth_mode:"chatgpt", tokens:{id_token,access_token,refresh_token,account_id}, last_refresh}` — store DEK-encrypted |
| Plan gating | id_token JWT claim `chatgpt_plan_type` (`free`/`plus`/`pro`/`business`) — reject `free` |
| Request auth (if calling backend directly) | `POST https://chatgpt.com/backend-api/codex/responses`, headers `Authorization: Bearer <access_token>`, `ChatGPT-Account-ID: <account_id>`, `originator: codex_cli_rs` |

## Sources
openai/codex@main: `codex-rs/login/src/device_code_auth.rs`, `server.rs`, `auth/manager.rs`,
`auth/storage.rs`, `token_data.rs`, `model-provider-info/src/lib.rs`, `bearer_auth_provider.rs`,
`core/src/{client,attestation}.rs`. Docs: developers.openai.com/codex/auth; GitHub
codex#3820/#9253/#8338; Codex lead X post 2026-01; The Register 2026-02-20; community client_id
thread; openai-python#2951.
