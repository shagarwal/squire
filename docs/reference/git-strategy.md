# Hermes Agent Git & Customization Strategy

## Problem

We maintain customizations on top of upstream NousResearch/hermes-agent. Currently all changes live in a feature branch that's painful to rebase because:
- Features, bug workarounds, and config are mixed in the same commits
- Both the human and the agent make changes directly to upstream files
- No automation to reapply patches after `hermes update`

## Principle: Separate by Lifecycle

Every customization falls into one of three categories. Handle each differently.

### Category 1: Config & External Files (zero maintenance)

These live **outside** the hermes-agent git repo and are never touched by updates.

| What | Where |
|---|---|
| Personality | `~/.hermes/SOUL.md` |
| Agent config | `~/.hermes/config.yaml` |
| Auth/tokens | `~/.hermes/.env`, `~/.hermes/auth.json` |
| Memory config | `~/.hermes/hindsight/config.json` |
| Custom tools | `~/.hermes/tools/` |
| Custom skills | `~/.hermes/skills/` |
| Systemd services | `~/.config/systemd/user/` |
| Helper scripts | `~/.local/bin/` |

**Rule:** If a change CAN be a config file, tool, skill, or external script, it MUST be. Never put config-like changes in upstream code.

### Category 2: Bug Workarounds (patch files, auto-applied)

These are fixes for upstream bugs that haven't been merged yet. They're temporary — once upstream fixes the issue, the patch gets deleted.

**Strategy:** Maintain `.patch` files in a `~/.hermes/patches/` directory. A post-update script applies them automatically.

Current workarounds:
- `prompt_builder.py` — remove OAuth trigger strings (`session_search`, `skill_manage`, `MEDIA:/absolute/path/to/file`)

**How it works:**

```
~/.hermes/patches/
├── apply-patches.sh          # Runs after hermes update
├── 001-oauth-trigger-strings.patch
└── README.md                 # Documents each patch and when it can be removed
```

The `apply-patches.sh` script:
```bash
#!/bin/bash
# Auto-apply local patches after hermes update
PATCH_DIR="$HOME/.hermes/patches"
REPO_DIR="$HOME/.hermes/hermes-agent"

for patch in "$PATCH_DIR"/*.patch; do
    [ -f "$patch" ] || continue
    name=$(basename "$patch")
    echo "Applying $name..."
    cd "$REPO_DIR"
    if git apply --check "$patch" 2>/dev/null; then
        git apply "$patch"
        echo "  ✓ Applied cleanly"
    else
        echo "  ✗ FAILED — patch may need updating for new upstream"
        echo "    Review: $patch"
    fi
done
```

**Generating patches:** After making a workaround change:
```bash
cd ~/.hermes/hermes-agent
# Make the fix, then:
git diff agent/prompt_builder.py > ~/.hermes/patches/001-oauth-trigger-strings.patch
git checkout agent/prompt_builder.py  # revert the repo file
# The patch lives externally now
```

### Category 3: Features (PRs to upstream OR a single `custom` branch)

Features that add new functionality (monitor_chat, persona reload, ignored_chats, read receipts, quoted messages) should ideally be **submitted as PRs to upstream**. Once merged, they disappear from our maintenance burden entirely.

**For features NOT yet merged upstream:**

Maintain a single `custom` branch on your fork. Rules:

1. **One branch, always rebased on main.** No feature branches that accumulate. The `custom` branch is your "main + my stuff."

2. **Each feature is a single, clean, squashed commit.** If the agent adds a feature across multiple commits, squash before pushing to `custom`.

3. **The branch tracks what's custom.** `git log main..custom --oneline` always shows exactly what you're maintaining.

4. **Update workflow:**
   ```bash
   git fetch origin main
   git checkout main && git reset --hard origin/main
   git checkout custom && git rebase main
   # Resolve any conflicts (fewer commits = fewer conflicts)
   ~/.hermes/patches/apply-patches.sh
   ```

5. **After upstream merges a feature PR:** Drop that commit from `custom` via interactive rebase.

## The Agent's Role

The agent (Hermes/Greg) should follow these rules when making changes to its own code:

1. **Config changes** → edit `~/.hermes/config.yaml` or other external files. Never touch repo code for config.

2. **Bug workarounds** → make the fix, generate a `.patch` file, save to `~/.hermes/patches/`, revert the repo file. Document in `patches/README.md` what it fixes and what upstream issue/PR tracks it.

3. **New features** → work on a temporary branch, get it working, then squash into a single commit on `custom`. Create a PR to upstream if appropriate.

4. **Never commit directly to `main`** — main always mirrors upstream exactly.

## Update Procedure (future)

```bash
# 1. Stop gateway
systemctl --user stop hermes-gateway.service

# 2. Backup (quick, just the branch state)
git -C ~/.hermes/hermes-agent stash  # if any uncommitted changes

# 3. Update main
cd ~/.hermes/hermes-agent
git checkout main
git pull origin main

# 4. Rebase custom branch
git checkout custom
git rebase main
# Fix conflicts if any (should be minimal with squashed commits)

# 5. Apply bug workaround patches
~/.hermes/patches/apply-patches.sh

# 6. Install deps
venv/bin/python -m pip install -q -r requirements.txt
cd scripts/whatsapp-bridge && npm install && cd ../..

# 7. Restart
systemctl --user start hermes-gateway.service
```

This should be 2-3 minutes with minimal conflict risk.

## Migration from Current State

To migrate from the current `feat/monitor-chat` branch to this strategy:

1. Create the `patches/` directory and extract the prompt_builder workaround as a patch
2. Squash the remaining feature commits into logical units on a new `custom` branch
3. Submit PRs for features that upstream should have (monitor_chat, read receipts, quoted messages)
4. Delete old feature branches

## Summary

| Change type | Where it lives | Survives update? | Maintenance |
|---|---|---|---|
| Config/tools/skills | `~/.hermes/` (outside repo) | Automatic | None |
| Bug workarounds | `~/.hermes/patches/*.patch` | Auto-applied by script | Update patch if it fails |
| Features (PR'd) | Upstream | Automatic once merged | None |
| Features (local) | `custom` branch (squashed) | Rebase on update | Resolve conflicts |
