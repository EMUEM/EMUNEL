# DpiMesh (`engines/mesh/`)

Crowd-sourced DPI intelligence: panel users share what the ISP's DPI does
to each protocol shape, and the engine builds a **live map of the Iranian
DPI landscape** — one policy per (ISP, region).

## Pipeline (operator spec, implemented as-is)

1. Client → `POST /api/mesh/report` (~200-byte payload)
2. Signature stored in SQLite (`/data/engines/mesh.db` on the volume)
3. Aggregator sweeps every 5 minutes over the last hour
4. New policy derived per (isp, region)
5. Client → `GET /api/mesh/policy?isp=X&region=Y`

## Schema (exactly the operator's spec)

```
dpi_signatures(id, isp_hash, region_hash, time_bucket, protocol, transport,
               sni, result, latency, created_at)
dpi_policies(isp_hash, region_hash, recommended_protocol,
             recommended_params, confidence, updated_at)
```

## Differential privacy

* `isp_hash = SHA256(ISP + ":" + MESH_SALT)[:12]`
* `region_hash = SHA256(Region + ":" + MESH_SALT)[:16]`
* **No IP, no user ID, ever.** Raw values are hashed at ingest and never
  stored or logged; bus events carry hashes only.
* `MESH_SALT` lives in env — change it to re-anonymise the whole map.

## Budget (0.5GB-RAM-friendly)

| Guard | Default |
|---|---|
| Per-ISP-hash rate limit | 30 reports/min |
| Global rate limit | 600 reports/min |
| Signature retention | 24 h (pruned each sweep) |
| Volume cap | `MESH_MAX_DB_MB=10` → degraded mode |

**Degraded mode (volume full):** reports are answered with a fallback
marker instead of being stored, policies keep serving last-known-good, a
one-line warning is logged. The Core is never involved (Test 7).

## Flags & env

```
DPI_MESH_ENABLED=false              # engine flag (default OFF)
MESH_SALT=change_me_please          # privacy salt
MESH_AGGREGATE_INTERVAL_SEC=300     # aggregator sweep
MESH_RETENTION_HOURS=24             # signature retention
MESH_MAX_DB_MB=10                   # volume guard
```

APIs: `POST /api/mesh/report` (public, rate-limited),
`GET /api/mesh/policy?isp=&region=` (public). The map itself is shown in
the Evolution tab via `/api/synergy/status`.
