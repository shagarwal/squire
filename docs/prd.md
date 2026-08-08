# PRD: Squire — Personal AI Agent as a Service

## Context

Shaurya spent months turning the open-source NousResearch `hermes-agent` into a genuinely capable personal WhatsApp agent: VPS provisioning, hardening, an 8-chapter interactive setup, LLM OAuth handoffs, QR pairing, custom patches, memory tuning, skills, personas, cron, approvals. The insight: **all of that pain is the product.** Nobody else should have to do it. This PRD defines a SaaS where anyone signs up on a website with email/password (or Google), and within a minute gets a text from a number/handle — that's their agent, already as capable as Shaurya's tuned setup. The agent conversationally onboards them (trial runs on our metered LLM account, then they connect their own), and everything lives in an isolated per-tenant space designed so **we cannot see their credentials, data, or conversations**.

Decisions already made with the user:
- **Stage:** launchable startup — public signup day one, hundreds → low-thousands of tenants.
- **Privacy:** strong per-tenant isolation (per-tenant runtime + per-tenant encryption keys + audited/blocked operator access). Not confidential-computing zero-knowledge (possible later).
- **Channel:** Telegram first (instant, free, fully programmatic). WhatsApp via Meta's official Cloud API as fast-follow. **No Baileys number farm** (ToS/ban risk, unautomatable QR pairing).
- **LLM:** trial on OUR commercial Anthropic API key (hard-capped, metered). Then user connects their own — **Anthropic or OpenAI, each by API key or subscription**: OpenAI ChatGPT subscription via Codex OAuth (explicitly sanctioned by OpenAI for agents), Anthropic Claude Max OAuth offered but labeled "unsupported by Anthropic, may break."

---

## 1. Product definition

**One-liner:** Sign up, get a text. That text is your personal AI agent — memory, tools, personality, always on — living in a private space we can't look into.

**Target user (initial):** tech-adjacent professionals who want a Shaurya-grade agent without touching a terminal. They have a ChatGPT or Claude subscription (or can paste an API key) but would never self-host.

**Core value props:**
1. **Zero setup.** Signup → first agent message in under 60 seconds. No VPS, no QR codes, no wizard.
2. **Pre-tuned.** Ships with the productized version of the founder's setup: long-term memory (Hindsight), skills, persona, natural-language cron, approval flow for risky actions. Day-one capability, not a blank bot.
3. **Bring your own AI.** Trial included; then it runs on *your* LLM account — your costs, your rate limits, your relationship with the provider.
4. **Private by architecture.** Isolated runtime per user, per-user encryption keys, credentials sealed inside the tenant, operator access audited and break-glass-only. Honest claim: "designed so we can't casually or silently see your data."

**Explicit non-goals (v1):** no Baileys/unofficial WhatsApp; no shared multi-tenant Hermes process; no confidential computing; no mobile app; no team/multi-user tenants.

## 2. User experience

### Signup → first message (< 60s)
1. Web: email+password or Google OAuth → choose **subscribe now** (card) or **3-day free trial** (no card) → "Your agent is waking up…" page.
2. Backend provisions the tenant, assigns a Telegram bot from a pre-created pool, page shows a `t.me/<bot>` deep link + QR.
3. User taps **Start** → agent greets them by a persona name (bot display name = persona, not the pool codename).

### Conversational onboarding (the concierge)
- Agent introduces itself, asks name/timezone/one thing to help with this week (seeds Hindsight memory).
- During the 3-day trial it runs on our capped key (Haiku-class, ~$2/72h — see §5.3) and makes **connecting the user's own LLM an early, natural part of the conversation** (pitched as unlocking the agent's full brain):
  - Presents 4 options with honest labels: OpenAI API key / OpenAI ChatGPT subscription (Codex OAuth — sanctioned) / Anthropic API key / Claude Max subscription OAuth (⚠ unsupported by Anthropic, may break; auto-fallback ladder to API key).
  - Sends a **one-time secure link served by the user's own tenant runtime** — credentials go browser → tenant directly, never through our shared infra in plaintext. If the user pastes a key in chat anyway, the agent immediately deletes the Telegram message and stores the key in-tenant; we document that pasted keys transit Telegram's and our relay's memory.
- After connection, the trial proxy key is revoked; **their LLM traffic never touches our infrastructure again.**

### Ongoing
- Later, users connect tools conversationally (Google Workspace, GitHub, …) via the same tenant-served OAuth callback pattern.
- Cross-chat approvals, cron briefings, voice notes, personas — inherited from the productized image.

## 3. Requirements

