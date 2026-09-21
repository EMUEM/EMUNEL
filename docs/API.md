# EMUNEL API

All Console routes are session-authenticated (GitHub OAuth cookie). Mutating
requests require the `X-EMUNEL-CSRF` header set to the session cookie value
(the frontend does this automatically). Worker routes use the shared
`EMUNEL_WORKER_TOKEN` bearer. Core management routes use the per-instance
`EMUNEL_CORE_API_TOKEN` bearer.

## Console API

### Auth

| Method | Path | Description |
|---|---|---|
| GET | `/auth/login` | Start GitHub OAuth (rate-limited per IP) |
| GET | `/auth/callback?code&state` | OAuth callback, sets session cookie |
| POST | `/auth/logout` | Destroy session |
| GET | `/auth/me` | `{authenticated, user, csrf_token}` |

### Instances

| Method | Path | Description |
|---|---|---|
| GET | `/api/instances` | List own instances (name, status, endpoint, region, counts) |
| POST | `/api/instances` | Create. Body: `{name, region, config:{protocol, cpu_limit, memory_mb, core_version}}` |
| GET | `/api/instances/:id` | Detail: config, domains, latest deployment |
| DELETE | `/api/instances/:id` | Tombstone + best-effort provider cleanup |
| POST | `/api/instances/:id/deploy` | Queue deployment → `{deployment_id}` |
| POST | `/api/instances/:id/restart` | Restart running instance |
| POST | `/api/instances/:id/stop` | Stop instance |
| POST | `/api/instances/:id/redeploy` | New deployment version of same instance |
| GET | `/api/instances/:id/status` | Live status + Core health probe (via worker) |
| GET | `/api/instances/:id/logs?tail=` | Recent Core log lines |
| GET | `/api/instances/:id/metrics` | CPU / memory / connections / traffic |
| GET | `/api/instances/:id/deployments` | Deployment history (id, version, status, duration) |
| GET | `/api/instances/:id/deployments/:depId/logs` | Pipeline logs for one deployment |
| GET | `/api/instances/:id/activity` | Instance activity feed |
| GET | `/api/activity` | User-wide activity feed |
| GET | `/api/instances/:id/volume` | Volume & time state: `{limit_bytes, unlimited, used_bytes, live, percent, exceeded, time_limit_days, expires_at, expired, seconds_remaining}` |
| PUT | `/api/instances/:id/volume` | Set/clear the caps. Body: `{limit_gb}` (or `{limit_bytes}`) and/or `{time_limit_days}` (fractional days). Each key applies independently — send only the volume keys to leave the time limit untouched. Empty/null/0 → Default = unlimited |
| POST | `/api/instances/:id/volume/reset` | Start a fresh accounting period (usage counter back to zero; Core counters untouched) |

Volume behavior: usage is the Core's own lifetime traffic counter (persisted
across restarts), read live while the instance runs and cached when stopped.
When usage reaches the limit the Console stops the instance through the normal
lifecycle path and records an activity event; deploy/redeploy answer `409`
until the cap is raised or cleared. `EMUNEL_VOLUME_CHECK_SECONDS` (default 45)
controls the enforcement interval.

Time-limit behavior: `time_limit_days` sets an absolute expiry (now + days);
empty clears it. When it passes, the instance is stopped through the same
lifecycle path and deploy/redeploy answer `409` until the limit is extended
or cleared.

Subscription propagation: the public feed (`/i/<token>/sub`, all formats)
reports the instance's real state through the standard `subscription-userinfo`
header — `download` = current usage, `total` = the volume cap, `expire` = the
time-limit timestamp (0 = unlimited, exactly the previous default behavior).
The browser subscription page renders the same numbers in its Remaining/Time
stat cards and an Active/Limited/Expired status. Values propagate live on
every fetch — no instance recreation needed.

