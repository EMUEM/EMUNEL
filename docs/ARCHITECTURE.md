# EMUNEL Architecture

## Overview

EMUNEL is a production evolution of the [Lunel](https://github.com/ArasTey/lunel)
proxy platform. The networking core is Lunel's proven engine, ported verbatim
(wire-compatible); EMUNEL adds the management layer, subscription system,
monitoring, diagnostics and the glass console UI on top.

```
                ┌─────────────────────────────────────────────────┐
                │                EMUNEL PROCESS                    │
   Browser ────▶│  dashboard/ (SPA, vanilla ES modules)          │
                │  emunel_api  (FastAPI console, JWT + RBAC)      │
                │      │                                          │
                │      ├── InstanceManager ──┐ subprocess + rlimits
                │      │   (ports, tokens,    │ per instance        │
                │      │    registry)         ▼                    │
                │      │               emunel_core #1  ── relay ──▶ internet
                │      │               emunel_core #2  ── relay ──▶ internet
                │      │                  ...                     │
                │      ├── LinkSync worker (policy push,          │
                │      │    batched traffic pull)                 │
                │      └── Diagnostics (on-demand, bounded)        │
                │  SQLite / PostgreSQL                             │
                └─────────────────────────────────────────────────┘
```

## The Core (ported from Lunel, unchanged wire behavior)

`core/emunel_core/` — an isolated, self-contained proxy runtime. Each EMUNEL
Instance launches one Core subprocess with its own:

- loopback port and management token (`EMUNEL_CORE_API_TOKEN`)
- JSON state file (atomic writes, debounced — links, quotas, counters)
- resource budget (RLIMIT_AS with a 512 MB VA floor, RLIMIT_CPU, RLIMIT_FSIZE)

### Wire surface

| Route | Protocol | Transport |
|---|---|---|
| `WS /ws/{uuid}` | VLESS | WebSocket |
| `WS /trojan-ws` | Trojan (SHA-224 password handshake) | WebSocket |
| `WS /ss-ws` | Shadowsocks AEAD (EVP_BytesToKey + HKDF-SHA1) | WebSocket |
| `WS /vmess-ws/{uuid}` | VMess AEAD via pinned Xray runtime (opt-in) | WebSocket |
| `POST /xhttp-siz10/{mode}/{uuid}/{session}[/seq]` | VLESS | xHTTP packet-up / stream-up |
| `POST /txhttp-siz10/...` | Trojan | xHTTP |
| `GET /health /ready /version` | health | HTTP |
| `/core/api/*` | management | HTTP + bearer token |

### Security invariants (preserved from the reference)

1. **Fail-closed credentials** — unknown, disabled, expired or over-quota links
   cannot open an outbound connection. An empty state file is a dead relay,
   never an open proxy.
2. **QuotaGate EWMA batching** — quota locks are taken adaptively
   (32 KiB–2 MiB batches), not per frame; traffic accounting never blocks the
   relay on disk I/O.
3. **Session keying `(uuid, session_id)`** in xHTTP — a valid link can never
   attach to another link's stream; global/per-link session caps and body caps
   bound memory.
4. **Secret redaction** — every log record passes a redaction filter; the
   management token and SS passwords never appear in API responses or logs.
5. **Protocol matrix is fixed** — only the combinations above exist; the UI's
   instance builder validates against this exact set.

## The Console (`api/emunel_api/`)

FastAPI + SQLAlchemy async (SQLite by default, PostgreSQL for scale).

| Module | Responsibility |
|---|---|
| `routers/v1/instances.py` | instance CRUD + lifecycle (start/stop/restart) + links + share URLs |
| `routers/v1/subscriptions.py` | plans: quota, expiry (1/7/30/60/90/custom days), extend, renew, revoke, reset |
| `routers/v1/traffic.py` | per-instance / per-subscription / top-links usage from synced counters + live cores |
| `routers/v1/analytics.py` | overview, protocol distribution, subscription status, hourly buckets |
| `routers/v1/connections.py` | live connections grouped by client IP, merged across instances |
| `routers/v1/network_tests.py` | the diagnostics subsystem (below) |
| `routers/v1/logs.py` | audit trail (DB) + live Core ring-buffer logs |
| `routers/v1/health.py` | independent component health states |
| `services/instance_manager.py` | subprocess driver: port allocation with bind-probe + ownership proof, durable registry, restart recovery |
| `services/core_client.py` | typed `/core/api/*` client (token stays inside the module) |
| `services/link_sync.py` | the subscription ↔ Core bridge (below) |
| `services/diagnostics.py` | real measurement probes |
| `services/health.py` | liveness / readiness / database / instances / manager / sync |

### Subscription ↔ Core bridge

The Core is the enforcement point — the console never second-guesses a relay
decision and never bypasses the Core quota system:

1. Creating a subscription (optionally bound to an instance + protocol)
   provisions Link rows and pushes them into the target Core's link registry.
2. Quota (`traffic_limit_gb`), expiry and revocation changes are pushed onto
   the bound links; the Core refuses them at connection time fail-closed.
3. A background worker (default every 10 s) pulls per-link counters from each
   running Core, updates only changed rows, aggregates onto subscriptions, and
   applies lifecycle transitions: `ACTIVE → EXPIRED | QUOTA_EXCEEDED | DISABLED`
   (auto-disable configurable), monthly resets on `reset_day`.
4. `GET /sub/{link_token}` serves the standard base64 subscription feed with
   `subscription-userinfo` headers for client-side usage display.

### Traffic accounting efficiency

- The Core already batches per-link accounting in memory (EWMA QuotaGate) and
  persists debounced — no per-packet disk writes.
- The console polls at a low frequency (10 s) and writes only changed rows.
- 64-bit counters everywhere (a 32-bit column overflows at 4 GiB).

## Diagnostics (real measurements only)

Every latency shown anywhere in EMUNEL is a measured `time.monotonic()` delta
around an actual operation. Latency types are labelled and never conflated:
`server` (API /health RTT), `tcp_connect`, `tls_handshake` (incl. SNI + cert),
`ws_tunnel` (upgrade completion), `e2e`. Probes: DNS, TCP, TLS/SNI, HTTP,
WebSocket (+ ping RTT), gRPC-style HTTP2 probe, xHTTP route probe, and a staged
chain. All probes are on-demand only, bounded by a per-probe timeout (6 s),
a global concurrency semaphore (8), and a per-user token bucket
(20 burst / 30 per minute).

## Health model

Independent states — one failed subsystem never marks the whole platform
broken: `liveness`, `readiness`, `database`, `instances` (per-instance Core
probes), `manager`, `sync` (poller freshness). The dashboard renders a badge
per component and the overview degrades, not dies.

## Frontend

`dashboard/` — vanilla ES modules, hash router, no build step, no framework,
no CDN. Views are lazy-loaded (`import()`); per-view pollers pause when the
tab is hidden. Glass styling is applied only to nav/cards/dialogs (bounded
`backdrop-filter` — no blur on scroll containers). Full i18n (English +
فارسی) with `dir=rtl`. Empty/loading/error states everywhere; every metric
comes from the live API — no simulated values.

## Deployment

Single container (`deploy/Dockerfile`): the API runs the InstanceManager which
spawns Core subprocesses under rlimits; state survives in the `/data` volume;
instances are automatically relaunched with identical credentials after a
restart (durable registry + state files). See [DEPLOYMENT.md](DEPLOYMENT.md).
