# Deploying EMUNEL on Railway

EMUNEL deploys as **one service** from the repo root. The Dockerfile is
auto-detected; no start command or variables are required.

## Quick deploy

1. **Fork** this repository.
2. In Railway: **New Project → Deploy from GitHub repo** → pick your fork.
   Railway detects the root `Dockerfile` and builds it (builder `DOCKERFILE`
   is also pinned in `railway.json`).
3. **Generate a domain** (Settings → Networking → Public domain). Railway
   injects `PORT` automatically — EMUNEL binds it.
4. Open the domain → sign in **admin / admin** → change the password in
   **Admin → System**.
5. **Create Instance → Deploy** → open the instance → **Config** tab →
   copy the `vless://` link or the subscription URL into your client.

## Persistence (required for instances to survive redeploys)

Attach a volume:

- **Settings → Volumes → New Volume** → mount path **`/data`**

Everything the platform persists lives under `/data` (or `.emunel-data/`
next to the app when `/data` is absent): the SQLite database, the session
secret, and every instance's state (links, quotas, traffic counters).

> Without a volume the panel still boots and works, but data resets on
> each deploy — the instance endpoint page will tell users to copy fresh
> configs after a redeploy.

## Auto-provisioned variables (zero config)

Nothing is required. For reference, these are resolved automatically:

| Variable | Behavior when unset |
|---|---|
| `PORT` | Railway injects it; EMUNEL binds `0.0.0.0:$PORT` |
| `EMUNEL_DATABASE_URL` / `DATABASE_URL` / `POSTGRES_*` / `PG*` | embedded SQLite under `/data` |
| `EMUNEL_SECRET_KEY` | generated, persisted 0600 under `/data` |
| `EMUNEL_WORKER_TOKEN` | generated per boot (console + embedded worker share one process tree) |
| `EMUNEL_PUBLIC_URL` | derived from request headers |
| admin account | seeded `admin` / `admin` on first boot (logged once) |

Optional: `EMUNEL_GITHUB_CLIENT_ID` + `EMUNEL_GITHUB_CLIENT_SECRET`
(GitHub OAuth login; callback `<public-url>/auth/callback`),
`EMUNEL_TELEGRAM_CHANNEL` (sidebar link), `EMUNEL_COOKIE_SECURE=1`.

## How configs stay reachable (single public port)

Railway exposes one HTTP port per service. EMUNEL routes **all** proxy
traffic through the console domain under a private endpoint token:

```
vless://<uuid>@<your-domain>:443?...&path=%2Fi%2F<endpoint-token>%2Fws%2F<uuid>
```

Client → Railway edge → Console gateway (`/i/<token>/...`) → embedded
Worker → Core. HTTP is streamed (xHTTP stream-up uploads work) and
WebSocket is relayed frame-by-frame. No per-port exposure is needed.

## Never-crash guarantees

- **Build time**: the Dockerfile verifies every critical package imports
  before the image is built (a missing dependency fails the build loudly,
  not a runtime crash-loop).
- **Boot**: no variable is required; SQLite + secrets + admin account
  auto-provision; `main.py` honors the injected `PORT`.
- **Runtime**: instance Cores run with `RLIMIT_AS`/`RLIMIT_CPU`/`RLIMIT_FSIZE`
  limits (per-UID `RLIMIT_NPROC` is deliberately not used — it aborts healthy
  processes in shared-user containers); dead instances are marked failed, the
  panel stays up.
- **Restarts**: Railway restarts on failure (`railway.json`), and the worker
  re-launches known instances after a pod restart (durable registry).

## What failed before (fixed)

| Symptom | Cause | Fix |
|---|---|---|
| Build rejected: `docker VOLUME not supported` | Dockerfile had a `VOLUME` instruction | removed; attach the volume via the Railway dashboard |
| Crash-loop: `ModuleNotFoundError: sqlalchemy` | two `requirements.txt` files collided in `COPY` | single root `requirements.txt` + build-time import gate |
| Instances unreachable ("no ping") | configs pointed at raw instance ports | all traffic now routes through `/i/<token>` on the public port |
| Instances failing to deploy on busy hosts | per-UID `RLIMIT_NPROC` aborted healthy Cores | dropped from the process driver (kept in Docker driver as `--pids-limit`) |

## Docker self-hosting

```bash
docker build -t emunel .
docker run -d -p 8080:8080 -v emunel-data:/data emunel
```

Or with compose: `docker compose -f deploy/docker/docker-compose.yml up -d`.
