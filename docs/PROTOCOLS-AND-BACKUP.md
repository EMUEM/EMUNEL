# Additional protocols and configuration backups

## Trojan xHTTP

The creation wizard supports `trojan-xhttp-packet-up` and
`trojan-xhttp-stream-up`. Both use the existing Core `/txhttp-siz10` HTTP
transport, reached through the normal `/i/<token>/...` gateway. Links advertise
`alpn=h2,http/1.1`. Use a client/version that explicitly supports Trojan xHTTP.
The local tests verify independently encoded Trojan request bytes over HTTP
packet-up and stream-up against a loopback echo server; they are not Xray-client
or platform-edge interoperability tests.

Raw subscriptions retain xHTTP links. The current sing-box and Clash exporters
reject subscriptions containing xHTTP with HTTP 422 rather than mislabeling them
as WebSocket. This is an exporter limitation, not a blanket claim about all
client versions.

## VMess AEAD over WebSocket (operator-configured Xray)

Select `vmess-ws` in the wizard. EMUNEL provides a WebSocket gateway to a
per-link Xray subprocess. It does not implement VMess cryptography and does not
download an executable. The process driver inherits these environment values:

- `EMUNEL_XRAY_BINARY`: absolute path to an operator-installed Xray executable.
- `EMUNEL_XRAY_SHA256`: 64-character expected SHA-256 digest, checked before launch.
- `EMUNEL_XRAY_MAX_RUNTIMES`: maximum concurrent per-instance Xray processes
  (default 32). Each active VMess link uses one subprocess, shared by that link's
  concurrent connections. Budget memory/process limits accordingly.

Obtain an appropriate Xray build through a trusted release channel and verify
its provenance before configuring the digest. A digest calculated from an
untrusted download is not provenance verification. The executable must not be
group/world writable. For Docker workers, bake the verified executable into a
custom Core image and configure these variables within that image; the stock
image does not contain Xray. PaaS build images likewise need operator setup.

The external path is `/i/<token>/vmess-ws/<uuid>`. Xray binds loopback only;
platform TLS terminates at the normal gateway. Share links are VMess v2 JSON
with `aid=0`, `scy=auto`, `net=ws`, `tls=tls`, and `alpn=http/1.1`.

Missing runtime configuration fails link creation with HTTP 503 and marks new
provisioning failed rather than advertising a working instance. A runtime that
cannot start closes the WebSocket with an error. Tests verify configuration,
share links, binary pin checks and unavailable-runtime behavior. Real Xray AEAD
interoperability has not been verified in this workspace because no Xray binary
is installed. Counters measure encrypted WebSocket bytes, not plaintext payload.

## Admin → Backup

This is a **Console configuration backup**, not a disaster-recovery image of the
whole deployment. Export is plaintext JSON and contains sensitive password
hashes, Core API tokens, endpoint secrets and link identifiers. Store it encrypted
offline; never commit or publish it.

Included: users, instances, instance configurations, worker definitions, domains,
and instance-link records. Excluded: Core `state.json` files (including
Shadowsocks passwords and live usage/policies), worker `registry.json`, sessions,
OAuth states, deployment history, metrics and platform/provider credentials.
For full disaster recovery, separately preserve the persistent data volume and
operator configuration using platform snapshot facilities while the service is
quiescent. The new API does not automate that operation.

### Import workflow

1. Use a separately prepared target whose `EMUNEL_SECRET_KEY` matches the source.
   Never change a populated target's key without accounting for existing tokens.
2. Choose the JSON file and click **Validate**. Read warnings and conflicts.
3. Click **Import configuration** and confirm. Every collision rejects the entire
   import. No existing rows or sessions are deleted or overwritten.
4. Imported instances are stopped, domains inactive, workers disabled, and
   external provider/public host bindings cleared. Reconcile runtime state,
   worker registry and domain ownership before bringing them online. Merely
   restoring Console rows does not restore working proxy credentials.

The importer is intentionally insert-only. A backup of the currently populated
installation will conflict on that same installation. Even on a fresh boot,
bootstrap admin/worker identities may conflict; use a separately prepared clean
recovery target rather than deleting production data to make an import fit.

### API

All endpoints require an administrator session. POST requests also require
`X-EMUNEL-CSRF`.

- `GET /api/admin/backup`: capabilities and scope warnings.
- `POST /api/admin/backup/export`: JSON download, no request body.
- `POST /api/admin/backup/validate`: raw JSON preview; `can_restore` and conflicts.
- `POST /api/admin/backup/restore`: raw JSON plus
  `X-EMUNEL-Backup-Confirm: import`.

Maximum file size: 16 MiB; maximum configuration rows: 10,000. No multipart or
compressed input. Restores are atomic. SQLite round trips, rollback, concurrent
collision handling, authentication and input limits are covered with temporary
DB tests. PostgreSQL adapter tests use mocks, not a live PostgreSQL server.

## Verification

Tests are in `tests/test_protocol_http.py`, `tests/test_vmess_runtime.py`,
`tests/test_backup.py`, `tests/test_subscription.py`, and `tests/test_protocols.py`.
Use an environment with both Core and Console dependencies plus pytest.
No live deployments, remote clients, production restores or provider APIs were
used for verification.
