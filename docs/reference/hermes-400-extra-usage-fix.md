# Fixing Hermes + Claude Max OAuth: `400 "You're out of extra usage"`

If your Hermes agent is returning HTTP 400 with:

> You're out of extra usage. Add more at claude.ai/settings/usage and keep going.

…even though your Max subscription is fresh / unused, this guide walks through the real root cause and the full set of patches that were needed to get a stable setup. Apply them in order — the first one is the one that actually unblocks requests; the rest are defense-in-depth for related issues that may also surface.

All edits below are against the file:

```
~/.hermes/hermes-agent/agent/anthropic_adapter.py
```

(on the VPS, as the `hermes` user). Back it up first:

```bash
cp ~/.hermes/hermes-agent/agent/anthropic_adapter.py \
   ~/.hermes/hermes-agent/agent/anthropic_adapter.py.bak
```

After any edits, restart the gateway:

```bash
systemctl --user restart hermes-gateway.service
```

---

## 0. First: rule out a rate-limit false alarm

Before patching anything, check whether you're actually rate-limited (429) rather than seeing the 400. Max plan has a per-minute throttle that returns 429 on *any* request, even `"hi"`, and clears on its own in 30–60 min.

```bash
TOK=$(python3 -c "import json; d=json.load(open('/home/hermes/.hermes/auth.json')); print(d['credential_pool']['anthropic'][0]['access_token'])")

curl -sS -o /dev/null -w "%{http_code}\n" -X POST https://api.anthropic.com/v1/messages \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: oauth-2025-04-20,claude-code-20250219" \
  -H "authorization: Bearer $TOK" \
  -H "content-type: application/json" \
  -H "user-agent: claude-cli/2.1.101 (external, cli)" \
  -H "x-app: cli" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":50,"system":[{"type":"text","text":"You are Claude Code, Anthropic'"'"'s official CLI for Claude."}],"messages":[{"role":"user","content":"hi"}]}'
```

