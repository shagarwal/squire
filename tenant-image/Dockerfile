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
#   7. squire-heartbeat    — counts-only fleet telemetry to control-api
#   8. squire-backup       — nightly restic snapshot of the volume to B2
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

# Human-readable provenance for the tag the digest above corresponds to.
# Metadata only — changing it does not change what is built, which is exactly why
# the FROM uses the digest.
#
# There is deliberately NO HERMES_DIGEST ARG. It would be a second copy of the
# literal on the FROM line, free to drift out of step with it, and a provenance
# label that disagrees with what was actually built is worse than no label at
# all. The base digest is already authoritative in one place (the FROM) and is
# recoverable from the built image with:
#
#     docker image inspect --format '{{index .RootFS.Layers 0}}' <image>
#     docker history --no-trunc <image>       # shows the base reference
#
# The CI job asserts instead on the property that matters — that the FROM line
# is digest-pinned at all.
ARG HERMES_VERSION=v2026.8.3

# Pinned exactly, matching the digest-pin discipline above: a floating range is
# the same mutable-input problem in a different coat. Every version below was
# resolved against the real index on 2026-08-08; bump deliberately.
ARG HINDSIGHT_VERSION=0.9.0
# `+cpu` is the local version the PyTorch CPU index uses for its Linux wheels.
ARG TORCH_VERSION=2.9.1+cpu
ARG SUPERVISOR_VERSION=4.3.0
ARG CRYPTOGRAPHY_VERSION=49.0.0

# restic — nightly volume backups (bin/squire-backup.sh, Task 0.6). Pinned by
# version AND by the SHA-256 of the exact release artifact, taken from restic's
# published SHA256SUMS for v0.19.1 on 2026-08-08. The checksum is what makes the
# pin real: a version tag alone still trusts whatever bytes the CDN serves today,
# and this binary is handed the tenant's data and a derived key.
#
# Installed from the release binary rather than Debian's `restic` package because
# the distro version floats with the base image's apt state — the same mutable-input
# problem the FROM digest exists to solve.
#
# To bump: pick the new version, take its line from
# https://github.com/restic/restic/releases/download/v<NEW>/SHA256SUMS, update both
# ARGs together. A mismatch fails the build loudly.
ARG RESTIC_VERSION=0.19.1
ARG RESTIC_SHA256=f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c

# Everything below runs as root (build time). Runtime drops to the `hermes`
# user (uid 10000) via upstream's main-wrapper.sh — see the CMD note at the
# bottom. This matters: pg0's initdb refuses to run as root, and so does
# hindsight's own embedded-mode guard.
USER root

LABEL org.opencontainers.image.title="squire-hermes-tenant" \
      org.opencontainers.image.description="Squire per-tenant agent runtime: hermes-agent + Hindsight + embedded Postgres" \
      ai.squire.hermes.version="${HERMES_VERSION}"

