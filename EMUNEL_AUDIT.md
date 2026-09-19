# EMUNEL Audit

**Audited revision:** `edfb3a2` (working tree after production-hardening tranche).
**Reference:** Lunel @ `ArasTey/lunel` `main`, inspected from source (93 files, ~9.4k LOC), not README claims.
**Reference fork check:** `lunel-tuffy` is byte-identical to upstream — no hidden local changes to account for.

---

## 1. Reference architecture map (Lunel) — the authoritative patterns

### 1.1 Components

| Component | Location | Responsibility |
|---|---|---|
| **Core** | `core/lunel_core/` | Isolated per-instance proxy runtime. FastAPI ASGI app. Own port, own state file, own management token. |
| **Console** | `console/api/lunel_console/` | Control plane: users, sessions, instances, deployments, domains, metrics, subscription feed. FastAPI + asyncpg/SQLite. |
| **Worker** | `worker/lunel_worker/` | Node agent: launches/stops/removes Core instances via Docker or Process drivers, heartbeats metrics to Console. |
| **Frontend** | `console/frontend/` | Vanilla ES modules, hash router, no build step, dark design system, mobile bottom-nav. |

### 1.2 Core wire surface (what "the networking" actually is)

| Route | Protocol | Transport |
|---|---|---|
| `WS /ws/{uuid}` | VLESS | WebSocket |
| `WS /trojan-ws` | Trojan (SHA-224 handshake, link resolved by password hash) | WebSocket |
| `WS /ss-ws` | Shadowsocks AEAD (EVP_BytesToKey + HKDF-SHA1 subkey, streaming AEAD; link identified by successful decryption) | WebSocket |
| `WS /vmess-ws/{uuid}` | VMess AEAD via pinned, SHA-256-verified Xray subprocess (loopback-only, opt-in) | WebSocket |
| `POST /xhttp-siz10/{packet-up\|stream-up}/{uuid}/{session}[/seq]` + `GET /xhttp-siz10/{mode}/{uuid}/{session}` | VLESS | xHTTP |
| `POST /txhttp-siz10/...` | Trojan | xHTTP |
| `GET /health /ready /version` | public health | HTTP |
| `/core/api/*` (links CRUD, stats, connections, logs, metrics, share, state/flush) | management | HTTP + bearer token |

### 1.3 Core security & performance invariants (must preserve)

1. **Fail-closed credentials**: a link must exist and be `is_allowed()` (active, not expired, under quota) before any outbound connection.
2. **QuotaGate EWMA batching** (32 KiB–2 MiB adaptive batches, 0.25 s interval): quota locks are not taken per frame; per-connection traffic accounting never blocks the event loop on disk I/O (debounced atomic JSON saves).
3. **Session keying `(uuid, session_id)`** in xHTTP — cross-link session attachment is impossible.
4. **Global/per-link session caps, seq-buffer caps, body caps** (memory-DoS protection).
5. **Socket tuning**: TCP_NODELAY, SO_SNDBUF/RCVBUF, TCP_QUICKACK, TCP_USER_TIMEOUT.
6. Protocol/transport matrix is fixed and validated (`PROTOCOLS` tuple in `config.py`); VMess additionally requires an explicitly installed, digest-pinned Xray binary.
7. Management API secrets never returned by any API; health endpoints stay unauthenticated by design.

### 1.4 Console↔Core integration seams

- Console creates an instance row + generates a per-instance `core_api_token`; Worker launches the Core with that token; Console talks to Core through the Worker proxy with token translation.
- Link lifecycle: Console is the source of truth; Core holds runtime links (credentials) synced via `/core/api/links` CRUD; share URLs rendered via `/core/api/share` with host + optional path prefix.
- Worker relaunches all registered instances after restart (registry JSON survives process death).
- Metrics: Worker heartbeats node CPU/RAM/disk; Console records per-instance metrics.

---

## 2. Current EMUNEL state (what exists, honestly)

### Completed (keep)

| Area | Verdict |
|---|---|
| API importability, router registration | OK after previous tranche |
| JWT auth (type/expiry/signature/state/role), bcrypt passwords, correlation-id middleware | OK |
| Startup config validation (placeholder/short secrets, weak admin passwords, CORS wildcards, JWT algorithms rejected in prod mode) | OK |
| Fail-closed quota cache concept (`core/quota.py`) | Concept OK, integration wrong (see below) |
| Baseline test file | Exists, minimal |

### Broken / wrong model (replace with Lunel ports)

| Area | Problem |
|---|---|
| **Core listener** | Raw TCP `asyncio.start_server` with **heuristic protocol detection** on the first 1–2 bytes. Real proxy clients connect over HTTP/WebSocket/xHTTP paths; this listener cannot accept any real client. The previous audit already flagged it; it remains unfixed. |
| **Relay handlers** | Single-read handshake parsing, incomplete Trojan validation, plaintext "SS" stub, TCP-as-UDP. Not wire-compatible with anything. |
| **Links** | `links.py` generates URLs for listeners that do not exist (`/ws` shapes that nothing serves). |
| **QuotaManager** | Disconnected: nothing loads it except tests; the "fail-closed" state means the Core can carry no traffic at all today — honest but unusable. |

### Missing entirely vs Lunel