**Functional:** web signup (email+pw, Google), Stripe subscription billing, automated tenant provisioning < 60s, Telegram bot per tenant, trial LLM metering with hard caps, 4-way LLM connection matrix, conversational onboarding skill, tenant status page (running/plan/connected-LLM — no content), full account deletion (GDPR-style crypto-shred).

**Non-functional:** infra cost < $5/tenant/mo (target ~$2); agent receives messages 24/7 + runs cron; fleet-wide upgrade with canary rings and rollback; no operator tooling that can read tenant volumes; all key-decryption events audited; break-glass SSH pages the other founder + immutable audit log.

## 4. Architecture (recommended design)

> **Platform decision (2026-08-07):** build Railway-first — control plane, ingress, trial proxy, web, AND per-tenant runtimes all on Railway (one service per tenant, serverless sleep), operated by Claude via the Railway CLI/MCP. The Hetzner container-per-tenant design below remains the rehearsed fallback if per-tenant cost exceeds $3/mo or wake latency exceeds 8s (**Gate G1**). See `implementation-plan.md` (this folder).

### Control plane / data plane split
- **Control plane (one boring monolith):** FastAPI + managed Postgres + Auth.js + Stripe + thin Next.js signup page. Owns: tenant registry, placement, bot pool, provisioning state machine, aggregate health. **Structurally cannot hold** conversation content or plaintext credentials — the schema has no columns for them.
- **Data plane:** per-tenant runtime + two thin stateless shared services:
  - **Ingress router** — Telegram webhook → tenant container; body-non-logging; also does SNI/TCP-passthrough for `t-<id>.tenant.<domain>` so TLS for credential links terminates *inside* the tenant.
  - **Trial metering proxy** — LiteLLM fronting our Anthropic org key; per-tenant virtual keys with budgets.

### Tenant runtime: container-per-tenant on big cheap metal
- One OCI image `hermes-tenant:<version>` = upstream hermes-agent pinned at a release tag + thin patch overlay + baked SOUL.md/skills/config template (same three-tier fork discipline as `git-strategy.md`, now in CI).
- Per tenant: distinct UID, ~1.2GB memory cgroup, own network namespace (no east-west), encrypted volume at `~/.hermes`. Hindsight/Postgres tuned per `hindsight-optimization-guide.md` → ~40–70 tenants per 64GB Hetzner box with overcommit (agents idle 95%+).
- **Orchestration:** no k8s. A `placement` table + a small per-host provisioner agent (systemd, pulls desired state, runs podman units). Revisit Nomad at >30 hosts. Hibernation of idle tenants (webhook-wake) is the Phase-2 density lever.
- Rejected: VM-per-tenant (2–3× cost, fleet-ops sprawl), shared multi-tenant gateway (deep Hermes surgery = fat fork; weakens the isolation story).

### Secrets: envelope encryption + crypto-shredding
- Per-tenant DEK from KMS; wrapped DEK in control plane, plaintext DEK delivered once at container boot into tenant-UID-only tmpfs. All credential-bearing files encrypted with the DEK (init shim, not a Hermes patch); host disks LUKS.
- Deletion = destroy container + volume + **delete wrapped DEK** → all backups unreadable (GDPR wipe even for cold copies).
- Nightly restic backups client-side-encrypted with the tenant DEK → object storage. Control plane can restore volumes it cannot read.
- **Honesty boundary (published on the privacy page):** root-on-host could read memory; the guarantee is architectural + audited-policy, not confidential computing. While on Railway, Railway Inc. is additionally a subprocessor (their platform can technically access containers/volumes; TLS terminates at their edge) — named plainly in the privacy policy; the Hetzner fallback (G1) restores full host control.

### Trial abuse controls
Trial is Haiku-class default, **~$2 hard budget / 75 msgs/day / 72 hours** per tenant (see §5.3) — low abuse value by construction. Belt-and-suspenders: one trial per Telegram user ID (phone-anchored), disposable-email block, signup velocity limits, trial tenants egress-allowlisted (Telegram + proxy only — kills spam-bot value); anomaly alerts auto-suspend. Expired trials hibernate after 48h and are crypto-shredded at day 14.

### Telegram bot supply
BotFather ≈ 20 bots/account → maintain 3–5 aged accounts, pre-create weekly batches, low-watermark alert. Onboarding nudges BYO-bot (user pastes their own bot token via the secure link) which recycles pool bots. WhatsApp Cloud API numbers (Phase 2) are fully programmatic.

### Fork discipline
Everything possible is a **separate service** (control plane, ingress, proxy, provisioner, secrets shim, heartbeat, backups) or **image config** (SOUL.md, skills, tuning). Actual Hermes patches capped at ~5 and PR'd upstream aggressively — the v2026.8.3 update proved upstream absorbs features fast and every patch is rebase debt.