# ===========================================================================
# LAYER ORDER
# ---------------------------------------------------------------------------
# The two venv layers come FIRST, before any COPY of our own source. They are
# by far the most expensive steps (torch alone is hundreds of MB) and they
# change only when a pinned version above changes. The patch overlay and the
# Squire scripts change on almost every commit. Building in this order means an
# ordinary edit to a shell script or a patch re-runs seconds of work instead of
# re-resolving and re-downloading the whole ML stack.
#
# ---------------------------------------------------------------------------
# EVERY uv COMMAND BELOW PASSES --no-config. THIS IS LOAD-BEARING.
# ---------------------------------------------------------------------------
# The upstream image sets WORKDIR /opt/hermes, and /opt/hermes/pyproject.toml
# contains:
#
#     [tool.uv]
#     exclude-newer = "14 days"
#
# uv discovers configuration from the working directory upward, so OUR install
# layers silently inherited upstream's rolling 14-day cutoff and refused to
# consider any wheel uploaded more recently than ~2 weeks ago. That is what
# broke the first CI build (tag tenant-image-v0.1.0).
#
# It is worth being precise about the failure, because the obvious reading of
# the error is wrong. uv reported:
#
#     Because only the following versions of torch are available: ...
#     hint: Wheels are available for `torch` (v2.9.1) on the following
#     platform: `macosx_11_0_arm64`
#
# which looks like a pin/platform problem. It is not. Reproduced locally with
# uv against a python 3.13 venv and a pyproject.toml carrying the same cutoff:
#
#     bare ==2.9.1,  cutoff ACTIVE   -> No solution found (macosx hint)  <- CI
#     bare ==2.9.1,  cutoff DISABLED -> resolves fine
#     ==2.9.1+cpu,   cutoff ACTIVE   -> No solution found                <- !!
#     ==2.9.1+cpu,   cutoff DISABLED -> resolves fine
#
# The cutoff filters the Linux `+cpu` wheels, leaving only the older macOS ones,
# and uv's hint then blames the platform. So fixing ONLY the pin would have
# failed the build again in exactly the same place.
#
# The cutoff would also have blocked the very next layer regardless of torch:
# hindsight-api-slim==0.9.0 (uploaded 2026-08-07) and its pg0-embedded==0.15.1
# dependency (2026-07-31) are both newer than the cutoff. Verified locally:
# without --no-config the resolve fails outright; with it, 190 packages resolve.
#
# --no-config is the documented way to ignore discovered pyproject.toml/uv.toml
# ("Avoid discovering configuration files", env: UV_NO_CONFIG). We do NOT want
# upstream's supply-chain-freshness policy governing our two independent venvs —
# we pin exact versions ourselves, which is the same guarantee reached honestly.
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. Squire control venv (small): supervisord + cryptography
# ---------------------------------------------------------------------------
# Deliberately NOT installed into /opt/hermes/.venv. That venv is upstream's
# and is treated as sealed (the image sets HERMES_DISABLE_LAZY_INSTALLS=1 for
# exactly this reason); adding our own resolver pressure there is how you end
# up debugging a transitive pin at 2am on a tenant.
#
#   cryptography — AES-256-GCM for the DEK-encrypted secret files
#   supervisor   — process supervision inside the container
RUN uv venv --no-config --python 3.13 /opt/squire/venv \
 && uv pip install --no-config --python /opt/squire/venv/bin/python --no-cache \
        "supervisor==${SUPERVISOR_VERSION}" "cryptography==${CRYPTOGRAPHY_VERSION}"

# ---------------------------------------------------------------------------
# 2. Hindsight venv (large): memory daemon + embedded Postgres
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
# hindsight-embed is deliberately NOT installed. It is the daemon *manager* for
# local_embedded mode, and the hermes plugin only imports it on that branch
# (plugins/memory/hindsight/__init__.py: `if self._mode == "local_embedded":
# import hindsight_embed.daemon_embed_manager`). We run local_external against a
# daemon supervised by supervisord, so the gateway reaches Hindsight over HTTP
# via hindsight-client (already baked into the upstream image) and never imports
# the manager at all.
#
# torch is installed FIRST from PyTorch's CPU-only index. The default PyPI torch
# wheel drags in the whole nvidia-cu* stack (multiple GB) a tenant can never
# use; installing the CPU build first means the resolve below sees torch as
# already satisfied.
#
# TORCH_VERSION carries the `+cpu` local version, which is how the Linux wheels
# on that index are actually named (torch-2.9.1+cpu-cp313-cp313-
# manylinux_2_28_x86_64.whl; manylinux_2_28 is satisfied by trixie's glibc
# 2.41). PEP 440 means a bare `==2.9.1` would ALSO match `2.9.1+cpu`, so this is
# not what broke CI — see the --no-config note above. It is pinned explicitly
# anyway because it makes the local version visible at the pin site, and because
# `2.9.1+cpu` does not exist on PyPI: if this resolve ever falls back to the
# default index it now fails loudly instead of quietly installing the
# multi-gigabyte CUDA build into a container that has no GPU.
RUN uv venv --no-config --python 3.13 /opt/squire/hindsight-venv \
 && uv pip install --no-config --python /opt/squire/hindsight-venv/bin/python --no-cache \
        --index-url https://download.pytorch.org/whl/cpu "torch==${TORCH_VERSION}" \
 && uv pip install --no-config --python /opt/squire/hindsight-venv/bin/python --no-cache \
        "hindsight-api-slim[embedded-db,local-ml]==${HINDSIGHT_VERSION}" \
 && /opt/squire/hindsight-venv/bin/hindsight-api --help > /dev/null

