/* Instances view — isolated Core runtimes: lifecycle, links, live stats. */
import { api, fmtBytes, fmtDate, esc } from "../api.js";
import { t } from "../i18n.js";
import { toast, modal, loading, emptyState, statusBadge } from "../components.js";
import { poller } from "../app.js";

let currentEl = null;

export async function mount(el) {
  currentEl = el;
  el.innerHTML = loading();
  await refresh(el, true);
  return poller(() => refresh(el, false), 12000, { immediate: false });
}

function rerender() {
  if (currentEl) refresh(currentEl, true);
}

async function refresh(el, first) {
  let data, matrix;
  try {
    [data, matrix] = await Promise.all([api.get("/instances"), api.get("/instances/meta/protocols")]);
  } catch (e) {
    if (first) { el.innerHTML = emptyState(); return; }
    return;
  }
  const instances = data.instances || [];

  if (!instances.length) {
    el.innerHTML = `
      ${toolbarHTML()}
      ${emptyState(t("inst.noInstancesSub"))}`;
    wireToolbar(el, matrix);
    return;
  }

  el.innerHTML = `
    ${toolbarHTML()}
    <div class="grid" style="grid-template-columns:1fr">
      ${instances.map((i) => instanceCard(i, matrix)).join("")}
    </div>`;
  wireCards(el, matrix);
  wireToolbar(el, matrix);
}

function toolbarHTML() {
  return `<div class="toolbar">
    <button class="btn primary" data-act="create">${t("inst.create")}</button>
  </div>`;
}

function instanceCard(i, matrix) {
  const protos = (i.protocols?.enabled || []).join(", ");
  return `
  <div class="card" data-id="${esc(i.id)}">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <div style="flex:1;min-width:160px">
        <div style="font-weight:650;font-size:15px">${esc(i.name)}</div>
        <div class="muted" style="font-size:12.5px">
          ${esc(i.region)} · ${esc(protos)} · ${t("inst.cpu")} ${i.cpu_limit} · ${i.memory_mb} MB
        </div>
      </div>
      ${statusBadge(i.status)}
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        ${i.status === "running"
          ? `<button class="btn small" data-act="stop">${t("inst.stop")}</button>
             <button class="btn small" data-act="restart">${t("inst.restart")}</button>
             <button class="btn small" data-act="links">${t("inst.links")}</button>
             <button class="btn small" data-act="share">${t("inst.shareLinks")}</button>`
          : `<button class="btn small primary" data-act="start">${t("inst.start")}</button>
             <button class="btn small" data-act="links">${t("inst.links")}</button>`}
        <button class="btn small danger" data-act="delete">✕</button>
      </div>
    </div>
    <div class="muted" style="font-size:12px;margin-top:6px">
      ${i.live?.healthy ? `● ${i.live.core_health?.connections ?? 0} ${t("inst.connections").toLowerCase()} · up ${i.live.core_health?.uptime ?? 0}s`
        : i.last_error ? esc(i.last_error) : ""}
      ${i.link_count != null ? ` · ${i.link_count} ${t("inst.links").toLowerCase()}` : ""}
    </div>
  </div>`;
}

function wireToolbar(el, matrix) {
  el.querySelector('[data-act="create"]')?.addEventListener("click", () => createDialog(matrix));
}

function wireCards(el, matrix) {
  el.querySelectorAll(".card[data-id]").forEach((card) => {
    const id = card.dataset.id;
    card.querySelector('[data-act="stop"]')?.addEventListener("click", async () => {
      await api.post(`/instances/${id}/stop`); toast(t("common.stopped"), "ok"); rerender();
    });
    card.querySelector('[data-act="start"]')?.addEventListener("click", async () => {
      await api.post(`/instances/${id}/start`); toast(t("common.running"), "ok"); rerender();
    });
    card.querySelector('[data-act="restart"]')?.addEventListener("click", async () => {
      await api.post(`/instances/${id}/restart`); toast(t("inst.restart") + " ✓", "ok");
    });
    card.querySelector('[data-act="delete"]')?.addEventListener("click", () => {
      modal(`${t("common.delete")} — ${t("inst.title")}`, `<p>${t("common.confirm")}?</p>`, [
        { label: t("common.cancel") },
        { label: t("common.delete"), kind: "danger", onClick: async (_b, close) => {
            await api.del(`/instances/${id}`); close(); toast(t("common.delete") + " ✓", "ok"); rerender();
          } },
      ]);
    });
    card.querySelector('[data-act="links"]')?.addEventListener("click", () => linksDialog(id, card, matrix));
    card.querySelector('[data-act="share"]')?.addEventListener("click", () => shareDialog(id));
  });
}