## 5. Business / cost model

**The model is BYO-LLM, full stop.** The math below shows why: a power user's token consumption is unaffordable to bundle, but a BYO user costs us almost nothing. Our only LLM spend is the onboarding runway, engineered to be negligible.

### 5.1 What a power user's tokens would cost (why we never bundle)

Anchor: the founder's own usage — ~33 inbound messages/day ≈ 1,000/mo. Each message is an agentic tool loop, not one call: assume ~6 LLM calls/turn, ~15k-token system/tools/memory context (cached after first call), ~2k fresh input + ~800 output per call. At Aug 2026 API prices (Sonnet-class $3/$15 per MTok, cache reads $0.30; Haiku 4.5 $1/$5):

| Routing | $/message | Power user (1,000 msg/mo) |
|---|---|---|
| All Sonnet-class | ~$0.14 | ~$140/mo |
| Mixed (Haiku default, Sonnet for ~40% tool-heavy) | ~$0.08 | ~$75/mo |
| All Haiku | ~$0.035 | ~$35/mo |

Plus ~$5–10/mo of cron/consolidation. **$80–150/mo per power user if we paid** — that's their problem to solve with their own subscription or API key, not ours.

### 5.2 What a BYO power user costs US

| Cost item | Telegram-only | + WhatsApp (Pro, Phase 2) |
|---|---|---|
| Infra slot (power users pack ~30/host vs 50 avg) | ~$3.50 | ~$3.50 |
| Backups, KMS, bandwidth | ~$0.25 | ~$0.25 |
| Channel | $0 | ~$1.50 number + ~$0.10–3.50 messaging¹ |
| Stripe fees + support amortized | ~$2 | ~$2 |
| **Total** | **~$6/mo** | **~$8–11/mo** |

¹ WhatsApp in-window service replies are free until **Oct 1, 2026**, when Meta starts charging (~$0.0034/msg US utility rate → ~$3.40/mo at power-user volume). Small, but it's why WhatsApp sits on the higher tier.

Average users are cheaper on every line (~$3–4 all-in, ~50/host density).

### 5.3 The 3-day trial

Signup offers two doors: **subscribe immediately** (card, agent live at once) or **start a 3-day free trial** (no card). Trial mechanics:

- Full agent for **72 hours**. If the user hasn't connected their own LLM yet, it runs on our key — **Haiku-class default, ~$2 hard budget, 75 msgs/day** (enough to genuinely feel the product; Sonnet-class reserved for a few showcase tool-turns). Connecting their own LLM mid-trial lifts the caps and upgrades the model — the concierge sells this as "unlock your agent's full brain," which doubles as the conversion step.
- The concierge still pushes "connect your AI" early in session one — trial users who connect are the highest-intent converts and cost us near zero.
- At 72h: **the agent stops working** — it replies only with a subscribe link (and a data note: tenant kept 14 days, then crypto-shredded unless subscribed). Container hibernates after 48h grace to reclaim capacity.
- Worst-case trial CAC: ~$2 tokens + ~$0.30 infra per trial signup.

### 5.4 Pricing structure — deliberate penetration pricing

| Tier | Price | Includes | Our cost (avg / power user) | Gross margin |
|---|---|---|---|---|
| **Starter** | **$5/mo** | BYO-LLM, Telegram, full agent (memory, skills, cron, approvals) | ~$3.50 / ~$6 | ~30% avg, **negative on power users** |
| **Pro** | **$10/mo** | + WhatsApp number, voice notes, extra personas/cron | ~$6 / ~$11 | ~40% avg, **negative on power users** |

This is intentional land-grab pricing, and the PRD is explicit about what makes it survivable and reversible:

