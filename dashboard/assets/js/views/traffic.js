/* Traffic view — real per-instance / per-subscription / top-links data. */
import { api, fmtBytes, esc } from "../api.js";
import { t } from "../i18n.js";
import { loading, emptyState } from "../components.js";
import { poller } from "../app.js";

export async function mount(el) {
  el.innerHTML = loading();
  await refresh(el, true);
  return poller(() => refresh(el, false), 20000, { immediate: false });
}

async function refresh(el, first) {
  let byInst, bySub, top;
  try {
    [byInst, bySub, top] = await Promise.all([
      api.get("/traffic/by-instance"),
      api.get("/traffic/by-subscription?limit=30"),
      api.get("/traffic/top-links?limit=15"),
    ]);
  } catch (e) {
    if (first) { el.innerHTML = emptyState(); return; }
    return;
  }

  const instRows = (byInst.instances || []).map((i) => `
    <tr>
      <td><b>${esc(i.name)}</b></td>
      <td>${fmtBytes(i.used_bytes)}</td>
      <td>${i.link_count ?? 0}</td>
      <td>${i.active_connections ?? "—"}</td>
      <td class="muted">${Object.keys(i.hourly || {}).length ? `${Object.keys(i.hourly).length}h` : "—"}</td>
    </tr>`).join("");

  const subRows = (bySub.subscriptions || []).map((s) => {
    const pct = s.usage_percent;
    return `<tr>
      <td>${esc(s.name)}</td>
      <td>${fmtBytes(s.used_bytes)}</td>
      <td>${s.limit_bytes ? fmtBytes(s.limit_bytes) : t("common.unlimited")}</td>
      <td style="min-width:130px">${pct != null ? `<div class="progress"><i style="width:${Math.min(pct, 100)}%"></i></div>` : "—"}</td>
    </tr>`;
  }).join("");

  const topRows = (top.links || []).map((l, i) => `
    <tr>
      <td class="muted">${i + 1}</td>
      <td>${esc(l.label)}</td>
      <td><span class="badge proto">${esc(l.protocol)}</span></td>
      <td>${fmtBytes(l.used_bytes)}</td>
    </tr>`).join("");

  el.innerHTML = `
    <div class="grid cols-2">
      <div class="card"><h3>${t("traffic.byInstance")}</h3>
        <div class="table-wrap"><table class="tbl">
          <thead><tr><th>${t("common.name")}</th><th>${t("subs.used")}</th><th>${t("inst.links")}</th><th>${t("inst.connections")}</th><th>h</th></tr></thead>
          <tbody>${instRows || `<tr><td colspan="5" class="muted">${t("traffic.noData")}</td></tr>`}</tbody>
        </table></div>
      </div>
      <div class="card"><h3>${t("traffic.topLinks")}</h3>
        <div class="table-wrap"><table class="tbl">
          <thead><tr><th>#</th><th>${t("inst.linkLabel")}</th><th>${t("inst.linkProtocol")}</th><th>${t("subs.used")}</th></tr></thead>
          <tbody>${topRows || `<tr><td colspan="4" class="muted">${t("traffic.noData")}</td></tr>`}</tbody>
        </table></div>
      </div>
    </div>
    <div class="card" style="margin-top:14px"><h3>${t("traffic.bySubscription")}</h3>
      <div class="table-wrap"><table class="tbl">
        <thead><tr><th>${t("subs.plan")}</th><th>${t("subs.used")}</th><th>${t("subs.limit")}</th><th>%</th></tr></thead>
        <tbody>${subRows || `<tr><td colspan="4" class="muted">${t("traffic.noData")}</td></tr>`}</tbody>
      </table></div>
    </div>`;
}
