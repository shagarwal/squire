# Pre-launch waitlist site + control-api waitlist endpoint

**Date:** 2026-08-20 · **Status:** approved (Approach A picked by founder; ad budget capped at $200)

## Goal

A public marketing page (the future `apps/web`) that sells Squire's vision and captures
`email + feature interest + UTM attribution` into the existing control-plane Postgres,
so pre-launch advertising is measurable per-channel and the launch email list is ours.

## Decisions already made

- **Approach A**: static landing page + a public `POST /waitlist` route on control-api.
  (Rejected: full Next.js now — too heavy; no-code — data lives in a third party.)
- Founder buys the domain (Cloudflare registrar per implementation-plan); assume present.
- Paid-ads budget: **$200** → two $100 experiments (Reddit ads, Google Search). X ads dropped.
- Double opt-in email via Resend is a **fast-follow**, not v1 — but the schema ships with
  `confirm_token`/`confirmed_at` NOW, because `create_all` never alters existing tables and
  adding columns later would mean live DDL on prod (see enum-migration gotcha).

## apps/web — static site

- Plain HTML/CSS/JS, no build step. Served by **Caddy** (`caddy:2-alpine` Dockerfile),
  deployed as Railway service `web` in squire-prod, custom domain via Cloudflare DNS.
- Caddy also reverse-proxies `/api/*` → control-api over Railway's **private network**
  (`CONTROL_API_ORIGIN` env, default `control-api.railway.internal:8080`). The browser only
  ever talks same-origin, so no CORS anywhere, and the waitlist route is not exposed on a
  second public domain.
- Pages: `index.html` (hero → how-it-works → features → waitlist form → footer) and
  `privacy.html` (required by every ad platform).
- Form fields: email (required); feature multi-select checkboxes (closed set below);
  optional free text "what would you want it to do?"; hidden honeypot field `website`;
  hidden UTM fields filled by JS from `location.search` + `document.referrer`.
- Success/duplicate both render the same "you're on the list" state.

## control-api — public waitlist route

- New router `routers/public.py`, **no** internal-token dependency, mounted alongside
  `/internal`. One route: `POST /waitlist`.
- Validation (`schemas.py`): email via conservative regex, lowercased; `features` a list
  drawn from a closed Literal set — `whatsapp, telegram, daily_briefings, email_calendar,
  github_dev, voice_notes, byo_subscription, privacy_isolation`; `use_case` free text
  capped at 500 chars; UTM fields capped at 100 each; `extra="forbid"`.
- Behavior: honeypot filled → 200 without storing (don't tip off bots). Upsert by email
  (repeat signup updates features/use_case, keeps original `created_at`). In-memory
  per-IP rate limit (5/hour, X-Forwarded-For aware) — fine under the existing
  single-replica assumption.
- v1 sets `confirmed_at` immediately (no email flow yet). Fast-follow: generate
  `confirm_token`, send Resend confirmation, `GET /waitlist/confirm/{token}` sets
  `confirmed_at`.

## models.py — `waitlist` table

`id, email (unique), features (JSON-string of the closed set), use_case, utm_source,
utm_medium, utm_campaign, referrer, confirm_token, confirmed_at, created_at`

**Privacy-schema note:** `test_privacy_schema.py` gains the `waitlist` table + column
whitelist. Justification: `email` is signup PII with precedent (`tenant.email`);
`use_case` is a bounded 500-char *marketing survey answer* volunteered on the public
site — it is not tenant conversation content and no credential fits the closed schema
(`extra="forbid"`). No forbidden substrings appear in any column name.

## Tests

- `test_waitlist.py`: happy path, dedupe/upsert, honeypot, rate limit, bad email,
  unknown feature rejected, UTM persisted.
- `test_privacy_schema.py` whitelist updated deliberately.

## Measurement

Signups per UTM source via SQL on the `waitlist` table (weekly 15-min review). No
dashboard until numbers justify one. Same DB as the future `tenant` table → at launch,
cost-per-*activated-user* per channel is a join, not a project.

## Advertising plan (budget: $200 paid + $0 organic)

- **Phase 0 (now → +2wk, $0):** build-in-public on X (2–3 posts/wk), Product Hunt
  "Coming Soon" page, genuine Reddit participation (r/ClaudeAI, r/ChatGPT,
  r/productivity), technical blog post on tenant isolation to front-run a launch-day
  Show HN. All links UTM-tagged.
- **Phase 1 (wk 3–6, $200):** two $100 experiments measured on cost per waitlist signup
  (target <$3, kill at >$8): Reddit ads (angles "BYO subscription" vs "privacy") and
  Google Search on high-intent queries ("personal AI assistant telegram/whatsapp",
  "hosted AI agent"). Scale the winner only if a future budget allows.
- **Phase 2 (launch):** Resend email sequence (announce → 48h reminder → last call),
  Show HN + Product Hunt launch same week; feature-checkbox data picks the headline.
