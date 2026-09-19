# EMUNEL Worker — node agent (runs on each infrastructure node)
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY worker/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/emunel_worker ./emunel_worker
COPY core/emunel_core ./emunel_core

RUN useradd --uid 10002 --shell /usr/sbin/nologin emunelw \
    && mkdir -p /var/lib/emunel/instances && chown -R emunelw:emunelw /var/lib/emunel
# NOTE: for the Docker driver the worker needs access to the host Docker
# socket. Preferred setup: run with the docker socket group mounted
# (--group-add <docker-gid> -v /var/run/docker.sock:/var/run/docker.sock:ro).
# The socket is NEVER mounted into EMUNEL Core containers.
USER 10002

EXPOSE 9100
HEALTHCHECK --interval=20s --timeout=4s --start-period=8s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9100/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "emunel_worker"]
