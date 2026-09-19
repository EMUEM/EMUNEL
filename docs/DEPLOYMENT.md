# EMUNEL Deployment

## 1. Docker (recommended)

```bash
git clone https://github.com/mehdialadi-star/EMUNEL.git
cd EMUNEL
cp .env.example .env
# EDIT .env — the service refuses to boot in production mode with
# placeholder secrets (EMUNEL_SECRET_KEY / EMUNEL_JWT_SECRET_KEY must be
# 32+ random chars; EMUNEL_ADMIN_PASSWORD 12+ chars).
docker compose -f deploy/docker-compose.yml up -d --build
```

- Console + API: `http://<host>:8080` (dashboard at `/`, docs at `/api/docs`)
- Persistent state: the `emunel-data` volume holds the DB (SQLite default) and
  every instance's Core state — quotas, links and counters survive restarts.
- Health: `GET /health` (liveness), `GET /ready` (readiness: DB reachable).
  The container has a Docker healthcheck wired to `/health`.

### PostgreSQL (optional)

1. Uncomment the `db` service in `deploy/docker-compose.yml`.
2. Set `DATABASE_URL=postgresql+asyncpg://emunel:<password>@db:5432/emunel` in `.env`.

Tables are created automatically on first boot (create-all). The platform runs
identically on SQLite for small deployments.

## 2. Local / bare-metal

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # set EMUNEL_DEBUG=true for local development
python main.py
```

Requirements: Python 3.11+ (3.12 recommended). No other services are needed —
the unified process runs the console API, the instance manager, and the Core
subprocesses it spawns.

## 3. Reverse proxy (public traffic)

Instance Cores bind loopback only. Expose them through your edge (Caddy/Nginx/
Cloudflare) on one hostname per instance or one hostname with distinct paths,
and set the instance's **public host** field so share links render correctly.
A typical Caddy site for one instance:

```
proxy.example.com {
    reverse_proxy 127.0.0.1:18100
}
```

TLS is terminated at the edge — the Core's WebSocket and xHTTP transports are
built for that topology (share links always carry `security=tls`).

## 4. VMess (optional)

VMess requires an explicitly installed Xray binary with a pinned digest. The
Core never downloads binaries. Set `EMUNEL_XRAY_BINARY` and
`EMUNEL_XRAY_SHA256` for the Core subprocess environment; per-link Xray
runtimes bind loopback only and are stopped after the last relay.

## 5. First boot checklist

1. Sign in with the seeded admin account (`EMUNEL_ADMIN_*`) and **change the password**.
2. Create an instance (choose protocols — only valid combinations are offered).
3. Create a user + subscription bound to that instance (quota, expiry).
4. Copy the subscription URL (`/sub/<token>`) or the share links into your client.
5. Watch real traffic appear on the dashboard within one sync interval (10 s).

## 6. Operations

- **Logs** — UI → Logs (audit trail + per-instance Core logs), or `docker logs emunel`.
- **Restart safety** — instances are relaunched automatically after a process
  or container restart, with identical credentials; the port allocator
  bind-probes to avoid double assignment and verifies ownership via the
  management token before declaring an instance healthy.
- **Backups** — back up the `/data` volume (SQLite + per-instance state).
- **Upgrades** — pull the new image and restart; DB schema is created
  idempotently and instance state files are forward-compatible JSON.

## 7. Security notes

- Management API of every Core is bearer-token protected; tokens are stored in
  the console DB and never returned by any API.
- Secret redaction runs in every Core's logging pipeline.
- Diagnostics are on-demand, rate limited, and concurrency bounded.
- In production mode (`EMUNEL_DEBUG=false`), startup validates secrets, CORS
  origins and the JWT algorithm before accepting traffic.
