# Deploying EMUNEL on Railway

EMUNEL deploys to Railway with **zero configuration**: the repo contains a root
`Dockerfile` (auto-detected by Railway), a `railway.json` healthcheck policy,
and a zero-config bootstrap mode that generates strong secrets on first boot.

## What caused failed deploys before (fixed)

| Problem | Symptom in Railway logs | Fix |
|---|---|---|
| Requirements collision in Dockerfile | Build succeeds but runtime crash-loops: `ModuleNotFoundError: No module named 'sqlalchemy'` (while `import fastapi` works) — `COPY requirements.txt core/requirements.txt ./` flattened both same-named files and core's 5 packages silently overwrote the root list | Dockerfile now installs from the single root `requirements.txt` (a strict superset) and a build-time import check fails the build loudly if any critical package is missing |
| Dockerfile `VOLUME` instruction | `dockerfile invalid: docker VOLUME at Line 49 is not supported, use Railway Volumes` → build fails instantly | Removed from the Dockerfile — attach a Railway volume at `/data` instead |
| Secrets required at boot | `RuntimeError: EMUNEL_SECRET_KEY and JWT_SECRET_KEY must be replaced before production startup` → `Application startup failed. Exiting.` | Secrets are now **auto-generated** (persisted under `/data`) when not provided via env vars |
| Port mismatch | App listening on `8000`, Railway routing to injected `PORT` → connection refused / "port not detected" | `main.py` honors the injected `PORT` (`EMUNEL_PORT` still wins if set) |
| No build recipe | `deploy/Dockerfile` was not at repo root, so Railway fell back to Nixpacks guessing | Root `Dockerfile` + `railway.json` pin the build explicitly |

## Deploy steps

1. **Create the service** — Railway → project → *New Service* → *GitHub Repo*
   → pick your EMUNEL fork. Railway detects the root `Dockerfile` and builds
   it (the `railway.json` config pins the builder, the `/health` healthcheck
   and an on-failure restart policy).
2. *(Optional but recommended)* **Attach a volume** — Settings → Volumes →
   mount at `/data`. This persists the SQLite database, generated secrets,
   instance registry and per-instance link/traffic state across redeploys.
   Without a volume everything still works but resets on each redeploy.
   (The Dockerfile deliberately contains no `VOLUME` instruction: Railway's
   builder rejects it — platform volumes are attached from the dashboard.)
3. *(Optional)* **Add PostgreSQL** — Settings → Database → add Railway
   Postgres. It injects `DATABASE_URL`, which EMUNEL normalizes to
   `postgresql+asyncpg://` automatically — no other change needed.
4. **Generate a domain** — Settings → Networking → Generate Domain. Railway
   injects `PORT`; EMUNEL listens on it. The dashboard is served at `/`,
   the API at `/api/v1/...`, docs at `/api/docs`.
5. **Get the initial admin password** — open *Deployments → Deploy Logs*.
   On the **first** boot (empty database) you will see:

   ```
   initial admin 'admin' seeded with auto-generated password: Xy3AbC...
   change it immediately after first login (Users -> admin), or set EMUNEL_ADMIN_PASSWORD to control it
   ```

   Log in with `admin` + that password and change it in the UI. The password
   is printed only that once; it is not stored in plain text anywhere else.

## Environment variables (all optional)

| Variable | Default | Notes |
|---|---|---|
| `EMUNEL_SECRET_KEY` | auto-generated | ≥32 chars; persisted in `/data/secrets.json` when unset |
| `EMUNEL_JWT_SECRET_KEY` | auto-generated | ≥32 chars; same persistence rule |
| `EMUNEL_ADMIN_PASSWORD` | auto-generated | ≥12 chars; printed once in logs on first seed |
| `EMUNEL_DATABASE_URL` / `DATABASE_URL` | SQLite at `/data/emunel.db` | accepts `postgresql://`, `postgres://`, asyncpg DSNs |
| `EMUNEL_DATA_ROOT` | `/data` (in image) | put the volume here |
| `EMUNEL_PORT` | injected `PORT` | don't set this on Railway |
| `EMUNEL_CORE_BIND_HOST` | `0.0.0.0` (in image) | see "Exposing proxy instances" below |
| `EMUNEL_PORT_RANGE_START/END` | `18100`–`18999` | internal ports allocated to instances |
| `EMUNEL_SYNC_INTERVAL` | `10` | seconds between link/traffic sync passes |

Explicit values always override generated ones; weak explicit values are
rejected at startup (fail-closed with a clear message in the logs).

## Exposing proxy instances on Railway

The console/dashboard traffic goes through the single service domain. Proxy
**instances** run on their own internal ports (`18100+`). To make a running
instance reachable from the internet, create an additional domain (or TCP
proxy) targeting that instance's port — Settings → Networking → New Domain →
select the instance port. The image sets `EMUNEL_CORE_BIND_HOST=0.0.0.0`
specifically so this works; Railway's edge only routes declared ports, so
nothing else is exposed.

For each instance, the dashboard shows its port; share links embed the
public host recorded for that instance.

## Resource notes

* The build is wheel-only (no compiler toolchain) — it fits comfortably in
  Railway's builder memory and takes well under a minute.
* Runtime: each proxy instance is a subprocess capped by per-instance rlimits
  (`RLIMIT_AS` floor 512 MB, CPU/fsize caps). On 512 MB plans a single
  instance plus the console may be tight — 1 GB+ is comfortable.

## Verifying the deployment

```bash
curl https://<your-app>.up.railway.app/health    # {"status":"ok",...}
curl https://<your-app>.up.railway.app/ready     # {"ready":true,"database":{...}}
```

Then log in, create an instance, provision a subscription, and watch real
traffic appear on the dashboard.
