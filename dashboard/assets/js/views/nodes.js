/* Nodes view — deployment hosts, live probed. */
import { api, esc } from "../api.js";
import { t } from "../i18n.js";
import { loading, emptyState, statusBadge } from "../components.js";
import { poller } from "../app.js";

export async function mount(el) {
  el.innerHTML = loading();
  await refresh(el, true);
  return poller(() => refresh(el, false), 30000, { immediate: false });
}

async function refresh(el, first) {
  let data;
  try { data = await api.get("/nodes"); }
  catch (e) { if (first) { el.innerHTML = emptyState(); return; } return; }

  const nodes = data.nodes || [];
  if (!nodes.length) {
    el.innerHTML = emptyState(t("nodes.noNodesSub"));
    return;
  }
  el.innerHTML = `
    <div class="table-wrap"><table class="tbl">
      <thead><tr>
        <th>${t("common.name")}</th><th>Address</th><th>${t("common.status")}</th>
        <th>Region</th><th>${t("inst.connections")}</th>
      </tr></thead>
      <tbody>
        ${nodes.map((n) => `
          <tr>
            <td><b>${esc(n.name)}</b></td>
            <td class="mono">${esc(n.address)}:${n.port}</td>
            <td>${statusBadge(n.status)}</td>
            <td class="muted">${esc(n.region || n.country || "—")}</td>
            <td>${n.active_connections ?? 0}</td>
          </tr>`).join("")}
      </tbody>
    </table></div>`;
}
