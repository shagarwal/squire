# control-api image. Railway builds this on merge (implementation-plan Task 0.1).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first so source edits don't reinstall the world.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Railway injects $PORT; default matches the tenant/ingress convention.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn control_api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
