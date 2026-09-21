# GeneticEngine (`engines/genetic/`)

A population of 20 **genomes** (complete protocol parameter sets) evolving
every 6 hours. Real outcomes from DpiMesh drive the fitness; ChaosProtocol
executes the winning genome.

## Genome (exactly the operator's spec)

```json
{
  "id": "g001",
  "padding": 12,
  "sni_strategy": "fragment",
  "transport": "ws",
  "cipher": "aes128gcm",
  "fingerprint": "chrome",
  "mtu": 1420,
  "rtt_delay": 5,
  "fec_ratio": 0.1,
  "compression": "brotli4",
  "fitness": 0.87,
  "generation": 3,
  "parent_ids": ["g004", "g007"]
}
```

## Algorithm (operator's spec)

| Stage | Rule |
|---|---|
| Population | 20 genomes (generation 0 seeded with `secrets`-grade randomness) |
| Selection | Tournament, size 3 |
| Crossover | Single-point over the parameter order |
| Mutation | 5 % per parameter (`secrets` — no `random` module anywhere) |
| Replacement | every 6 h: bottom 5 out, children of top 5 in |
| Fitness | from DpiMesh — see below |

## Fitness

```
fitness = 0.5 * successRate + 0.3 * invLatency + 0.2 * invJitter
invLatency = 1 / (1 + latency_ms / 100)
invJitter  = 1 / (1 + jitter_ms  / 20)
```

The spec's raw `1/latency` is unbounded (0.2 s latency → score 5); the
normalised inverse keeps fitness in [0, 1], matching the spec's own
example (fitness 0.87). The weights — 0.5 / 0.3 / 0.2 — are exactly as
specified.

## Budget

CPU: one short spike every 6 hours (a few ms of HMAC-free `secrets` draws).
RAM: the population is a few KB. Volume: `genetic.db` capped by
`GENETIC_MAX_DB_MB` (default 1MB; history keeps the last 200 generations).
On cap: degraded mode — population frozen read-only, the Core unaffected.

## Flags & env

```
GENETIC_ENGINE_ENABLED=false        # engine flag (default OFF)
GENETIC_POPULATION_SIZE=20          # population
GENETIC_MUTATION_RATE=0.05          # per-parameter mutation rate
GENETIC_EVOLVE_INTERVAL_SEC=21600   # 6h
GENETIC_MAX_DB_MB=1                 # volume guard
```

APIs: `GET /api/genetic/population`, `GET /api/genetic/status`,
`POST /api/genetic/evolve` (Force Evolution button).
