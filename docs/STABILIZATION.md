# EMUNEL Stabilization Report (v1.4.0)

Branch `feature/stabilization` · Core untouched (snapshot-proven) ·
All new flags default OFF — a deployment that sets none of them behaves
byte-identically to v1.3.1.

---

## Deliverable 1 — Data persistence

**Diagnosis of "instances disappear after deploy":** on Railway, everything
outside a mounted volume is ephemeral. The image already prefers `/data`
(console DB `/data/emunel.db`, instances `/data/instances`, engine state
`/data/engines`) — **without a volume attached at `/data` every redeploy
wipes the instance list.** That is a platform configuration, not a code bug;
the code now also defends against it:

| Item | Status |
|---|---|
| Volume mount `/data` | **Operator action** — Settings → Volumes → mount at `/data` |
| `RAILWAY_RUN_UID=0` | **Operator action** — Variables → add it (Railway volumes are root-owned; the container otherwise runs as `emunel` uid 65532 and cannot write the volume) |
| DATA_DIR resolution | already in place: `/data/engines` → repo `.emunel-data` → `/tmp` fallback (loud warning printed when the directory did not survive the previous boot — the volume probe) |
| SQLite paths | no hardcoding: every engine DB resolves through `EMUNEL_ENGINE_DATA` / the shared resolver |
| WAL mode | every SQLite the engines layer touches runs `journal_mode=WAL` (mesh, genetic, consolidated, and the **console DB is flipped to WAL from the outside** by the backup sweep — no console code touched) |
| Automatic backups | **new** `engines/persistence/backup.py`: cron every `EMUNEL_BACKUP_INTERVAL_H` (default 6h), keeps `EMUNEL_BACKUP_KEEP` (default 7) rotating copies in `DATA_DIR/backups/backup-<ts>/`, sqlite files copied through the sqlite3 backup API (consistent even mid-write), JSON state plain-copied (atomic writes already), 64MB volume guard, first run 90s after boot, never raises into the host |

**Scope (allowlist, deliberately tiny):** `DATA_DIR/state.json` (engine
toggles — the crown jewels), `DATA_DIR/*.json`, `DATA_DIR/*.db`
(mesh/genetic/consolidated), and the console DB when it is a real SQLite
file next to the engines dir. A PostgreSQL deployment simply has no such
file and is skipped.

---

## Deliverable 2 — Consolidated engine modules

New package `engines/consolidated/` — legacy engine CODE IS PRESERVED and
wrapped (the modules instantiate the very same classes from
`engines/engines/*.py`):

| Module | Flag | Children (run order, env-tunable) |
|---|---|---|
| **TrafficShaping** | `TRAFFIC_SHAPING_MERGED` | Morph → SNIEnhanced → Chaos |
| **Transport** | `TRANSPORT_MERGED` | PreConnect → Congestion → FEC → SessionResumption |
| **Learning** | `LEARNING_MERGED` | Mesh → Genetic → Synergy |
| **Payload** | `PAYLOAD_MERGED` | Coalesce → Compress |

Adaptation notes (spec written for a JS codebase, this is Python):
* *Padding/Jitter* and *Living Mirror* and *Dedup* and *0-RTT* do not
  exist as separate engines here — Morph's profiles carry padding,
  SessionResumption is the 0-RTT/TLS-session-cache engine. The groups wrap
  what is real.
* Internal pipeline order: `EMUNEL_TS_PIPELINE` etc. — the module joins the
  manager pipeline at the position of its FIRST child; children are skipped
  as individuals (they run inside).
* Shared state (`engines/consolidated/shared.py`): ONE SQLite
  (`consolidated.db`, WAL) — Learning's Mesh + Genetic tables live side by
  side in it (disjoint schemas; `mesh.db`/`genetic.db` are never created in
  merged mode); ONE DecisionCache (TTL 300s / 100 entries, LRU); ONE
  BufferPool (16KB per stream, 4MB total).
* Backward compatible: children are published into `manager.engines` under
  their own names, so `/api/engines/sni/enhanced/*`, `/api/mesh/*`,
  `/api/genetic/*` and the dial hook's `PreConnect` lookup keep working.
* **Multi-layer fallback** (golden rule 4): a child error inside
  `process()` bypasses that child for the batch (the batch keeps flowing);
  a merged module that fails to start makes the manager **automatically**
  activate the group's legacy engines individually (logged as
  `merged-module-fallback`); everything off → Core default.
* `LEGACY_ENGINES_ENABLED=false` + a group's merged flag off → the whole
  group is absent (each engine visible in Settings with the honest reason).