# ---------------------------------------------------------------------------
# 2b. restic (nightly volume backups)
# ---------------------------------------------------------------------------
# Downloaded and verified with the Squire venv's own Python rather than
# curl+sha256sum: neither curl nor bzip2 is a declared dependency of the upstream
# image, and `apt-get install` would drag apt state (and its own floating versions)
# into an image whose whole discipline is pinned inputs. The interpreter is already
# here and its stdlib does all three jobs — fetch, verify, decompress.
#
# The verification is a hard gate: a mismatched digest fails the BUILD. This binary
# is handed the tenant's entire volume and a DEK-derived key, so "probably the right
# bytes" is not a standard it gets to meet.
RUN RESTIC_VERSION="${RESTIC_VERSION}" RESTIC_SHA256="${RESTIC_SHA256}" \
    /opt/squire/venv/bin/python <<'PY'
import bz2
import hashlib
import os
import urllib.request

version = os.environ["RESTIC_VERSION"]
expected = os.environ["RESTIC_SHA256"]
url = (
    "https://github.com/restic/restic/releases/download/"
    f"v{version}/restic_{version}_linux_amd64.bz2"
)
with urllib.request.urlopen(url, timeout=180) as response:
    blob = response.read()

# The checksum is over the published .bz2 artifact, exactly as restic's own
# SHA256SUMS lists it -- so this verifies the bytes that crossed the network,
# not the bytes after our own decompression.
digest = hashlib.sha256(blob).hexdigest()
if digest != expected:
    raise SystemExit(f"restic {version}: sha256 {digest} != expected {expected}")

path = "/usr/local/bin/restic"
with open(path, "wb") as fh:
    fh.write(bz2.decompress(blob))
os.chmod(path, 0o755)
print(f"restic {version} installed (sha256 verified)")
PY

# Separate layer so the binary being runnable is asserted independently of the
# download step -- and so a `restic` that cannot exec fails the build here rather
# than at 03:00 UTC on a tenant.
RUN /usr/local/bin/restic version

