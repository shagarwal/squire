# Squire ingress v0 -- thin Telegram webhook router.
#
# Small on purpose: this service should be cheap to run always-on (it's the
# one thing on the hot path of *every* tenant's *every* message, so it can't
# itself be a sleeping/scale-to-zero service). Slim base image, no build
# toolchain kept in the final layer, non-root runtime user.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy only what's needed to resolve + install dependencies first, so
# dependency layers cache independently of application code changes.
COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

# Non-root runtime user -- defense in depth. This service holds the
# internal API token and per-tenant webhook secrets in memory (never on
# disk), so there's no volume/secret file to worry about ownership of, but
# running as root in a container is never necessary here.
RUN useradd --create-home --uid 10001 ingress
USER ingress

# Railway injects $PORT at deploy time; default lets `docker run` work
# locally without extra flags.
ENV PORT=8000
EXPOSE 8000

# --no-access-log: uvicorn's default access-log middleware would print a
# line like `<client> - "POST /telegram/12345 HTTP/1.1" 200` per request.
# The path only ever contains bot_id (no body, no headers), so this isn't
# actually a privacy leak -- but we disable it anyway so that
# ingress.logging.log_event() stays the *only* code path that emits logs in
# this service. One auditable emitter, not two.
CMD ["sh", "-c", "uvicorn ingress.main:app --host 0.0.0.0 --port ${PORT} --no-access-log"]
