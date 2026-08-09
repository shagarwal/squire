# apps/trial-proxy/Dockerfile
#
# Squire's trial-LLM metering proxy (Task 0.5, docs/implementation-plan.md).
# Thin wrapper around the official LiteLLM proxy image: bakes in
# config.yaml (model list + budget/privacy settings, see that file for the
# full rationale) and starts the proxy listening on Railway's
# dynamically-assigned $PORT.
#
# This image has NO tenant-specific state or secrets baked in. Per-tenant
# virtual keys are created/revoked at runtime by control-api (a separate
# service/task) calling this proxy's admin API with LITELLM_MASTER_KEY — see
# config.yaml's header comment for the full division of responsibility.
#
# Required Railway service env vars (none are defaults/placeholders — all
# resolved via os.environ/* in config.yaml, so the proxy will fail closed,
# not silently misbehave, if any are missing):
#   ANTHROPIC_API_KEY  - our Anthropic org key that funds all trial usage.
#   LITELLM_MASTER_KEY - admin-API auth for control-api's key management
#                        calls. Must start with "sk-" (LiteLLM convention).
#   DATABASE_URL       - Postgres connection string (Railway Postgres plugin
#                        attached to this service). Required for virtual
#                        keys / budgets to work at all — see config.yaml.
#   PORT               - injected by Railway at deploy time; NOT fixed, do
#                        not hardcode a value anywhere that matters.

# Pinned to a specific stable release so a plain `docker build` / Railway
# rebuild doesn't silently pick up upstream changes. Bump this ARG
# deliberately (and re-check the config-settings/API notes in config.yaml
# still hold) when we want to move.
#
# Checked https://github.com/berriai/litellm/pkgs/container/litellm on
# 2026-08-07: latest stable tag was v1.94.2 (plain `vX.Y.Z` tags are current
# convention; the older `main-vX.Y.Z-stable` rolling tag is being phased out
# per LiteLLM's "cleaner release versions" blog post). TO VERIFY: re-check
# this is still a real, current tag before first deploy — pin dates fall out
# of date fast on a fast-moving upstream.
ARG LITELLM_VERSION=v1.94.2
FROM ghcr.io/berriai/litellm:${LITELLM_VERSION}

# Confirmed (not just assumed) via the actual v1.94.2 source, pulled with
# `gh api repos/BerriAI/litellm/contents/<path>?ref=v1.94.2` — i.e. the real
# pinned-tag source, not docs paraphrase — since an earlier pass of this
# file got two things wrong by inferring from docs alone. Both corrections
# below matter enough to cite exactly, so a future version bump can recheck
# them against the new tag instead of re-deriving from scratch:
#
# 1. Signal handling: the base image's own ENTRYPOINT is
#    `["docker/prod_entrypoint.sh"]` (docker/prod_entrypoint.sh, v1.94.2),
#    whose only job is `exec litellm "$@"` (`exec ddtrace-run litellm "$@"`
#    if USE_DDTRACE=true) — i.e. it already `exec`s so `litellm` itself
#    becomes PID 1 and receives signals directly. Our own ENTRYPOINT below
#    does the same (no `sh -c` wrapper) so a Railway redeploy sends SIGTERM
#    straight to litellm and it can drain, instead of a shell swallowing the
#    signal and litellm getting SIGKILLed once the stop-timeout expires.
#
# 2. Virtual-keys DB schema: the plain `litellm` image (this one, not the
#    `litellm-database` variant) DOES self-apply the Prisma schema on every
#    normal `litellm --config ...` boot -- no separate migration step or
#    image variant needed. This isn't documented anywhere reachable, but is
#    directly in the source: `litellm/proxy/proxy_cli.py`'s `run_server()`
#    (the function both `docker/prod_entrypoint.sh`'s plain `litellm` CLI
#    AND the separate `docker/entrypoint.sh` migration-job script route
#    through) runs a block gated only on `DATABASE_URL` being set --
#    `if os.getenv("DATABASE_URL", None) is not None or ...:` -- which,
#    when `should_update_prisma_schema()` returns true (the default; see
#    `litellm/proxy/db/prisma_client.py`, controlled by config's
#    general_settings.disable_prisma_schema_update /
#    DISABLE_SCHEMA_UPDATE env var, both left at their enabled default in
#    config.yaml), calls `PrismaManager.setup_database()` (idempotent by
#    design -- v2 resolver + retries, built specifically to be safe on every
#    boot / across rolling-deploy replica races, per that function's own
#    docstring) BEFORE the `--skip_server_startup` check. `docker/
#    entrypoint.sh` (which calls `litellm/proxy/prisma_migration.py`, which
#    itself just calls this same `run_server(["--skip_server_startup"])`)
#    is a separate *migration-only job* entrypoint for init-container-style
#    deploys, not the only path that migrates -- normal server boot already
#    does it. So: no custom migration script here, on purpose, and adding
#    one that reran `prisma migrate deploy`/`prisma db push` a second time
#    on top of litellm's own would risk two concurrent migration attempts
#    racing on the same DB rather than adding any real safety.
#    Re-verify this block still exists near-identically in proxy_cli.py
#    before bumping LITELLM_VERSION -- it's exactly the kind of internal
#    behavior that could change without a docs update.

