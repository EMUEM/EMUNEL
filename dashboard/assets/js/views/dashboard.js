/* Dashboard view — real platform overview. */
import { api, fmtBytes, esc } from "../api.js";
import { t } from "../i18n.js";
import { loading, emptyState, statusBadge, lineChart } from "../components.js";
import { poller } from "../app.js";
import { icon } from "../icons.js";

export async function mount(el) {
  el.innerHTML = loading();
  await refresh(el, true);

  return poller(() => refresh(el, false), 15000, { immediate: false });
}

async function refresh(el, first) {
  let overview, health, conn, hourly;
  try {
    [overview, health, conn, hourly] = await Promise.all([
      api.get("/analytics/overview"),
      api.get("/health/full"),
      api.get("/connections/summary"),
      api.get("/analytics/hourly"),
    ]);
  } catch (e) {
    if (first) { el.innerHTML = emptyState(); return; }
    return;
  }

  const degraded = health.status && health.status !== "ok";
  const hours = Object.entries(hourly.hourly || {}).sort().slice(-24);
  const totalNow = conn.total_connections || 0;

  el.innerHTML = `
    ${degraded ? `<div class="card" style="border-color:rgba(227,179,65,.4);margin-bottom:14px;display:flex;gap:10px;align-items:center">
      <span style="color:var(--amber)">${icon("alert", 16)}</span><span>${t("dash.degradedNote")}</span></div>` : ""}
    <div class="grid cols-4">
      <div class="card stat-card">
        <div class="stat-value" id="st-traffic">${fmtBytes(overview.traffic_total_bytes)}</div>
        <div class="stat-label">${t("dash.totalTraffic")}</div>
      </div>
      <div class="card stat-card">
        <div class="stat-value" id="st-conn">${totalNow}</div>
        <div class="stat-label">${t("dash.activeConnections")}</div>
        <div class="stat-sub">${overview.instances?.running || 0}/${overview.instances?.total || 0} ${t("dash.runningInstances").toLowerCase()}</div>
      </div>
      <div class="card stat-card">
        <div class="stat-value">${overview.links?.active ?? 0}<span class="muted" style="font-size:15px"> / ${overview.links?.total ?? 0}</span></div>
        <div class="stat-label">${t("dash.activeLinks")}</div>
      </div>
      <div class="card stat-card">
        <div class="stat-value">${overview.users?.total ?? 0}</div>
        <div class="stat-label">${t("dash.users")}</div>
        <div class="stat-sub">${overview.subscriptions?.total ?? 0} ${t("dash.subscriptions").toLowerCase()}</div>
      </div>
    </div>

    <div class="grid cols-2" style="margin-top:14px">
      <div class="card">
        <h3>${t("dash.traffic24h")}</h3>
        <div class="chart-box"><canvas id="dash-chart"></canvas></div>
      </div>
      <div class="card">
        <h3>${t("dash.health")}</h3>
        <div class="grid cols-2" style="gap:8px">
          ${Object.entries(health.components || {}).map(([name, c]) => `
            <div style="display:flex;align-items:center;gap:10px;padding:7px 10px;border:1px solid var(--border);border-radius:9px">
              ${statusBadge(c.status)}
              <span style="font-size:13px">${esc(name)}</span>
            </div>`).join("")}
        </div>
      </div>
    </div>`;

  const canvas = el.querySelector("#dash-chart");
  if (canvas && hours.length > 1) {
    lineChart(canvas, hours.map(([h, v]) => [h, v]), { labelLast: fmtBytes });
  } else if (canvas) {
    canvas.closest(".card").insertAdjacentHTML(
      "beforeend", `<p class="muted" style="font-size:12.5px">${t("traffic.noData")}</p>`
    );
  }
}
