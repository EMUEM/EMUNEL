# EMUNEL Engines

A plugin layer that adds traffic engines to EMUNEL **without touching the
proxy Core or the existing APIs/UI**:

```
client ──▶ Console (public $PORT)
             └─ /i/<token>/... gateway hop ──[engine middleware]──▶ worker ──▶ Core ──▶ destination
                                          (frames + feeds)                (dial hook)
```

* **Console side** — `main.py` wraps the console app with an ASGI
  middleware: downlink WS frames are batched and flow through the frame
  pipeline; subscription feeds (`/i/<token>/sub`) are rewritten by the
  configgen engines; non-browser probes get an nginx-style page.
* **Core side** — when a core-host engine is active, the worker launches
  the Core through `python -m engines.core_host`, which serves the
  UNMODIFIED Core app plus one dial hook (warm pool + measurement). No
  file under `core/emunel_core/` changes, ever.
* **Never-crash contract** — an engine that raises is bypassed for the
  batch; after `EMUNEL_ENGINE_BYPASS_ERRORS` consecutive failures it is
  disabled for `EMUNEL_ENGINE_BYPASS_COOLDOWN_SEC`. With
  `EMUNEL_ENGINES_ENABLED=0` (or an empty pipeline) the wrapper is not
  even installed and the platform behaves bit-for-bit as before
  (`tests/test_engines_passthrough.py` proves it on every run).

## Enabling / disabling

| How | Effect |
|---|---|
| Panel → **Engine Settings** (admin) | hot enable/disable per engine, live status, metrics, logs — **choices persist across restarts** in the engine state store |
| `EMUNEL_ENGINE_<NAME>_ENABLED=1/0` | boot-time default; an explicit `0` is a kill-switch the panel cannot override (and beats a persisted panel enable) |
| `EMUNEL_PIPELINE_ORDER=Coalesce,Morph,...` | execution order; engines not listed are inactive |
| `EMUNEL_ENGINES_ENABLED=0` | whole layer off — zero overhead, identical behaviour |

Panel toggles are recorded under the `EngineToggles` key of the state
store (`/data/engines/state.json`): an enable is re-applied with
`force=True` on the next boot, a disable beats an on-by-default engine,
and the env kill-switch always wins. On the Engine Settings page engines
are grouped by where they run — this deployment, inside each Core, or
off/needs-configuration — so an inactive engine always says why.

Engine names: `Coalesce, Morph, Compress, PreConnect, FEC, Congestion,
SessionResumption, FakeHandshake, SplitTunnel, SNIRotation,
DomainFronting, PortHopping, SNISpoof, Reality`. Morph ships ON: its
default "baseline" profile is a byte-exact passthrough.

## The engines — what each one REALLY does

| Engine | Default | Where | Real effect |
|---|---|---|---|
| **Coalesce** | on | console | merges small downlink WS frames (≤`MAX_COALESCE_SIZE`, `COALESCE_TIMEOUT_MS`): fewer frame headers, continuous-download look for DPI. Byte stream preserved exactly. |
| **Morph** | off | console | shapes downlink frame sizes/pacing per ISP profile; LinUCB bandit learns from real connection outcomes; per-ISP profiles from `EMUNEL_MORPH_PROFILES`; optional self-play. Padding is NOT possible on stream transports (documented no-op). |
| **Compress** | off | core | real zlib/brotli codec with the 5% minimum-saving rule and content sniffing. **Inactive with a stated reason**: client cores cannot decompress, so no hop can be safely compressed today; the codec is armed + tested for when a compression-capable transport exists. |
| **PreConnect** | on | core | warm TCP pool per recently dialed (host,port): repeat destinations skip TCP+DNS. Bounded (`PRECONNECT_POOL_SIZE`, TTL, max hosts). VMess runs in a pinned Xray subprocess and is not covered. |
| **FEC** | off | core | XOR erasure codec (k data + ratio·k parity, single-loss recovery per group), unit-tested. **Dormant**: every current transport is TCP — FEC pays only on lossy UDP channels. |
| **Congestion** | on | core | measures dial RTT + container retransmit ratio (/proc/net/snmp), adapts socket buffers/QUICKACK/NODELAY, emits a CC advisory. Kernel CC switching (BBR↔Cubic↔Vegas) is impossible unprivileged — stated, not faked. |
| **SessionResumption** | on | both | keep-alive pool + TLS session cache for engine-layer outbound calls (probes, checks). Client-edge TLS resumption is the platform edge's job (Railway/Caddy do it). |
| **FakeHandshake** | on | console | probes (non-browser UA) that hit unknown paths get a byte-exact nginx/apache 404 instead of the panel HTML. Browsers still get the panel. |
| **SplitTunnel** | on | console | injects Iran direct rules (`.ir`, banks, Aparat, Snapp, …) into singbox/clash subscription feeds: domestic traffic bypasses the tunnel (faster + saves quota). Raw base64 lists carry no routing — counted honestly, not faked. |
| **SNIRotation** | off | console | rotates the connect domain across `EMUNEL_SNI_DOMAINS` (≥2 domains attached to the deployment). Inactive with a reason otherwise. |
| **DomainFronting** | off | console | rewrites configs to SNI=domestic front + Host=real backend. Requires the self-hosted Caddy edge (see below). |
| **PortHopping** | off | console | rotates the connect port across `EMUNEL_PORT_HOPPING_PORTS`. Railway exposes one public port — inactive with a reason there. |
| **SNISpoof** | on | console | SNI-spoofing PROFILE GENERATOR + client helper distributor (see below). The spoofing itself runs on the user's device — a panel on Railway is already past the DPI. |
| **Reality** | on | console | X25519 keypairs, VLESS+REALITY inbound/outbound config generation (RAW/XHTTP/gRPC) and vless:// share links; optional pinned-Xray runtime (see below). |

