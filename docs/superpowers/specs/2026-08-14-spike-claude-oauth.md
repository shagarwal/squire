# Spike: headless Claude-subscription OAuth — VERDICT: NO-GO (as of 2026-08-14)

**Question:** can a tenant container drive `claude setup-token`-style OAuth so a Claude
Pro/Max subscriber authorizes the container from their own browser?

**Verdict: NO-GO on product/legal grounds** (the mechanism is technically buildable; the
destination is now prohibited and enforced).

## The decisive change since this integration was first assessed

Our current onboarding label is "works today, unsanctioned, may break." That is **no longer
accurate.** As of ~Feb 17–19 2026 Anthropic:
- Added explicit Consumer-ToS language: subscription OAuth auth (Free/Pro/Max) is intended
  exclusively for Claude Code and Claude.ai; using OAuth tokens in any other product, tool,
  or service — including the Agent SDK — is **not permitted and violates the Consumer ToS**.
- Deployed **server-side enforcement**: `sk-ant-oat01` tokens hit against the raw Messages
  API now return "OAuth authentication is currently not supported" / "This credential is
  only authorized for use with Claude Code." (GitHub #28091, no advance notice.)
- Reportedly issued legal takedowns (opencode removed the feature) and account bans for
  subscription-pricing exploitation; reportedly fingerprinting harness spoofing.

Accurate label now: **"prohibited by Anthropic's Consumer ToS and actively enforced."**

## The one nuance

The block targets the raw Messages API. A token still works when consumed by the **genuine
Claude Code runtime** (which is what reads `CLAUDE_CODE_OAUTH_TOKEN`). Squire's container, if
it runs the real `claude` binary as the agent runtime, sits in a grey-but-currently-working
zone — but the *arrangement* (using subscription pricing via a third-party SaaS) is exactly
what the ToS change and bans target. Technical possibility ≠ sanctioned.

## Mechanism (if pursued despite NO-GO)

Redirect-to-tenant is NOT available (the shared public client_id rejects non-allowlisted
redirect_uris). Only the manual code-paste variant works: container generates PKCE + state,
prints authorize URL into chat, user approves in browser, Anthropic's hosted callback shows
`code#state`, user pastes it back, container exchanges. Constants captured (client_id
`9d1c250a-e61b-44d9-88ed-5944d1962f5e`, authorize `https://claude.com/cai/oauth/authorize`,
token exchange `/v1/oauth/token` on `api.anthropic.com`, S256 PKCE, setup-token scope
`user:inference`, token `sk-ant-oat01-…` via `Authorization: Bearer`) — recorded for
completeness only.

## Recommendation

**Drop the Claude-subscription path from 1C.** Promote the **Anthropic Console API key
(`sk-ant-api03-…`)** to the recommended Anthropic path — it is the sanctioned credential,
has no harness restriction, and is what Anthropic directs third-party tools to use. If any
subscription affordance is kept at all, it must carry the prohibited-and-enforced label, not
the old "unsanctioned, may break" one — and legal/founder sign-off is required before
shipping it.

## Sources

Primary: string extraction from installed Claude Code binary v2.1.228 + live
`.credentials.json` structure (values redacted). Secondary: code.claude.com Authentication +
Legal-and-compliance docs; GitHub #28091/#34917/#36215/#59700; The Register 2026-02-20;
winbuzzer/alternativeto 2026-02-19; opencode#18267. (Full list in the completing agent's
report, session 2026-08-14.)
