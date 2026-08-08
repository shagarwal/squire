# syntax=docker/dockerfile:1
#
# Squire tenant image ("hermes-tenant") — Task 0.2, Phase 0.
# =========================================================
#
# ONE container per tenant. It bundles, under supervisord:
#
#   1. hermes-gateway      — the agent itself (upstream hermes-agent, pinned)
#   2. hindsight-api       — long-term memory daemon (127.0.0.1:9177)
#   3. pg0 (PostgreSQL 18) — embedded Postgres, started *by* hindsight-api
#   4. squire-webhook      — tiny shim that owns $PORT and forwards
#                            POST /webhook/telegram to the gateway's Telegram
#                            adapter (see bin/squire-webhook-shim.py)
#   5. squire-secrets-sync — re-encrypts credential files from tmpfs back onto
#                            the volume whenever the agent rewrites them
#   6. squire-hindsight-healthcheck — the wedge/OOM auto-recovery loop from
#                            docs/reference/hindsight-optimization-guide.md §4
#
# WHY WE BUILD *FROM* THE PUBLISHED UPSTREAM IMAGE
# ------------------------------------------------
# Upstream ships a production image (docker.io/nousresearch/hermes-agent:<tag>)
# built from the same Dockerfile we would otherwise copy. Building hermes from
# source takes 15-45 min (Playwright, npm workspaces, a from-source SQLite, a
# full `uv sync`), which would make the CI image build the slowest thing in the
# repo. Because upstream installs itself EDITABLE (`uv pip install -e .` over
# the source tree at /opt/hermes), our patch overlay can still be applied to
# real .py files at /opt/hermes and takes effect immediately — same result as a
# source build, minutes instead of an hour.
#
# PINNING / BUMPING
# -----------------
# The base is pinned BY DIGEST, not by tag. A tag is a mutable pointer: upstream
# re-tagging v2026.8.3 would silently change what every tenant runs, and the
# patch overlay below would be applying to a tree we never verified. The digest
# makes the base immutable and the build reproducible.
#
# Digest below == docker.io/nousresearch/hermes-agent:v2026.8.3 (linux/amd64),
# resolved 2026-08-07. That is the current upstream release, and the same tag
# docs/reference/hermes-update-plan-v2026.8.3.md rehearsed and executed against,
# so the patch overlay and marker greps are known-good on it.
#
# NOTE: pinning a per-arch digest makes this image amd64-only. That is what
# Railway runs, and a tenant image is not something anyone builds on a laptop.
# For multi-arch, swap to the multi-arch manifest digest.
#
# To bump: `docker buildx imagetools inspect nousresearch/hermes-agent:<newtag>`,
# update BOTH the digest below and HERMES_VERSION (which is metadata only), then
# re-run the patch overlay and re-verify the markers. Never bump without the
# canary drill in the update plan (§7 verification gates).
#
FROM docker.io/nousresearch/hermes-agent@sha256:c0cab4e3711bcb27a312be1b3776254fc06fd50d5f7a6b8017915fc7171cb39e

# Human-readable provenance for the digest above. Metadata only — changing it
# does not change what is built, which is exactly why the FROM uses the digest.
ARG HERMES_VERSION=v2026.8.3
ARG HERMES_DIGEST=sha256:c0cab4e3711bcb27a312be1b3776254fc06fd50d5f7a6b8017915fc7171cb39e

# The memory server. Pinned; bump deliberately.
ARG HINDSIGHT_VERSION=0.9.0

# Everything below runs as root (build time). Runtime drops to the `hermes`
# user (uid 10000) via upstream's main-wrapper.sh — see the CMD note at the
# bottom. This matters: pg0's initdb refuses to run as root, and so does
# hindsight's own embedded-mode guard.
USER root

