# ⚡ EMUNEL

```
███████╗███╗   ███╗██╗   ██╗███╗   ██╗███████╗██╗
██╔════╝████╗ ████║██║   ██║████╗  ██║██╔════╝██║
█████╗  ██╔████╔██║██║   ██║██╔██╗ ██║█████╗  ██║
██╔══╝  ██║╚██╔╝██║██║   ██║██║╚██╗██║██╔══╝  ██║
███████╗██║ ╚═╝ ██║╚██████╔╝██║ ╚████║███████║███████╗
╚══════╝╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝╚══════╝
```

> **Production-ready proxy management platform** — an evolution of the
> [Lunel](https://github.com/ArasTey/lunel) architecture with the proven
> networking core preserved verbatim, plus subscription management, real
> monitoring, real diagnostics and a fast glass console UI.

The networking engine is Lunel's battle-tested relay (VLESS / Trojan /
Shadowsocks / VMess over WebSocket + xHTTP) ported wire-compatibly —
fail-closed credentials, EWMA batched quota accounting, per-instance isolation.
EMUNEL adds the management layer on top.

---

## What works (and is tested)

- **Multi-protocol core** — VLESS, Trojan, Shadowsocks AEAD over WebSocket;
  VLESS/Trojan over xHTTP (packet-up & stream-up); VMess via pinned Xray (opt-in)
- **Instance architecture** — every instance is an isolated Core subprocess:
  own port, own state file, own management token, own protocols, own quota
  budget. Restart-safe with automatic recovery.
- **Subscriptions** — traffic quotas (GB / unlimited), expiry presets
  (1/7/30/60/90/custom days), extend, renew, revoke, reset, monthly resets,
  device counting — enforced fail-closed by the Core, never bypassed.
- **Traffic accounting** — batched at the Core (EWMA), synced to the console
  at low frequency, visible per user / subscription / instance / link.
- **Dashboard** — 11 sections, every number from live backend data. Empty
  states when there is nothing to show; never simulated values.
- **Diagnostics** — real, measured DNS / TCP / TLS / SNI / HTTP / WebSocket /
  xHTTP probes with labelled latency types, on-demand, rate limited.
- **Health system** — liveness, readiness, database, instances, manager, sync
  reported independently; failures degrade, never cascade.
- **UI** — dark glass design (restrained blur), EN + فارسی with full RTL,
  mobile-first responsive, PWA manifest, no build step, no framework.
- **Security** — JWT auth, RBAC, bcrypt (with >72-byte pre-hashing), startup
  secret validation, secret redaction in every Core log, tokens never in API
  responses.

## Quick start

```bash
git clone https://github.com/mehdialadi-star/EMUNEL.git
cd EMUNEL
docker compose -f deploy/docker-compose.yml up -d --build
```

With no `EMUNEL_*` secrets configured, strong ones are generated on first
boot and persisted under the data volume; the initial admin password is
printed once in the logs. Or set them explicitly via `.env`
(`cp .env.example .env`).

**Railway:** deploy the GitHub repo directly — the root `Dockerfile` +
`railway.json` make it zero-config (see [docs/RAILWAY.md](docs/RAILWAY.md)).

Then open `http://localhost:8080` → sign in with the seeded admin → create an
instance → create a subscription → copy the `/sub/<token>` URL into your
client. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

Local development:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
EMUNEL_DEBUG=true python main.py
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — components, wire surface, security invariants, sync model
- [Deployment](docs/DEPLOYMENT.md) — Docker, PostgreSQL, reverse proxy, VMess, operations
- [Railway](docs/RAILWAY.md) — one-click cloud deploy, zero-config secrets, volume setup
- [Audit](EMUNEL_AUDIT.md) — reference comparison and honest state assessment
- [Performance](PERFORMANCE.md) — measured benchmarks

## Tests

```bash
python -m pytest tests/ -q
```

The suite covers wire-level protocol round-trips (headers, AEAD streams,
share links), real end-to-end tunnels through a live Core (VLESS relay, quota
cut-off, fail-closed rejects), and the full platform integration: instance
launch → subscription provisioning → real traffic → counter sync-back →
revocation.

## Project structure

```
EMUNEL/
├── core/emunel_core/     # the networking engine (ported from Lunel)
│   ├── relay/            # vless, trojan, shadowsocks, vmess, xhttp + base
│   ├── state.py          # LinkStore / ConnectionTracker / RuntimeStats / StateStore
│   └── links.py          # share-link + subscription payload generation
├── api/emunel_api/       # console API
│   ├── models/           # user, node, instance+link, subscription, audit
│   ├── routers/v1/       # 13 routers (instances, subscriptions, traffic, ...)
│   └── services/         # instance_manager, core_client, link_sync,
│                         # diagnostics, health
├── dashboard/            # SPA (vanilla ES modules, no build step)
├── deploy/               # Dockerfile + docker-compose
├── docs/                 # architecture + deployment
└── tests/                # protocol, e2e core, platform integration
```

## License

MIT — same as the Lunel reference.
