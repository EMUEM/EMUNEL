# Synergy (`engines/synergy/`)

The coordinator that closes the loop between the three evolution engines:

```
DpiMesh ──(real outcomes)──> GeneticEngine ──(best genome)──> ChaosProtocol
    ^                                                            │
    └───────────────(probe outcome via EventBus)────────────────┘
```

* **`synergy_manager.py`** — `SynergyManager.cycle()` runs the three steps
  every `SYNERGY_LOOP_SEC` (default 300 s). Each step is individually
  wrapped: an exception in one engine is recorded (`last_errors`) and the
  rest continue — one failing engine never stops the others.
* **`peers.py`** — the tiny registry engines use to find each other (the
  EngineManager keeps engines isolated by design; this is the opt-in
  rendezvous).
* **`engine.py`** — `SynergyEngine`, a zero-pipeline-kind engine so the
  loop gets the standard lifecycle, circuit-breaker isolation, hot
  toggles and status reporting for free.

## Standalone vs coordinated

* `SYNERGY_ENABLED=false` (default): the three engines still cooperate
  loosely over the EventBus — `mesh.policy` refreshes genome fitness,
  `genetic.generation` re-shapes Chaos's hint set, `chaos.outcome` lands
  in the mesh as a signature row.
* `SYNERGY_ENABLED=true`: adds the pull-based supervisor above — fresher
  fitness, explicit genome application, a probe cadence and a health
  report (`/api/synergy/status`).

## Flags & env

```
SYNERGY_ENABLED=false        # coordination loop flag (default OFF)
SYNERGY_LOOP_SEC=300         # cycle cadence
```

API: `GET /api/synergy/status` — flags, engine states, cycle health and
the DpiMesh map for the Evolution tab.