function createDialog(matrix) {
  const protocols = (matrix?.protocols || []).map((p) => `
    <label style="display:flex;gap:8px;align-items:center;font-size:13px">
      <input type="checkbox" name="proto" value="${esc(p.id)}" ${p.id === "vless-ws" ? "checked" : ""} />
      <span><b>${esc(p.protocol.toUpperCase())}</b> · ${esc(p.transport)}${p.mode ? " " + esc(p.mode) : ""} · ${esc(p.security)}</span>
    </label>`).join("");

  modal(t("inst.create"), `
    <label class="field"><span>${t("inst.name")}</span><input name="name" required minlength="2" maxlength="60" /></label>
    <label class="field"><span>${t("inst.region")}</span><input name="region" value="local" /></label>
    <label class="field"><span>${t("inst.publicHost")}</span><input name="public_host" placeholder="proxy.example.com" /></label>
    <div class="field"><span>${t("inst.protocols")}</span>
      <div style="display:grid;gap:7px">${protocols}</div></div>
    <div class="row">
      <label class="field"><span>${t("inst.cpu")}</span><input name="cpu_limit" type="number" step="0.1" min="0.1" max="8" value="0.5" /></label>
      <label class="field"><span>${t("inst.memory")}</span><input name="memory_mb" type="number" min="128" max="8192" value="256" /></label>
    </div>`,
    [
      { label: t("common.cancel") },
      { label: t("common.create"), kind: "primary", onClick: async (back, close) => {
          const f = back.querySelector(".modal-body");
          const fd = new FormData(f);
          const body = {
            name: fd.get("name"), region: fd.get("region") || "local",
            public_host: fd.get("public_host") || null,
            protocols: {
              enabled: [...f.querySelectorAll('input[name="proto"]:checked')].map((c) => c.value),
              default: "vless-ws",
            },
            cpu_limit: parseFloat(fd.get("cpu_limit")) || 0.5,
            memory_mb: parseInt(fd.get("memory_mb")) || 256,
            start: true,
          };
          await api.post("/instances", body);
          close(); toast(t("common.running"), "ok");
          rerender();
        } },
    ]);
}

async function linksDialog(id, card, matrix) {
  let data;
  try {
    data = await api.get(`/instances/${id}/links`);
  } catch (e) { toast(e.message, "err"); return; }
  const running = card.querySelector('[data-act="share"]') !== null;
  const enabled = new Set(data.enabled_protocols || []);

  const rows = (data.links || []).map((l) => `
    <tr>
      <td>${esc(l.label)}</td>
      <td><span class="badge proto">${esc(l.protocol)}</span></td>
      <td class="mono" title="${esc(l.uuid)}">${esc(l.uuid.slice(0, 13))}…</td>
      <td>${statusBadge(l.active ? "active" : "disabled")}</td>
      <td>${fmtBytes(l.used_bytes)}${l.limit_bytes ? ` / ${fmtBytes(l.limit_bytes)}` : ""}</td>
      <td style="white-space:nowrap">
        <button class="btn small" data-lact="revoke" data-lid="${esc(l.id)}">${t("subs.revoke")}</button>
        <button class="btn small" data-lact="reset" data-lid="${esc(l.id)}">${t("subs.resetTraffic")}</button>
        <button class="btn small danger" data-lact="del" data-lid="${esc(l.id)}">✕</button>
      </td>
    </tr>`).join("");

  const m = modal(t("inst.links"), `
    ${!running ? `<p class="muted" style="font-size:13px">${t("inst.stoppedHint")}</p>` : ""}
    <div class="table-wrap"><table class="tbl">
      <thead><tr><th>${t("inst.linkLabel")}</th><th>${t("inst.linkProtocol")}</th><th>UUID</th><th>${t("common.status")}</th><th>${t("inst.usage")}</th><th></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6" class="muted" style="text-align:center;padding:18px">${t("subs.noLinks")}</td></tr>`}</tbody>
    </table></div>
    ${running ? `<div class="toolbar" style="margin-top:12px">
      <select id="link-proto">${[...enabled].map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("")}</select>
      <input id="link-label" placeholder="${t("inst.linkLabel")}" style="flex:1;min-width:120px" />
      <button class="btn primary" id="link-add">${t("inst.addLink")}</button>
    </div>` : ""}`,
    [{ label: t("common.close") }]);

  m.el.querySelectorAll("[data-lact]").forEach((b) => {
    b.addEventListener("click", async () => {
      const lid = b.dataset.lid;
      if (b.dataset.lact === "revoke") await api.post(`/instances/${id}/links/${lid}/revoke`);
      if (b.dataset.lact === "reset") await api.patch(`/instances/${id}/links/${lid}`, { reset_usage: true });
      if (b.dataset.lact === "del") await api.del(`/instances/${id}/links/${lid}`);
      m.close(); linksDialog(id, card, matrix);
    });
  });
  m.el.querySelector("#link-add")?.addEventListener("click", async () => {
    const proto = m.el.querySelector("#link-proto").value;
    const label = m.el.querySelector("#link-label").value || "Link";
    await api.post(`/instances/${id}/links`, { protocol: proto, label });
    m.close(); linksDialog(id, card, matrix);
  });
}

async function shareDialog(id) {
  modal(t("inst.shareLinks"), `
    <label class="field"><span>${t("inst.shareHostPrompt")}</span><input id="share-host" placeholder="proxy.example.com" /></label>
    <div id="share-out"></div>`,
    [{ label: t("common.close") }, { label: t("inst.shareLinks"), kind: "primary", onClick: async (back) => {
        const host = back.querySelector("#share-host").value.trim();
        if (!host) return;
        const out = back.querySelector("#share-out");
        out.innerHTML = `<div class="spinner"></div>`;
        const data = await api.get(`/instances/${id}/share?host=${encodeURIComponent(host)}`);
        out.innerHTML = (data.links || []).map((l) => `
          <div style="margin:8px 0;display:flex;gap:8px">
            <input readonly value="${esc(l.share_url)}" style="flex:1;font-size:11.5px" onclick="this.select()" />
          </div>`).join("") || `<p class="muted">${t("subs.noLinks")}</p>`;
      } }]);
}
