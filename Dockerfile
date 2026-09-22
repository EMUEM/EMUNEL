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

# Build stamp — /health, /version and the panel sidebar show it so a stale
# deployment is recognizable at a glance.
RUN date -u "+%Y-%m-%dT%H:%MZ" > /app/.emunel_build

# ── OPTIONAL: bake a pinned Xray into the image (REALITY runtime) ──────────
# Railway passes service variables to the Docker build as ARGs. Set BOTH:
#   XRAY_VERSION  = an Xray-core release tag (e.g. v25.8.6)
#   XRAY_SHA256   = the 64-hex digest of that release's Xray-<arch>.zip
# What happens: the release zip is downloaded ONCE at BUILD time, the digest
# is verified (a mismatch FAILS the build), and the binary is installed to
# /opt/xray/xray (root-owned, 0755, digest file alongside). The REALITY
# engine detects it on boot and starts the VLESS+REALITY listener — no
# runtime download ever happens. Leave either variable unset and this stage
# is a strict no-op: the image is byte-for-byte the same as without it.
ARG XRAY_VERSION=""
ARG XRAY_SHA256=""
RUN if [ -n "$XRAY_VERSION" ]; then \
      set -eux; \
      [ ${#XRAY_SHA256} -eq 64 ] || { echo "FATAL: XRAY_SHA256 must be 64 hex chars (from the release page)" >&2; exit 1; }; \
      case "$(uname -m)" in \
        x86_64)  XRAY_ARCH=linux-64 ;; \
        aarch64) XRAY_ARCH=linux-arm64-v8a ;; \
        *) echo "FATAL: unsupported architecture for the Xray bake" >&2; exit 1 ;; \
      esac; \
      python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-${XRAY_ARCH}.zip', '/tmp/xray.zip')"; \
      echo "${XRAY_SHA256}  /tmp/xray.zip" | sha256sum -c - || { echo "FATAL: Xray release digest mismatch — refusing to bake" >&2; exit 1; }; \
      mkdir -p /opt/xray /tmp/xray-unpack; \
      python -c "import zipfile; zipfile.ZipFile('/tmp/xray.zip').extractall('/tmp/xray-unpack')"; \
      mv /tmp/xray-unpack/xray /opt/xray/xray; \
      chmod 0755 /opt/xray/xray; \
      echo "$XRAY_SHA256" > /opt/xray/xray.sha256; \
      rm -rf /tmp/xray.zip /tmp/xray-unpack; \
    fi

# ── runtime user + writable data dir (persistent volume mount point) ─────────
# MALLOC_ARENA_MAX=2: stops glibc from multiplying 64MB arenas per thread —
# a real RSS reducer for the threaded Python runtime (STABILIZATION stage 3).
ENV MALLOC_ARENA_MAX=2
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