WORKDIR /app

# Bake config in at build time — no secrets in this file (every value is an
# os.environ/* reference resolved from Railway service variables at
# container start), so it's safe to ship inside the image layer.
COPY config.yaml /app/config.yaml

# PORT / NUM_WORKERS: litellm's own CLI (`litellm/proxy/proxy_cli.py`)
# declares `--port` with `envvar="PORT"` (default 4000) and `--num_workers`
# with `envvar="NUM_WORKERS"` (default 1, DEFAULT_NUM_WORKERS_LITELLM_PROXY)
# — these are litellm-native env vars read by its own click CLI, not
# something we invented or need to pass as flags. ENV defaults below are
# just so a plain `docker run` (no Railway) still boots sensibly; Railway
# overrides PORT itself at deploy time. NUM_WORKERS=1 matches litellm's own
# default already, set explicitly here (rather than left implicit) since
# this proxy fronts a metered, low-volume trial workload (a handful of
# tenants in Phase 0) — bump via a Railway env var if that stops being true.
ENV PORT=4000
ENV NUM_WORKERS=1
EXPOSE 4000

# No Docker HEALTHCHECK instruction: Railway performs its own service-level
# health checks (configured on the Railway service itself, not in this
# Dockerfile) and its restart/redeploy behavior is driven by those, not by
# `docker inspect` health status.
#
# Runs as root (USER root, inherited from the base image — v1.94.2's own
# Dockerfile sets this explicitly too, so we're not deviating from upstream
# default). LiteLLM ships a `docker/Dockerfile.non_root` build target
# upstream as the hardened alternative; worth switching to
# `ghcr.io/berriai/litellm:${LITELLM_VERSION}-non_root` (or building that
# Dockerfile ourselves — check what upstream actually publishes under that
# tag before assuming it exists) if/when this proxy's threat model calls for
# it. Not done here to keep this task's scope to what was asked.
#
# Exec-form, no shell wrapper: litellm becomes PID 1 directly and receives
# SIGTERM from Railway's redeploy/stop directly, so it can drain in-flight
# requests instead of being SIGKILLed. (See point 1 in the version-pin
# comment above for how this matches the base image's own entrypoint
# behavior.)
# --host "::" (NOT the 0.0.0.0 default): Railway's private DNS returns an
# IPv6 address for <service>.railway.internal, and a process bound to
# 0.0.0.0 is IPv4-only, so every private-network client gets ECONNREFUSED
# on IPv6 first. httpx/anyio callers silently recover by retrying IPv4
# (which is why this was invisible until a raw-socket probe on 2026-08-09),
# but it costs a failed connection per request and breaks clients without
# fallback. "::" accepts both families via dual-stack.
ENTRYPOINT ["litellm", "--config", "/app/config.yaml", "--host", "::"]
