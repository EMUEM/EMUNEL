/* Settings view — read-only platform info (config comes from environment). */
import { api, esc } from "../api.js";
import { t } from "../i18n.js";
import { loading, emptyState, statusBadge } from "../components.js";

export async function mount(el) {
  el.innerHTML = loading();
  let settings, health;
  try {
    [settings, health] = await Promise.all([api.get("/settings"), api.get("/health/full")]);
  } catch (e) { el.innerHTML = emptyState(); return; }

  el.innerHTML = `
    <div class="grid cols-2">
      <div class="card">
        <h3>${t("settings.platform")}</h3>
        <dl class="kv">
          <dt>App</dt><dd>${esc(settings.app_name || "EMUNEL")}</dd>
          <dt>Version</dt><dd>1.0.0</dd>
          <dt>Database</dt><dd>${esc(settings.database || "sqlite")}</dd>
          <dt>Debug</dt><dd>${settings.debug ? "true" : "false"}</dd>
          <dt>Rate limit</dt><dd>${settings.rate_limit_per_minute ?? "—"} / min</dd>
        </dl>
        <p class="muted" style="font-size:12px">${t("settings.note")}</p>
      </div>
      <div class="card">
        <h3>${t("dash.health")}</h3>
        ${Object.entries(health.components || {}).map(([name, c]) => `
          <div style="display:flex;align-items:center;gap:10px;margin:8px 0">
            ${statusBadge(c.status)}
            <span style="flex:1">${esc(name)}</span>
            <span class="muted" style="font-size:12px">${esc(c.error || c.latency_ms != null ? (c.latency_ms + " ms") : "")}</span>
          </div>`).join("")}
      </div>
    </div>
    <div class="card" style="margin-top:14px">
      <h3>${t("settings.about")}</h3>
      <p class="muted" style="font-size:13px;line-height:1.7;margin:0">
        EMUNEL — multi-protocol proxy management platform.<br/>
        Networking core: VLESS / Trojan / Shadowsocks / VMess over WebSocket + xHTTP.<br/>
        Reference architecture: Lunel. Every dashboard metric on this console comes from live
        backend data — no simulated values.
      </p>
    </div>`;
}
