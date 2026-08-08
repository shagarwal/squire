# Hermes Update Plan: v2026.6.5 base → v2026.8.3

**Written:** 2026-08-04 · **Status:** ✅ EXECUTED 2026-08-04 22:35–23:00 PDT — gateway live on v0.20.0 (2026.8.3), `custom` @ `f4c135b08`, billing verified clean. Remaining: user-interactive checks (live message, approval mirror, home-channel notification).

Updates the VPS Hermes install (`custom` @ `81440ac49`, base `c02192ff6` ≈ June 5) onto
upstream release tag **`v2026.8.3`**. We are **8,691 commits behind** upstream HEAD
(`aec331899`). All findings below were verified live on the VPS on 2026-08-04 via
rehearsal in a throwaway worktree — the running gateway was never touched.

---

## 1. Reconnaissance summary (verified 2026-08-04)

### Anthropic subscription billing — SAFE

The critical concern. Verified against upstream HEAD:

- `_to_oauth_wire_name` in `agent/anthropic_adapter.py` is **intact and hardened** —
  every tool name is promoted to the `mcp__` double-underscore form, and upstream now
  explicitly documents the extra-usage billing lane problem (comment at ~line 2915:
  *"single `mcp_` flips billing from plan-billing to the extra-usage lane;
  `mcp__foo` is accepted"*). They also fixed the `mcp_x` → `mcp__x` promotion gap.
- Our patch `001-oauth-system-prompt-scrubber.patch` (system-prompt scrubber, bare
  compound-tool-ref promotion, `MEDIA:`→`ATTACH:`) **applies cleanly** on the new tree
  (`git apply --check` — zero errors).
- The self-update script's verification markers (`/home/agent` for patch 001,
  `_to_oauth_wire_name` for the upstream fix) are both still valid.

### Patches — all three apply clean on upstream HEAD

| Patch | Target file | Status |
|---|---|---|
| `001-oauth-system-prompt-scrubber.patch` | `agent/anthropic_adapter.py` | ✅ clean |
| `002-cross-chat-approval-routing.patch` | `tools/approval.py` | ✅ clean |
| `003-cross-chat-approval-sessionkey.patch` | `gateway/slash_commands.py` | ✅ clean |

### Custom branch rebase rehearsal

Rebasing the 6 custom commits onto upstream HEAD:

