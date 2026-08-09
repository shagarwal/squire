# Squire — Phased Implementation Plan (Railway-first)

> **For agentic workers:** This is a program-level roadmap. Phase 0 tasks are directly executable; each Phase 1/2 workstream gets its own detailed task-level plan (superpowers:writing-plans) when its phase begins. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship **Squire** — "sign up → get a Telegram text → that's your agent" — as a paid product ($5/$10 early-bird, 3-day trial), per `prd.md` (this folder).

**Architecture:** Everything that can run on Railway runs on Railway — control plane, ingress, trial proxy, and (pending the economics gate) the per-tenant agent runtimes as one-container-per-tenant Railway services with volumes. Claude operates Railway directly via the authenticated CLI + official MCP; no manual dashboard work required from Shaurya except one-time OAuth/account actions.

**Tech Stack:** Railway (compute, Postgres, volumes, cron, serverless sleep), FastAPI control plane, Next.js signup page, LiteLLM trial proxy, hermes-agent tenant image (podman/OCI built in CI, deployed as Railway services), Stripe, AWS KMS, Backblaze B2, Telegram Bot API.

---

## 0. Tooling & operating model (DONE / one-offs)

- [x] Railway CLI installed (v4.31.0) and authenticated as shaurya123@gmail.com — Claude drives Railway via `railway ... --json` in Bash.
- [x] Official Railway MCP registered (`https://mcp.railway.com`, HTTP transport) — Shaurya: run `/mcp` once to complete OAuth when prompted.
- [ ] Shaurya one-offs (only manual steps in the whole plan): ~~upgrade Railway to **Pro**~~ (done 2026-08-08), create Stripe account, create AWS account (KMS only), create Backblaze B2 bucket, create 1–2 Telegram accounts for the bot pool. Claude will prompt for each exactly when needed with `!`-prefixed commands where possible.

**Division of labor from here on:** Claude executes (Railway provisioning, deploys, env vars, logs, debugging) and reports; Shaurya approves spend, does OAuth/browser-only steps, and scans QR-free (no QR anywhere in this product — that died with Baileys).

---

## 1. Railway platform mapping

One Railway **project** per environment (`squire-prod`, `squire-staging`). Services:

| Railway service | What | Notes |
|---|---|---|
| `web` | Next.js signup/billing/status page | Replaces Vercel |
| `control-api` | FastAPI monolith: tenant registry, provisioning state machine, bot pool, Stripe webhooks | |
| `control-db` | Railway Postgres | Replaces Supabase/Neon |
| `ingress` | Telegram webhook router → tenant services (private networking) | Body-non-logging |
| `trial-proxy` | LiteLLM + our Anthropic key, per-tenant virtual keys/budgets | |
| `tenant-<id>` × N | **One service per tenant**: single container bundling hermes-gateway + Hindsight + embedded PG (supervisord), one Railway volume as `~/.hermes` | Created programmatically by `control-api` via Railway GraphQL API |

**Provisioning is Railway-API-driven:** `control-api` calls Railway's GraphQL API to create/start/stop/delete `tenant-<id>` services — this replaces the PRD's per-host provisioner agent entirely. The PRD's placement table becomes a mirror of Railway state.

**Irreducible non-Railway services** (each has no Railway equivalent): Stripe (payments), AWS KMS (per-tenant DEK wrapping + CloudTrail audit), Backblaze B2 (DEK-encrypted restic backups — Railway volumes are not backup storage), Telegram/BotFather (bot pool), an email sender (Resend, transactional only), DNS/domain (Cloudflare registrar, DNS only — no proxy needed, Railway terminates TLS).

**Privacy note (PRD §4 honesty boundary, amended):** on Railway, Railway Inc. is a subprocessor — their platform can technically access containers and volumes, and TLS terminates at Railway's edge. The credential one-time link still never transits *our* shared services, and all at-rest secrets remain DEK-encrypted app-side; but the published privacy claim must name Railway as a subprocessor. LUKS/host-disk control from the PRD applies only if/when the data plane moves to Hetzner (Gate G1).

---

## 2. The economics gate (the one big Railway caveat)

Railway compute is ~**$10/GB-RAM/mo + $20/vCPU/mo** (+ $0.15/GB volume). A naive always-on 1GB Hermes tenant ≈ **$12/mo — 2.4× our $5 price**. The plan bets on two levers, measured in Phase 0:

