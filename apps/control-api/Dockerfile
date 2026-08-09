# control-api image. Railway builds this on merge (implementation-plan Task 0.1).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first: pyproject alone (plus a stub package dir, which `pip
# install .` needs to resolve the build) so that editing source does not
# reinstall FastAPI/SQLModel/psycopg on every build.
COPY pyproject.toml ./
RUN mkdir -p src/control_api \
    && touch src/control_api/__init__.py \
    && pip install --no-cache-dir .

# Real source last -- this is the layer that actually changes between deploys.
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# Run unprivileged. Nothing here writes to the filesystem at runtime (state lives
# in Postgres), so an unwritable /app is a feature.
RUN useradd --create-home --uid 10001 squire
USER squire

# Railway injects $PORT; the default matches the tenant/ingress convention.
ENV PORT=8080
EXPOSE 8080

# `sh -c` is needed for ${PORT} expansion, and `exec` is what makes uvicorn PID 1
# rather than a child of the shell -- without it Railway's SIGTERM on redeploy or
# serverless-sleep would go to `sh`, which does not forward it, and the container
# would be SIGKILLed after the grace period instead of shutting down cleanly.
CMD ["sh", "-c", "exec uvicorn control_api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