| Commit | Result |
|---|---|
| `11cbfa72b` /persona reload | ✅ applies clean |
| `cba65c66a` persona broaden | ✅ applies clean |
| `f8691a300` monitor_chat | ❌ conflicts: `gateway/run.py` (489 upstream commits of churn), `hermes_cli/config.py` — **being dropped & rewritten, see §2** |
| `16f740d2c` local customizations | ❌ conflicts: `prompt_builder.py`, `bridge.js` — **mostly absorbed upstream, slimming, see below** |
| `b23596700` tz-aware suspend fix | ✅ applies clean |
| `81440ac49` chore (prompt_builder revert) | ❌ conflicts — **dropping** (only existed to cancel 16f740d2c's prompt_builder hunks) |

### Upstream absorbed our features (drop from custom)

- **Quoted-message reply context** — native now. Bridge sends `quotedText`;
  `run.py:15947` injects `[Replying to: "…"]`. Drop our bridge.js + whatsapp.py hunks.
- **Read receipts** — `sock.readMessages` is native in bridge.js. Only our
  `markOnlineOnConnect: true` one-liner remains custom.
- **WhatsApp platform moved**: `gateway/platforms/whatsapp.py` →
  `plugins/platforms/whatsapp/adapter.py` (plugin architecture). Still uses the
  Baileys node bridge — not deprecated.

### Other notable upstream changes

- **Baileys bumped** from a git-pinned commit to `7.0.0-rc13` — WhatsApp session
  invalidation risk (re-pair contingency in §7).
- **Config auto-migration** (`hermes_cli/config_migrations.py`): our
  `display.tool_progress_overrides` auto-migrates to
  `display.platforms.whatsapp.tool_progress` (v15→16). `fallback_providers` schema
  unchanged.
- `pyproject.toml`/`setup.py` heavily reworked — full deps reinstall required.
- New: `/platform` pause/resume commands, per-adapter circuit breakers, restart
  notifications, session auto-resume after restart.

---

## 2. The monitor_chat decision (investigated 2026-08-04)

**Old feature:** 154-line insertion in `gateway/run.py` + config key, routing approval
requests and status updates from any session to Shaurya's home DM, with
`/approve <session_key>` cross-chat approval via patches 002/003. Progress-mirroring
was already dropped in the June rebase.

**Upstream check results:**

- ✅ Upstream now has a native **home-channel concept**: `WHATSAPP_HOME_CHANNEL` env
  var in the WhatsApp plugin (`plugins/platforms/whatsapp/adapter.py:1828`), used for
  cron delivery, gateway restart notifications, circuit-breaker operator alerts, and
  session handoff threads.
- ❌ **Approval prompts still go only to the session's own chat** —
  `_approval_notify_sync` (`gateway/run.py:~5031`) sends to `ctx._status_chat_id`.
- ❌ Native `/approve` accepts **no session-key argument** (only
  `all|session|always` modifiers) — patch 003 is still required for cross-chat approve.

**Decision: drop the old commit, rewrite as a slim hook on upstream plumbing.**

New implementation (~25 lines, one new squashed commit on `custom`):

1. Configure `WHATSAPP_HOME_CHANNEL` (native env var — **no config.py changes at
   all**, which eliminates that conflict).
2. In `_approval_notify_sync`, after the normal in-chat approval prompt: if the
   session's chat ≠ home channel, also send the prompt to the home channel including
   the `session_key`, formatted for `/approve <session_key>` (patch 003's syntax).
3. Keep patches 002 + 003 unchanged (both apply clean).

**Why this beats re-targeting the old commit:**

- The rebase becomes **fully conflict-free** (the only other conflicting commits are
  being dropped/slimmed anyway).
- Hooks one small, well-defined function instead of weaving through run.py's dispatch
  paths → far cheaper to carry through future upstream refactors (this was the third
  re-target; each big upstream refactor cost hours).
- Free bonuses from native home-channel: restart notifications, circuit-breaker
  alerts, cron delivery to the home DM.

**Accepted degradation:** our custom *status-update* routing to home chat goes away.
Largely compensated by upstream's native restart/breaker/operator notifications. If
missed in practice, extend the same hook later.

---

## 3. Execution phases

Run everything **manually over SSH** (`ssh hermes` / key at
`~/.ssh/hermes-bootstrap/hermes_bootstrap_ed25519`, host `207.246.98.195`), not via the
self-update script — SSH is outside the gateway cgroup (no self-eviction risk) and the
monitor_chat rewrite needs a human/agent in the loop anyway. Repo:
`~/.hermes/hermes-agent`, branch `custom`.

### Phase 0 — Safety net (~5 min)

1. Pick a quiet window; confirm no active user session on the gateway.
2. `git branch backup/pre-v2026.8.3-cutover-$(date +%Y%m%d) custom` and push to `fork`.
3. `mkdir -p ~/.hermes/patches/pre-v2026.8.3-backup && cp ~/.hermes/patches/*.patch ~/.hermes/patches/pre-v2026.8.3-backup/`
4. Snapshot `~/.hermes/config.yaml` + `~/.hermes/auth.json` (config migration rewrites
   config.yaml).
5. Disk verified OK (28G free). Gateway stays **up** through Phases 1–2 (work happens
   on branches/worktrees; live tree has patches applied — leave it alone until cutover).

### Phase 1 — Slim the custom branch (interactive rebase on current base)

Rewrite `custom` (still on old base) down to 4 lean commits:

- **Drop** `f8691a300` (monitor_chat — being rewritten, §2).
- **Drop** `81440ac49` (chore revert).
- **Edit** `16f740d2c`: remove `prompt_builder.py` hunks (cancel against dropped
  chore), remove `gateway/platforms/whatsapp.py` hunk (native now), remove bridge.js
  `quotedText`/`readMessages` hunks (native now). **Keep**: `markOnlineOnConnect: true`
  (bridge.js) + google-workspace GAS scope (`setup.py`).
- **Keep unchanged**: persona reload, persona broaden, tz-aware fix.

### Phase 2 — Rebase onto v2026.8.3 (expected conflict-free)

```bash
cd ~/.hermes/hermes-agent
git checkout main && git reset --hard v2026.8.3   # also fixes the stale-main drift from June
git checkout custom && git rebase main
```

Per rehearsal, all 4 remaining commits apply clean. If the slimmed `16f740d2c` still
brushes bridge.js context, resolution is trivial (one boolean line). Watch for
commits silently becoming empty (upstream absorbed them) — that's fine, let rebase
drop them.

### Phase 3 — Rewrite monitor-chat as the home-channel approval mirror

1. Set `WHATSAPP_HOME_CHANNEL=<Shaurya's DM chat id>` in `~/.hermes/.env` (get the id
   from existing config/monitor_chat setting before dropping it).
2. New commit on `custom`: hook in `_approval_notify_sync` (see §2). Include a small
   test if practical (model on the old `test_monitor_chat.py`, now dropped).
3. Verify patches 002/003 anchors unaffected (they patch different files).

### Phase 4 — Reapply billing patches

```bash
~/.hermes/patches/apply-patches.sh    # 001, 002, 003 — all pre-verified clean
grep '/home/agent' agent/anthropic_adapter.py        # patch 001 marker
grep '_to_oauth_wire_name' agent/anthropic_adapter.py # upstream billing fix marker
```

### Phase 5 — Dependencies

```bash
venv/bin/python -m pip install -q -r requirements.txt
venv/bin/python -m pip install -q psutil    # graceful bridge shutdown needs it
cd scripts/whatsapp-bridge && npm install    # pulls Baileys 7.0.0-rc13
```

Contingency: if the venv's Python is too old for the new `pyproject.toml`, rebuild the
venv (`uv venv` + reinstall) before proceeding.

### Phase 6 — Cutover

```bash
systemctl --user stop hermes-gateway.service
pkill -f 'whatsapp-bridge/bridge.js' || true          # orphan sweep
~/.local/bin/hermes --version                          # triggers config migration; review output
# confirm display.platforms.whatsapp.tool_progress: off landed in config.yaml
systemctl --user start hermes-gateway.service
```

### Phase 7 — Verification gates (the "subscription doesn't break" proof)

1. **Services:** gateway active with 0 restarts; `hindsight-daemon` + `pg0-hindsight`
   unaffected; bridge `/health` → `{"status":"connected"}`.
2. **Billing A/B curl** (canonical test — see `project_mcp_prefix_fix` memory):
   minimal payload with tool named `mcp__x` → expect **200**; bare `x` → **200**.
   - `mcp__x` → 400 "out of extra usage" ⇒ upstream regressed wire-naming. Stop,
     diagnose, don't guess.
   - Both → 429 ⇒ per-minute throttle, not a failure; wait 30–60 min.
3. **Live message test:** send a WhatsApp message; reply must come from
   **Opus/Sonnet**, not gpt-5.5 fallback ("switching to fallback … openai-codex" in
   gateway logs = billing breakage tell).
4. **30-min canary:** watch logs for `out of extra usage` / HTTP 400. 8,691 new
   commits may have introduced new bare compound tool-name tokens in the system prompt
   (the June-19 third vector). Patch 001's generic `mcp_→mcp__` regex +
   `_promote_bare_tool_refs` should catch them. If a 400 appears: dump request
   (`~/.hermes/sessions/request_dump_*.json`), A/B-bisect the new token, extend 001.
5. **Feature checks:** quoted-reply context works natively (reply to a message →
   `[Replying to:]` in agent context); read receipts / blue ticks; approval mirror —
   trigger a dangerous command from a group chat, confirm the prompt lands in the home
   DM and `/approve <session_key>` from there unblocks it.
6. **WhatsApp session survived Baileys 7.0.0-rc13** — if logged out, re-pair (§7).
7. Hindsight recall works; `hermes auth` pool all `ok`.

### Phase 8 — Bookkeeping

1. Push `custom` to `fork`.
2. Regenerate any patch whose anchors changed (file-disjoint rule:
   001=`anthropic_adapter.py`, 002=`approval.py`, 003=`slash_commands.py`).
3. Update `~/.hermes/patches/README.md`; update the self-update script if paths/marker
   expectations moved; update Claude memory files
   (`project_hermes_setup`, `project_self_update_fix`, `project_mcp_prefix_fix`).
4. Keep the backup branch + patch backups until the next successful update.

---

## 4. Rollback

```bash
systemctl --user stop hermes-gateway.service
cd ~/.hermes/hermes-agent
git checkout backup/pre-v2026.8.3-cutover-<date>
cp ~/.hermes/patches/pre-v2026.8.3-backup/*.patch ~/.hermes/patches/
~/.hermes/patches/apply-patches.sh
cd scripts/whatsapp-bridge && npm install     # lockfile restores old Baileys
cd ../.. && systemctl --user start hermes-gateway.service
```

Restore the config.yaml snapshot if the migration misbehaved. WhatsApp session files
live outside git — rollback does **not** require re-pairing.

## 5. Risks, ranked

1. **Baileys 7.0.0-rc13** may invalidate the WhatsApp session → re-pair via the
   headless QR→PNG procedure (`project_whatsapp_repair` memory;
   `~/.hermes/platforms/whatsapp/`).
2. **New billing-trigger strings** in the heavily-changed system prompt → caught by
   Phase 7 canary; patch 001 is generic and already applies clean.
3. **Home-channel approval hook is new code** → small surface (one function), tested
   in Phase 7.5; worst case approvals still work in-chat (upstream default).
4. **Config migration surprises** → snapshot + review of migration output.
5. **venv/Python mismatch** from pyproject rework → rebuild venv contingency (Phase 5).