---

## Deliverable 3 — RAM / CPU before → after (measured)

`scripts/stabilization_bench.py`, same process/machine, identical feature
sets, realistic 13KB subscription bodies:

| Configuration | RSS (engines layer) | configgen p50 | status build p50 | 100 conns |
|---|---|---|---|---|
| Legacy defaults (7 engines) | 13.8 MB | 0.003 ms | 0.057 ms | 0.24 s |
| Same features standalone (17 engines) | **17.3 MB** | 0.013 ms | 0.230 ms | 0.24 s |
| Same features merged (4 modules) | **16.3 MB** | 0.013 ms | 0.230 ms | 0.24 s |

Honest reading (the spec hoped for 40–60% RAM; that assumed N independent
engine runtimes — here all engines already shared one process, so the
ceiling is smaller):

* engines-layer RSS: **17.3 → 16.3 MB (−6%)** for the same functionality;
* timer wakeups: the Learning group's 5-min loops **halved to 10-min**;
* `/api/engines` status: rebuild cost 0.23 ms, but the **5s TTL cache means
  the panel's 10s poll hits the cache** — effective per-poll cost ~0
  (was: full rebuild every poll);
* render-skip: dashboard/instance DOM no longer rebuilds on unchanged data;
* decision cache: measured neutral at today's cheap profiles (hashing ≈
  transform cost); it pays as profiles/children grow and always skips the
  full group dispatch on hits (299/300 hits in the cache check);
* the real OOM protection stack: **OOM guard** (RSS watchdog, soft 380MB →
  caches+gc, hard 460MB → pools drained too, every 30s, never kills),
  **MALLOC_ARENA_MAX=2** in the Dockerfile (glibc arena multiplier off),
  bounded structures everywhere (session LRU 1024, caches 100/64, warm
  throttle, 16KB stream buffers).

Full-stack context from the v1.3.1 rehearsal: console+worker+2 cores+engines
≈ 159 MB — comfortably under the 512 MB plan with the guard as backstop.

---

## Deliverable 4 — Pre-Connect + FEC activation

**Pre-Connect had a real bug:** the warm pool had `take_warm()` but nothing
ever called `pool.put()` — `warm_hits` stayed 0 forever, so the engine
looked armed but did nothing. Now:

* the dial hook records every dialed destination (`note_dial`);
* a destination dialed ≥2 times gets ONE speculative background connection
  (`should_warm` → `warm()` through the ORIGINAL open_connection — no
  recursion), throttled 1/5s/host, bounded by pool caps;
* warm sockets carry **TCP_NODELAY** (first byte not stuck in Nagle);
* with `TRANSPORT_MERGED=true` Pre-Connect is **always** part of the
  module (spec 5.2) — console host: persists the intent; instance cores:
  the pool actually fills and serves warm hits (test 3b proves the fill +
  the NODELAY + the take).
* FEC: honest status kept (TCP transports retransmit in the kernel;
  FEC arms on datagram channels), but the module now runs the spec's
  adaptive ratio `min(0.30, max(0.05, loss × 3))` live off Congestion's
  measured loss, and the codec + ratio are exercised/tested.
* Temporary debug logs (spec 5.3): `EMUNEL_ENGINE_DEBUG=true` — one line
  per init/start, off by default.

---

## Deliverable 5 — UI responsiveness

* `/api/engines` status cached **TTL 5s** (`EMUNEL_ENGINES_STATUS_TTL`,
  spec 4.2); `/api/mesh/policy` **TTL 60s**; any hot-toggle clears the
  cache instantly so Settings always shows fresh state.
* Panel polls: instance/dashboard 5–6s → **10s** (spec 4.3); engines and
  evolution tabs were already 30s.
* **Render-skip**: `snapChanged()` compares the fetched payload; unchanged
  data does not re-render the DOM (spec 4.3).
* Lazy loading was already the SPA's model (each view fetches on open; the
  Evolution/SNI-Enhanced boot probes are one-shot best-effort, admin-only).
* Worker-thread equivalents (spec 4.1): Python's asyncio runs these
  engines in the same event loop at µs cost — the measured p95 pipeline
  cost is 0.047 ms, four orders below a frame time; the honest adaptation
  is the TTL cache + render-skip above (no thread pool needed at this
  workload, and one would add RSS).

---

## Deliverable 6 — Connection quality / egress

* `TCP_NODELAY` on every socket the engines layer owns: warm pool entries,
  scanner probes; Congestion already tunes relay dials (NODELAY + adaptive
  SO_SNDBUF/SO_RCVBUF + QUICKACK).
