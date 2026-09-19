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
