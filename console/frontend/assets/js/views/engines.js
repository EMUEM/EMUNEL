import { api, toast, esc } from "../api.js";

function engineChip(e) {
  if (e.active) return `<span class="chip" style="color:var(--green)">● Active</span>`;
  if (e.reason && e.reason.startsWith("disabled by env"))
    return `<span class="chip" style="color:var(--red)">● Env-off</span>`;
  return `<span class="chip">○ Inactive</span>`;
}

function metricRows(e) {
  const m = e.metrics || {};
  const keys = Object.keys(m).filter(
    (k) => typeof m[k] === "number" || typeof m[k] === "string"
  );
  if (!keys.length) return `<div class="faint" style="font-size:12px">No metrics yet.</div>`;
  return keys.slice(0, 8).map((k) => {
    let v = m[k];
    if (typeof v === "number" && v > 9999) v = v.toLocaleString();
    return `<div class="kv"><span>${esc(k)}</span><code>${esc(String(v))}</code></div>`;
  }).join("");
}

function paramRows(e) {
  const p = e.params || {};
  const keys = Object.keys(p);
  if (!keys.length) return "";
  return keys.slice(0, 6).map((k) => `<div class="kv"><span>${esc(k)}</span><code>${esc(String(p[k]))}</code></div>`).join("");
}

export default {
  async render(root) {
    root.innerHTML = `
      <div class="page-head"><div><h1>Engine Settings</h1>
        <div class="sub">Traffic engines — plugin layer over the gateway hop, subscription feeds and Core dials. The proxy Core itself is never modified.</div></div>
        <button class="btn sm" id="eg-refresh">Refresh</button></div>
      <div id="eg-sys"></div>
      <div class="stat-grid" id="eg-stats"></div>
      <div id="eg-cards"></div>
      <div class="card" id="eg-cores" style="margin-top:14px"></div>`;

    const sysEl = root.querySelector("#eg-sys");
    const statsEl = root.querySelector("#eg-stats");
    const cardsEl = root.querySelector("#eg-cards");
    const coresEl = root.querySelector("#eg-cores");

    async function load() {
      let data;
      try {
        data = await api.get("/api/engines");
      } catch (err) {
        if (err.status === 403) { location.hash = "#/"; return; }
        sysEl.innerHTML = `<div class="card"><p class="muted">Engines API unavailable: ${esc(err.message)}</p></div>`;
        return;
      }
      const active = (data.engines || []).filter((e) => e.active).length;
      sysEl.innerHTML = data.volume_warning
        ? `<div class="card" style="border-color:var(--amber,#e0b34a);margin-bottom:12px">
             <h3 style="margin:0 0 6px">⚠ Engine state is not persisting</h3>
             <p class="muted" style="margin:0">${esc(data.volume_warning)}</p></div>`
        : "";
      statsEl.innerHTML = `
        <div class="stat"><div class="label">Engines active</div><div class="value">${active} / ${(data.engines || []).length}</div></div>
        <div class="stat"><div class="label">Pipeline order</div><div class="value" style="font-size:14px">${esc((data.pipeline_order || []).join(" → "))}</div></div>
        <div class="stat"><div class="label">Engine data</div><div class="value" style="font-size:14px">${esc(data.data_dir || "")}</div></div>
        <div class="stat"><div class="label">Uptime</div><div class="value">${Math.floor((data.uptime || 0) / 60)}m</div></div>`;

      cardsEl.innerHTML = `<div class="cards">${(data.engines || []).map((e) => `
        <div class="card" data-name="${esc(e.name)}" style="display:flex;flex-direction:column;gap:10px">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
            <div><strong>${esc(e.name)}</strong>
              <div class="faint" style="font-size:12px">${esc(e.title)}</div></div>
            ${engineChip(e)}
          </div>
          ${e.reason ? `<div class="faint" style="font-size:12px;border-left:2px solid var(--line,#232a36);padding-left:8px">${esc(e.reason)}</div>` : ""}
          <div>${paramRows(e)}</div>
          <div style="border-top:1px solid var(--line,#232a36);padding-top:8px">${metricRows(e)}</div>
          <div class="row" style="gap:6px;margin-top:auto">
            <button class="btn sm" data-act="${e.active ? "disable" : "enable"}">${e.active ? "Disable" : "Enable"}</button>
            <button class="btn sm ghost" data-act="logs">Logs</button>
          </div>
        </div>`).join("")}</div>`;

      const cores = data.cores || [];
      coresEl.innerHTML = `
        <h3 style="margin:0 0 8px">Core-side engines (per running instance)</h3>
        ${cores.length === 0
          ? `<p class="muted">No running instance reported core-side engine status yet (embedded cores report through the worker proxy).</p>`
          : cores.map((c) => `
            <div style="padding:8px 0;border-top:1px solid var(--line,#232a36)">
              <strong>${esc(c.instance_name || c.instance_id)}</strong>
              ${(c.engines || []).filter((e) => e.active).map((e) => `<span class="chip" style="margin-left:6px">${esc(e.name)}</span>`).join("")
                || `<span class="faint" style="font-size:12px">none active</span>`}
            </div>`).join("")}`;

      cardsEl.querySelectorAll("[data-act]").forEach((btn) =>
        btn.addEventListener("click", async () => {
          const card = btn.closest("[data-name]");
          const name = card.dataset.name;
          const act = btn.dataset.act;
          try {
            if (act === "logs") {
              const { logs } = await api.get(`/api/engines/logs?name=${encodeURIComponent(name)}`);
              toast(logs && logs.length ? `${logs.length} recent lines logged to console`
                : "No engine logs yet", "ok");
              if (logs && logs.length) console.log(`[engines:${name}]\n` + logs.join("\n"));
              return;
            }
            await api.post(`/api/engines/${encodeURIComponent(name)}/${act}`);
            toast(`${name} ${act === "enable" ? "enabled" : "disabled"}`, "ok");
            load();
          } catch (err) {
            toast(err.message, "err");
          }
        })
      );
    }

    root.querySelector("#eg-refresh").addEventListener("click", load);
    load();
    const timer = setInterval(load, 20000);
    return () => clearInterval(timer);
  },
};
