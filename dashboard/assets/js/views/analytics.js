/* Analytics view — real aggregates, canvas charts, no fake data. */
import { api, fmtBytes, esc } from "../api.js";
import { t } from "../i18n.js";
import { loading, emptyState, lineChart, barChart } from "../components.js";

export async function mount(el) {
  el.innerHTML = loading();
  let overview, proto, subStatus, hourly, events;
  try {
    [overview, proto, subStatus, hourly, events] = await Promise.all([
      api.get("/analytics/overview"),
      api.get("/analytics/protocol-distribution"),
      api.get("/analytics/subscription-status"),
      api.get("/analytics/hourly"),
      api.get("/analytics/recent-events?limit=20"),
    ]);
  } catch (e) { el.innerHTML = emptyState(); return; }

  const protoTotal = (proto.distribution || []).reduce((s, d) => s + d.used_bytes, 0);
  const hours = Object.entries(hourly.hourly || {}).sort().slice(-24);

  el.innerHTML = `
    <div class="grid cols-4">
      ${stat(t("dash.users"), overview.users?.total ?? 0)}
      ${stat(t("dash.subscriptions"), overview.subscriptions?.total ?? 0)}
      ${stat(t("dash.totalTraffic"), fmtBytes(overview.traffic_total_bytes))}
      ${stat("Events 24h", overview.audit_events_24h ?? 0)}
    </div>
    <div class="grid cols-2" style="margin-top:14px">
      <div class="card"><h3>${t("analytics.hourly")}</h3>
        <div class="chart-box"><canvas id="an-hourly"></canvas></div>
      </div>
      <div class="card"><h3>${t("analytics.protocolDist")}</h3>
        <div class="chart-box"><canvas id="an-proto"></canvas></div>
        <div class="muted" style="font-size:12px;margin-top:6px">${(proto.distribution || []).map((d) =>
          `${esc(d.protocol)}: ${fmtBytes(d.used_bytes)}`).join(" · ") || t("traffic.noData")}</div>
      </div>
    </div>
    <div class="grid cols-2" style="margin-top:14px">
      <div class="card"><h3>${t("analytics.subStatus")}</h3>
        ${(subStatus.distribution || []).map((d) => `
          <div style="display:flex;align-items:center;gap:10px;margin:6px 0">
            <span style="width:120px;font-size:13px">${esc(d.status)}</span>
            <div class="progress" style="flex:1"><i style="width:${Math.min(d.count * 25, 100)}%"></i></div>
            <b>${d.count}</b>
          </div>`).join("") || `<p class="muted">${t("traffic.noData")}</p>`}
      </div>
      <div class="card"><h3>${t("analytics.recentEvents")}</h3>
        ${(events.events || []).slice(0, 10).map((e) => `
          <div class="log-line"><span class="lvl INFO">${esc(e.action)}</span> <span class="muted">${esc(e.resource_type)} ${esc(e.resource_id || "")}</span></div>
        `).join("") || `<p class="muted">${t("common.empty")}</p>`}
      </div>
    </div>`;

  const hCanvas = el.querySelector("#an-hourly");
  if (hCanvas && hours.length > 1) lineChart(hCanvas, hours.map(([h, v]) => [h, v]), { labelLast: fmtBytes });
  const pCanvas = el.querySelector("#an-proto");
  if (pCanvas) {
    const d = proto.distribution || [];
    if (d.length) barChart(pCanvas, d.map((x) => [x.protocol, x.used_bytes || 1]));
  }
}

function stat(label, value) {
  return `<div class="card stat-card"><div class="stat-value">${value}</div><div class="stat-label">${label}</div></div>`;
}
