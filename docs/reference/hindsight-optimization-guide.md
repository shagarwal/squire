# Hindsight Memory Optimization Guide for 4GB VPS

## Problem

Hindsight v0.8.x in `local_embedded` mode runs local embedding + reranking ML models, an embedded PostgreSQL instance, and background worker tasks (retain, consolidation) — all inside the Hermes gateway's systemd cgroup. Combined with the agent (Claude Code CLI processes) and WhatsApp bridge, total memory regularly exceeds 3.5GB, triggering the Linux OOM killer on the entire gateway. Symptoms:

- "Session automatically reset (previous session was stopped or interrupted)"
- Gateway restart loops
- `/login` commands interrupted
- Hindsight recall/retain/reflect all returning errors

## Optimizations Applied

### 1. Separate Hindsight into its own systemd service

**Impact: Prevents Hindsight crashes from killing the gateway**

By default, Hindsight runs embedded inside the gateway process (`local_embedded` mode). When it OOMs, the kernel kills the entire gateway.

**Fix:** Run Hindsight as a standalone daemon with its own memory limits.

1. Change Hermes config (`~/.hermes/hindsight/config.json`):
```json
{
  "mode": "local_external",
  "api_url": "http://127.0.0.1:9177",
  "bank_id": "hermes",
  "recall_budget": "mid"
}
```

2. Create `~/.config/systemd/user/hindsight-daemon.service`:
```ini
[Unit]
Description=Hindsight Memory Daemon (standalone)
After=pg0-hindsight.service
Requires=pg0-hindsight.service
Before=hermes-gateway.service

[Service]
Type=simple
ExecStart=/path/to/hindsight-api --host 127.0.0.1 --port 9177 --log-level info

# LLM provider config (adjust to your setup)
Environment="HINDSIGHT_API_LLM_PROVIDER=claude-code"
Environment="HINDSIGHT_API_LLM_MODEL=claude-haiku-4-5"
Environment="HINDSIGHT_API_DATABASE_URL=postgresql://hindsight:hindsight@127.0.0.1:5432/hindsight"

# Memory limits (adjust based on your VPS)
MemoryHigh=1500M
MemoryMax=1800M
MemorySwapMax=0
OOMPolicy=stop

Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Find the hindsight-api binary path with:
```bash
find ~/.cache/uv -name "hindsight-api" -type f 2>/dev/null
```

3. Also reduce the gateway's memory cap since it no longer carries Hindsight:
```ini
# ~/.config/systemd/user/hermes-gateway.service.d/memory.conf
[Service]
MemoryHigh=1500M
MemoryMax=2G
MemorySwapMax=1G
OOMPolicy=continue
```

### 2. Disable the local reranker

**Impact: ~350MB RAM saved**

Hindsight loads two ML models by default:
- Embeddings: `BAAI/bge-small-en-v1.5` (~500-600MB with runtime)
- Reranker: `cross-encoder/ms-marco-MiniLM-L-6-v2` (~350-500MB with runtime)

The reranker re-orders recall results by reading actual text against the query. For conversational memory, the quality difference is negligible — embedding similarity ranking is good enough.

**Fix:** Add to the hindsight-daemon service:
```ini
Environment="HINDSIGHT_API_RERANKER_PROVIDER=rrf"
```

`rrf` (Reciprocal Rank Fusion) is a built-in passthrough that skips neural reranking entirely. No model loaded, no memory used.

### 3. Reduce worker concurrency

**Impact: Prevents memory spikes and DB connection gridlock**

Defaults are tuned for beefy servers:
- 10 concurrent worker task slots
- 2 concurrent consolidation slots
- 100 max DB pool connections

On a 4GB VPS, this causes: multiple Claude Code CLI processes spawning simultaneously, DB pool with 98+ waiters gridlocking, and memory spiking past limits.

**Fix:** Add to the hindsight-daemon service:
```ini
Environment="HINDSIGHT_API_WORKER_MAX_SLOTS=3"
Environment="HINDSIGHT_API_WORKER_CONSOLIDATION_MAX_SLOTS=1"
Environment="HINDSIGHT_API_DB_POOL_MAX_SIZE=15"
Environment="HINDSIGHT_API_DB_POOL_MIN_SIZE=3"
```

### 4. Health check with stuck task cleanup

**Impact: Auto-recovers from wedged daemon + prevents OOM restart loops**

Hindsight can wedge (process alive but unresponsive) under memory pressure. A simple systemd restart isn't enough because:
- The stuck process holds port 9177, blocking the new one
- Pending/processing tasks in the DB get recovered on restart, causing the same OOM

**Fix:** Create `~/.local/bin/hindsight-healthcheck.sh`:
```bash
#!/bin/bash
HEALTH_URL="http://127.0.0.1:9177/health"
SERVICE="hindsight-daemon.service"
LOG_TAG="hindsight-healthcheck"
PG_BIN=/path/to/pg0/bin  # find with: find ~/.pg0 -name "psql" -type f