1. **Footprint**: Hindsight/PG tuning (per `reference/hindsight-optimization-guide.md`) to get idle RSS ≤ 512MB.
2. **Serverless sleep**: agents are idle 95%+; Railway scale-to-zero + webhook wake + Railway cron for scheduled jobs. Sleeping 90% of the time at 512MB ≈ **$1–2/mo/tenant**.

**Gate G1 (end of Phase 0, hard commitment):** measure real per-tenant $/mo and wake-from-sleep latency across 10 alpha tenants.
- **Pass** (≤ $3/tenant AND p95 wake ≤ 8s to first typing indicator): data plane stays on Railway through Phase 1; revisit at 500 tenants.
- **Fail**: tenant data plane moves to Hetzner boxes (PRD §4 original design, provisioner agent revived); control plane, ingress, proxy, web all **stay on Railway** — no fragmentation of the app layer either way.

**Also verify during Phase 0 (ask Railway support):** max services per project/environment (a 1,000-tenant fleet = 1,000+ services), GraphQL API rate limits on service creation, and whether serverless wake fires on private-network requests from `ingress`.

---

## 3. Phase 0 — Founder-scale alpha (weeks 1–6, 10 invited tenants)

Exit criteria: 10 real tenants chatting on Telegram, provisioned end-to-end by `control-api` with no dashboard clicks; G1 measured; image upgrade drill passed.

