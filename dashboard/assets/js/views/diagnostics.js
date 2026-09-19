/* Network diagnostics view — real measurements, labelled latency types,
   on-demand only. Never simulated. */
import { api, fmtMs, esc } from "../api.js";
import { t } from "../i18n.js";
import { toast } from "../components.js";

export async function mount(el) {
  el.innerHTML = `
    <div class="card" style="margin-bottom:14px">
      <p class="muted" style="margin:0 0 4px;font-size:13px">${t("diag.intro")}</p>
      <p class="muted" style="margin:0;font-size:12px">${t("diag.limits")}</p>
    </div>

    <div class="card">
      <h3>${t("diag.title")}</h3>
      <div class="row">
        <label class="field"><span>${t("diag.target")}</span><input id="dg-host" value="localhost" /></label>
        <label class="field" style="max-width:110px"><span>${t("diag.port")}</span><input id="dg-port" type="number" value="443" /></label>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn" data-test="dns">${t("diag.dns")}</button>
        <button class="btn" data-test="tcp">${t("diag.tcp")}</button>
        <button class="btn" data-test="tls">${t("diag.tls")}</button>
        <button class="btn" data-test="http">${t("diag.http")}</button>
      </div>
      <div class="row" style="margin-top:10px">
        <label class="field" style="flex:2"><span>WebSocket URL</span><input id="dg-ws" placeholder="ws://host:port/path" /></label>
        <label class="field"><span>xHTTP base URL</span><input id="dg-xhttp" placeholder="https://host" /></label>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn" data-test="ws">${t("diag.ws")}</button>
        <button class="btn" data-test="xhttp">${t("diag.xhttp")}</button>
        <button class="btn primary" data-test="chain">${t("diag.chain")}</button>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <h3>${t("diag.result")}</h3>
      <div id="dg-out"><p class="muted" style="font-size:13px">—</p></div>
    </div>`;

  el.querySelectorAll("[data-test]").forEach((btn) => {
    btn.addEventListener("click", () => runTest(el, btn.dataset.test));
  });
}

async function runTest(el, kind) {
  const out = el.querySelector("#dg-out");
  const host = el.querySelector("#dg-host").value.trim();
  const port = parseInt(el.querySelector("#dg-port").value) || 443;
  const wsUrl = el.querySelector("#dg-ws").value.trim();
  const xhttpBase = el.querySelector("#dg-xhttp").value.trim();

  const map = {
    dns: () => api.post("/network-tests/dns", { host, port }),
    tcp: () => api.post("/network-tests/tcp", { host, port }),
    tls: () => api.post("/network-tests/tls", { host, port }),
    http: () => api.post("/network-tests/http", { url: `http://${host}:${port}` , method: "HEAD" }),
    ws: () => api.post("/network-tests/websocket", { url: wsUrl || `ws://${host}:${port}` }),
    xhttp: () => api.post("/network-tests/xhttp", { base_url: xhttpBase || `http://${host}:${port}` }),
    chain: () => api.post("/network-tests/chain", { ws_url: wsUrl || `ws://${host}:${port}` }),
  };
  if (!map[kind]) return;
  if ((kind === "ws" || kind === "chain") && !wsUrl && !host) { toast("host?", "err"); return; }

  out.innerHTML = `<div class="spinner"></div>`;
  try {
    const res = await map[kind]();
    out.innerHTML = kind === "chain" ? renderChain(res) : renderProbe(res);
  } catch (e) {
    out.innerHTML = `<p style="color:var(--err)">${esc(e.message)}</p>`;
  }
}

function renderProbe(p) {
  const rows = [
    ["✓/✕", p.ok ? "✓" : "✕"],
    [t("diag.latency"), fmtMs(p.latency_ms)],
    ...Object.entries(p.detail || {}).map(([k, v]) => [k, esc(typeof v === "object" ? JSON.stringify(v) : v)]),
  ];
  if (p.error) rows.push(["error", esc(p.error)]);
  return `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
}

function renderChain(res) {
  const stageName = {
    dns: t("diag.dns"), tcp: t("diag.tcp"), tls: t("diag.tls"),
    websocket: t("diag.wsTunnelLatency"),
  };
  return `
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
      <span class="badge ${res.ok ? "ok" : "err"}">${res.ok ? "OK" : "FAIL"}</span>
      <span class="badge dim mono">${esc(res.target || "")}</span>
    </div>
    <div class="table-wrap"><table class="tbl">
      <thead><tr><th>Stage</th><th>${t("common.status")}</th><th>${t("diag.latency")}</th><th>Detail</th></tr></thead>
      <tbody>
        ${(res.stages || []).map((s) => `
          <tr>
            <td><b>${esc(stageName[s.name] || s.name)}</b></td>
            <td>${s.ok ? "✓" : "✕"}</td>
            <td class="mono">${fmtMs(s.latency_ms)}</td>
            <td class="muted" style="font-size:12px">${esc(s.error || brief(s))}</td>
          </tr>`).join("")}
      </tbody>
    </table></div>`;
}

function brief(s) {
  if (!s.detail) return "";
  const d = s.detail;
  if (d.addresses) return `${d.count} addr`;
  if (d.tls_version) return `${d.tls_version} · ${d.cipher || ""}`;
  if (d.status_code) return `HTTP ${d.status_code}`;
  if (d.ping_rtt_ms != null) return `ping RTT ${fmtMs(d.ping_rtt_ms)}`;
  return Object.entries(d).slice(0, 2).map(([k, v]) => `${k}: ${v}`).join(" · ");
}