LABEL org.opencontainers.image.title="squire-hermes-tenant" \
      org.opencontainers.image.description="Squire per-tenant agent runtime: hermes-agent + Hindsight + embedded Postgres" \
      ai.squire.hermes.version="${HERMES_VERSION}" \
      ai.squire.hermes.digest="${HERMES_DIGEST}"

# ---------------------------------------------------------------------------
# 1. Patch overlay
# ---------------------------------------------------------------------------
# The three-tier fork discipline from docs/reference/git-strategy.md, moved into
# the image build: upstream is pinned and untouched; our deltas live as ordered
# .patch files; a marker grep proves both our patches AND the upstream fixes we
# depend on are present in the built tree.
#
# apply-patches.sh is a no-op when patches/ contains no .patch files, so the
# image builds cleanly before the founder's 001/002/003 patches are copied in
# from ~/.hermes/patches/. verify-markers.sh, however, ALWAYS runs and always
# checks the upstream markers, so a silent upstream regression (e.g. the
# _to_oauth_wire_name billing fix disappearing) fails the build rather than
# reaching a tenant.
COPY --chmod=0755 patches/ /opt/squire/patches/
RUN /opt/squire/patches/apply-patches.sh /opt/hermes \
 && /opt/squire/patches/verify-markers.sh /opt/hermes

# ---------------------------------------------------------------------------
# 2. Squire control venv (small): supervisord + cryptography
# ---------------------------------------------------------------------------
# Deliberately NOT installed into /opt/hermes/.venv. That venv is upstream's
# and is treated as sealed (the image sets HERMES_DISABLE_LAZY_INSTALLS=1 for
# exactly this reason); adding our own resolver pressure there is how you end
# up debugging a transitive pin at 2am on a tenant.
#
#   cryptography — AES-256-GCM for the DEK-encrypted secret files
#   supervisor   — process supervision inside the container
RUN uv venv --python 3.13 /opt/squire/venv \
 && uv pip install --python /opt/squire/venv/bin/python --no-cache \
        "supervisor==4.3.0" "cryptography>=45,<50"

# ---------------------------------------------------------------------------
# 3. Hindsight venv (large): memory daemon + embedded Postgres
# ---------------------------------------------------------------------------
# Also isolated from /opt/hermes/.venv — hindsight-api-slim pins sqlalchemy,
# litellm, anthropic, openai and tornado, all of which hermes also depends on.
# Two venvs, zero shared resolution, no cross-breakage. The gateway talks to
# the daemon over HTTP (local_external mode), never by import, so isolation
# costs us nothing.
#
# WHY hindsight-api-slim[embedded-db,local-ml] AND NOT hindsight-all
# ------------------------------------------------------------------
# `hindsight-all` pulls `hindsight-api-slim[all]`, and [all] = [embedded-db] +
# [local-ml] + [local-onnx]. We do not use the ONNX embeddings backend (the
# default `local` backend is sentence-transformers/torch), so [local-onnx]'s
# onnxruntime is pure image weight on every tenant — and image size feeds
# straight into the G1 economics gate (docs/implementation-plan.md §2).
#
# Requesting the two extras we actually need is a strict subset of [all], so it
# cannot remove anything [all] provided that we depend on. numpy and
# huggingface-hub are dropped from the *direct* set but come back transitively
# via sentence-transformers.
#
# Honest caveat: [local-ml] still carries mlx, mlx-lm and flashrank, which this
# container never uses. There is no finer-grained extra upstream, so trimming
# those would mean hand-listing dependencies and owning that list across
# upgrades — not worth the rebase debt for the size. If the image turns out to
# be the binding constraint at G1, that is the next lever to pull.
#
# torch is installed FIRST from PyTorch's CPU-only index. The default PyPI torch
# wheel drags in the whole nvidia-cu* stack (multiple GB) a tenant can never
# use; installing the CPU build first means the resolve below sees torch as
# already satisfied.
RUN uv venv --python 3.13 /opt/squire/hindsight-venv \
 && uv pip install --python /opt/squire/hindsight-venv/bin/python --no-cache \
        --index-url https://download.pytorch.org/whl/cpu "torch>=2.6.0" \
 && uv pip install --python /opt/squire/hindsight-venv/bin/python --no-cache \
        "hindsight-api-slim[embedded-db,local-ml]==${HINDSIGHT_VERSION}" \
        "hindsight-embed>=0.1.0" \
 && /opt/squire/hindsight-venv/bin/hindsight-api --help > /dev/null

