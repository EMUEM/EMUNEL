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

## Engines — deploy guide (traffic plugin layer)

The engines layer ships enabled with a conservative, real-by-default
pipeline: **Coalesce** (downlink frame merging), **PreConnect** + **Congestion**
(inside each Core), **SessionResumption**, **FakeHandshake** (probe defense),
**SplitTunnel** (Iran direct rules in singbox/clash feeds). Engines that need
operator assets (Morph profiles, SNI domains, fronting edge, extra ports)
stay inactive and SAY WHY on the panel.

### 1. Volume (do this once)

Engine state — learned ISP profiles (LinUCB models), rotation counters,
logs — lives in `/data/engines`:

```
Railway project → your EMUNEL service → Settings → Volumes
  Mount path: /data
```

Without a volume the platform still works, but engine learning resets on
every redeploy and the panel shows a warning banner. This is STORAGE
persistence only — it is not related to user traffic quotas.

### 2. Environment variables

All engine variables are optional (documented in `.env.example` and
`engines/README.md`). The ones worth setting first:

| Variable | Default | Effect |
|---|---|---|
| `EMUNEL_ENGINES_ENABLED` | `1` | `0` = bit-for-bit pre-engine behaviour |
| `EMUNEL_ENGINE_MORPH_ENABLED` | `0` | turn on the morphing/bandit engine |
| `EMUNEL_MORPH_PROFILES` | built-ins | per-ISP shaping profiles (JSON) |
| `EMUNEL_SNI_DOMAINS` | — | ≥2 domains attached to the service → enables SNI rotation |
| `EMUNEL_FAKE_SERVER_TYPE` | `nginx` | probe-page flavour (`nginx`/`apache`) |
| `EMUNEL_SPLIT_DOMAINS` | bundled Iran list | direct-routing domain list |

### 3. Start command

Unchanged: `python main.py` (the `railway.json` build/deploy config needs no
edit). The worker automatically launches instance Cores through
`engines.core_host` whenever a core-side engine is active — with all of them
off it launches the plain `emunel_core` exactly as before.

### 4. Verify after deploy

* Panel → **Engine Settings** (admin): every engine shows Active/Inactive
  with a reason, params, metrics; hot Enable/Disable works without a restart.
* `GET /health` stays `200`; the panel loads unchanged.
* A `curl -A curl/8 $APP_URL/random-path` returns an nginx-style 404 (probe
  defense), while a browser still gets the panel.
* Subscription feeds: `?fmt=singbox` contains the `emunel-direct` route
  rules; `?fmt=clash` contains `DOMAIN-SUFFIX,ir,DIRECT`.
* CLI (local clone): `python engine_manager.py doctor && python engine_manager.py selftest`.

## Bypass — SNI Spoofing & REALITY (admin tab)

The **Bypass** tab (admin) holds the Iran-bypass tooling. Nothing here
changes the proxy Core or the gateway.

### SNI Spoofing (client-side, panel-served)

Spoofing executes on the user's **device**, not on Railway. The panel
generates the profile (method / fragment strategy / delay / TTL / SNI pool)
and serves a single-file Python helper (Download button) that the user runs
next to their client:

```bash
python3 emunel_sni_helper.py --connect <your-emunel-domain>:443 \
    --method combined --strategy sni_split --delay 0.1 --ttl 1
# then point the browser / proxy client at 127.0.0.1:40443
```

No env vars required; everything is editable in the tab and persisted with
the engine state (on the `/data` volume).

### REALITY — generating (zero config) vs running (TCP Proxy)

Generating keys and client/server configs works out of the box on any
plan: **Bypass → REALITY → Generate** gives a `vless://` link
(`security=reality`, RAW/XHTTP/gRPC) plus the inbound JSON for your own
Xray server.

To also RUN the VLESS+REALITY listener inside this deployment:

1. Provide an Xray binary the container can read (e.g. place it on the
   volume at `/data/xray/xray`) and set:

   ```
   EMUNEL_XRAY_BINARY=/data/xray/xray
   EMUNEL_XRAY_SHA256=<sha256 of that binary>
   EMUNEL_REALITY_LISTEN_PORT=8443
   ```

   The panel never downloads Xray; the digest pin is mandatory and a
   mismatch is refused (same provenance rule as the VMess runtime).

2. Expose the listener: **Settings → Networking → TCP Proxy → target port
   `8443`**. Railway gives you a public `host:port`; if that public port
   differs from 8443, also set `EMUNEL_REALITY_PUBLIC_PORT` so generated
   links carry the right port.

3. Bypass → REALITY → **Start runtime** (or restart via the button). The
   status card turns Running; core clients connect with the generated
   `vless://` link.

Why a TCP proxy: Railway's normal HTTPS domain terminates HTTP/2 and
cannot pass raw REALITY/gRPC; the TCP Proxy forwards the raw stream.
On the free/hobby plan attach the `/data` volume first (Xray binary +
engine state live there).

### Iran target guidance

Prefer domestic heavy-traffic targets the ISP cannot block wholesale —
`blubank.com`, `divar.ir`, `snapp.ir`. Avoid `google.com` /
`microsoft.com` (censor-monitored). REALITY works with RAW, XHTTP and
gRPC transports only.

## Troubleshooting: "the new feature is not in my panel"

Symptoms like "the Bypass tab is missing" or "only 4 engines work" almost
always mean the **deployment is stale** — Railway is still serving an older
build of the repo. Verify and fix in this order:

1. **Check the build stamp.** The panel sidebar (bottom of the desktop
   drawer) and `/health` both show `build <timestamp>`. If the timestamp is
   older than your last push, the running deployment predates it.
2. **Check the service source.** If the GitHub repository was moved or
   renamed (e.g. to a new owner/org), Railway's webhook may still point at
   the old location and never sees your pushes. Service → Settings → Source
   (or the GitHub tab) must point at the **current** repo + branch.
3. **Trigger a deploy manually.** Service → Deployments → **Deploy latest
   commit** (or push any commit). Watch the build finish; the panel's build
   stamp should refresh within a minute of startup.
4. **Confirm what shipped.** After the deploy: `/health` shows the new build
   stamp, the sidebar shows `v1.1.0` in the panel footer info, Engines shows
   grouped sections, and Bypass is next to Engines in the nav (admin
   accounts; on phones it is in the bottom bar and the drawer).

### Why the panel may have felt unstable (fixed in v1.1.0)

- **Mobile bottom nav was dead CSS** — on phones the only navigation was
  the hidden hamburger drawer, so Engines/Bypass looked "missing".
- **Engine hot-toggles reset on every restart** — enable/disable choices
  now persist in the engine state store (attach the `/data` volume so they
  survive redeploys).
- **Request storms slowed everything down** — endpoint resolution is now
  cached (no more 2 DB queries + log lines per proxied request), proxy
  clients hammering a dead endpoint no longer touch the database, and
  panel polling backs off instead of piling on when the server struggles.
- **Rate limiting was never wired** — /auth/* and /api/* are now limited
  (30 / 1200 / 240 per minute per IP); proxy traffic on /i/* is never
  limited (xHTTP packet-up and carrier-NAT users would break).