When no instance-level volume/time limit is set, the per-config quotas take
over: `total` = the sum of the configs' caps, `expire` = the earliest config
expiry (AHB group-subscription semantics). The subscription stays honestly
unlimited only when nothing anywhere sets a limit. The browser page also
renders a per-config quota strip inside every config card (usage meter,
remaining, validity, speed, IP limit, status).

### Per-config traffic management (AHB capability set)

Every config (link) carries its own traffic policy, enforced for real by the
Core at relay time — quota accounting is per relayed chunk, expiry/active are
checked at connection accept and on every chunk, the speed cap is a token
bucket, and the concurrent-IP limit counts distinct client IPs. Empty values
always mean the Default — unlimited.

| Method | Path | Description |
|---|---|---|
| GET | `/api/instances/:id/links` | All configs: DB policy merged with the Core's live counters (Total/Used/Remaining, expiry, speed, IP, status `active|limited|expired|disabled`) |
| POST | `/api/instances/:id/links` | Create a config: `{protocol, label?, limit?, unit?: KB\|MB\|GB\|TB, expiry_days? | expires_at?, speed_mbps?, ip_limit?}` — live immediately, no redeploy |
| PATCH | `/api/instances/:id/links/:uuid` | Edit after creation — key-presence semantics: only sent keys change. Same fields as create, plus `{active: bool}`. `{limit: null}` clears the quota |
| POST | `/api/instances/:id/links/:uuid/reset` | Fresh accounting period for one config (its Core counter drops to zero) |
| DELETE | `/api/instances/:id/links/:uuid` | Delete the config (clients using it stop working immediately) |

Semantics (shared with the instance-level volume feature):

* quota — `limit` + `unit` (KB/MB/GB/TB) or raw `limit_bytes`; empty/0/null → unlimited
* expiry — `expiry_days` (fractional, from now) or absolute `expires_at` (ISO); empty → never
* speed — `speed_mbps` → bytes/s (Mbps × 1024²/8); empty → unlimited
* IP — `ip_limit` concurrent unique client IPs; empty → unlimited
* wizard — `POST /api/instances` accepts the same fields inside `config`
  (`{limit, unit, expiry_days, speed_mbps, ip_limit}`); they are applied to
  every config provisioned at deploy time and stored in
  `instance_configs.link_policy`

Persistence: the Console database (`instance_links` policy columns,
`instance_configs.link_policy`) is the policy source of truth; the Core
persists per-config usage counters in its state file. After every deploy or
redeploy the Console reconciles the Core against the database — links lost
to a wiped state file are restored with the same UUIDs, quotas and counters,
and drifted policies are re-applied. Editing a quota propagates to a running
instance immediately, without recreation.

Protocols: `vless-ws`, `trojan-ws`, `shadowsocks`, `xhttp-packet-up`,
`xhttp-stream-up`.

### Domains & endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/instances/:id/domains` | Endpoints: `kind=path` (console URL + token) and `kind=http` (provider hostname) |
| POST | `/api/instances/:id/domains` | **Regenerate**: rotates the path token and (when supported) the provider hostname |
| DELETE | `/api/instances/:id/domains/:domainId` | Deactivate a provider/custom domain (path endpoints can only be rotated) |

### Admin (`is_admin` required; audited)

| Method | Path | Description |
|---|---|---|
| GET | `/api/admin/overview` | Users / instances / running / failed / deployments-24h / workers-online |
| GET | `/api/admin/users` · PATCH `/api/admin/users/:id` | List; `{is_admin, is_disabled}` (disabling kills sessions) |
| GET | `/api/admin/instances` | All instances with owner |
| POST | `/api/admin/instances/:id/actions/{restart\|stop\|redeploy}` | Act on any instance |
| DELETE | `/api/admin/instances/:id` | Delete any instance |
| GET | `/api/admin/workers` · PATCH `/api/admin/workers/:id` | List node health; `{enabled}` |
| GET | `/api/admin/deployments` | Recent deployments platform-wide |
| GET | `/api/admin/system` | DB/provider/OAuth configuration status |

### Internal (worker token)

