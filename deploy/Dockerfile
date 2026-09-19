# EMUNEL — unified production image (Console API + InstanceManager + Core runtimes)
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: cryptography wheels fallback + curl for the healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Python deps (API + Core in one environment: InstanceManager spawns Core
# subprocesses with the same interpreter)
COPY requirements.txt core/requirements.txt ./
RUN cat requirements.txt core/requirements.txt | sort -u > all-requirements.txt \
    && pip install --no-cache-dir -r all-requirements.txt

# Application code
COPY api ./api
COPY core ./core
COPY dashboard ./dashboard
COPY main.py .

# Non-root user; instance state survives under /data
RUN useradd --system --uid 65532 --create-home emunel \
    && mkdir -p /data \
    && chown -R emunel:emunel /data /app
USER emunel

ENV EMUNEL_DATA_ROOT=/data \
    EMUNEL_CORE_HOME=/app/core \
    EMUNEL_HOST=0.0.0.0 \
    EMUNEL_PORT=8080

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=4s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["python", "main.py"]
