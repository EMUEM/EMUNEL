/* Online connections view — live grouped-by-IP data from running Cores. */
import { api, fmtBytes, fmtDate, esc } from "../api.js";
import { t } from "../i18n.js";
import { loading, emptyState } from "../components.js";
import { poller } from "../app.js";

export async function mount(el) {
  el.innerHTML = loading();
  await refresh(el, true);
  return poller(() => refresh(el, false), 8000, { immediate: false });
}

async function refresh(el, first) {
  let data;
  try { data = await api.get("/connections"); }
  catch (e) { if (first) { el.innerHTML = emptyState(); return; } return; }

  const groups = data.connections || [];
  if (!groups.length) {
    el.innerHTML = emptyState(t("conn.noConnSub"));
    return;
  }

  el.innerHTML = `
    <div class="grid cols-4" style="margin-bottom:14px">
      ${stat(t("conn.sessions"), data.raw_count ?? 0)}
      ${stat(t("conn.ip"), groups.length)}
      ${stat(t("nav.instances"), data.instances_running ?? 0)}
      ${stat("Σ", fmtBytes(groups.reduce((s, g) => s + (g.bytes || 0), 0)))}
    </div>
    <div class="table-wrap"><table class="tbl">
      <thead><tr>
        <th>${t("conn.ip")}</th><th>${t("nav.instances")}</th><th>${t("conn.sessions")}</th>
        <th>${t("conn.bytes")}</th><th>${t("conn.transports")}</th><th>${t("conn.lastSeen")}</th>
      </tr></thead>
      <tbody>
        ${groups.map((g) => `
          <tr>
            <td class="mono">${esc(g.ip)}</td>
            <td>${esc(g.instance_name || "—")}</td>
            <td>${g.sessions}</td>
            <td>${fmtBytes(g.bytes)}</td>
            <td>${(g.transports || []).map((x) => `<span class="badge proto">${esc(x)}</span>`).join(" ")}</td>
            <td class="muted">${fmtDate(g.last_connected_at)}</td>
          </tr>`).join("")}
      </tbody>
    </table></div>`;
}

function stat(label, value) {
  return `<div class="card stat-card"><div class="stat-value">${value}</div><div class="stat-label">${label}</div></div>`;
}