| Method | Path | Description |
|---|---|---|
| POST | `/api/internal/heartbeat` | Node metrics upsert; marks stale workers offline |

### Public instance gateway

| Method | Path | Description |
|---|---|---|
| ANY | `/i/{endpoint-token}/{core-path}` | HTTP proxy into the instance's Core |
| WS | `/i/{endpoint-token}/{core-path}` | Frame-level WebSocket relay into Core |

## Worker API (worker token)

| Method | Path | Description |
|---|---|---|
| POST | `/worker/api/instances/:id/launch` | Launch Core (limits + isolation flags) → `{port, driver}` |
| POST | `/worker/api/instances/:id/stop` · `/restart` · `/remove` | Lifecycle |
| GET | `/worker/api/instances/:id/status` | Driver status + Core health probe |
| GET | `/worker/api/instances/:id/logs?tail=` | Core logs |
| ANY | `/worker/api/instances/:id/proxy/{path}` | HTTP proxy into Core (token-translated) |
| WS | `/worker/api/instances/:id/ws-proxy/{path}?token=` | WebSocket relay into Core |
| GET | `/worker/api/metrics` | Node metrics |
| GET | `/health`, `/ready` | Unauthenticated health |

## EMUNEL Core API

Public: `GET /health`, `GET /ready`, `GET /version` — and the protocol
transports `/ws/{uuid}`, `/trojan-ws`, `/ss-ws`, `/xhttp-siz10/*`,
`/txhttp-siz10/*` (RVG-compatible paths).

Management (bearer `EMUNEL_CORE_API_TOKEN`):

| Method | Path | Description |
|---|---|---|
| GET | `/core/api/stats` | Traffic counters, hourly buckets, link totals, recent errors |
| GET | `/core/api/connections` | Live connections grouped by IP |
| GET | `/core/api/logs?limit=` | Redacted runtime log ring |
| GET | `/core/api/metrics` | Process CPU/memory |
| GET/POST | `/core/api/links` | List (no secrets) / create link |
| PATCH/DELETE | `/core/api/links/{uuid}` | Update (label/quota/active/reset) / delete |
| POST | `/core/api/state/flush` | Force persistence |

## Bypass — SNI Spoofing & REALITY (admin session + CSRF)

The Bypass engines expose their own namespace under `/api/engines/*`.
All routes require an admin session (401 anonymous; CSRF header on POSTs
like every other mutation).

| Route | What it does |
|---|---|
| `GET /api/engines/sni/status` | the active bypass profile, metrics, helper usage line |
| `POST /api/engines/sni/config` | update the profile (key-presence: only sent keys change; `400` on nonsense) |
| `POST /api/engines/sni/restart` | full reload (env + persisted profile) |
| `POST /api/engines/sni/test` | server-side proof of the fragment plan (parse → plan → stream preserved) |
| `GET /api/engines/sni/helper` | the standalone client helper script (`?download=1` for attachment) |
| `GET /api/engines/reality/status` | profile, active public key, client uuid, runtime state (honest when unconfigured) |
| `POST /api/engines/reality/config` | update target / server names / fingerprint / short ids / listen port |
| `POST /api/engines/reality/keys` | generate a fresh X25519 keypair — the private key is returned ONCE |
| `POST /api/engines/reality/restart` | reload env/keys and cycle the pinned-Xray runtime |
| `POST /api/engines/reality/generate` | `{transport: raw\|xhttp\|grpc}` → inbound + outbound JSON + `vless://` link **+ a real TCP reachability probe** of the generated endpoint (`server_reachable`, honest `warning` when nothing listens there — the “no ping” clients show) |
| `GET /api/engines/sni/enhanced/status` | SNI Enhanced: profile, pool, per-technique success rates, scanner state, fallback ladder |
| `GET /api/engines/sni/enhanced/snis` | allowed-SNI pool snapshot (weighted, adaptive) |
| `POST /api/engines/sni/enhanced/config` | update the enhanced profile / apply an ISP strategy (`irancell_mci`, `mokhaberat_shatel`, `hard`, `auto`) |
| `POST /api/engines/sni/enhanced/scan` | `{targets?}` — parallel TCP+TLS probe of CDN/decoy targets (≤32, 2.5 s timeout, rate-limited) |
| `GET /api/engines/sni/enhanced/logs` | recent engine log lines |
| `POST /api/engines/sni/enhanced/test` | `{technique?}` — server-side plan proof per technique (stream-preservation invariant) |
| `GET /api/engines/sni/enhanced/helper` | the enhanced standalone client helper (`?download=1`) |

