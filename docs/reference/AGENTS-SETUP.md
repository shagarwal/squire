# AGENTS-SETUP.md — Hermes Bootstrap Runbook (for Claude Code)

> **Audience:** Claude Code (or another capable coding agent) running in this repository, with the user present at the keyboard.
>
> **Pair file:** [README.md](README.md) — the human-facing overview. Assume the user has read it and completed the pre-flight checklist.
>
> **Mode of operation:** Work through the chapters **in order**. At every 🛑 **CHECKPOINT**, stop and wait for the user to verify. At every ✋ **HANDOFF**, stop and wait for the user to do something physical (scan QR, click email link, buy Burner number). Do not skip checkpoints.

---

## Ground rules for the agent

1. **Read `.env` first.** It lives next to this file and is the single source of truth for user input. If `.env` does not exist yet, the user skipped the copy step — run `cp .env.template .env` yourself (or on Windows, use `Edit`/`Read`+`Write` to create it from the template) and tell the user it's ready for them to fill in. Variables the user has already filled in are authoritative — do not ask for them again. For anything missing, ask with `AskUserQuestion` or plain chat, then **write the answer back into `.env`** using `Edit` so the file stays a complete record of the setup. Only write values the user has explicitly provided — never auto-populate secret fields.
2. **`.env` is gitignored, `.env.template` is not.** Never write secrets into `.env.template` and never suggest committing `.env`. When echoing commands that contain secrets, redact them in your chat output.
3. **Prefer idempotency.** Every command you run on the VPS should be safe to re-run. If a step fails partway, you should be able to resume from the same chapter.
4. **Pause, don't power through.** When a checkpoint says *wait for the user*, wait. Do not start the next chapter until the user confirms.
5. **If something breaks, debug live.** Read logs, ask the user to paste output, fix it. This runbook is a guide, not a script.
6. **One SSH session at a time.** Keep a persistent SSH connection open via a background Bash process where possible; don't open/close for every command.

---

## Chapter 0 — Intake

**Goal:** Confirm what the user has, pick the paths.

1. Read `.env` and list back to the user what you found (redact secrets, just show key names that have values).
2. Ask (use `AskUserQuestion` for multi-choice):
   - **VPS path:** Vultr (have `VULTR_API_KEY`) or existing VPS (`VPS_HOST` filled in)?
   - **LLM path:** Claude Max OAuth, Anthropic API, ChatGPT Codex OAuth, or OpenAI API? (Only offer options where credentials are present or the OAuth flag is set.)
   - **WhatsApp:** confirm they have a Burner number provisioned and WhatsApp Business installed and registered on it. If not → ✋ **HANDOFF**: wait for them to finish.
3. Summarise the plan back in 4–6 bullets and get a thumbs-up before proceeding.

---

## Chapter 1 — SSH key

**Goal:** Make sure there is a usable SSH keypair **inside this repo** at `./keys/` and that the absolute paths are recorded in `.env`. Co-locating the key with `.env` means the entire agent bootstrap travels as one directory — if the user backs up the repo, they back up their VPS access. `keys/` is already in `.gitignore` so the private key cannot be accidentally committed.

1. Read `SSH_PUBLIC_KEY_PATH` and `SSH_PRIVATE_KEY_PATH` from `.env`.
2. **If both are filled in** → verify both files exist and are readable. If yes, skip to Chapter 2. If no, tell the user the paths are bad and ask whether to fall back to generating a new key in-repo.
3. **If either is blank** → generate a fresh, dedicated keypair into the repo:
   ```bash
   mkdir -p ./keys && chmod 700 ./keys
   ssh-keygen -t ed25519 -N "" -C "hermes-bootstrap" -f ./keys/hermes_bootstrap_ed25519
   chmod 600 ./keys/hermes_bootstrap_ed25519
   chmod 644 ./keys/hermes_bootstrap_ed25519.pub
   ```
   No passphrase — this key only exists to bootstrap and reach this one VPS; the user can rotate later if they want.
