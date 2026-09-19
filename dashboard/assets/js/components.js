/* EMUNEL UI helpers: toast, modal, states, badges, tiny canvas charts. */
import { t } from "./i18n.js";
import { esc as _esc } from "./api.js";
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
    <span class="ico">◇</span>
    <div>${t("common.empty")}</div>
    <div class="muted" style="font-size:13px">${esc(sub || t("common.emptySub"))}</div>
  </div>`;
}

export function errorState(e, retry) {
  return `<div class="state-box">
    <span class="ico">✕</span>
    <div>${t("common.error")}</div>
    <div class="muted" style="font-size:13px">${esc(e?.message || e)}</div>
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

/* ---- tiny canvas chart (no external dependency) ---- */
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
  const pad = { l: 4, r: 4, t: 8, b: 16 };

  // grid lines
  ctx.strokeStyle = "rgba(148,163,184,0.12)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const y = pad.t + ((H - pad.t - pad.b) * i) / 3;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
  }

  const grad = ctx.createLinearGradient(0, 0, W, 0);
  grad.addColorStop(0, "#3b82f6");
  grad.addColorStop(0.55, "#06b6d4");
  grad.addColorStop(1, "#8b5cf6");
  ctx.strokeStyle = grad;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = pad.l + ((W - pad.l - pad.r) * i) / (points.length - 1);
    const y = H - pad.b - ((H - pad.t - pad.b) * p[1]) / max;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();

  // soft area fill
  const fill = ctx.createLinearGradient(0, 0, 0, H);
  fill.addColorStop(0, "rgba(6,182,212,0.22)");
  fill.addColorStop(1, "rgba(6,182,212,0)");
  ctx.lineTo(W - pad.r, H - pad.b);
  ctx.lineTo(pad.l, H - pad.b);
  ctx.closePath();
  ctx.fillStyle = fill;
  ctx.fill();

  if (opts.labelLast && points.length) {
    const last = points[points.length - 1];
    ctx.fillStyle = "#e6edf7";
    ctx.font = "11px " + getComputedStyle(document.body).fontFamily;
    ctx.textAlign = "end";
    ctx.fillText(opts.labelLast(last[1]), W - pad.r, pad.t + 4);
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
  grad.addColorStop(0, "rgba(59,130,246,0.85)");
  grad.addColorStop(1, "rgba(139,92,246,0.45)");
  ctx.font = "10px " + getComputedStyle(document.body).fontFamily;
  entries.forEach((e, i) => {
    const h = ((H - pad.t - pad.b) * e[1]) / max;
    const x = pad.l + i * bw + bw * 0.18;
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.roundRect(x, H - pad.b - h, bw * 0.64, Math.max(h, 2), 4);
    ctx.fill();
    ctx.fillStyle = "rgba(139,148,163,0.9)";
    ctx.textAlign = "center";
    ctx.fillText(String(e[0]).slice(0, 9), x + bw * 0.32, H - 7);
  });
}