> ### ▶ NEXT-SESSION START HERE (rewritten 2026-08-09, end of second session)
>
> **Phase 0 code is complete and LIVE. A real tenant was provisioned end-to-end and held a real conversation.** All six tasks merged to `main` through implementer + spec + quality review. What remains is (a) three UX/security fixes discovered by live use, (b) Gate G1 measurement + tuning, (c) real alpha users.
>
> **Live staging** (see memory `staging-deployment.md`): project `squire-staging` `ddbcfda7-e6f6-47a9-87cd-903b6efeb8c6`, env `production` `0923ea14-9a5e-4d87-990b-be31e5bc76ed`. Services: `control-api` (https://control-api-production-588b.up.railway.app, /healthz ok), `ingress` (https://ingress-production-6c96.up.railway.app), `trial-proxy` (internal :4000), `control-db`, `litellm-db`. Images `hermes-tenant:0.1.0/0.1.1/0.1.2` on GHCR (public). Bot pool: 5 bots (`squire_alpha_01..05_bot`), 4 free. Live tenant: `86b245e9cc9c5a4d` on `@squire_alpha_02_bot`. Tenant `38c2ebc966c30509` was crypto-shredded to verify deprovisioning (volume marked pending-delete, service gone, bot released — all correct).
>
> **Trial config as of now:** model `anthropic:claude-sonnet-5` (raised from Haiku), budget `$10` hard cap (raised from $2 so the advertised 75 msgs/day is actually reachable), PRD §4/§5.3/§6 + CAC line updated to match.
>
> #### 1. FIRST: finish the in-flight branch `fix/onboarding-double-reply` (pushed to origin, HEAD `ee3fc07`)
> The deterministic-onboarding work SHIPPED as v0.1.2 and **works** — founder tapped Start and got the intended greeting (identity, three concrete capabilities, trial + connect-your-LLM, one question). Mechanism: entrypoint seeds `.squire/concierge-state.json`, a `pre_llm_call` shell hook restates the current step until `complete`, patch 005 suppresses upstream's competing "one or two sentences max" note, SOUL.md mandate is section 1. Verify a fresh tenant with `hermes hooks list` → `pre_llm_call ... ✓ allowed`.
> Three defects found in live use. Branch `origin/fix/onboarding-double-reply` @ `ee3fc07` contains work on (a) and (b) — it touches `squire-concierge-hook.py`, `patches/005-*.patch`, `markers.tsv`, `tests/test_concierge_onboarding.py`. **It has NOT been reviewed and (c) is NOT in it.** Verify what actually landed before trusting this list:
> - **(a) Double reply.** The hook fires per LLM CALL, not per USER TURN; the agent's state-file tool call creates a continuation turn, so the next step gets emitted as a second standalone message. Fix = inject at most once per user turn (dedupe on turn id). NOT an ingress retry — ingress logged exactly one forward.
> - **(b) "No home channel is set for Telegram" notice** fires before the greeting and advertises `/sethome`, a surface Squire doesn't sell. Preferred fix: when autopair binds the owner, persist that DM as the home channel (upstream `gateway/config.py:466 persist_home_channel`) so the notice never fires AND cron briefings work by default. Fallback: suppress via patch.
> - **(c) Deep-link nonce binding (SECURITY).** Bots are recycled between tenants, and the previous owner keeps the chat forever. Autopair binds "first human when the store is empty", so a previous owner can silently become owner of the NEXT tenant on that bot. Fix: `t.me/<bot>?start=<nonce>`; bind only on a matching one-time nonce. **Needs a control-api contract change** (new tenant env var e.g. `SQUIRE_BIND_NONCE` set at provision, exact-set env test updated, `provision.py` prints the nonce link) — that side is NOT yet briefed or built. Must degrade safely when the var is absent, and the nonce must be stripped before the message reaches memory.
>
> #### 2. THEN: Gate G1 — the architectural decision Phase 0 exists to make
> Measured idle RSS on a live tenant: **~1.3GB vs the 512MB target** (Hindsight local embeddings + CPU torch + embedded PG + gateway). Both levers are built but unpulled:
> - Point `HINDSIGHT_API_EMBEDDINGS_PROVIDER` at a cloud embedder (knob exposed in the image, currently unset) — biggest single win.
> - Confirm Railway serverless sleep actually engages. **Suspicion worth testing first:** our own 300s heartbeat may keep every tenant permanently awake, which would nullify the sleep half of the cost model entirely. `SQUIRE_HEARTBEAT_INTERVAL` is the knob; `HEARTBEAT_STALE_SECONDS` must move with it.
> Then measure real $/tenant/mo and p95 wake latency across tenants. **Pass = ≤$3/tenant AND p95 wake ≤8s → stay on Railway. Fail → tenant data plane moves to Hetzner.**
>
> #### 3. THEN: the rest of Phase 0's exit criteria
> - Run `infra/upgrade_drill.py` for real (canary → roll → rollback) — tooling is live-verified but the drill has never driven an actual fleet; needs ≥2 tenants.
> - Onboard 10 alpha users (needs ~7 more BotFather bots, or see workstream **1H** below).
> - Backups: code ships but is inert until a **Backblaze B2** account exists. Do not claim backups work until a real restore has been performed.
>
> #### Known gaps / decisions parked
> - **No template-migration path.** `seed_template` is keep-existing, so EXISTING tenants get none of a new image's config/skills/SOUL.md — only a re-provision does. This bit us twice tonight. A baked `template-version` marker that refreshes image-managed files while leaving agent-owned ones alone would fix it.
> - **Workstream 1H (added tonight)**: shared bots + an `egress` service, so tenants stop holding `TELEGRAM_BOT_TOKEN` and one bot can serve unlimited users. Decide before investing in more Telegram accounts.
> - Gate G2 secrets hardening (fleet-wide `INTERNAL_API_TOKEN` blast radius — the autopair takeover was one symptom).
> - Follow-up: hindsight `claude-code` provider for Claude Max tenants; `CONNECTED_MARKERS` only inspects `.env` and needs revisiting when 1C writes `auth.json`.
> - Deploy quirk: `railway up` from inside the repo dir fails (`exclude-patterns` non-printable-ASCII); deploy from a clean copy outside the repo, or rely on CI `deploy.yml`. Local CLI 5.34.2.
>
> #### Shaurya one-offs still open
> Backblaze B2 bucket (blocks backups), more Telegram accounts (blocks 10 users, unless 1H). Stripe + AWS/KMS are Phase 1, not needed yet. Railway Pro ✅, tokens ✅, bots ✅, GHCR public ✅.
>
### Task 0.1 — Repos & CI skeleton
- [x] GitHub repo `squire` created and linked to this folder (2026-08-07); grow into monorepo layout: `apps/web`, `apps/control-api`, `apps/ingress`, `tenant-image/`, `infra/`, `docs/`.
- [x] Create Railway projects `squire-staging`, `squire-prod` — created 2026-08-08 via `railway init` (no services yet; RAILWAY_TOKEN_* GitHub secrets still to set).
- [x] GitHub Actions: build + push `tenant-image` to GHCR on tag; deploy `web`/`control-api`/`ingress` to Railway on merge (`deploy.yml` matrix job + `test.yml` per-service suites; first tag build being debugged — torch +cpu wheel pin).

### Task 0.2 — Tenant image v0
- [x] Dockerfile: upstream hermes-agent pinned at v2026.8.3 (digest-pinned) + patch overlay (all four VPS patches vendored 2026-08-08, incl. 004; fail-closed marker CI check) + supervisord running gateway + Hindsight daemon + embedded PG in one container. *(First CI image build in progress — never built before merge.)*
- [x] Bake productized `~/.hermes` template: SOUL.md (de-personalized), skills, config.yaml with Telegram adapter (webhook mode via shim), Hindsight tuned per guide (with corrections — guide's MAX_SLOTS knob was a deprecated no-op alias).
- [x] Concierge skill v1 (baked): 9-state machine in `state-machine.yaml`, 4 provider options with honest labels.
- [x] Secrets init shim: AES-256-GCM, plaintext only on tmpfs (/dev/shm), first-boot init, wrong-DEK boot refusal, volume-durability gates.
- [ ] Measure: idle RSS, boot time, wake time. Targets: ≤ 512MB / ≤ 20s / ≤ 8s. *(Needs first live deploy.)*

### Task 0.3 — Control API v0 + provisioning
- [x] FastAPI + SQLModel on `control-db`: `tenants`, `bots`, `provision_jobs` tables (140 tests; privacy-schema guard is build-breaking).
- [x] Railway GraphQL client: create service from GHCR image, attach volume, set variables, deploy, delete. Idempotent state machine with atomic job claims + retries. *(Every mutation still unverified against the live API — in-code first-live-deploy checklist.)*
- [ ] Bot pool: manual BotFather batch (20 bots via Shaurya's Telegram) → tokens loaded via CLI script (`infra/load_bots.py` ready) → `control-api` assigns + sets webhook. *(Code done; Shaurya's BotFather batch pending.)*
- [x] CLI command (`infra/provision.py`): `provision --email x@y.com` → tenant live + t.me link printed. **This is the alpha signup.**

### Task 0.4 — Ingress v0
- [x] Thin FastAPI/uvicorn service: `POST /telegram/<bot_id>` → look up tenant → forward over Railway private networking → 200 to Telegram. No body logging (structural: log schema has no body field). Buffer-and-wake path for sleeping tenants. (34 tests.)

### Task 0.5 — Trial proxy v0
- [x] LiteLLM on Railway, virtual key per tenant, $2 hard budget / Haiku-class default (75 msg/day approximated — no native LiteLLM daily-count primitive; documented in config); `control-api` creates/revokes keys on trial start / expiry. *(LLM-connect revocation trigger is Phase 1C.)*

### Task 0.6 — Alpha operations
- [x] Heartbeat: tenant emits counts-only metrics to `control-api`; `/internal/fleet` status endpoint.
- [x] Nightly restic → B2, client-side encrypted with tenant DEK (in-container supervisord loop — Railway cron can't exec into a running service; documented deviation). *(Inert until B2 account exists.)*
- [ ] Upgrade drill: build image vN+1 → redeploy 1 canary tenant → verify → roll fleet via API loop. Rollback = redeploy previous image tag. *(Tooling landed — `infra/upgrade_drill.py` + redeploy endpoint; the drill itself needs a live fleet.)*
- [ ] Onboard 10 invited alpha users. Run 2 weeks. **Measure G1.**

---

## 4. Phase 1 — Public beta (weeks 7–14, target ~300 tenants)

Each workstream gets a detailed plan at kickoff. Exit criteria: stranger can sign up, trial, pay $5/$10, and churn — all untouched by us.

- **1A Signup web app**: Next.js on Railway; Auth.js (email+password, Google); signup → `control-api` provision → "agent waking up" page with t.me deep link; status page (running/plan/connected-LLM only).
- **1B Billing**: Stripe Checkout ($5 Starter / $10 Pro / annual $50/$100 push); 3-day trial state machine (no card) → hard stop at 72h (subscribe-link-only replies) → hibernate +48h (Railway service stop) → crypto-shred day 14 (delete service + volume + wrapped DEK); webhook-driven resume on payment.
  - **Never-said-hello email nudge** (added 2026-08-09 after the first live signup walkthrough): Telegram forbids a bot from contacting a user who has not messaged it first, so a tenant whose owner never taps **Start** is unreachable *on Telegram entirely* — the trial clock runs down in silence and we cannot say a word about it in-channel. Email is the only channel we have for that user. Fire a nudge from `control-api` when a tenant has been `running` for N hours with zero inbound updates (`updates_forwarded == 0` on the heartbeat already reports this — see Task 0.6 `/internal/fleet`), re-linking the `t.me` deep link and explaining the one tap needed. Suppress once the first update arrives. Same constraint applies to WhatsApp in 2A (24h session window), so treat this as the general "user hasn't opened the channel yet" path, not a Telegram special case.
- **1C Credential one-time link**: tenant serves `GET /connect/<nonce>` on its Railway-provided domain; all 4 LLM paths (Anthropic/OpenAI × key/OAuth device-code); Telegram paste-fallback with immediate message delete; trial-proxy key revoked on connect.
- **1D Gate G2 — secrets hardening**: move DEK delivery off Railway variables → `control-api` hands a one-time sealed token at boot; KMS decrypt only in-tenant; CloudTrail alerting; break-glass policy doc.
- **1E Abuse & fair use**: one trial per Telegram ID, disposable-email block, velocity limits, egress allowlist for trial tenants, cron-frequency + storage quotas, anomaly auto-suspend.
- **1F Fleet automation**: canary-ring rollout (ring 0 = our own tenants), bot-pool watermark alerts + BYO-bot nudge flow, synthetic signup canary in CI (<60s SLA).
- **1H Shared bots + egress service (decide before scaling the pool)**: today the bot pool is one Telegram bot per tenant, which caps the fleet at ~20 bots per Telegram account and forces bot recycling between users. A Telegram bot can serve unlimited users (each in their own private chat), so the per-tenant model is not a platform limit — it is a consequence of `TELEGRAM_BOT_TOKEN` living inside each tenant container. That token is not conversation-scoped: it can send to any chat the bot has, so sharing one bot across tenants would let any tenant container impersonate the bot to every other user of it, breaking the isolation the one-container-per-tenant design exists to provide.
  - **The change**: tenants stop holding bot tokens. A new `egress` service (mirror of `ingress`) holds them and sends on a tenant's behalf, authenticated as that tenant and scoped to its own chat. Ingress routing moves from `bot_id` → tenant to `(bot_id, telegram_user_id)` → tenant, which is a small change it is already shaped for.
  - **Wins**: unlimited users per bot; no pool, no watermark alerts, no recycling; the recycled-bot binding race disappears (a previous owner of a recycled bot can otherwise silently bind as owner of the next tenant — mitigated in Phase 0 by the deep-link nonce, but eliminated entirely here); bot tokens live in exactly two services and never in tenant containers, retiring part of Gate G2.
  - **Costs / risks to weigh honestly**: a new hop on every outbound message (a failure mode that did not exist); per-tenant persona names disappear from the chat header — everyone sees the same bot name; and a shared bot is a shared blast radius — Telegram rate limits are per-bot (~30 msg/s), and a restriction or ban earned by one user's behaviour hits everyone on that bot, whereas per-tenant bots contain it to one person.
  - **Decision point**: this likely replaces the BYO-bot nudge as the primary scaling answer (BYO-bot remains for users who want their own branded bot). Decide before investing in more Telegram accounts.
- **1G Launch checklist**: privacy page with honesty boundary (incl. Railway-as-subprocessor), ToS, GDPR deletion flow verified end-to-end, PRD §8 red-team checklist executed.

## 5. Phase 2 — Expansion (weeks 15+)

- **2A WhatsApp Cloud API** channel on Pro tier (programmatic numbers, 24h-window + template logic for cron output; re-check Meta's Oct 1 2026 in-window pricing before build).
- **2B Tool connections** (Google Workspace, GitHub) via tenant-served OAuth callbacks.
- **2C Density push**: hibernation tuning or Hetzner migration per G1; re-price when unit economics are proven (PRD §5.4 raise plan).
- **2D Hardening**: SOC2-lite audit logging, status page, on-call rotation.

---

## 6. Verification (program level)

- Every phase ends against PRD §8: synthetic signup <60s, isolation red-team, 4-path LLM switchover, upgrade drill with forced rollback, trial-cap hard stops.
- G1 (economics) and G2 (secrets) are blocking gates — Phase 1 public launch does not happen without both resolved.
- Weekly: infra $/tenant from Railway usage API vs. plan price; alert if trailing margin < 20%.

## 7. Risk register (delta vs PRD §6)

1. **Railway tenant economics** — the plan's biggest bet; bounded by G1 with a rehearsed Hetzner exit.
2. **Railway service-count / API limits** — unverified; ask support in week 1 (Task 0.3 blocker if creation is rate-limited).
3. **Wake latency UX** — sleeping tenants must feel alive; mitigation: ingress sends Telegram "typing…" action instantly on wake, before the gateway is up.
4. **Railway as subprocessor** — weakens the "we can't see" claim vs self-managed hosts; disclosed honestly; Hetzner path restores it later.