4. **Persist the ABSOLUTE paths back into `.env`** using the `Edit` tool. Resolve `./keys/...` to the full path (e.g. `C:/Users/Waz/Github/hermes-agent-bootstrap/keys/hermes_bootstrap_ed25519`) so SSH and any other tooling can find the files regardless of working directory:
   ```
   SSH_PUBLIC_KEY_PATH=<absolute path>/keys/hermes_bootstrap_ed25519.pub
   SSH_PRIVATE_KEY_PATH=<absolute path>/keys/hermes_bootstrap_ed25519
   ```
5. **Verify `keys/` is gitignored.** Run `git check-ignore -v keys/hermes_bootstrap_ed25519` — it should print a rule match. If it doesn't, stop and fix `.gitignore` before going further. Do not proceed until the private key is provably untracked.
6. **Tell the user, clearly and explicitly:**

   > I generated a fresh SSH keypair inside this repo at `./keys/hermes_bootstrap_ed25519` (private) and `./keys/hermes_bootstrap_ed25519.pub` (public), and saved the absolute paths into `.env`. The `keys/` folder is gitignored so it will never be pushed.
   >
   > **⚠️  This key is your only way to SSH into your VPS. If you lose this repo, you lose access.** Please back up the entire `hermes-agent-bootstrap/` folder somewhere safe *right now* — cloud storage, an encrypted USB stick, a password manager attachment, or just a second copy on another machine. I'll wait.

### 🛑 CHECKPOINT 1

Ask the user to **explicitly confirm two things**:
1. They can see `./keys/hermes_bootstrap_ed25519` and `./keys/hermes_bootstrap_ed25519.pub` in the repo.
2. They have backed up (or committed to backing up today) the full repo folder including `keys/` and `.env`.

Do not proceed until both are acknowledged. If the user is reluctant to back up right now, note it in chat and bring it up again at the end of Chapter 7.

---

## Chapter 2 — Provision the VPS

### 2a. Vultr path (happy path)

Use the Vultr API with `curl` via Bash. Don't install any SDK.

1. **Sanity-check the token:**
   ```bash
   curl -s -H "Authorization: Bearer $VULTR_API_KEY" https://api.vultr.com/v2/account
   ```
   If 401/403 → stop, ask the user to re-check `VULTR_API_KEY`.

2. **Upload the SSH public key** (idempotent — check if one with the same name already exists first):
   ```bash
   curl -s -H "Authorization: Bearer $VULTR_API_KEY" https://api.vultr.com/v2/ssh-keys
   ```
   If `hermes-bootstrap` isn't there, POST it:
   ```bash
   curl -s -X POST https://api.vultr.com/v2/ssh-keys \
     -H "Authorization: Bearer $VULTR_API_KEY" \
     -H "Content-Type: application/json" \
     -d "{\"name\":\"hermes-bootstrap\",\"ssh_key\":\"$(cat $SSH_PUBLIC_KEY_PATH)\"}"
   ```

3. **Pick a region** if `VULTR_REGION` is empty. List regions via `GET /v2/regions`, show the user 5–6 common ones, ask them to pick.

4. **Create the instance:**
   ```bash
   curl -s -X POST https://api.vultr.com/v2/instances \
     -H "Authorization: Bearer $VULTR_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "region":"'"$VULTR_REGION"'",
       "plan":"'"$VULTR_PLAN"'",
       "os_id":'"$VULTR_OS_ID"',
       "label":"'"$VULTR_LABEL"'",
       "sshkey_id":["<id from step 2>"],
       "backups":"disabled",
       "enable_ipv6":true
     }'
   ```

5. **Poll** `GET /v2/instances/{id}` every ~10 seconds until `status=active` and `main_ip` is non-zero. This can take 60–120 seconds. Save the IP as `VPS_HOST`.

### 2b. Existing-VPS path

Skip creation. Read `VPS_HOST`, `VPS_USER`, `VPS_SSH_PORT` from `.env`. Verify you can reach port 22 (or custom) with `nc -zv` or `ssh -o BatchMode=yes`.

### 🛑 CHECKPOINT 2

Attempt SSH:
```bash
ssh -i $SSH_PRIVATE_KEY_PATH -o StrictHostKeyChecking=accept-new $VPS_USER@$VPS_HOST 'uname -a && lsb_release -a'
```
Show the user the output. Confirm they can see "Ubuntu 24.04" (or whatever they picked). **Wait for thumbs-up before continuing.**

Also persist `VPS_HOST` back into `.env` (via `Edit`) so the file is a full record of the provisioned infra.

