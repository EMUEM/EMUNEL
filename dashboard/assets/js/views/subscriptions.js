/* Subscriptions view — plans, quotas, expiry, lifecycle, sub URL. */
import { api, fmtBytes, fmtDate, esc } from "../api.js";
import { t } from "../i18n.js";
import { toast, modal, loading, emptyState, statusBadge } from "../components.js";
import { poller } from "../app.js";

const DURATIONS = [1, 7, 30, 60, 90];

export async function mount(el) {
  el.innerHTML = loading();
  await refresh(el, true);
  return poller(() => refresh(el, false), 15000, { immediate: false });
}

async function refresh(el, first) {
  let subs, users, instances;
  try {
    [subs, users, instances] = await Promise.all([
      api.get("/subscriptions?limit=200"),
      api.get("/users"),
      api.get("/instances"),
    ]);
  } catch (e) {
    if (first) { el.innerHTML = emptyState(); return; }
    return;
  }

  if (!subs.subscriptions?.length) {
    el.innerHTML = `${toolbar()}${emptyState(t("subs.noSubsSub"))}`;
    el.querySelector("#sub-create")?.addEventListener("click", () => createDialog(el, users.users || [], instances.instances || []));
    return;
  }

  el.innerHTML = `
    ${toolbar()}
    <div class="table-wrap"><table class="tbl">
      <thead><tr>
        <th>${t("subs.plan")}</th><th>${t("subs.user")}</th><th>${t("common.status")}</th>
        <th>${t("subs.used")} / ${t("subs.limit")}</th><th>${t("subs.expires")}</th>
        <th>${t("subs.activeDevices")}</th><th>${t("common.actions")}</th>
      </tr></thead>
      <tbody>
        ${subs.subscriptions.map(subRow).join("")}
      </tbody>
    </table></div>`;

  el.querySelector("#sub-create")?.addEventListener("click", () => createDialog(el, users.users || [], instances.instances || []));

  el.querySelectorAll("[data-sact]").forEach((b) => {
    b.addEventListener("click", () => lifecycle(el, b.dataset.sact, b.dataset.sid, b.dataset.sname));
  });
  el.querySelectorAll("[data-copy]").forEach((b) => {
    b.addEventListener("click", () => {
      navigator.clipboard.writeText(b.dataset.copy);
      toast(t("common.copied"), "ok");
    });
  });
}

function toolbar() {
  return `<div class="toolbar">
    <button class="btn primary" id="sub-create">${t("subs.create")}</button>
  </div>`;
}

function subRow(s) {
  const pct = s.traffic_usage_percent;
  return `<tr data-sid="${esc(s.id)}">
    <td><b>${esc(s.name)}</b><div class="muted mono" style="font-size:11px">${esc(s.id.slice(0, 8))}</div></td>
    <td class="mono">${esc(s.user_id.slice(0, 8))}…</td>
    <td>${statusBadge(s.effective_status)}</td>
    <td>
      ${fmtBytes(s.traffic_used_bytes)} ${s.traffic_limit_gb != null ? `/ ${fmtBytes(s.traffic_limit_bytes ?? s.traffic_limit_gb * 1073741824)}` : `/ ${t("common.unlimited")}`}
      ${pct != null ? `<div class="progress" style="margin-top:5px;min-width:110px"><i style="width:${Math.min(pct, 100)}%"></i></div>` : ""}
    </td>
    <td>${s.expires_at ? fmtDate(s.expires_at) : `<span class="muted">${t("common.unlimited")}</span>`}</td>
    <td>${s.active_devices}${s.device_limit ? ` / ${s.device_limit}` : ""}</td>
    <td style="white-space:nowrap">
      <button class="btn small" title="${t("subs.subURL")}" data-copy="${esc(location.origin + "/sub/" + s.link_token)}">🔗</button>
      <button class="btn small" data-sact="extend" data-sid="${esc(s.id)}" data-sname="${esc(s.name)}">${t("subs.extend")}</button>
      <button class="btn small" data-sact="renew" data-sid="${esc(s.id)}" data-sname="${esc(s.name)}">${t("subs.renew")}</button>
      <button class="btn small" data-sact="reset" data-sid="${esc(s.id)}">${t("subs.resetTraffic")}</button>
      <button class="btn small danger" data-sact="revoke" data-sid="${esc(s.id)}">${t("subs.revoke")}</button>
    </td>
  </tr>`;
}