# ---------------------------------------------------------------------------
# 4. Squire runtime files
# ---------------------------------------------------------------------------
COPY --chmod=0755 bin/ /opt/squire/bin/
COPY supervisord.conf /opt/squire/supervisord.conf
# The baked ~/.hermes template. Seeded onto the (empty) volume on first boot
# only — see bin/squire-entrypoint.sh. Never overwrites tenant-owned files.
COPY home-template/ /opt/squire/home-template/

# Byte-compile our Python so a syntax error fails the BUILD, not a tenant boot.
RUN /opt/squire/venv/bin/python -m py_compile \
        /opt/squire/bin/squire_secrets.py \
        /opt/squire/bin/squire-webhook-shim.py \
        /opt/squire/bin/squire-hindsight-healthcheck.py \
 && chmod -R a+rX /opt/squire \
 && mkdir -p /opt/squire/run \
 && chown hermes:hermes /opt/squire/run

# ---------------------------------------------------------------------------
# 5. Runtime environment
# ---------------------------------------------------------------------------

# --- Container contract (control-api + ingress depend on these) -------------
# PORT                  HTTP port the container listens on (webhook shim).
# /webhook/telegram     Path that receives raw Telegram update JSON.
# /opt/data             The single mounted volume. It is also HERMES_HOME, so
#                       upstream's own stage2 bootstrap (uid remap, dir seed,
#                       targeted chown) manages it unchanged.
ENV PORT=8080 \
    SQUIRE_WEBHOOK_PATH=/webhook/telegram \
    SQUIRE_VOLUME=/opt/data \
    SQUIRE_STATE_DIR=/opt/data/.squire \
    SQUIRE_HOME_TEMPLATE=/opt/squire/home-template

# --- HOME is load-bearing, not cosmetic ------------------------------------
# pg0 resolves its data root from $HOME: the embedded PostgreSQL cluster lands
# in $HOME/.pg0/instances/hindsight/data. If HOME is anything other than the
# mounted volume, the entire memory database sits on the container filesystem
# and every Railway redeploy silently wipes the tenant's long-term memory —
# the agent would forget the user, with no error anywhere.
#
# Upstream's main-wrapper.sh already exports HOME=/opt/data on the supervised
# path, but relying on that is relying on an implementation detail of a
# dependency we pin by digest and bump on a schedule. Setting it here makes it
# ours. squire-entrypoint.sh additionally ASSERTS it before starting anything,
# because a wrong value is silent and expensive.
ENV HOME=/opt/data

# --- Secrets ---------------------------------------------------------------
# Plaintext credentials live ONLY here, and this is a tmpfs. /dev/shm is a
# tmpfs in every OCI runtime with no extra capabilities required (mounting our
# own tmpfs would need CAP_SYS_ADMIN, which Railway does not grant). The
# encrypted-at-rest copies live under SQUIRE_STATE_DIR on the volume.
ENV SQUIRE_SECRETS_TMPFS=/dev/shm/squire \
    SQUIRE_SECRET_FILES=".env auth.json"