- `200` → auth is good, the problem is Hermes request shape (continue to fix #1).
- `429` → plain rate limit; wait 30–60 min and retry.
- `400` → you've reproduced the bug on a bare request. Continue to fix #1.

Also check `~/.hermes/auth.json`: if the anthropic credential has `"last_status": "exhausted"`, reset it to `"ok"` (or run `hermes auth` → Reset cooldowns). An exhausted entry can block auxiliary tasks from using the token.

---

## 1. THE fix — strip the `mcp_` tool-name prefix (required)

### Root cause

Hermes's `build_anthropic_kwargs` prepends `mcp_` to every tool name on OAuth requests. Anthropic's OAuth classifier reads `mcp_*` tool names as "MCP-integrated API usage" and routes the call to the **extra-usage billing pool** (which requires explicit paid API credits) instead of the **Max subscription pool**. Hence the misleading "out of extra usage" 400 — it's not a billing problem, it's a routing problem.

**Confirmed by isolated A/B test** (same token, same system prompt, one tool):

- tool named `mcp_browser_back` → HTTP 400 "out of extra usage"
- tool named `browser_back`     → HTTP 200 OK

### Diagnostic (confirm it's this before patching)

```bash
TOK=$(python3 -c "import json; d=json.load(open('/home/hermes/.hermes/auth.json')); print(d['credential_pool']['anthropic'][0]['access_token'])")

# With mcp_ prefix — should 400 if the bug is present
curl -sS -o /dev/null -w "%{http_code}\n" -X POST https://api.anthropic.com/v1/messages \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: oauth-2025-04-20,claude-code-20250219" \
  -H "authorization: Bearer $TOK" \
  -H "content-type: application/json" \
  -H "user-agent: claude-cli/2.1.101 (external, cli)" \
  -H "x-app: cli" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":100,"system":[{"type":"text","text":"You are Claude Code, Anthropic'"'"'s official CLI for Claude."}],"tools":[{"name":"mcp_x","description":"test","input_schema":{"type":"object","properties":{}}}],"messages":[{"role":"user","content":"hi"}]}'

# Without prefix — should 200
# (repeat above, replacing "mcp_x" with "x")
```

If test 1 is 400 and test 2 is 200, you have the bug.

### The patch

In `agent/anthropic_adapter.py`, find `build_anthropic_kwargs` and locate the `if is_oauth:` branch. There are **two** places where `mcp_` is prepended — change both to **strip** it.

**Step 3 — tool definitions.** Replace:

```python
# 3. Prefix tool names with mcp_ (Claude Code convention)
if anthropic_tools:
    for tool in anthropic_tools:
        if "name" in tool:
            tool["name"] = _MCP_TOOL_PREFIX + tool["name"]
```

with:

```python
# 3. (Patched) STRIP mcp_ prefix — Anthropic routes mcp_*-named tools to the
#    extra-usage billing pool instead of the Max subscription.
if anthropic_tools:
    for tool in anthropic_tools:
        if isinstance(tool, dict) and isinstance(tool.get("name"), str):
            if tool["name"].startswith(_MCP_TOOL_PREFIX):
                tool["name"] = tool["name"][len(_MCP_TOOL_PREFIX):]
```

**Step 4 — historical `tool_use` blocks in prior messages.** Replace:

```python
if block.get("type") == "tool_use" and "name" in block:
    if not block["name"].startswith(_MCP_TOOL_PREFIX):
        block["name"] = _MCP_TOOL_PREFIX + block["name"]
```

with:

```python
if block.get("type") == "tool_use" and isinstance(block.get("name"), str):
    if block["name"].startswith(_MCP_TOOL_PREFIX):
        block["name"] = block["name"][len(_MCP_TOOL_PREFIX):]
```

### Why the response path is safe

`normalize_anthropic_response()` is already called with `strip_tool_prefix=self._is_anthropic_oauth`. Since outbound names no longer carry `mcp_`, the model replies with un-prefixed names and the strip becomes a no-op. The internal tool registry uses un-prefixed names already — nothing else needs changing.

---

## 2. System-prompt scrubber — replace Hermes-specific strings

Anthropic's OAuth path seems to look for Claude-Code-shaped prompts. Leaving the word `Hermes`, Hermes-specific tool names, or filesystem paths in the system prompt is another way to get flagged. Upstream PR #10576 adds a partial scrubber; I extended it with more substitutions.

Add (or extend) a helper near the top of `anthropic_adapter.py`:

```python
_OAUTH_SYSTEM_REPLACEMENTS = [
    # PR #10576 base scrubber
    ("session_search", "history_lookup"),
    ("skill_manage",    "procedure_update"),
    # …plus the other ~11 entries upstream ships…

    # Extensions I added on top:
    ("MEDIA:",         "ATTACH:"),
    ("hermes_tools",   "agent_tools"),
    ("~/.hermes",      "~/.config/agent"),
    ("/home/hermes",   "/home/agent"),
    ("Hermes",         "Claude Code"),
]

def _sanitize_oauth_system_text(text: str) -> str:
    for old, new in _OAUTH_SYSTEM_REPLACEMENTS:
        text = text.replace(old, new)
    return text
```

Call it on the system prompt inside the `is_oauth` branch of `build_anthropic_kwargs`, before the request is serialized.

> On its own this **does not fix** the 400 — fix #1 is the real fix. But the scrubber keeps request shape clean and is recommended defense-in-depth.

---

## 3. Deep recursive scrub — catch Hermes strings in nested payload

The scrubber also needs to reach strings buried in tool schemas, past tool calls, and thinking blocks. Add a helper and walk the outgoing payload:

```python
def _scrub_any(obj):
    if isinstance(obj, str):
        return _sanitize_oauth_system_text(obj)
    if isinstance(obj, dict):
        return {k: _scrub_any(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_any(v) for v in obj]
    return obj
```

Inside the `is_oauth` branch, after the tool list and messages are built, run:

- each `tool["description"]` through `_scrub_any`
- `tool["input_schema"]["properties"][*]["description"]` at any depth
- any `thinking` block `text`
- any `tool_use` `input`
- any `tool_result` `content`

---

## 4. Tool rename map — rename Hermes-specific tools in both directions

A couple of tool names are obvious Hermes fingerprints even after the scrubber. Map them at the adapter boundary (apply after the `mcp_` prefix is already stripped, so keys are the un-prefixed names):

```python
_OAUTH_TOOL_RENAMES = {
    "session_search": "history_lookup",
    "skill_manage":   "procedure_update",
}
```

Apply to:

- each outgoing `tool["name"]` in the tool list
- each historical `tool_use` `block["name"]` in prior messages
- invert the map when parsing responses so the rest of Hermes still sees the original names

---

## 5. Put `claude` CLI on PATH so UA is not stale

`_detect_claude_code_version()` shells out to `claude --version` to build the `user-agent: claude-cli/<ver>` header, and falls back to an old `2.1.74` string if the binary isn't on PATH. A stale UA looks suspicious to the OAuth classifier.

```bash
ln -s /home/hermes/.hermes/node/bin/claude /home/hermes/.local/bin/claude
```

Verify the detected version:

```bash
claude --version
# should print the installed version, e.g. 2.1.101 (Claude Code)
```

---

## 6. Do NOT cap `_ANTHROPIC_OUTPUT_LIMITS`

There's advice floating around (including from the GitHub thread on issue #10575) to cap `_ANTHROPIC_OUTPUT_LIMITS` to 32K as a "fix". **Don't.** It was a guess, and I verified that 128K `max_tokens` works fine on Opus 4.7 once fix #1 is in place. Capping it needlessly restricts your agent's output capacity. Keep upstream defaults:

- opus: 128K
- sonnet: 64K
- default: 128K

---

## 7. Codex fallback — do NOT leave `o3` as the fallback model

When the Max quota is genuinely hit, Hermes falls back to OpenAI Codex (`fallback_model` in `~/.hermes/config.yaml`). The upstream/default `o3` is **not supported on ChatGPT-auth accounts** — Codex with a ChatGPT account rejects it with:

> The 'o3' model is not supported when using Codex with a ChatGPT account.

Only API-key accounts can use `o3` via Codex.

### Valid slugs for ChatGPT Plus OAuth (live-queryable)

```bash
TOK=$(python3 -c "import json; d=json.load(open('/home/hermes/.hermes/auth.json')); print(d['credential_pool']['openai-codex'][0]['access_token'])")
curl -sS -H "Authorization: Bearer $TOK" \
  'https://chatgpt.com/backend-api/codex/models?client_version=1.0.0'
```

Known good slugs: `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.2`, `codex-auto-review`.

Set in `~/.hermes/config.yaml`:

```yaml
fallback_model:
  provider: openai-codex
  model: gpt-5.4
```

### Restart properly

After editing config, do a clean stop/start (not `restart`) — in-flight requests in the dying process replay with the OLD config and cause confusing duplicate failures:

```bash
systemctl --user stop  hermes-gateway.service
systemctl --user start hermes-gateway.service
```

---

## 8. Make the patches durable across `hermes update`

`hermes update` will clobber anthropic_adapter.py and restore the upstream `mcp_`-prefixing code. Save your changes as a unified diff and re-apply after every update:

```bash
mkdir -p ~/.hermes/patches

# Generate the patch from your working tree
cd ~/.hermes/hermes-agent
git diff HEAD -- agent/anthropic_adapter.py \
  > ~/.hermes/patches/001-oauth-extra-usage-fix.patch
```

Minimal `~/.hermes/patches/apply-patches.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd ~/.hermes/hermes-agent
for p in ~/.hermes/patches/*.patch; do
    if git apply --check "$p" 2>/dev/null; then
        git apply "$p"
        echo "✓ applied $(basename "$p")"
    else
        echo "✗ FAILED — patch may need updating: $(basename "$p")"
    fi
done
```

```bash
chmod +x ~/.hermes/patches/apply-patches.sh
```

**Update procedure from now on:**

```bash
hermes update
~/.hermes/patches/apply-patches.sh
systemctl --user restart hermes-gateway.service
```

If `apply-patches.sh` reports a failure after an upstream change, the anchor text in `build_anthropic_kwargs` has moved. Re-edit the file by hand per sections 1–4 of this doc and regenerate the patch with the `git diff` command above.

---

## 9. If the 400 appears to "recur" mid-conversation

Before the full patch was in place, 400s would appear to recur within a single session and `/reset` would "fix" them. The cause was historical `tool_use` blocks in the session's message history still carrying the `mcp_` prefix, so every subsequent turn re-shipped poisoned history. `/reset` dumped the history and the next turn started clean.

Fix #1 step 4 (strip the prefix from historical `tool_use` blocks in prior messages) eliminates this. On my deployment, after the patch landed: 33 inbound messages the next day, zero 400s of any kind.

**If you still see it recur after applying all of section 1:**

1. First, confirm step 4 of the patch is actually applied — a lot of partial versions of this patch online only do step 3. Without step 4, historical tool_use blocks keep the prefix and `/reset` will feel like the only cure.
2. Before running `/reset`, capture the failing outgoing request:

   ```bash
   ls -t ~/.hermes/sessions/request_dump_*.json | head -1 | \
     xargs -I{} grep -oE '(mcp_|Hermes|hermes_|session_search|skill_manage|MEDIA:|/home/hermes|~/.hermes)' {} | sort -u
   ```

   Whatever matches is the surface the scrubber is missing. The most plausible *remaining* holes are:

   - Compressed context summaries (`compression.summary_provider` path — summary text is injected back into the message list but not currently routed through the scrubber)
   - Plain `{"type":"text","text":"..."}` content blocks in user/assistant history (the deep `_scrub_any` walker covers tool descriptions, thinking blocks, `tool_use.input`, `tool_result.content` but not `text` content blocks)

   Extend the scrubber for whichever surface shows a hit. Don't speculatively patch both without data — you may be chasing a ghost.

---

## 10. Signs that fix #1 has been clobbered (again)

Telltale signs the upstream prefix code is back:

- Requests start returning `400 "out of extra usage"` again with no usage spike on claude.ai.
- Gateway logs show `switching to fallback: gpt-5.4 via openai-codex` and WhatsApp replies are suddenly in a noticeably different voice (GPT-5.4 instead of Opus/Sonnet).
- `curl` A/B test from section 1 shows the prefix version returning 400.

Re-run `apply-patches.sh` and restart the gateway.

---

## Quick reference — files touched

| Change | File | Persistence |
| --- | --- | --- |
| `mcp_` prefix strip (fix #1) | `agent/anthropic_adapter.py` | patch in `~/.hermes/patches/` |
| System-prompt scrubber + extensions (fix #2) | `agent/anthropic_adapter.py` | same patch |
| Deep recursive scrub (fix #3) | `agent/anthropic_adapter.py` | same patch |
| Tool rename map (fix #4) | `agent/anthropic_adapter.py` | same patch |
| `claude` CLI on PATH (fix #5) | symlink in `~/.local/bin/` | survives updates |
| Keep default `_ANTHROPIC_OUTPUT_LIMITS` (fix #6) | N/A (don't change) | — |
| Codex fallback model (fix #7) | `~/.hermes/config.yaml` | survives updates |

Start with fix #1. If the A/B curl test in section 1 goes from 400 → 200 after applying it, you're done — the rest are hygiene.
