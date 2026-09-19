# EMUNEL Performance Baseline

**Measurement date:** 2026-09-19  
**Environment:** sandbox Linux, Python 3.12, local SQLite, no Docker daemon.  
**Scope:** import/startup readiness and deterministic unit-level Core checks. Full network throughput and production database benchmarks are pending because the current repository has no Worker/deployment path.

## Current measurements

| Measurement | Result | Method | Interpretation |
|---|---:|---|---|
| API import and route assembly | Passed | `python` import of `api.emunel_api.main` | The application composition is now loadable. |
| Registered OpenAPI paths | 15 routes/groups | `app.openapi()` | This measures route registration, not endpoint correctness. |
| Core unknown-credential check | Passed | Async quota regression test | Unknown credentials fail closed before outbound connection. |
| Core quota boundary check | Passed | Async quota regression test | A loaded credential is denied once usage reaches its limit. |
| Core connection limit | Implemented | Semaphore admission in `ProxyServer` | Needs an integration benchmark with concurrent sockets. |
| API latency | Not measured | No stable running deployment or representative data set | Must be measured after control-plane and database integration. |
| Database query performance | Not measured | No migration/index baseline exists | Requires PostgreSQL schema and representative records. |
| Instance startup | Not measured | Worker/instance lifecycle is absent | Requires a real Worker driver and Core management API. |
| Network throughput | Not measured | Current relay is not production-safe or protocol-compatible | Must use real protocol clients and controlled echo targets. |

## Validation command

The reproducible local validation is:

```bash
python -m compileall -q api core main.py
pytest -q
```

The baseline suite covers API composition, protocol classification, fail-closed credential handling, quota enforcement, and disabled credentials. It does not substitute for real interoperability or throughput testing.

## Required next benchmark plan

After the Lunel Core and Worker seams are integrated, run each benchmark against an isolated test node and a controlled echo service. Record the environment, dataset size, concurrency, p50/p95/p99 latency, error rate, CPU, resident memory, and bytes per second. The dashboard initial-load benchmark must use persisted records rather than fabricated fixtures. Database measurements must compare indexed and unindexed paths for users, subscriptions, traffic events, instances, and logs. Instance startup must include cold start, restart reconciliation, and concurrent instance creation.

No performance claim should be presented in the UI until the value comes from one of these measurements or from persisted runtime telemetry.