* MTU 1420 / BBR: not applicable inside a Railway container (kernel-level
  sysctls; MTU applies to TUN interfaces this platform does not run) —
  documented rather than faked.
* `*.railway.internal`: single-service deployment — worker↔core traffic is
  localhost; nothing to switch.
* Railway "internal CDN" does not exist for this shape; the honest egress
  reduction is the 24h budget analysis (deliverable below) + response
  compression (v1.3.0, 74% measured saving).
* `EMUNEL_HTTP_COMPRESS_MIN_BYTES` knob added (256 default unchanged; set
  1024 with `PAYLOAD_MERGED=true` per spec 3.2).

---

## Deliverable 7 — NewYork (x4gpanell) pattern review

| Pattern | Decision |
|---|---|
| Single port + Nginx reverse proxy | **Not adopted** — Railway exposes exactly one `$PORT` and the app already serves panel + API + instance proxying through it; adding Nginx would add RSS (the opposite of the goal) |
| State on disk (`x4g_state.json` in DATA_DIR) | **Adopted** — this IS the platform's model: `state.json` per host + per-instance state dirs on the volume; merged modules add the shared `consolidated.db` on the same volume |
| XHTTP (packet-up / stream-up) | **Already present** — wizard exposes xhttp-packet-up/stream-up and Core serves `/xhttp-siz10` paths |

---

## Deliverable 8 — Test scripts (stage 8)

`tests/test_stabilization.py` — the operator's 10 mandatory tests verbatim
plus unit coverage (20 tests; full suite **298 passed + 13 subtests**,
baseline was 278 + 13):

1. flags-off → Core identical (byte-level passthrough + effective order ==
   legacy order + zero merged engines active)
2. `TRAFFIC_SHAPING_MERGED` → Morph+SNIEnhanced+Chaos run in one module,
   API lookups still resolve, shared sqlite, cache hit proven
3. `TRANSPORT_MERGED` → FEC armed + adaptive ratio formula proven at the
   floor/slope/cap, Pre-Connect active **and the warm pool actually fills
   through the dial hook** (loopback TCP, NODELAY verified on the socket)
4. `LEARNING_MERGED` → one `consolidated.db` (no mesh.db/genetic.db),
   both schemas in one file, 10-minute cadence
5. `PAYLOAD_MERGED` → Coalesce+Compress in the module, level ≤ 4, 16KB
   stream cap, stream-preserving
6. crashed module → automatic fallback to legacy engines, pipelines rebuilt
7. backup rotation: 9 runs → 7 copies, sqlite-consistent copies, WAL
   applied, size cap prunes
8. 100 concurrent connections → RSS < 400 MB, CPU < 5%
9. 24h egress budget: default **0** scheduled outbound calls; worst case
   (scanner 6h × 32 targets + self-play 300s) = 416 < 2400
10. 1000 sessions → chaos LRU 1024, caches 100/64 — flat RAM

---

## Deliverable 9 — Core snapshot comparison

`scripts/sni_enhanced_snapshot_compare.py` (allowlist extended with the
four documented module entries, same process as v1.2/v1.3 releases):
**VERDICT PASS** — 8/8 protocol share links byte-identical, REALITY config
diffs `[]`, FEC codec identical, configgen passthrough sha in == out;
engine-layer diffs are exactly the additive
`TrafficShaping/Transport/Learning/Payload` pipeline+flag entries.

---

## Deliverable 10 — Engine inventory (before → after)

| Legacy engine (still present — fallback path) | Wrapped by |
|---|---|
| Morph, SNIEnhanced, Chaos | TrafficShaping |
| FEC, PreConnect, SessionResumption, Congestion | Transport |
| Mesh, Genetic, Synergy | Learning |
| Coalesce, Compress | Payload |
| FakeHandshake, SplitTunnel, SNIRotation, DomainFronting, PortHopping, SNISpoof, Reality, (http_compress middleware) | **not merged** — no spec group; unchanged |

Nothing was deleted: with every merged flag off (the default) the panel,
API surface and engine list behave exactly as v1.3.1.

---

## Operator quick start

```bash
# Railway → Variables
RAILWAY_RUN_UID=0            # with a volume at /data
# optional, one at a time:
TRANSPORT_MERGED=true         # FEC+PreConnect+Congestion+SessionResumption
PAYLOAD_MERGED=true           # Coalesce+Compress (+EMUNEL_HTTP_COMPRESS_MIN_BYTES=1024)
```