# --- Telegram adapter, webhook mode ----------------------------------------
# Supplied per tenant by control-api at provision time (NOT baked here):
#
#   TELEGRAM_BOT_TOKEN       enables the platform (gateway/config.py keys off it)
#   TELEGRAM_WEBHOOK_URL     the PUBLIC ingress URL for this tenant's bot
#   TELEGRAM_WEBHOOK_SECRET  shared with Telegram, ingress and this container
#
# The adapter switches to webhook mode purely on TELEGRAM_WEBHOOK_URL being set,
# and derives its local listen path from that URL's path component. It then calls
# setWebhook itself. That is safe precisely because control-api owns both the URL
# and the secret: both writers register identical values, so the calls are
# idempotent last-writer-wins rather than a fight, and ingress's stored secret
# stays valid across tenant restarts. See squire-entrypoint.sh for the matching
# logic and for the standalone-dev fallback when the secret is not supplied.
#
# With TELEGRAM_WEBHOOK_URL absent the adapter falls back to long polling, which
# keeps `docker run` local testing working with no ingress at all.
#
# It binds 127.0.0.1 on purpose — the shim is the only thing that should own a
# public port, so the adapter's HTTP server is unreachable except through it.
ENV TELEGRAM_WEBHOOK_PORT=8443 \
    TELEGRAM_WEBHOOK_HOST=127.0.0.1

# --- Hindsight (docs/reference/hindsight-optimization-guide.md) -------------
# The guide's systemd-unit knobs, translated to this container. Cgroup limits
# from the guide are NOT set here: on Railway the whole service is one cgroup,
# so per-process MemoryMax has no equivalent. The healthcheck loop (guide §4)
# does carry over and is a supervisord program.
#
# DATABASE_URL: `pg0://<instance>:<port>` makes hindsight-api start its bundled
# PostgreSQL 18 on a FIXED port instead of auto-allocating one, which is what
# lets the healthcheck reach the DB to clear wedged tasks. pg0 keeps its data
# in $HOME/.pg0/instances/hindsight/data — and $HOME is /opt/data — so the
# memory database lives on the volume and survives redeploys.
ENV HINDSIGHT_API_HOST=127.0.0.1 \
    HINDSIGHT_API_PORT=9177 \
    HINDSIGHT_API_LOG_LEVEL=info \
    HINDSIGHT_API_DATABASE_URL=pg0://hindsight:5432 \
    HINDSIGHT_API_RERANKER_PROVIDER=rrf \
    HINDSIGHT_API_WORKER_MAX_SLOTS=3 \
    HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS=1 \
    HINDSIGHT_API_DB_POOL_MIN_SIZE=3 \
    HINDSIGHT_API_DB_POOL_MAX_SIZE=15

# --- Gateway supervision opt-out -------------------------------------------
# CRITICAL, and non-obvious: upstream's `hermes gateway run` detects that it is
# inside an s6-overlay container and *redirects itself* into s6 supervision
# (hermes_cli/gateway.py::_maybe_redirect_run_to_s6_supervision), leaving the
# invoking process as a heartbeat stub. Under supervisord that would mean
# supervisord watching a stub while the real gateway runs somewhere it cannot
# see or restart. HERMES_GATEWAY_NO_SUPERVISE=1 is the documented opt-out; the
# supervisord program ALSO passes --no-supervise as a belt-and-braces guard.
ENV HERMES_GATEWAY_NO_SUPERVISE=1

EXPOSE 8080

# Health endpoint served by the shim. Kept cheap — it must answer while the
# gateway is still booting so a platform health probe doesn't kill a cold start.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD /opt/squire/venv/bin/python -c \
        "import os,urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'), timeout=4).read()" \
        || exit 1

# ENTRYPOINT stays upstream's (/opt/hermes/docker/entrypoint-dispatch.sh). That
# gives us, for free: s6 as PID 1 (zombie reaping for MCP stdio children), the
# stage2 volume bootstrap (uid remap + targeted chown of the mounted volume),
# and the privilege drop to the `hermes` user with the hermes venv activated.
# Upstream's main-wrapper.sh passes an absolute executable straight through, so
# our entrypoint becomes the container's main program and its exit code is the
# container's exit code.
CMD ["/opt/squire/bin/squire-entrypoint.sh"]