The SNI helper itself runs on the **client device** (it is downloaded and
executed next to the proxy client); the panel never performs spoofing.
The REALITY runtime accepts a pinned Xray via `EMUNEL_XRAY_BINARY` +
`EMUNEL_XRAY_SHA256`, **or an Xray baked into the image at build time**
(set the Railway service variables `XRAY_VERSION` + `XRAY_SHA256` — the
Dockerfile downloads the pinned release once at build, verifies the digest
and installs it to `/opt/xray/xray`; the engine detects it on boot). Then
expose `EMUNEL_REALITY_LISTEN_PORT` through a Railway TCP Proxy and set
`REALITY_PUBLIC_HOST` to the proxy host:port so generated links point at
the real endpoint. See `engines/README.md` and `docs/RAILWAY.md`.

## Volume enforcement — relay-time (the AHB-bypass closure)

The instance volume limit is now enforced by the **Core at relay time**
(`PUT /core/api/quota`, guarded by the instance's management token): the
Console pushes `baseline + limit` as an absolute lifetime cap and the
Core's QuotaGate cuts traffic the moment the lifetime counter reaches it —
including new connections — with only a bounded one-batch overshoot. The
Console's 45 s loop remains as the second line (reconciling drifted caps,
stopping capped instances, alerting on unreachable stats via
`EMUNEL_VOLUME_STALE_ALERT_MISSES` / `EMUNEL_VOLUME_STALE_STOP_MINUTES`)
and repairs core-state regression so a wiped state file can never
silently renew a quota.

## Evolution engines (v1.2 — additive, flag-gated OFF)

All routes below are NEW; nothing existing moved. The two `/api/mesh`
routes are **public** (they are what clients call — no session, no PII,
rate-limited both by the console limiter and in-engine); everything else
mirrors the admin conventions of `/api/engines/*`.

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/mesh/report` | public (rate-limited, ≤1KB JSON) | Submit a DPI signature: `{isp, region, protocol, transport, sni, result, latency}` — ISP/region are stored only as salted hashes (`MESH_SALT`), never raw; answer `{"stored": true}` / fallback marker |
| `GET /api/mesh/policy?isp=X&region=Y` | public | Best-transport policy for an ISP+region: `{policy: {recommended_protocol, params{success_rate, avg_latency_ms, jitter_ms, samples}, confidence}}` |
| `GET /api/chaos/status` | admin | Window state (current frame, seed, switch-in-ms), session count, self-play integrity + RTT stats, applied genome hints |
| `GET /api/genetic/population` | admin | The genome table: all genomes sorted by fitness with generation + parent ids |
| `GET /api/genetic/status` | admin | Generation, best genome, per-generation history (best/avg fitness), timers, metrics |
| `POST /api/genetic/evolve` | admin | Force Evolution — breed the next generation now (bottom-5 out, children of top-5 in) |
| `GET /api/synergy/status` | admin | Engine flags + active states + synergy cycle health + the DpiMesh map (top 50 policies) — this is also what the panel's Evolution tab uses for visibility |

Engine flags (all default `false`, verbatim from the operator spec):
`CHAOS_PROTOCOL_ENABLED`, `DPI_MESH_ENABLED`, `GENETIC_ENGINE_ENABLED`,
`SYNERGY_ENABLED` — plus `CHAOS_SECRET` and `MESH_SALT` secrets. See
`.env.example` and `engines/README.md` for the full tunable list.