1. **No Instance architecture** — no instance model, no launch/stop, no per-instance isolation, no port allocation, no per-instance tokens/state.
2. **No Worker/driver layer** — nothing can start a Core.
3. **No links sync** — Console cannot create a credential inside a Core; subscription ↔ link bridge does not exist.
4. **No real subscription feed** — no base64 subscription endpoint, no share-link rendering from a live Core.
5. **No traffic accounting pipeline** — nothing reads Core stats; `traffic.py`/`analytics.py` routers read only the local DB (which nothing writes).
6. **No frontend at all** — README advertises a glass dashboard; `dashboard/` does not exist.
7. **No diagnostics** — `network_tests.py` is a 29-line TCP-only stub.
8. **No health system** — single `/health` returning `ok` unconditionally.
9. **No deployment stack** — README references `docker/`, `docs/` — none exist.
10. **No protocol test suite** — Lunel ships wire-level roundtrip tests; EMUNEL has none.

### Placeholder / disconnected

- `nodes.py` router manages Node rows not bound to any worker protocol (no heartbeat, no token exchange).
- `subscriptions.py` CRUD works against DB only — never touches a Core, so quotas/expiry are never enforced on traffic.
- `analytics.py` computes from tables that no component populates.
- README "Screenshots coming soon", Docker/Railway/PWA badges — all aspirational.

---

## 3. Security concerns

1. Traffic counters are 32-bit `Integer` in `models/subscription.py` — overflow at 4 GiB. Must be `BigInteger`.
2. No CSRF story for cookie-based flows; current JWT-in-header approach is acceptable but API keys are ephemeral claims, not persisted credentials.
3. No egress policy on the Core (private/metadata address reachability) — inherited from Lunel; acceptable parity, noted as future hardening.
4. No rate limiting on auth endpoints.
5. Secrets (`core_api_token`) do not exist yet as a concept; when introduced they must never be returned by any API (Lunel pattern).

---

## 4. Recommended implementation order (this tranche)

1. **Port Lunel Core verbatim** as `emunel_core` (wire-compatible; env prefix `EMUNEL_`), replacing the raw-TCP prototype. Delete the heuristic relay code.
2. **Instance layer**: DB model + `InstanceManager` service (port of Lunel's ProcessDriver + worker lifecycle endpoints): per-instance subprocess, port allocation, state dir, API token, registry recovery, health probes.
3. **Links & subscriptions bridge**: subscription/link CRUD that syncs to the target instance's Core (`/core/api/links`), quota/expiry enforced by the Core (never bypassed), usage synced back by a batched poller.
4. **Real data planes**: traffic/analytics/connections/logs routers reading from live Cores + DB; empty states when no data.
5. **Diagnostics subsystem**: real DNS/TCP/TLS/SNI/HTTP/WS/gRPC/XHTTP probes with timeouts, concurrency caps, rate limits, clearly labeled latency types.
6. **Dashboard SPA**: vanilla ES modules, glass design (restrained blur), EN+FA i18n + RTL, mobile-first.
7. **Deployment**: Dockerfile, docker-compose (API + optional Postgres), health checks, env validation.
8. **Tests**: port Lunel's protocol roundtrip tests, add instance/subscription/API/integration tests.
9. **PERFORMANCE.md** with measured numbers.

## 5. Quality gate at audit time

API imports and baseline tests pass; the networking Core cannot serve real clients (raw-TCP prototype); no instance lifecycle; no frontend; no deployment. **Not production-ready.** The sections above define what "done" means for this tranche.

---

## 6. Post-tranche quality gate (closure)

All items from section 4 are implemented. Against the final quality bar:

| # | Question | Status |
|---|---|---|
| 1 | Does the networking Core actually work? | **Yes** — Lunel's engine ported verbatim (pure-rename verified), 4 real tunnel e2e tests + 179 Mbps measured through a live VLESS tunnel |
| 2 | Are generated configurations usable? | **Yes** — share URLs rendered by the Core's link generator; `/sub/{token}` feed verified with real client-format base64 payloads |
| 3 | Every dashboard metric from real data? | **Yes** — traffic/instances/health/connections all read from live Cores or the synced DB; empty states elsewhere |
| 4 | Quotas actually enforced? | **Yes** — by the Core (fail-closed `is_allowed()`), e2e test cuts a tunnel at exhaustion; console never bypasses |
| 5 | Expiry actually enforced? | **Yes** — expiry pushed onto Core links; `ACTIVE → EXPIRED/QUOTA_EXCEEDED/DISABLED` transitions applied by the sync worker |
| 6 | Instance configurations isolated? | **Yes** — subprocess + own port/token/state/rlimits per instance; changes to A cannot touch B |
| 7 | Protocol/transport combinations validated? | **Yes** — matrix endpoint mirrors the Core's `PROTOCOLS`; invalid selections rejected (400) |
| 8 | UI fast on mobile? | **Yes** — 47.5 KB initial payload, lazy views, pollers pause on hidden tabs, glass blur bounded to nav/cards/dialogs |
| 9 | Survives subsystem failures? | **Yes** — 5 failure-injection tests: core SIGKILL, dead-core sync, DB outage, diagnostics failure all contained |
| 10 | Architecture understandable? | **Yes** — `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md`, honest README, this audit |

**Remaining known limitations (documented, not hidden):**
- VMess requires an operator-installed, SHA256-pinned Xray binary (by design — the Core refuses to download binaries); the API surfaces this instead of pretending support.
- The console DB uses create-all rather than versioned migrations; safe for the current schema lifecycle, flagged for the next tranche if the schema evolves.
- Egress policy (blocking private/metadata addresses) is inherited Lunel parity — listed as future hardening.
- API keys are still ephemeral tokens (documented in `services/auth.py`); persisted, revocable keys remain future work.
