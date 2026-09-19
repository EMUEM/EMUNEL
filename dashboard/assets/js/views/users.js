/* Users view — accounts and roles. */
import { api, fmtDate, esc } from "../api.js";
import { t } from "../i18n.js";
import { toast, modal, loading, emptyState, statusBadge } from "../components.js";

export async function mount(el) {
  el.innerHTML = loading();
  let data;
  try { data = await api.get("/users"); }
  catch (e) { el.innerHTML = emptyState(); return; }

  const users = data.users || [];
  if (!users.length) { el.innerHTML = emptyState(t("users.noUsers")); return; }

  el.innerHTML = `
    <div class="toolbar"><button class="btn primary" id="user-create">${t("users.create")}</button></div>
    <div class="table-wrap"><table class="tbl">
      <thead><tr>
        <th>${t("users.username")}</th><th>${t("users.email")}</th><th>${t("users.role")}</th>
        <th>${t("common.status")}</th><th>Last login</th><th>${t("common.actions")}</th>
      </tr></thead>
      <tbody>
        ${users.map((u) => `
          <tr>
            <td><b>${esc(u.username)}</b></td>
            <td class="muted">${esc(u.email || "—")}</td>
            <td><span class="badge ${u.role === "admin" ? "violet" : "dim"}">${esc(u.role)}</span></td>
            <td>${statusBadge(u.is_active ? "active" : "disabled")}</td>
            <td class="muted">${u.last_login ? fmtDate(u.last_login) : t("common.never")}</td>
            <td>
              <button class="btn small" data-uact="toggle" data-uid="${esc(u.id)}" data-active="${u.is_active}">
                ${u.is_active ? t("common.disabled") : t("common.active")}
              </button>
            </td>
          </tr>`).join("")}
      </tbody>
    </table></div>`;

  el.querySelector("#user-create").addEventListener("click", () => {
    modal(t("users.create"), `
      <label class="field"><span>${t("users.username")}</span><input name="username" required minlength="3" maxlength="64" /></label>
      <label class="field"><span>${t("login.password")}</span><input name="password" type="password" required minlength="8" /></label>
      <label class="field"><span>${t("users.email")}</span><input name="email" type="email" /></label>`,
      [
        { label: t("common.cancel") },
        { label: t("common.create"), kind: "primary", onClick: async (back, close) => {
            const fd = new FormData(back.querySelector(".modal-body"));
            await api.post("/auth/register", {
              username: fd.get("username"), password: fd.get("password"), email: fd.get("email") || null,
            });
            close(); toast("✓", "ok"); mount(el);
          } },
      ]);
  });

  el.querySelectorAll('[data-uact="toggle"]').forEach((b) => {
    b.addEventListener("click", async () => {
      await api.patch(`/users/${b.dataset.uid}`, { is_active: b.dataset.active !== "true" });
      toast("✓", "ok"); mount(el);
    });
  });
}
