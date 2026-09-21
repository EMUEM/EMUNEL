# SNI Enhanced — Stateful DPI Evasion (isolated engine package)

Iranian DPI went **stateful** (full TLS re-assembly, per-flow tracking) — plain
fragmentation and TTL tricks alone stopped being enough. This package is the
advanced control plane for the next tier of SNI-spoofing technique, kept
strictly isolated from the proxy Core (Core files: zero changes).

**Language note:** the operator spec named the modules `enhanced_engine.js`,
`sni_pool.js`, `handshake_builder.js`, `injection.js`, `state_confusion.js`.
EMUNEL's runtime is Python (FastAPI on Railway) and the "no new dependencies"
rule rules out a Node sidecar, so the SAME modules live here as Python files
with the same names and responsibilities. Every technique, flag name and the
`ir_allowed_snis.json` list follow the spec verbatim.

## What runs where (honest scope)

```
Railway panel (this engine)         user's device (emunel_sni_enhanced_helper.py)
────────────────────────────         ───────────────────────────────────────────
profiles + ISP strategies      ───►  executes the plan on REAL connections
byte-level plan + proof        ───►  fragment / tlsrec / hostfakesplit fully
allowed-SNI pool (adaptive)   ───►  low-TTL decoys (userspace, no admin)
CDN/decoy-target scanner             raw-socket techniques only with admin
per-technique success rates           + fallback ladder (below)
```

Spoofing runs on the CLIENT — the panel is already past the censor. Nothing
here touches relay traffic: the engine declares zero pipeline kinds.

## Techniques (15)

| technique | what it does | userspace? |
|---|---|---|
| `fragment` | real ClientHello split per strategy | yes |
| `fake_sni` | decoy hello (allowed SNI) first | yes (low-TTL socket) |
| `combined` | decoy first, then fragmented real | yes |
| `hostfakesplit` | same-length random-SNI hello, then real | yes |
| `multisplit` | split with overlapping seq (`split-seqovl`, default 568) | raw* |
| `multidisorder` | out-of-order segments, decoy first (zapret-style) | raw* |
| `fakedsplit` | decoy + forward split | yes (degraded) |
| `fakeddisorder` | decoy + reverse-order split | yes (degraded) |
| `tlsrec` | handshake wrapped in two TLS records | yes |
| `oob` / `disoob` | TCP urgent byte mid-handshake | raw* |
| `wrong_seq` | decoy with wrong sequence numbers | raw* |
| `md5sig` | decoy with invalid TCP-MD5 signature | raw* |
| `syndata` | payload bytes inside SYN (state confusion) | raw* |
| `synack` | fake SYN-ACK from the client side | raw* |

\* raw = the TRUE form needs raw-socket injection (helper run as admin/root).
Without it the helper degrades to the userspace ladder (decoy + split) and
SAYS so — the connection never breaks because of the helper.

`fake-quic` from the spec is intentionally absent: UDP is forbidden by the
project's hard rules (TCP only).

## Fooling options

`md5sig` (default — strongest against stateful NAT), `badseq`, `badsum`,
`ts`, `autottl`. Applied to decoy packets; exact templates in
`state_confusion.py`.

## ISP strategies (`ISP_STRATEGIES.json`)

| key | preset |
|---|---|
| `auto` | combined + md5sig, repeats 6 (safe default) |
| `irancell_mci` | fake,multidisorder split-pos=1,midsld fooling=md5sig repeats=6 |
| `mokhaberat_shatel` | multisplit split-seqovl=568 split-pos=1 fooling=ts repeats=6 |
| `hard` | fake,fakedsplit fooling=ts repeats=6, pattern 0x00 |

Selectable per deployment from the panel (SNI Enhanced tab → ISP selector).

## Fallback ladder (never breaks the connection)

1. `SNIEnhanced` — enabled and profile valid
2. `SNISpoof` (basic engine) — enhanced disabled or erroring
3. direct connection — basic engine failing
4. Core default — everything failed (plain relay)

The helper implements the same ladder client-side; the status endpoint
reports the current position.

## Scanner (bounded — Railway free tier)

Parallel TCP+TLS probes of ≤32 targets (2.5s timeout), min 30s between
scans, 10-scan history. Proves targets are alive/latency/TLS-answering
from the panel's vantage — the Iran-side verdict is the client's.

## API (spec paths, `/enhanced` prefix because `/sni/status` + `/sni/config`
are already owned by the basic SNISpoof engine — a collision would break
the existing Bypass tab)

```
GET  /api/engines/sni/enhanced/status    profile + pool + rates + scanner + ladder
GET  /api/engines/sni/enhanced/snis      allowed-SNI pool (weighted, adaptive)
POST /api/engines/sni/enhanced/config    update profile / apply ISP strategy
POST /api/engines/sni/enhanced/scan      probe CDN/decoy targets (bounded)
GET  /api/engines/sni/enhanced/logs      recent engine log lines
POST /api/engines/sni/enhanced/test       server-side plan proof per technique
GET  /api/engines/sni/enhanced/helper    download the enhanced client helper
```

## Flags (all default OFF — deployment without them is byte-identical)

```
SNI_ENHANCED_ENABLED=false
SNI_ENHANCED_METHOD=combined
SNI_ENHANCED_FOOLING=md5sig
SNI_ENHANCED_MULTISPLIT_SEQOVL=568
SNI_ENHANCED_FRAGMENT_DELAY=0.15
SNI_ENHANCED_TTL_VALUE=4
SNI_ENHANCED_SCAN_INTERVAL_HOURS=6
SNI_ENHANCED_FINGERPRINT=chrome
SNI_ENHANCED_POOL=                     # csv; blank = ir_allowed_snis.json
```

(The spec's `SNI_METHOD` / `SNI_FRAGMENT_DELAY` / `SNI_TTL_*` names already
belong to the basic SNISpoof engine's knobs — changing their defaults would
change existing behavior, so this engine reads the `SNI_ENHANCED_*` namespace
with the spec's values.)

## Panel tab

"SNI Enhanced" appears (admin-only) once `SNI_ENHANCED_ENABLED=true` or the
engine was hot-enabled in Engine Settings: allowed-SNI list, scanner status
+ "scan new IPs" button, per-technique success rates, manual ISP selector.

## Files

```
engines/sni_enhanced/
├── enhanced_engine.py                  # engine (control plane)
├── sni_pool.py                         # weighted allowed-SNI pool
├── handshake_builder.py                # fingerprinted decoy hellos
├── injection.py                        # technique planner + proof
├── state_confusion.py                  # syndata/synack packet templates
├── scanner.py                          # CDN/decoy-target scanner
├── ir_allowed_snis.json                # allowed SNI list (spec, verbatim)
├── ISP_STRATEGIES.json                 # per-ISP presets
├── emunel_sni_enhanced_helper.py       # standalone stdlib client helper
└── README.md
```