- **Framed as "early-bird pricing" everywhere** (site, checkout, agent's own answers about cost) so raising it later is expected, not a betrayal. Decide at raise time whether early adopters get grandfathered (recommended: yes, it converts them into evangelists).
- **Stripe drag is real at $5**: 2.9% + $0.30 ≈ $0.45 (9% of revenue). Push annual prepay ($50/yr, $100/yr) hard — one fee instead of twelve, plus upfront cash.
- **Fair-use caps are load-bearing at these prices**, not fine print: CPU/scheduler weight per tenant, cron-frequency ceiling, storage quota. The power user who costs $6 is fine as a minority; caps stop the pathological tail.
- **Watch-items that eat the margin**: WhatsApp per-message fees post-Oct 1 2026 (~$3.40/mo at power-user volume — priced into Pro), and density (if we land at 30/host instead of 50, Starter average margin ≈ 0). Density work (§6 risk 1) is now a pricing prerequisite, not just an optimization.
- Breakeven sanity check: at $5/$10 the business is roughly breakeven-to-thin until either prices rise or hibernation density lands — acceptable for a growth phase, but the raise is planned, not hypothetical.

### 5.5 Fleet infra at scale (unchanged)

| | 100 tenants | 1,000 tenants |
|---|---|---|
| Tenant hosts (Hetzner 64GB, ~50/host) | ≈ $190/mo | ≈ $1,450/mo |
| Control plane + ingress + proxy + ops | $60 | $150 |
| Backups + KMS/audit | $15 | $95 |
| **Infra per tenant** | **≈ $2.60** | **≈ $1.70** |
| Onboarding LLM (amortized) | < $50/mo | < $500/mo |

## 6. Risks

1. **Per-tenant memory footprint** — it *is* the unit economics. Mitigate first (PG/Hindsight tuning, zswap, overcommit); Phase-2 hibernation doubles density.
2. **Claude Max OAuth breakage** — already labeled unsupported; automatic fallback ladder to API key; the sanctioned Codex-subscription path is the marketed default for subscription users.
3. **Bot-pool scaling / Telegram account risk** — BYO-bot nudge + WhatsApp Cloud API remove the ceiling.
4. **Upstream velocity** (8,691-commit rebases) — thin-fork + CI image builds + canary rings (ring 0 = founders' own tenants).
5. **Trial abuse** — controls in §4; ~$2 hard budget means worst case is bounded, noise-level spend.
6. **WhatsApp 24h window** (Phase 2) — agent-initiated sends (cron briefings) need approved template messages; design the cron output path around this.
7. **Penetration-pricing margin** — at $5/$10 power users are unprofitable and average margin is thin (§5.4); survivable only with fair-use caps + density work + the planned price raise. If density lands at 30/host instead of 50, Starter is breakeven.

## 7. Build plan (1–2 engineers + Claude Code)

**Phase 0 — founder-scale alpha (4–6 wks).** Tenant image + patch/CI pipeline; per-host provisioner; manual provisioning CLI; ingress router; concierge skill v1; heartbeats; 10 hand-invited tenants on one host. *Proves density, upgrade path, Telegram UX.*

**Phase 1 — public beta (6–8 wks).** Signup web app + Auth.js + Stripe; automated <60s pipeline; bot-pool tooling; LiteLLM trial proxy + abuse limits; DEK envelope + one-time-link credential flow (all four LLM options); backups + crypto-shred deletion; canary-ring rollout automation. Target ~300 tenants.

**Phase 2 — expansion (8–12 wks).** WhatsApp Cloud API channel (template messages, 24h-window logic); conversational tool connections (Google/GitHub via tenant-served OAuth); BYO-bot migration; hibernation/scale-to-idle; audit-logging hardening toward SOC2-lite; Nomad evaluation at >30 hosts.

## 8. Verification / success criteria

- **Provisioning:** fresh signup → first agent reply < 60s, measured end-to-end by a synthetic signup in CI.
- **Isolation:** red-team checklist — from a tenant container, prove no access to other volumes/network namespaces; from the control plane DB, prove no route to message content or plaintext creds; restore a backup and prove it's unreadable without the tenant DEK.
- **LLM switchover:** connect each of the 4 credential paths on a test tenant; verify trial proxy key revoked and traffic goes direct.
- **Upgrade drill:** image vN → vN+1 through canary rings on synthetic tenants with forced rollback.
- **Trial caps:** drive a tenant to each cap and verify hard stop + graceful agent messaging.
- **North-star metrics:** signup→first-message conversion, D7 message activity, trial→BYO-LLM conversion, infra $/tenant.

## 9. Open questions (not blockers)

- ~~Product name/brand~~ — **resolved 2026-08-07: Squire.** Persona naming scheme for bots still open.
- When and how much to raise prices from the $5/$10 early-bird levels; whether early adopters are grandfathered (leaning yes).
- Legal: ToS/privacy-policy drafting for the honesty-boundary language; data-processing terms for EU users.
- Whether founder's existing Vultr box becomes ring-0 or stays personal.

## Reference material (`docs/reference/`, copied from the founder's hermes-agent-bootstrap repo)
- `reference/git-strategy.md` — three-tier fork discipline to scale into CI.
- `reference/hermes-update-plan-v2026.8.3.md` — upgrade/canary evidence base.
- `reference/AGENTS-SETUP.md` — the manual runbook the provisioner + image replace.
- `reference/hindsight-optimization-guide.md` — memory tunings that make tenant density work.
- `reference/hermes-400-extra-usage-fix.md` — basis for the "unsupported" labeling of Claude Max OAuth.