# ---------------------------------------------------------------------------
# 3. Patch overlay
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
#
# Copied without a blanket --chmod: markers.tsv and the .patch files are data
# and have no business being executable. Only the two scripts get the bit.
COPY patches/ /opt/squire/patches/
RUN chmod 0755 /opt/squire/patches/*.sh \
 && /opt/squire/patches/apply-patches.sh /opt/hermes \
 && /opt/squire/patches/verify-markers.sh /opt/hermes

# ---------------------------------------------------------------------------
# 4. Squire runtime files
# ---------------------------------------------------------------------------
COPY --chmod=0755 bin/ /opt/squire/bin/
COPY supervisord.conf /opt/squire/supervisord.conf
# The baked ~/.hermes template. Seeded onto the (empty) volume on first boot
# only — see bin/squire-entrypoint.sh. Never overwrites tenant-owned files.
#
# WHY home-template/hindsight/config.json CARRIES NO COMMENTARY
# -------------------------------------------------------------
# It used to hold a "_comment" key explaining the design. JSON has no comments,
# and an unknown key is at the mercy of whatever loader reads it — a strict
# schema would reject the file, and the plugin's failure mode for an unreadable
# config is to fall back to defaults, i.e. mode="cloud"/local_embedded. That is
# exactly the embedded-daemon-inside-the-gateway topology that
# docs/reference/hindsight-optimization-guide.md §1 exists to eliminate, and it
# would fail SILENTLY: memory would appear to work while the daemon ran in the
# gateway's own process, ready to take the gateway down with it on OOM.
#
# So the file is now strictly schema-clean and the rationale lives here:
#
#   mode=local_external + api_url=127.0.0.1:9177
#       Talk to the hindsight-api daemon supervised alongside the gateway
#       (supervisord.conf) instead of running it in-process. Guide §1/§5: an
#       embedded daemon that OOMs kills the gateway; a separate process can be
#       restarted by the healthcheck loop without dropping the conversation.
#   recall_types=observation
#       Upstream's own narrowed default — consolidated beliefs are denser per
#       token than the raw facts underneath them.
#   retain_every_n_turns=1, retain_async=true
#       Remember everything, but never block a reply on extraction.
#   bank_retain_mission
#       Steers extraction toward durable personal facts and away from small
#       talk. This is what makes the agent feel like it knows the user rather
#       than merely having a transcript.
#
# FIRST-LIVE-BOOT VERIFICATION (do this on the first real tenant — it cannot be
# checked without a running daemon, and the silent-fallback risk above is
# exactly why it must be checked at least once):
#
#   1. `curl -s 127.0.0.1:9177/health`            -> daemon is up and separate
#   2. `pgrep -af hindsight-api`                  -> its own PID, not the gateway's
#   3. `ls $HOME/.pg0/instances/hindsight/data`   -> cluster is on the volume
#   4. In the gateway logs, confirm NO line about starting an embedded daemon
#      and no "hindsight-embed" activity — that would mean local_embedded.
COPY home-template/ /opt/squire/home-template/

# Byte-compile our Python so a syntax error fails the BUILD, not a tenant boot.
RUN /opt/squire/venv/bin/python -m py_compile \
        /opt/squire/bin/squire_secrets.py \
        /opt/squire/bin/squire-webhook-shim.py \
        /opt/squire/bin/squire-hindsight-healthcheck.py \
        /opt/squire/bin/squire-heartbeat.py \
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
#
# HERMES_HOME is set alongside it as the single source of truth for the Squire
# scripts. Every one of them defaults to `${HERMES_HOME:-/opt/data}`, which meant
# the real value was a literal repeated across six files; setting it here makes
# those defaults a fallback for running the scripts outside a container rather
# than six independent chances to disagree.
#
# All three of HOME, HERMES_HOME and SQUIRE_VOLUME must be the same path, and
# must equal control-api's TENANT_VOLUME_MOUNT_PATH -- the volume Railway mounts.
# apps/control-api/tests/test_cross_service_contracts.py reads this file and
# fails if they drift; the container-contract job in tenant-image.yml pins them
# too. A mismatch is not a degradation: the entrypoint's durability gate refuses
# to boot, because pg0 would put the tenant's memory on ephemeral storage.
ENV HOME=/opt/data \
    HERMES_HOME=/opt/data

# --- Secrets ---------------------------------------------------------------
# Plaintext credentials live ONLY here, and this is a tmpfs. /dev/shm is a
# tmpfs in every OCI runtime with no extra capabilities required (mounting our
# own tmpfs would need CAP_SYS_ADMIN, which Railway does not grant). The
# encrypted-at-rest copies live under SQUIRE_STATE_DIR on the volume.
#
# SQUIRE_SEALED_FILE_NAMES was previously SQUIRE_SECRET_FILES, which tripped
# BuildKit's SecretsUsedInArgOrEnv lint on the first CI build. That was a false
# positive — the value is a list of FILENAMES ('.env auth.json'), never a
# credential — but a standing warning on every build trains people to ignore
# warnings, and the new name is also just more accurate about what it holds.
#
# SQUIRE_SECRETS_TMPFS is deliberately NOT renamed. Its value is a directory
# path, it did not trigger the lint on the first build, and renaming it would
# ripple through five scripts and three test suites for a cosmetic change. If a
# future BuildKit tightens the heuristic and flags it, rename it then — the
# change is mechanical.
ENV SQUIRE_SECRETS_TMPFS=/dev/shm/squire \
    SQUIRE_SEALED_FILE_NAMES=".env auth.json"

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
# WORKER SLOTS — read this before "fixing" it to match the guide
# --------------------------------------------------------------
# The guide (§3) says to set HINDSIGHT_API_WORKER_CONSOLIDATION_MAX_SLOTS=1 and
# describes it as reducing "2 concurrent consolidation slots" to 1. That reading
# is wrong, and it was wrong in 0.8.x too. Verified against hindsight-api-slim
# 0.9.0's config.py:
#
#   * config.py:726-758 `_parse_worker_slot_reservations` — docstring:
#     "``HINDSIGHT_API_WORKER_<TYPE>_MAX_SLOTS`` is a deprecated alias for
#      ``..._RESERVED_SLOTS`` — despite its name it always set the reservation
#      floor, never a ceiling."
#   * config.py:707-712 — a reservation is a guaranteed MINIMUM within the
#     global WORKER_MAX_SLOTS pool; spare capacity stays shared, so "a
#     reservation does NOT cap the type — it may overflow the shared pool."
#   * config.py:740-744 — setting BOTH names raises ValueError at startup.
#   * config.py:716-723 — the consolidation default reservation is 2.
#
# So: the knob that actually bounds concurrency (and therefore peak RSS) is
# WORKER_MAX_SLOTS, which we set to 3 (upstream default 10). Lowering the
# consolidation reservation from 2 to 1 is a *scheduling fairness* change — it
# frees a slot for retain/graph work instead of guaranteeing it to
# consolidation — and does not by itself reduce memory. We keep it because a
# 3-slot pool with 2 slots pre-committed to consolidation starves everything
# else, but the memory win the guide claims comes from WORKER_MAX_SLOTS alone.
#
# We use the modern RESERVED_SLOTS name. Using the deprecated MAX_SLOTS alias
# would be identical in effect but logs a deprecation warning on every boot, and
# setting both would be a hard startup failure.
#
# RETAIN_MAX_CONCURRENT (default 4, config.py:1272) is the other real memory
# lever: it bounds concurrent retain DB phases (HNSW reads + writes). Halved.
ENV HINDSIGHT_API_HOST=127.0.0.1 \
    HINDSIGHT_API_PORT=9177 \
    HINDSIGHT_API_DATABASE_URL=pg0://hindsight:5432 \
    HINDSIGHT_API_RERANKER_PROVIDER=rrf \
    HINDSIGHT_API_WORKER_MAX_SLOTS=3 \
    HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS=1 \
    HINDSIGHT_API_RETAIN_MAX_CONCURRENT=2 \
    HINDSIGHT_API_DB_POOL_MIN_SIZE=3 \
    HINDSIGHT_API_DB_POOL_MAX_SIZE=15

# --- Log-level policy: the gateway and Hindsight both see message content ----
# Everything these two processes write to stdout is shipped to Railway's log
# aggregator, which is outside the tenant's isolation boundary and readable by
# anyone with project access. Debug logging in either would put fragments of the
# user's conversation — and Hindsight's extracted memories, which are *more*
# concentrated personal data than the raw messages — into that aggregator.
#
# So the levels are pinned conservatively here rather than left to a default
# that a future upstream bump could change:
#
#   HINDSIGHT_API_LOG_LEVEL=warning  quieter than upstream's `info` default
#                                    (config.py:1105), which logs per-operation
#                                    detail on every retained turn
#   HINDSIGHT_API_ACCESS_LOG=false   already the default (config.py:1108); pinned
#                                    so it cannot drift, since request paths can
#                                    carry bank and document identifiers
#
# The GATEWAY side is NOT set here, because upstream exposes no log-level
# environment variable — it is a config key, `logging.level` (default "INFO",
# hermes_cli/config_defaults.py:2379). It is therefore pinned to WARNING in
# home-template/config.yaml instead. Setting a HERMES_LOG_LEVEL env var here
# would look like policy while doing precisely nothing.
#
# Mitigating context, not an excuse: upstream runs a RedactingFormatter
# (agent/redact.py:1000) over its own log files, so known credential shapes are
# scrubbed. That covers secrets, not conversation content, and it is applied to
# hermes' file handlers rather than to everything on stdout.
#
# Raise these ONLY for a deliberate, time-boxed debugging session on a tenant
# whose owner has consented — and drop them back afterwards. tests/
# test_boot_path.sh asserts that no plaintext secret reaches captured output.
ENV HINDSIGHT_API_LOG_LEVEL=warning \
    HINDSIGHT_API_ACCESS_LOG=false

# --- Fleet heartbeat + backups (Task 0.6) ----------------------------------
# The heartbeat emitter posts counts-only telemetry to control-api every
# SQUIRE_HEARTBEAT_INTERVAL seconds. 300s (5 min) against control-api's 900s
# staleness window means a tenant has to miss three beats before /fleet calls it
# stale — one missed beat is a redeploy, three is a problem.
#
# !! VERIFY ON THE FIRST LIVE DEPLOY — THIS INTERVAL MAY COST US GATE G1 !!
# ---------------------------------------------------------------------------
# Railway's serverless sleep is half of the unit-economics bet
# (docs/implementation-plan.md §2, Gate G1): tenants are supposed to scale to zero
# between conversations, and the whole $5/month price depends on them actually
# doing so. A container that makes an outbound HTTPS request every 5 minutes may
# never look idle to Railway — in which case this monitoring feature would
# quietly destroy the economics it was added to measure. That is a genuinely
# expensive way to be wrong, and it cannot be checked without the platform.
#
# What to check on the first real tenant, before onboarding anyone:
#   1. Does Railway's sleep trigger on INBOUND request idleness only, or on any
#      process activity / outbound traffic? (Docs say inbound; unverified.)
#   2. Leave one provisioned tenant untouched for longer than the sleep window
#      and confirm from Railway usage that it actually slept.
#   3. If it did not: raise SQUIRE_HEARTBEAT_INTERVAL well above the sleep window
#      (a sleeping tenant reports nothing anyway, and /fleet already treats
#      silence as "stale"), or gate beats on there having been activity since the
#      last one. Do NOT keep 300s and hope.
#
# control-api's HEARTBEAT_STALE_SECONDS must be raised alongside it (it is sized
# at 3x this interval).
#
# Supplied per tenant by control-api, NOT baked here:
#   CONTROL_API_URL, INTERNAL_API_TOKEN, TENANT_ID  (provisioning contract)
#   SQUIRE_IMAGE_REF   set by POST /internal/tenants/{id}/redeploy; this is what
#                      the upgrade drill checks to prove a canary converged
#
# Backups are OFF until a B2 account exists: with RESTIC_REPOSITORY and the
# B2_*/AWS_* credentials unset, squire-backup.sh logs one line and exits 0. The
# schedule knobs are baked so the behaviour is visible in the repo rather than
# hidden in a Railway variable nobody remembers setting.
ENV SQUIRE_HEARTBEAT_INTERVAL=300 \
    SQUIRE_BACKUP_HOUR_UTC=3 \
    SQUIRE_BACKUP_JITTER_SECONDS=3600 \
    SQUIRE_BACKUP_KEEP_DAILY=7 \
    SQUIRE_BACKUP_KEEP_WEEKLY=4

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