function createDialog(el, users, instances) {
  const running = instances.filter((i) => i.status === "running");
  const userOpts = users.map((u) => `<option value="${esc(u.id)}">${esc(u.username)} (${esc(u.role)})</option>`).join("");
  const instOpts = running.length
    ? running.map((i) => `<option value="${esc(i.id)}">${esc(i.name)} — ${(i.protocols?.enabled || []).join(", ")}</option>`).join("")
    : "";
  const protoSel = running.length
    ? `<label class="field"><span>${t("subs.protocol")}</span>
         <select name="protocol">${(running[0].protocols?.enabled || ["vless-ws"]).map((p) => `<option>${esc(p)}</option>`).join("")}</select>
       </label>`
    : `<p class="muted" style="font-size:13px">${t("inst.stoppedHint")}</p>`;

  modal(t("subs.create"), `
    <div class="row">
      <label class="field"><span>${t("subs.user")}</span><select name="user_id">${userOpts}</select></label>
      <label class="field"><span>${t("subs.plan")}</span><input name="name" required maxlength="128" /></label>
    </div>
    <div class="row">
      <label class="field"><span>${t("subs.trafficLimit")}</span><input name="traffic_limit_gb" type="number" min="0" step="0.1" placeholder="∞" /></label>
      <label class="field"><span>${t("subs.duration")}</span>
        <select name="days_preset">
          <option value="">${t("common.unlimited")}</option>
          ${DURATIONS.map((d) => `<option value="${d}" ${d === 30 ? "selected" : ""}>${d} ${t("subs.days")}</option>`).join("")}
          <option value="custom">${t("subs.custom")}</option>
        </select>
      </label>
      <label class="field" data-custom-days style="display:none"><span>${t("subs.days")}</span>
        <input name="days_custom" type="number" min="1" max="3650" /></label>
    </div>
    <div class="row">
      <label class="field"><span>${t("subs.deviceLimit")}</span><input name="device_limit" type="number" min="1" max="64" placeholder="—" /></label>
      <label class="field"><span>${t("subs.autoRenew")}</span><select name="auto_renew"><option value="false">—</option><option value="true">✓</option></select></label>
    </div>
    <label class="field"><span>${t("subs.targetInstance")}</span>
      ${instOpts ? `<select name="instance_id"><option value="">—</option>${instOpts}</select>` : `<input disabled placeholder="${t("inst.stoppedHint")}" />`}
    </label>
    ${protoSel}`,
    [
      { label: t("common.cancel") },
      { label: t("common.create"), kind: "primary", onClick: async (back, close) => {
          const f = back.querySelector(".modal-body");
          const fd = new FormData(f);
          const daysKey = fd.get("days_preset");
          const days = daysKey === "custom" ? parseInt(fd.get("days_custom") || 0) || null
                     : daysKey ? parseInt(daysKey) : null;
          const body = {
            user_id: fd.get("user_id"),
            name: fd.get("name"),
            traffic_limit_gb: fd.get("traffic_limit_gb") === "" ? null : parseFloat(fd.get("traffic_limit_gb")),
            days_limit: days,
            device_limit: fd.get("device_limit") === "" ? null : parseInt(fd.get("device_limit")),
            auto_renew: fd.get("auto_renew") === "true",
            instance_id: fd.get("instance_id") || null,
            protocol: fd.get("protocol") || null,
          };
          await api.post("/subscriptions", body);
          close(); toast(t("common.create") + " ✓", "ok");
          refresh(el, true);
        } },
    ]);

  const back = document.querySelector(".modal-backdrop");
  back.querySelector('[name="days_preset"]').addEventListener("change", (e) => {
    back.querySelector("[data-custom-days]").style.display = e.target.value === "custom" ? "" : "none";
  });
  const instSel = back.querySelector('[name="instance_id"]');
  const protoField = back.querySelector('[name="protocol"]');
  instSel?.addEventListener("change", () => {
    const inst = running.find((i) => i.id === instSel.value);
    if (inst && protoField) {
      protoField.innerHTML = (inst.protocols?.enabled || ["vless-ws"])
        .map((p) => `<option>${esc(p)}</option>`).join("");
    }
  });
}

function lifecycle(el, act, sid, sname) {
  if (act === "extend" || act === "renew") {
    modal(`${t("subs." + act)} — ${sname || ""}`, `
      <label class="field"><span>${t("subs.extendDays")}</span>
        <input id="ext-days" type="number" min="1" max="3650" value="${act === "renew" ? 30 : 7}" /></label>`,
      [
        { label: t("common.cancel") },
        { label: t("common.confirm"), kind: "primary", onClick: async (back, close) => {
            const days = parseInt(back.querySelector("#ext-days").value) || 1;
            await api.post(`/subscriptions/${sid}/${act}`, { days });
            close(); toast("✓", "ok"); refresh(el, true);
          } },
      ]);
    return;
  }
  if (act === "revoke") {
    modal(`${t("subs.revoke")} — ${sname || ""}`, `<p>${t("common.confirm")}?</p>`, [
      { label: t("common.cancel") },
      { label: t("subs.revoke"), kind: "danger", onClick: async (_b, close) => {
          await api.post(`/subscriptions/${sid}/revoke`); close(); toast("✓", "ok"); refresh(el, true);
        } },
    ]);
    return;
  }
  if (act === "reset") {
    api.post(`/subscriptions/${sid}/reset-traffic`).then(() => { toast("✓", "ok"); refresh(el, true); });
  }
}
