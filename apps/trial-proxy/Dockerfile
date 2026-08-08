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

# NOTE / TO VERIFY: LiteLLM also publishes a `litellm-database` image
# variant (ghcr.io/berriai/litellm-database) built specifically for the
# virtual-keys-on-Postgres use case, which reportedly runs its Prisma schema
# migration automatically on boot. We're using the plain `litellm` image
# here on the assumption that it too applies its schema against
# DATABASE_URL on first connect (this proxy is useless without virtual keys,
# so this assumption matters) — could not confirm from reachable docs at
# build time which image variant is actually required for that. Verify
# against the pinned version's docs/changelog before first deploy; switch
# the image name above to `litellm-database` if the plain image does not
# self-migrate.

WORKDIR /app

# Bake config in at build time — no secrets in this file (every value is an
# os.environ/* reference resolved from Railway service variables at
# container start), so it's safe to ship inside the image layer.
COPY config.yaml /app/config.yaml

# TO VERIFY: could not confirm the base image's own default ENTRYPOINT/CMD
# from reachable docs at build time (LiteLLM's own Railway deployment guide
# only says "set PORT=4000 as an env var", without showing what reads it).
# We override ENTRYPOINT explicitly below rather than relying on inherited
# CMD args, specifically so `$PORT` — which Railway assigns dynamically
# per-deploy, not always 4000 — is resolved via shell expansion at container
# *start* time, not baked in at image build time.
#
# LITELLM_NUM_WORKERS defaults to 1: this proxy fronts a metered, low-volume
# trial workload (a handful of tenants in Phase 0), not production LLM
# traffic at scale — bump via a Railway env var if/when that stops being
# true, rather than editing this file.
ENV PORT=4000
EXPOSE 4000

ENTRYPOINT ["/bin/sh", "-c", "litellm --config /app/config.yaml --port \"${PORT:-4000}\" --num_workers \"${LITELLM_NUM_WORKERS:-1}\""]