Turn one flag on, watch Engine Settings (each child shows
`inside <Module>` with its own status/reason), then the next.

---

# v1.4.1 — Reconnect-storm & OOM hotfix (WS lifecycle / stream-up 404)

## What the Railway logs showed, and the real mechanisms

| Symptom | Root cause found in code | Fix |
|---|---|---|
| `stream-up` GET+POST → 404 flood | The **console gateway** returned 404 for BOTH "instance deleted" and "instance exists but not running" (restart/crash/OOM). xHTTP clients treat 404 as "endpoint gone" and reconnect with zero backoff | `_resolve_endpoint` now distinguishes the two: stopped → **503 + Retry-After: 5** (and WS close **1013**), unknown → 404 (WS **1008**). Non-browser clients get a ~30-byte body instead of the HTML page |
| WebSocket count grows, never drains | Relay tasks were `cancel()`ed but **never awaited** (half-open upstreams); upstream websockets had `max_size=None` and no write bound; worker built a **fresh httpx.AsyncClient per proxied request**, whose BackgroundTask cleanup does NOT run on client disconnect → every aborted stream leaked a client+pool (unbounded RSS) | Pending relay tasks awaited (`gather(return_exceptions=True)`); upstream sockets get `max_size`/`max_queue`/`write_limit` + every relay send wrapped in `asyncio.wait_for` (websockets 16 has no `write_timeout` kwarg — that regression was caught by the passthrough suite); worker uses **one shared bounded client per Core port**; response streaming wrapped in try/finally generators so `aclose()` ALWAYS runs |
| CPU heat | accept→resolve→close cycles thousands/min during the storm | uvicorn `limit_concurrency` (default 512, `EMUNEL_LIMIT_CONCURRENCY`), WS cap, tiny 404/503 bodies, negative endpoint cache unchanged (4 s) |
| OOM kills the UI with the engine | Everything shared one process; the memory guard only trimmed caches | **Emergency engine shed**: after `EMUNEL_MEM_EMERGENCY_STOPS` (2) consecutive hard RSS ticks the engines layer stops itself, pipelines go empty, the panel (UI+API) keeps serving; re-enable from Engine Settings. Cores were already separate processes |
| No visibility | — | 30 s monitor line: console `mem: rss=…MB state=… asyncio_tasks=… gc=(…) ws_conns=…`; worker `monitor: rss=… fds=… tasks=… ws_active=… core_clients=…` |

## The operational root cause (no code can fix it)

Every Railway redeploy on an **ephemeral filesystem wipes the SQLite DB** →
all endpoint tokens vanish → every client with a saved config hammers the
domain with 404s. The panel now survives this gracefully; making it stop
entirely requires the **Volume at `/data`** (see RAILWAY.md).

## Storage self-check (every deploy)

`main.py` prints one loud line at boot so a missing or mis-mounted volume
is visible in the deploy logs immediately — the exact failure that wiped
the DB and triggered the storm above. Detection uses Railway's
auto-injected `RAILWAY_VOLUME_MOUNT_PATH` (present whenever a volume is
attached) plus a POSIX mountpoint check on `/data`; the check never raises
and never blocks boot. Log signatures:

```
[emunel] storage : volume attached at /data — state persists across redeploys
[emunel] storage : WARNING — no volume at /data; … railway volume add -m /data …
[emunel] storage : WARNING — volume mounted at /app/data but EMUNEL writes state under /data; …
```

Note: volumes cannot be declared in `railway.json` (Railway's config-as-code
schema has no volume field) — attach once via CLI/UI and every subsequent
deploy re-mounts it automatically.

## New knobs

`EMUNEL_WS_MAX_CONNECTIONS` (60) · `EMUNEL_WS_MAX_FRAME_BYTES` (4 MiB) ·
`EMUNEL_WS_WRITE_TIMEOUT` (10 s) · `EMUNEL_ENGINE_BACKPRESSURE_TIMEOUT` (15 s) ·
`EMUNEL_LIMIT_CONCURRENCY` (512, 0=off) · `EMUNEL_MONITOR_SEC` (30) ·
`EMUNEL_MEM_EMERGENCY_STOPS` (2)

Tests: `tests/test_ws_storm_fixes.py` (22) — statuses/close codes, cap,
relay teardown, shared clients, abort-cleanup, backpressure timeout,
zero-frame feedback skip, emergency shed + panel restart, monitor line.
Full suite: 320 passed + 13 subtests (was 298+13). Core/ and the panel UI
files are untouched (git diff empty on both).
