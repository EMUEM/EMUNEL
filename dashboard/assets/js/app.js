/* EMUNEL app — hash router, auth gate, lazy-loaded views.
   Views are only mounted when navigated to; data polling stops on leave. */

import { api, getToken, setToken } from "./api.js";
import { t, setLang, toggleLang, applyDirection, applyTranslations, lang } from "./i18n.js";
import { toast } from "./components.js";

const ROUTES = [
  { id: "dashboard", ico: "◈", view: () => import("./views/dashboard.js") },
  { id: "instances", ico: "▤", view: () => import("./views/instances.js") },
  { id: "nodes", ico: "⬡", view: () => import("./views/nodes.js") },
  { id: "users", ico: "◍", view: () => import("./views/users.js") },
  { id: "subscriptions", ico: "◆", view: () => import("./views/subscriptions.js") },
  { id: "traffic", ico: "∿", view: () => import("./views/traffic.js") },
  { id: "analytics", ico: "◨", view: () => import("./views/analytics.js") },
  { id: "connections", ico: "◉", view: () => import("./views/connections.js") },
  { id: "diagnostics", ico: "⊹", view: () => import("./views/diagnostics.js") },
  { id: "logs", ico: "≣", view: () => import("./views/logs.js") },
  { id: "settings", ico: "⚙", view: () => import("./views/settings.js") },
];
const MOBILE_ROUTES = ["dashboard", "instances", "subscriptions", "connections", "diagnostics"];

let currentCleanup = null;

function route() {
  const hash = (location.hash || "#/dashboard").replace(/^#\/?/, "").split("?")[0];
  return ROUTES.find((r) => r.id === hash) || ROUTES[0];
}

function renderNav() {
  const nav = document.getElementById("nav");
  nav.innerHTML = ROUTES.map(
    (r) => `<a href="#/${r.id}" data-route="${r.id}"><span class="ico">${r.ico}</span><span>${t(`nav.${r.id}`)}</span></a>`
  ).join("");
  const bottom = document.getElementById("bottom-nav");
  bottom.innerHTML = ROUTES.filter((r) => MOBILE_ROUTES.includes(r.id)).map(
    (r) => `<a href="#/${r.id}" data-route="${r.id}"><span class="ico">${r.ico}</span><span>${t(`nav.${r.id}`)}</span></a>`
  ).join("");
}

async function render() {
  const r = route();
  document.getElementById("page-title").textContent = t(`nav.${r.id}`) || r.id;
  document.querySelectorAll("[data-route]").forEach((a) =>
    a.classList.toggle("active", a.dataset.route === r.id)
  );
  document.getElementById("sidebar").classList.remove("open");

  if (currentCleanup) { try { currentCleanup(); } catch {} currentCleanup = null; }

  const view = document.getElementById("view");
  view.innerHTML = `<div class="spinner"></div>`;
  try {
    const mod = await r.view();
    currentCleanup = await mod.mount(view) || null;
  } catch (e) {
    view.innerHTML = `<div class="state-box"><span class="ico">✕</span><div>${t("common.error")}</div><div class="muted">${(e.message || e)}</div></div>`;
  }
}

/* ---- health dot (periodic, cheap, stops on hidden tab) ---- */
async function refreshHealth() {
  const dot = document.getElementById("health-dot");
  try {
    const h = await api.get("/health/full");
    dot.className = `health-dot ${h.status === "ok" ? "ok" : h.status === "down" ? "down" : "degraded"}`;
    dot.title = JSON.stringify(h.components ? Object.fromEntries(
      Object.entries(h.components).map(([k, v]) => [k, v.status])
    ) : {});
  } catch {
    dot.className = "health-dot down";
  }
}

/* ---- poll helper used by views (auto-stops on navigation) ---- */
export function poller(fn, intervalMs, { immediate = true } = {}) {
  let stopped = false;
  let timer = null;
  const run = async () => {
    if (stopped || document.hidden) return;
    try { await fn(); } catch {}
  };
  const start = () => { if (!timer && !stopped) timer = setInterval(run, intervalMs); };
  if (immediate) run();
  start();
  const vis = () => { if (document.hidden) { clearInterval(timer); timer = null; } else start(); };
  document.addEventListener("visibilitychange", vis);
  return () => {
    stopped = true;
    if (timer) clearInterval(timer);
    document.removeEventListener("visibilitychange", vis);
  };
}

function showShell() {
  document.getElementById("login-screen").classList.add("hidden");
  document.getElementById("shell").classList.remove("hidden");
  renderNav();
  render();
  refreshHealth();
  window._emunelHealthTimer = setInterval(() => { if (!document.hidden) refreshHealth(); }, 20000);
}

function showLogin() {
  clearInterval(window._emunelHealthTimer);
  document.getElementById("shell").classList.add("hidden");
  document.getElementById("login-screen").classList.remove("hidden");
  applyTranslations();
}

async function boot() {
  applyDirection();

  document.getElementById("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("login-error");
    err.classList.add("hidden");
    try {
      const data = await api.login(
        document.getElementById("login-username").value.trim(),
        document.getElementById("login-password").value
      );
      document.getElementById("user-chip").textContent = data.user?.username || "";
      toast(t("nav.dashboard") + " ✓", "ok");
      showShell();
    } catch (ex) {
      err.textContent = ex.message || t("login.failed");
      err.classList.remove("hidden");
    }
  });

  document.getElementById("logout-btn").addEventListener("click", () => {
    setToken(null);
    showLogin();
  });

  document.getElementById("lang-toggle").addEventListener("click", () => {
    toggleLang();
    applyTranslations();
    const authed = !document.getElementById("shell").classList.contains("hidden");
    if (authed) {
      renderNav();      // nav labels follow the active language
      render();         // re-render the current view
    }
  });

  document.getElementById("menu-btn").addEventListener("click", () =>
    document.getElementById("sidebar").classList.toggle("open")
  );

  window.addEventListener("hashchange", render);
  window.addEventListener("emunel:unauthorized", showLogin);

  // language from storage, translations applied to login screen
  setLang(lang());
  applyDirection();

  if (getToken()) {
    try {
      const me = await api.get("/auth/me");
      document.getElementById("user-chip").textContent = me.username || "";
      showShell();
    } catch {
      showLogin();
    }
  } else {
    showLogin();
  }
}

boot();