if ! systemctl --user is-active --quiet "$SERVICE"; then
    exit 0
fi

if timeout 10 curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
    exit 0
fi

logger -t "$LOG_TAG" "Health check failed. Killing stuck processes."
pkill -9 -f hindsight-api 2>/dev/null
sleep 1

# Clear stuck tasks so the daemon doesn't recover into the same OOM loop
CLEARED=$(PGPASSWORD=hindsight $PG_BIN/psql -h 127.0.0.1 -p 5432 -U hindsight -d hindsight -t -c "
UPDATE async_operations
SET status = 'failed', error_message = 'auto-cleared by healthcheck after OOM/wedge'
WHERE status IN ('pending', 'processing');" 2>/dev/null | tr -d ' ')
logger -t "$LOG_TAG" "Cleared $CLEARED stuck tasks from queue."

systemctl --user restart "$SERVICE"
logger -t "$LOG_TAG" "Restart issued."
```

Run it on a 2-minute timer:
```ini
# ~/.config/systemd/user/hindsight-healthcheck.timer
[Unit]
Description=Run Hindsight health check every 2 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
AccuracySec=15s

[Install]
WantedBy=timers.target
```

```ini
# ~/.config/systemd/user/hindsight-healthcheck.service
[Unit]
Description=Hindsight daemon health check

[Service]
Type=oneshot
ExecStart=/home/hermes/.local/bin/hindsight-healthcheck.sh
```

Enable with:
```bash
systemctl --user daemon-reload
systemctl --user enable --now hindsight-healthcheck.timer
```

Check logs with: `journalctl -t hindsight-healthcheck`

## Results

| Metric | Before (defaults) | After (all optimizations) |
|---|---|---|
| Hindsight RSS | 1,100-1,500 MB | **~720 MB** |
| Cgroup headroom | 0 MB (constantly OOM) | **~780 MB** |
| System available | 1.5 GB | **2.4 GB** |
| Swap usage | 300-600 MB | **~0 MB** |
| Worker slots | 10 (gridlock) | 3 (sustainable) |
| DB pool waiters | 98 (deadlock) | ~14 (draining) |
| Gateway isolation | None (OOM kills everything) | Full (Hindsight crashes alone) |
| Auto-recovery | None | Health check + task cleanup every 2min |

### 5. Disable swap for Hindsight

**Impact: Prevents swap thrashing that wedges the daemon**

When Hindsight exceeds RAM, Linux pushes memory pages to swap (disk). SSDs are ~100x slower than RAM, so the daemon becomes effectively frozen — alive but unable to respond. This causes consolidation tasks to pile up in the queue because each one takes forever to process in swap-backed memory.

**Fix:** Set `MemorySwapMax=0` in the hindsight-daemon service (already shown above). This ensures Hindsight gets OOM-killed and cleanly restarted rather than crawling through swap indefinitely.

Also consider lowering system swappiness for the whole VPS:
```bash
# Check current value (default is 60)
cat /proc/sys/vm/swappiness
# Lower it (persistent across reboots)
echo "vm.swappiness=10" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

## Additional options if still unstable

- **Switch embeddings to cloud:** Set `HINDSIGHT_API_EMBEDDINGS_PROVIDER=gemini` or `cohere` with an API key. Saves another ~400MB but sends message text to a third-party API for vectorization.
- **Upgrade VPS to 8GB:** Keeps everything local and private with room to spare.
- **Switch memory provider entirely:** Honcho or Mem0 are cloud-hosted alternatives with free tiers and zero local memory footprint.
