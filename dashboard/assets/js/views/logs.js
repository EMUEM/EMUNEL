/* Logs view — audit trail + live Core logs per instance. */
import { api, fmtDate, esc } from "../api.js";
import { t } from "../i18n.js";
import { loading, emptyState } from "../components.js";

export async function mount(el) {
  el.innerHTML = loading();
  let instances, audit;
  try {
    [instances, audit] = await Promise.all([
      api.get("/instances"),
      api.get("/logs/audit?limit=100"),
    ]);
  } catch (e) { el.innerHTML = emptyState(); return; }

  const running = (instances.instances || []).filter((i) => i.status === "running");

  el.innerHTML = `
    <div class="card" style="margin-bottom:14px">
      <h3>${t("logs.core")}</h3>
      ${running.length
        ? `<div class="toolbar">
             <select id="log-inst">${running.map((i) => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join("")}</select>
             <button class="btn" id="log-load">${t("common.refresh")}</button>
           </div>
           <div id="core-log-out" class="mono" style="max-height:320px;overflow-y:auto"></div>`
        : `<p class="muted" style="font-size:13px">${t("logs.pickInstance")}</p>`}
    </div>
    <div class="card">
      <h3>${t("logs.audit")}</h3>
      <div id="audit-out"></div>
    </div>`;

  const renderAudit = (events) => {
    el.querySelector("#audit-out").innerHTML = events.length
      ? events.map((e) => `<div class="log-line"><span class="lvl INFO">${esc(e.action)}</span>
          <span class="muted">${esc(e.resource_type)}</span> ${esc(e.detail || "")}
          <span class="muted" style="float: inline-end">${fmtDate(e.created_at)}</span></div>`).join("")
      : `<p class="muted" style="font-size:13px">${t("logs.noLogs")}</p>`;
  };
  renderAudit(audit.events || []);

  const loadCore = async () => {
    const id = el.querySelector("#log-inst").value;
    const outBox = el.querySelector("#core-log-out");
    outBox.innerHTML = `<div class="spinner"></div>`;
    try {
      const data = await api.get(`/logs/instance/${id}?limit=200`);
      outBox.innerHTML = (data.logs || []).map((l) => {
        const lvl = String(l.level || "INFO").toUpperCase();
        return `<div class="log-line"><span class="lvl ${lvl}">${esc(lvl)}</span>
          <span class="muted">${new Date((l.ts || 0) * 1000).toISOString().slice(11, 19)}</span>
          ${esc(l.message)}</div>`;
      }).join("") || `<p class="muted">${t("logs.noLogs")}</p>`;
    } catch (e) { outBox.innerHTML = `<p style="color:var(--err)">${esc(e.message)}</p>`; }
  };
  el.querySelector("#log-load")?.addEventListener("click", loadCore);
  if (running.length) loadCore();
}
