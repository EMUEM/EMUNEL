# EMUNEL — unified production image (Console API + InstanceManager + Core runtimes)
#
# This file lives at the REPO ROOT on purpose: Railway (and most PaaS builders)
# auto-detect a root Dockerfile and prefer it over Nixpacks, which makes the
# build deterministic. Works identically for plain Docker and docker-compose:
#
#   docker build -t emunel .
#   docker run -p 8080:8080 -v emunel-data:/data emunel
#
# Zero-config: with no env vars set, strong secrets are generated on first
# boot and persisted under /data (attach a volume to keep them), and the
# initial admin password is printed once in the logs.
#
# LESSON LEARNED (the 15:32/16:04 Railway crash loops): an earlier version
# did `COPY requirements.txt core/requirements.txt ./` — both files share
# the same basename, so one silently OVERWROTE the other and pip only ever
# saw core's 5 packages (fastapi present, sqlalchemy missing → ModuleNotFound
# crash loop). This Dockerfile now installs from exactly ONE requirements
# file (the root one — a strict superset of core's) and verifies every
# critical import AT BUILD TIME so a missing dependency can never reach
# the runtime again.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# All Python dependencies ship manylinux wheels for cp312 (cryptography,
# asyncpg, bcrypt, psutil, pydantic-core) — no toolchain needed, which keeps
# the image small and the build light on memory-constrained builders.
# requirements.txt (root) is the single source of truth: API + Core deps.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# BUILD-TIME verification: if any critical dependency is missing, fail the
# build here with a clear message instead of crash-looping at runtime.
RUN python -c "import sys, importlib.util; critical = ['fastapi','uvicorn','websockets','cryptography','psutil','httpx','sqlalchemy','asyncpg','aiosqlite','pydantic','pydantic_settings','jose','bcrypt','multipart']; missing = [m for m in critical if importlib.util.find_spec(m) is None]; sys.exit('FATAL missing packages: ' + ', '.join(missing)) if missing else print('dependency check OK — %d critical packages importable' % len(critical))"

# Application code
COPY api ./api
COPY core ./core
COPY dashboard ./dashboard
COPY main.py ./

# Non-root user; instance state and generated secrets survive under /data
RUN useradd --system --uid 65532 --create-home emunel \
    && mkdir -p /data \
    && chown -R emunel:emunel /data /app
USER emunel

ENV EMUNEL_DATA_ROOT=/data \
    EMUNEL_CORE_HOME=/app/core \
    EMUNEL_HOST=0.0.0.0 \
    EMUNEL_CORE_BIND_HOST=0.0.0.0

# EMUNEL_PORT is deliberately NOT set here:
#   * Railway injects PORT at runtime — main.py honors it automatically.
#   * Elsewhere the app defaults to 8000 (override with EMUNEL_PORT/-p).
#
# NOTE: no VOLUME instruction — Railway's builder REJECTS Dockerfile VOLUME
# (attach a Railway volume at /data instead; docker-compose declares the
# bind mount itself, and plain `docker run` users pass -v emunel-data:/data).
EXPOSE 8000 8080

HEALTHCHECK --interval=15s --timeout=4s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:' + (os.getenv('EMUNEL_PORT') or os.getenv('PORT') or '8000') + '/health', timeout=3)"

CMD ["python", "main.py"]
