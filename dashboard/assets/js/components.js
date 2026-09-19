/* EMUNEL UI helpers: toast, modal, states, badges, tiny canvas charts. */
import { t } from "./i18n.js";
import { esc as _esc } from "./api.js";
import { icon } from "./icons.js";
export { t } from "./i18n.js";
export const esc = _esc;

export function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  document.getElementById("toast-root").appendChild(el);
  setTimeout(() => el.remove(), 3800);
}

export function modal(title, bodyHtml, actions = []) {
  const root = document.getElementById("modal-root");
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  back.innerHTML = `
    <div class="modal" role="dialog" aria-modal="true">
      <h3>${esc(title)}</h3>
      <form class="modal-body" onsubmit="return false">${bodyHtml}</form>
      <div class="modal-actions"></div>
    </div>`;
  const close = () => back.remove();
  const actionsEl = back.querySelector(".modal-actions");
  for (const a of actions) {
    const b = document.createElement("button");
    b.className = `btn ${a.kind || ""}`;
    b.textContent = a.label;
    b.onclick = async () => {
      if (a.onClick) {
        try {
          await a.onClick(back, close);
        } catch (e) {
          toast(e.message || String(e), "err");
        }
      } else close();
    };
    actionsEl.appendChild(b);
  }
  if (!actions.length) actionsEl.remove();
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  root.appendChild(back);
  return { el: back, close };
}

export function loading() {
  return `<div class="spinner" role="status" aria-label="${t("common.loading")}"></div>`;
}

export function emptyState(sub) {
  return `<div class="state-box">
    ${icon("shield", 28)}
    <div class="big">${t("common.empty")}</div>
    <div class="muted">${esc(sub || t("common.emptySub"))}</div>
  </div>`;
}

export function errorState(e, retry) {
  return `<div class="state-box">
    ${icon("alert", 28)}
    <div class="big">${t("common.error")}</div>
    <div class="muted">${esc(e?.message || e)}</div>
    ${retry ? `<button class="btn small" style="margin-top:12px" onclick="(${retry})()">${t("common.retry")}</button>` : ""}
  </div>`;
}

export function statusBadge(status) {
  const map = {
    running: "ok", active: "ok", online: "ok", ok: "ok",
    stopped: "dim", disabled: "dim", expired: "warn",
    degraded: "warn", starting: "warn", quota_exceeded: "err",
    failed: "err", offline: "err", down: "err",
  };
  const kind = map[status] || "dim";
  return `<span class="badge ${kind}">${esc(status || "—")}</span>`;
}

/* ---- tiny canvas chart (no external dependency) ----
   Single-accent line with soft fill — calm and technical. */
export function lineChart(canvas, points, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);
  if (!points || points.length < 2) return;

  const values = points.map((p) => p[1]);
  const max = Math.max(...values, 1);
  const pad = { l: 4, r: 4, t: 10, b: 16 };

  // horizontal grid lines
  ctx.strokeStyle = "rgba(151,161,182,0.10)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const y = pad.t + ((H - pad.t - pad.b) * i) / 3;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
  }

  ctx.strokeStyle = "rgba(151,161,182,0.22)";
  ctx.beginPath();
  ctx.moveTo(pad.l, H - pad.b); ctx.lineTo(W - pad.r, H - pad.b); ctx.stroke();

  const path = [];
  points.forEach((p, i) => {
    const x = pad.l + ((W - pad.l - pad.r) * i) / (points.length - 1);
    const y = H - pad.b - ((H - pad.t - pad.b) * p[1]) / max;
    path.push([x, y]);
  });

  // area fill under the line
  const fill = ctx.createLinearGradient(0, pad.t, 0, H - pad.b);
  fill.addColorStop(0, "rgba(103,195,232,0.16)");
  fill.addColorStop(1, "rgba(103,195,232,0)");
  ctx.beginPath();
  path.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.lineTo(path[path.length - 1][0], H - pad.b);
  ctx.lineTo(path[0][0], H - pad.b);
  ctx.closePath();
  ctx.fillStyle = fill;
  ctx.fill();

  // the line itself
  ctx.strokeStyle = "#67c3e8";
  ctx.lineWidth = 1.8;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.beginPath();
  path.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.stroke();

  // last point marker
  const [lx, ly] = path[path.length - 1];
  ctx.fillStyle = "#0a0c11";
  ctx.beginPath(); ctx.arc(lx - 1, ly, 3.4, 0, 7); ctx.fill();
  ctx.fillStyle = "#67c3e8";
  ctx.beginPath(); ctx.arc(lx - 1, ly, 2.2, 0, 7); ctx.fill();

  if (opts.labelLast && points.length) {
    const last = points[points.length - 1];
    ctx.fillStyle = "#97a1b6";
    ctx.font = "11px " + getComputedStyle(document.body).fontFamily;
    ctx.textAlign = "end";
    ctx.fillText(opts.labelLast(last[1]), W - pad.r - 4, pad.t + 4);
  }
}

export function barChart(canvas, entries) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);
  if (!entries || !entries.length) return;
  const max = Math.max(...entries.map((e) => e[1]), 1);
  const pad = { l: 4, r: 4, t: 10, b: 22 };
  const bw = (W - pad.l - pad.r) / entries.length;
  const grad = ctx.createLinearGradient(0, pad.t, 0, H);
  grad.addColorStop(0, "rgba(103,195,232,0.75)");
  grad.addColorStop(1, "rgba(103,195,232,0.22)");
  ctx.font = "10px " + getComputedStyle(document.body).fontFamily;
  entries.forEach((e, i) => {
    const h = ((H - pad.t - pad.b) * e[1]) / max;
    const x = pad.l + i * bw + bw * 0.18;
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.roundRect(x, H - pad.b - h, bw * 0.64, Math.max(h, 2), 3);
    ctx.fill();
    ctx.fillStyle = "rgba(92,101,119,0.95)";
    ctx.textAlign = "center";
    ctx.fillText(String(e[0]).slice(0, 9), x + bw * 0.32, H - 7);
  });
}
