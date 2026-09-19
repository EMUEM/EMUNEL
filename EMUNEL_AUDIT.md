# EMUNEL Audit

**Audited revision:** `a60a863` plus the production-hardening tranche in the working tree.  
**Reference:** Lunel `3e0b467`, inspected from source rather than README claims.

## Executive conclusion

EMUNEL was an incomplete scaffold rather than a deployable product. Before this tranche, the API could not import because middleware, authentication services, and three routers were absent. The raw Core accepted unknown VLESS and Trojan identities because an empty quota cache returned **allowed**, creating an unauthenticated arbitrary TCP relay. The repository also lacked a frontend, Worker, deployment stack, migrations, and tests.

The implementation direction is therefore **stabilization before feature expansion**. Lunel’s explicit credential registry, protocol-specific transport matrix, persistent state boundary, quota gate, worker isolation, and real health/deployment topology are the authoritative patterns. EMUNEL should integrate those seams rather than replace them with a second networking implementation.

## Completed in this tranche

The FastAPI application is now importable. Missing authentication helpers and middleware exist, and the missing diagnostics, logs, and settings routers are registered with honest behavior. JWT authentication validates token type, expiry, signature, account state, and role. Passwords use bcrypt through Passlib. The request middleware adds a correlation identifier without pretending to authenticate requests; authentication remains explicit at route dependencies.

Startup now validates production configuration before the database is opened. Placeholder secrets, short secrets, weak administrator passwords, wildcard credentialed CORS, and unsupported JWT algorithms are rejected when debug mode is disabled. Secret material is not returned by the settings endpoint.

The Core now fails closed for unknown credentials. A credential must be explicitly loaded into the quota manager before outbound traffic is allowed. Disabled, expired, and exhausted credentials are rejected. The configured maximum connection count is enforced with an admission semaphore. These changes intentionally make the current Core unusable until a real control-plane synchronization path is implemented; that is safer than preserving the previous open-proxy behavior.

A tracked baseline test suite now covers API importability, route registration, protocol detection, unknown-credential denial, quota enforcement, and disabled credentials.

## Incomplete

EMUNEL still does not have Lunel’s full Console, Worker, instance lifecycle, authenticated Core management API, persistent link registry, protocol-specific WebSocket/xHTTP transports, subscription feed, traffic event pipeline, monitoring dashboard, or deployment stack. The new TCP diagnostics endpoint is on-demand and bounded, but it is not a complete DNS/TLS/WebSocket/gRPC/XHTTP diagnostic subsystem.

API-key issuance remains ephemeral because no API-key table or revocation model exists. It must not be advertised as a durable credential until persistence, hashing, rotation, and revocation are implemented. The database still uses create-all startup initialization rather than versioned migrations, and traffic counters remain 32-bit integers in the existing schema.

## Broken or unsafe areas still requiring work

The raw EMUNEL relay handlers are not protocol-complete. They use heuristic protocol detection, single-read handshake parsing, incomplete Trojan credential validation, a plaintext Shadowsocks stub, and TCP substitution for UDP. Generated links do not correspond to a working WebSocket listener. These handlers should not be exposed publicly.

The control plane has no mechanism to load subscription credentials into the Core. Consequently, the fail-closed Core behavior currently blocks all credentials unless an integration explicitly loads them. This is an intentional safety state, not a completed subscription system.

There is no real Worker isolation or durable deployment reconciliation. There is no egress policy preventing connections to private or metadata addresses. The system still needs trusted-proxy handling, request rate limiting, structured audit persistence, secret encryption, log redaction, and dependency/version lock discipline.

## Recommended implementation order

1. Port or directly reuse Lunel Core’s tested relay, state, link, quota, and management seams. Do not extend the current raw relay heuristics.
2. Add a versioned migration system and 64-bit traffic counters. Introduce persisted credentials that map users and subscriptions to exact protocol/transport combinations.
3. Build the Worker boundary with per-instance identities, Docker isolation as the production default, health heartbeats, reconciliation after restart, and bounded port allocation.
4. Add a real subscription feed and synchronized credential lifecycle. Quota and expiry changes must revoke active sessions and be reflected in the Core.
5. Add a real console/dashboard backed only by persisted API/Core/Worker data. Missing data must render as degraded or empty, never simulated values.
6. Add a deployment smoke test, protocol interoperability tests with fragmented handshakes, authorization tests, quota race tests, and failure-injection tests for database, worker, node, and diagnostics failures.

## Quality gate

At the current checkpoint, the application imports, the new baseline tests pass, and unknown Core credentials are rejected. The project is **not yet production-ready** because the networking foundation and control-plane integration described above remain incomplete. This report must be updated after each implementation tranche rather than treating the scaffold’s README claims as delivered functionality.