Every inactive engine shows WHY in the panel (Engine Settings → reason line).

## Operator CLI

```bash
python engine_manager.py status    # env/activation matrix
python engine_manager.py doctor    # data dir, volume probe, pipeline sanity
python engine_manager.py selftest  # codec + pipeline unit checks
python engine_manager.py list      # registry
```

## Railway notes

* **Volume**: engine state (bandit models, counters, logs) lives in
  `/data/engines`. Attach a Railway volume mounted at `/data`
  (Settings → Volumes) or learned state resets on every redeploy — the
  panel shows a warning banner when that is detected. This is STORAGE
  persistence and is unrelated to user traffic quotas.
* **Resources**: engines are stdlib-only (brotli optional), bounded by
  env caps (`EMUNEL_ENGINE_MAX_OPS_PER_SEC`, buffer ceilings, pool
  sizes). No external database, no Redis, no background CPU burn.
* **PORT** is read from the environment everywhere; nothing is hardcoded.

## Bypass tab — SNI Spoofing + REALITY (how they really work)

The **Bypass** tab (admin only) manages two Iran-bypass engines. Both are
honest about what a server can and cannot do.

### SNI Spoofing — client side, panel-served

SNI spoofing executes on the CLIENT device, never on Railway — by the time
traffic reaches the panel it has already passed the censor. The panel is
the profile generator and helper distributor:

1. **Profile** (`/api/engines/sni/*`): method (`fragment` / `fake_sni` /
   `combined`), fragment strategy (`sni_split` / `half` / `multi` /
   `tls_record_frag`), inter-fragment delay, TTL trick value, fake SNI and
   the allowed-SNI pool. Persisted in engine state, editable from the
   Bypass tab.
2. **Helper** (Download button): a single-file stdlib-only Python script
   (`emunel_sni_helper.py`) that runs next to the proxy client. It listens
   locally (default `127.0.0.1:40443`), intercepts the ClientHello, sends a
   synthetic allowed-SNI hello on a low-TTL connection (dies mid-path; DPI
   sees it, the real server never does), fragments the real ClientHello per
   the strategy, then relays both directions. No raw sockets, no admin
   rights — `IP_TTL` on a normal TCP socket.
3. **Test** button proves the planner server-side (parse → plan → stream
   preservation) so a broken profile is caught before it ships to clients.

### REALITY — keys + configs always, optional pinned runtime

REALITY borrows a real target site's TLS handshake as camouflage. The panel
ALWAYS provides: X25519 keypair generation (key format matches
`xray x25519`), full inbound (server) + outbound (client) JSON for
RAW / XHTTP / gRPC, and an importable `vless://` link with
`security=reality&pbk=…&sid=…&spx=…`.

Optionally the engine can RUN the server side inside the container — one
VLESS+REALITY listener on `EMUNEL_REALITY_LISTEN_PORT` — under the same
provenance rule as the Core's VMess runtime:

```bash
# install an Xray release somewhere the container can read it, then set:
EMUNEL_XRAY_BINARY=/data/xray/xray          # absolute path
EMUNEL_XRAY_SHA256=<sha256 of the binary>   # digest pin
EMUNEL_REALITY_LISTEN_PORT=8443             # expose via a Railway TCP Proxy
```

The binary is NEVER downloaded by the panel; a wrong digest is refused.
Expose the port through Railway: Settings → Networking → TCP Proxy → target
port `8443`; clients connect to the host:port the TCP proxy gives you
(the generated links use `EMUNEL_REALITY_PUBLIC_PORT` when it differs
from the listen port). Iran guidance: prefer domestic heavy-traffic
targets (blubank.com, divar.ir, snapp.ir); avoid google/microsoft
(censor-monitored).

## Domain fronting on a self-hosted edge

1. Run a domestic server with the bundled Caddy template
   (`deploy/proxy/Caddyfile.template`) holding a cert for
   `EMUNEL_FRONTING_SNI_DOMAIN`.
2. Point that server's reverse_proxy at your EMUNEL public URL and let it
   route by Host header.
3. Set `EMUNEL_FRONTING_SNI_DOMAIN` + `EMUNEL_FRONTING_REAL_HOST` and
   enable the engine — generated configs get SNI=front, Host=real.

## Adding an engine

1. Create `engines/engines/your_engine.py` with a class extending
   `engines.base.Engine` (implement `init/process/stop` as needed).
2. Register it in `engines/engines/__init__.py`.
3. Add its enable flag to `ENGINE_FLAG_VARS` in `engines/config.py` and
   document the variables in `.env.example`.
4. It appears in the pipeline (`EMUNEL_PIPELINE_ORDER`), the Engine
   Settings page and the CLI automatically.
