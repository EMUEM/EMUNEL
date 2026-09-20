# EMUNEL — unified platform image (Console + embedded Worker + Core runtime).
# Railway-compatible: single service, honors $PORT, no VOLUME instruction
# (attach a volume at /data from your platform dashboard instead).
FROM python:3.12-slim

WORKDIR /app

# ── dependencies (single source of truth at repo root) ──────────────────────
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && python -c "import sys, importlib.util; critical = ['fastapi','uvicorn','asyncpg','httpx','websockets','cryptography','psutil','aiosqlite','qrcode']; missing = [m for m in critical if importlib.util.find_spec(m) is None]; sys.exit('FATAL missing packages: ' + ', '.join(missing)) if missing else print('dependency check OK — %d critical packages importable' % len(critical))"

# ── application code ─────────────────────────────────────────────────────────
COPY main.py engine_manager.py ./
COPY core/ ./core/
COPY console/ ./console/
COPY worker/ ./worker/
COPY engines/ ./engines/

# ── runtime user + writable data dir (persistent volume mount point) ─────────
RUN useradd --uid 65532 --shell /usr/sbin/nologin emunel \
 && mkdir -p /data /data/instances \
 && chown -R emunel:emunel /data /app
USER emunel

# Zero-config: EMUNEL_SECRET_KEY, EMUNEL_WORKER_TOKEN, the embedded SQLite
# database and the admin/admin account are auto-provisioned on first boot.
# Optional platform variables: PORT (injected), DATABASE_URL (PostgreSQL),
# EMUNEL_GITHUB_CLIENT_ID / EMUNEL_GITHUB_CLIENT_SECRET (OAuth login).
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=5 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('PORT','8080'), timeout=4)" || exit 1

CMD ["python", "main.py"]