---

## Chapter 3 — Baseline hardening & prereqs

Everything from here runs over SSH. Keep commands chained with `&&` where sensible and use `sudo -n` to catch missing privileges early.

1. `apt-get update && apt-get -y upgrade`
2. Install prereqs: `apt-get -y install curl git build-essential python3 python3-pip python3-venv ufw fail2ban unattended-upgrades ca-certificates`
3. Create a non-root user `hermes` with sudo and the same SSH key authorized. Lock root SSH password login. Don't yet touch SSH port or pubkey-only — that's the optional security chapter.
4. Set timezone to the user's timezone (ask if not inferrable).
5. Enable `unattended-upgrades` with default config.

### 🛑 CHECKPOINT 3

`ssh hermes@$VPS_HOST 'whoami && sudo -n true && echo ok'` should print `hermes` and `ok`. Confirm.

---

## Chapter 4 — Install Hermes

From here, all commands run as the `hermes` user.

1. Run the official one-liner:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
   ```
2. `source ~/.bashrc`
3. Verify the binary is on PATH: `command -v hermes && hermes --version`
4. **Do NOT run `hermes setup` yet** — the next chapter decides *who* runs it and *how*.

### 🛑 CHECKPOINT 4

Show the user `hermes --version`. Confirm it looks sensible.

---

## Chapter 5 — Choose your onboarding path

Hermes is installed on the VPS. Before we configure a model and a WhatsApp gateway, the user needs to pick **how** the onboarding happens. There are two paths; **strongly recommend Path B** (hands-on) and frame Path A (white-glove) as a fallback.

Present this to the user verbatim (or close to it) and wait for their choice with `AskUserQuestion`:

> **You're at a fork in the road. Pick one:**
>
> **Path A — White-glove (I do it for you).**
> I stay in this chat and run `hermes setup` over SSH on your behalf. When it asks me a question I can't answer (your timezone, your preferred default model, which tools to enable, etc.) I'll pause and ask you here in chat. You barely touch a keyboard.
>
> *Trade-off:* you don't get a feel for the Hermes CLI or the interactive setup wizard — which is half the charm of the tool. If something breaks later, you won't know the muscle memory of how to log in and poke around.
>
> **Path B — Hands-on (you do it, I coach). ⭐ Recommended.**
> You open a new terminal on your own machine, SSH into the VPS yourself using the key I just generated, and run `hermes setup` interactively — live, with me watching from this chat. I'll tell you exactly what to type, stay on standby for every prompt, and troubleshoot anything that looks weird. You end up knowing how to reach your agent and how to reconfigure it later.
>
> *Trade-off:* you do a bit more typing. That's it.
>
> **Which would you like?**

### 5A — White-glove path

If the user picks Path A:

1. Create the Hermes config file over SSH: `touch ~/.hermes/.env && chmod 600 ~/.hermes/.env`.
2. Run `hermes setup` over SSH in a way that lets you capture prompts (pty or step-by-step subcommands: `hermes model`, `hermes tools`, `hermes config set`).
3. When Hermes asks something you can't answer from the repo `.env`, stop and ask the user in chat via `AskUserQuestion`. Pipe their answer back into the wizard.
4. Jump down to the **Provider-specific notes** section below — those steps (token setup, model selection) apply to both paths.
5. Skip the "Path B" block entirely.

### 5B — Hands-on path (recommended)

If the user picks Path B:

**Step 1 — Tell the user exactly how to open a terminal.**

Adapt to their OS (ask if unknown):

> - **Windows:** press `Win + R`, type `wt` (Windows Terminal) or `powershell`, hit Enter. Or open PowerShell from the Start menu. A second terminal window will pop up — leave your Claude Code / this chat open in the first one.
> - **macOS:** press `Cmd + Space`, type `Terminal`, hit Enter. Or open Terminal from Applications → Utilities.
> - **Linux:** `Ctrl + Alt + T` usually works, or open whatever terminal your distro ships with (GNOME Terminal, Konsole, etc.).

**Step 2 — Tell the user exactly what to paste.**

Look up `VPS_HOST` and `SSH_PRIVATE_KEY_PATH` from the repo `.env` and substitute them into the template below. Give the user a **copy-pasteable** command with real values, not placeholders:

```bash
ssh -i "<SSH_PRIVATE_KEY_PATH>" hermes@<VPS_HOST>
```

Example (do not show the user the placeholder version — show the filled-in one):

```bash
ssh -i "C:/Users/Waz/Github/hermes-agent-bootstrap/keys/hermes_bootstrap_ed25519" hermes@192.0.2.55
```

Tell them: *"Paste that into your new terminal and press Enter. The first time it connects, it'll ask `Are you sure you want to continue connecting? (yes/no/[fingerprint])` — type `yes` and press Enter. You should land at a prompt that looks like `hermes@hermes-agent:~$`. Tell me when you see it."*

### 🛑 CHECKPOINT 5a — user is SSHed in

Ask: *"Did you land at a `hermes@...` prompt? If you got any error (permission denied, connection refused, host key warning), paste it here and I'll help."* Wait. Common issues to be ready for:

- **`Permission denied (publickey)`** → wrong key path or the public key wasn't uploaded to the VPS. You (Claude Code) can fix it server-side via your own SSH session.
- **`Connection refused` / timeout** → VPS hasn't finished booting, or SSH port mismatch. Check `VPS_SSH_PORT`.
- **Windows quoting** → backslashes in the path can confuse PowerShell; forward slashes are safer.

Do not move on until the user confirms they're in.

**Step 3 — Walk them through `hermes setup`.**

Give them, one at a time, the commands to run. Start with:

```bash
hermes setup
```

Tell them: *"This kicks off the interactive wizard. It'll ask you a series of questions — timezone, default model provider, tools to enable, messaging gateways. Read each question out loud to me here and I'll tell you what to pick based on what's in your `.env`. We'll go one prompt at a time."*

For each wizard prompt, the user tells you what they see, and you reply with the exact answer to type. Pull defaults from:

- **LLM provider & model** → from `.env` (USE_CLAUDE_OAUTH / ANTHROPIC_API_KEY / USE_CODEX_OAUTH / OPENAI_API_KEY). See **Provider-specific notes** below for each path.
- **Timezone** → ask the user directly if not known; default to UTC if they're unsure.
- **Tools** → recommend enabling the defaults; note that they can tweak later with `hermes tools`.
- **WhatsApp** → tell them to say yes if prompted, but the detailed QR-scan dance happens in Chapter 6. If the wizard offers to set it up now, either path works — if the wizard handles it, you'll skip most of Chapter 6.

### 🛑 CHECKPOINT 5b — wizard is running

Every 2–3 prompts, check in: *"Still with me? Any prompt you're not sure about?"* This is the chapter most likely to confuse a new user, so over-communicate rather than under.

### 🛑 CHECKPOINT 5c — wizard finished

Ask the user to run `hermes "Say hello and tell me which model you are."` in their terminal and paste the reply here. Confirm it's a sensible response from the expected model. **Do not proceed until the smoke test passes.**

---

### Provider-specific notes (applies to both paths)

Hermes reads its runtime config from `~/.hermes/.env` on the VPS. Depending on the path and provider, secrets land there either via the wizard, via `claude setup-token` / `codex login`, or by you appending them over SSH.

> **Note on two `.env` files:** the `.env` in *this repo* is your bootstrap input (the user's keys and choices). The `.env` at `~/.hermes/.env` on the *VPS* is what Hermes actually reads at runtime. You (Claude Code) are the bridge — read from the repo file, make sure the right values end up on the VPS.

> **Who actually runs these commands:**
> - **Path A (white-glove):** you (Claude Code) run them over your own SSH session.
> - **Path B (hands-on):** the user runs them in their own terminal; you dictate each command and watch for their output. Paste each command as a fenced code block in chat so they can copy-paste it verbatim.

### 5a. Claude Max OAuth (recommended)

This path avoids Anthropic billing entirely — it rides on the user's existing Claude subscription.

1. Run on the VPS:
   ```bash
   npm i -g @anthropic-ai/claude-code   # if not already present from installer
   claude setup-token
   ```
2. `claude setup-token` prints a URL. ✋ **HANDOFF:** the user opens the URL in a browser where they're logged into Claude, approves, and pastes the resulting code back into the SSH session. Wait. In Path B, the user is already in the right terminal — remind them to paste the code there, not into this chat.
3. The command writes a token to `~/.claude/` and prints it. Append to `~/.hermes/.env`:
   ```
   CLAUDE_CODE_OAUTH_TOKEN=<token>
   ```
4. Tell Hermes to use Anthropic/Claude as the provider:
   ```bash
   hermes model anthropic:claude-sonnet-4-5
   ```
   (Adjust model name per what the user wants — default to the latest Sonnet for cost/speed balance.)

### 5b. Anthropic API key

```
ANTHROPIC_API_KEY=<from .env>
```
Then `hermes model anthropic:claude-sonnet-4-5`.

### 5c. ChatGPT Codex OAuth

1. Install Codex CLI on the VPS: `npm i -g @openai/codex`
2. `codex login` → ✋ **HANDOFF**, same as Claude flow.
3. Point Hermes at the local Codex-proxied endpoint (Codex exposes an OpenAI-compatible endpoint when logged in). Set:
   ```
   OPENAI_API_KEY=codex
   OPENAI_BASE_URL=http://127.0.0.1:<codex port>
   ```
   If the user doesn't know the port, check `codex --help` or ask them.
4. `hermes model openai:gpt-5` (or whichever model Codex exposes).

### 5d. OpenAI API key

```
OPENAI_API_KEY=<from .env>
```
Then `hermes model openai:gpt-4.1` (or latest).

The smoke test for this chapter is **CHECKPOINT 5c** above (Path B) or the equivalent `hermes "..."` call you run yourself (Path A). Do not proceed to Chapter 6 until the model has replied with a sensible message.

---

## Chapter 6 — WhatsApp gateway (Burner → WhatsApp Business → Hermes)

This is the most hands-on chapter because of the QR scan. Take it slow.

> **Which terminal?** In **Path B**, the user should keep using the SSH terminal they opened in Chapter 5 — the gateway needs a live terminal to print the QR code into, and they've already got one. In **Path A**, you (Claude Code) run these commands over your own SSH session, but when the QR code appears, you must either render it into chat as ASCII for the user to scan, or paste the QR URL the wizard prints. The user's phone still has to do the scanning — there is no way around the physical step.

1. In `~/.hermes/.env` append:
   ```
   WHATSAPP_ENABLED=true
   WHATSAPP_MODE=bot
   WHATSAPP_ALLOWED_USERS=<from .env>
   WHATSAPP_ALLOW_ALL_USERS=true
   ```
2. Run the gateway setup wizard (non-interactive flags if available, else walk the user through it):
   ```bash
   hermes gateway setup whatsapp
   ```
3. Start the gateway in a foreground session the user can watch:
   ```bash
   hermes gateway start
   ```
4. The first WhatsApp start prints a **QR code in the terminal** (or a URL to a QR image). ✋ **HANDOFF:**
   - Tell the user to open **WhatsApp Business** on the Burner phone.
   - Settings → **Linked devices** → *Link a device*.
   - Point the phone camera at the terminal QR.
   - Wait for "linked successfully".
5. Once linked, the gateway should log `whatsapp: ready`. If it doesn't, check `hermes gateway logs whatsapp`.

### 🛑 CHECKPOINT 6 — the big one

From the user's **personal** WhatsApp, send a message to the **Burner number**: *"Hey, are you there?"*

The agent should reply within a few seconds. **If the user confirms a reply arrived → success, move on. If not → debug together** (common causes: `WHATSAPP_ALLOWED_USERS` missing the sender's number, gateway crashed, LLM provider error — check `hermes logs`).

---

## Chapter 7 — Make it survive reboots

1. Create a `systemd` unit for Hermes (and one for the gateway, or a single combined one if `hermes gateway start` runs both). Minimal template:
   ```ini
   [Unit]
   Description=Hermes Agent
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=simple
   User=hermes
   WorkingDirectory=/home/hermes
   ExecStart=/home/hermes/.local/bin/hermes gateway start
   Restart=on-failure
   RestartSec=5
   EnvironmentFile=/home/hermes/.hermes/.env

   [Install]
   WantedBy=multi-user.target
   ```
2. `sudo systemctl daemon-reload && sudo systemctl enable --now hermes`
3. `systemctl status hermes` → should be `active (running)`.

### 🛑 CHECKPOINT 7

Ask the user to **reboot the VPS** (`sudo reboot`). Wait ~60 seconds. SSH back in, check `systemctl status hermes`, and then ask the user to send another WhatsApp message. **Confirm reply.**

---

## 🎉 Core setup complete

At this point the user has a self-hosted Hermes agent reachable via WhatsApp that will survive reboots. Tell them explicitly that the mandatory runbook is done, and offer the optional chapters:

> **You're live.** Want to keep going? I can walk you through any of:
> 1. Skills expansion (installing community skills + authoring your own)
> 2. Google Workspace integration (Gmail, Calendar, Drive)
> 3. GitHub integration
> 4. Cron jobs (natural-language scheduled tasks)
> 5. Security hardening (UFW, fail2ban, SSH lockdown, secrets hygiene)
> 6. Crowd favorites (voice mode, personas, multi-gateway, self-chat)
>
> Or we can stop here and you can text your agent.

Only proceed with the chapter(s) the user picks.

---

## Optional Chapter A — Skills expansion

1. List already-enabled skills: `hermes tools` on the VPS.
2. Walk through the [agentskills.io](https://agentskills.io) registry. Ask what domains they care about (research, coding, finance, home automation, writing?).
3. Install a skill: `hermes skills install <name>`.
4. **Author a custom skill** — Hermes supports autonomous skill creation, but for bespoke ones:
   - Skills live under `~/.hermes/skills/<skill-name>/`
   - Each has a `skill.md` with frontmatter (`name`, `description`, `triggers`) and a body of instructions
   - Optionally a `scripts/` folder for executable helpers
5. Teach the user the *procedural memory* trick: after completing a tricky task, say "remember how to do that as a skill" and Hermes will autogenerate one.

### 🛑 Checkpoint

Install one skill together and trigger it via WhatsApp. Confirm it runs.

---

## Optional Chapter B — Google Workspace

1. ✋ **HANDOFF:** user creates a Google Cloud project at [console.cloud.google.com](https://console.cloud.google.com/), enables **Gmail API**, **Calendar API**, **Drive API**, and creates an **OAuth 2.0 Client ID** (Desktop app type). They download the client JSON.
2. User sets `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in their local `.env`, re-runs this chapter.
3. You scp the client JSON onto the VPS into `~/.hermes/google/credentials.json`.
4. Install the Google skill/tool pack (check `hermes skills search google`).
5. Run `hermes auth google` — it prints a URL. ✋ **HANDOFF:** user opens it in a browser, consents (will see an "unverified app" warning — that's expected for a personal OAuth client), and pastes the code back.
6. Test via WhatsApp: *"What's on my calendar tomorrow?"*

### 🛑 Checkpoint

Confirm Hermes can read a real Gmail thread and list real calendar events. Do NOT grant write scopes yet unless the user explicitly asks — read-only first.

---

## Optional Chapter C — GitHub

1. Ask: **PAT** (quick, less secure) or **GitHub App** (more work, cleaner audit trail, scoped to specific repos)?
2. **PAT path:**
   - ✋ User creates a fine-grained PAT at github.com/settings/tokens → *Fine-grained tokens*.
   - Scopes: Contents (r/w), Issues (r/w), Pull requests (r/w) on selected repos.
   - Append `GITHUB_TOKEN=<pat>` to `~/.hermes/.env`.
3. **App path:**
   - Walk through creating a GitHub App, installing it on the user's account/org, downloading the private key, scp'ing it to the VPS, and setting `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY_PATH`.
4. Install the GitHub skill: `hermes skills install github` (or equivalent).

### 🛑 Checkpoint

Via WhatsApp: *"List my open PRs on <repo>."*

---

## Optional Chapter D — Cron jobs

Hermes has a built-in natural-language scheduler. No need to touch system `cron`.

1. Teach the user the command shape:
   ```
   hermes cron add "every weekday at 8am: send me a summary of overnight GitHub notifications and today's calendar"
   ```
2. Or via WhatsApp directly: *"Schedule a daily 8am briefing with my calendar and email."*
3. List jobs: `hermes cron list`. Remove: `hermes cron rm <id>`.
4. Suggest 2-3 starter schedules based on the integrations they installed above:
   - Daily AM briefing (calendar + email)
   - Weekly GitHub review (open PRs, stale issues)
   - Nightly `~/.hermes/` backup to the user's Drive

### 🛑 Checkpoint

Add one real cron, wait for it to fire (or manually trigger with `hermes cron run <id>`), confirm the WhatsApp message arrives.

---

## Optional Chapter E — Security hardening

Do these **in order** — each one can lock you out if mis-sequenced, so verify SSH still works after every step.

1. **UFW firewall**
   ```bash
   sudo ufw default deny incoming
   sudo ufw default allow outgoing
   sudo ufw allow 22/tcp
   sudo ufw --force enable
   ```
2. **fail2ban** — enable the `sshd` jail (default config is fine for most users).
3. **SSH lockdown** in `/etc/ssh/sshd_config.d/99-hermes.conf`:
   ```
   PasswordAuthentication no
   PermitRootLogin no
   KbdInteractiveAuthentication no
   ```
   `sudo systemctl reload ssh`. **Do not close the current SSH session until a new one confirms it works.**
4. **Move SSH off port 22** (optional but noisy-log reducer). If you do this: update UFW, update `VPS_SSH_PORT` in `.env`, update your local `~/.ssh/config`.
5. **Secrets hygiene:**
   - `chmod 600 ~/.hermes/.env`
   - Confirm no world-readable files under `~/.hermes/`
   - Rotate any credential ever pasted into a terminal that wasn't the VPS itself
6. **Audit logging:** install `auditd`, enable the default rules, teach the user `ausearch`.
7. **Backups:** a `cron` (system cron, not Hermes cron) that tars `~/.hermes/` nightly and ships it to S3 / Backblaze / Drive. Discuss tradeoffs.
8. **Optional but recommended:** put the VPS behind Tailscale and close port 22 from the public internet entirely — users can still SSH over the tailnet, and the WhatsApp gateway doesn't need inbound ports at all (it dials out).

### 🛑 Checkpoint

From a second terminal, open a fresh SSH session to confirm you haven't locked anyone out. Then run `sudo ufw status verbose` and `sudo fail2ban-client status sshd` and show the user.

---

## Optional Chapter F — Crowd favorites

Present these as a menu; implement whichever the user picks. Each is ~5–15 minutes.

- **Voice mode.** Set `VOICE_TOOLS_OPENAI_KEY` (STT via Whisper, TTS via OpenAI tts-1). Send a voice note on WhatsApp → Hermes transcribes → replies with text (or voice, if `hermes config set voice_replies true`).
- **Persistent persona.** `hermes persona create <name>` → walk the user through writing a short system-prompt describing tone, boundaries, expertise. Set as default.
- **Multi-gateway fan-out.** Add Telegram and/or Discord alongside WhatsApp: `hermes gateway setup telegram` / `discord`. The same agent serves all channels with shared memory.
- **Self-chat mode.** `WHATSAPP_MODE=self-chat` — instead of a Burner number, Hermes listens to messages the user sends to *themselves* on WhatsApp. Cheaper (no Burner) but conflates with personal chat. Offer this as an alternative to the Burner path.
- **OpenClaw migration.** If the user previously ran OpenClaw: `hermes claw migrate --dry-run` then `hermes claw migrate` to import personas, skills, memories, keys.
- **Procedural memory.** After any non-trivial multi-step task, prompt Hermes with "remember this as a skill" — it'll autogenerate a reusable skill from the session.
- **Model hot-swap.** Teach `hermes model <provider:model>` — users often want to flip between a cheap default and Opus for heavy tasks.

### 🛑 Checkpoint per feature

Each of these ends with a WhatsApp-side smoke test. Don't mark a feature done without the user confirming it from the phone.

---

## Wrapping up

When the user says they're done with optional chapters:

1. Summarise what's now installed and what env vars are in play (redacted).
2. Remind them where things live:
   - Config: `~/.hermes/.env`, `~/.hermes/skills/`, `~/.hermes/personas/`
   - Logs: `journalctl -u hermes -f`
   - Updating: `hermes update`
3. Remind them to **back up `~/.hermes/`** — that directory is their entire agent.
4. Point at the Hermes docs: [hermes-agent.nousresearch.com](https://hermes-agent.nousresearch.com/)

Done. Close the session.
