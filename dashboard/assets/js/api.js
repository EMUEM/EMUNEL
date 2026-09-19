/* EMUNEL API client — token auth, error shaping, tiny response cache. */

const TOKEN_KEY = "emunel.token";
let token = localStorage.getItem(TOKEN_KEY) || null;

export function getToken() { return token; }
export function setToken(t) {
  token = t;
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
  }
}

async function request(method, path, body, opts = {}) {
  const headers = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (body !== undefined && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
  const init = { method, headers, credentials: "same-origin" };
  if (body instanceof FormData) { init.body = body; delete headers["Content-Type"]; }
  else if (body !== undefined) init.body = JSON.stringify(body);

  let resp;
  try {
    resp = await fetch(`/api/v1${path}`, init);
  } catch (e) {
    throw new ApiError(0, "Network unreachable");
  }
  if (resp.status === 401) {
    setToken(null);
    window.dispatchEvent(new Event("emunel:unauthorized"));
  }
  if (resp.status === 204) return null;
  let data = null;
  try { data = await resp.json(); } catch { /* empty body */ }
  if (!resp.ok) {
    throw new ApiError(resp.status, (data && (data.detail || data.message)) || `HTTP ${resp.status}`);
  }
  return data;
}

export const api = {
  get: (p) => request("GET", p),
  post: (p, b) => request("POST", p, b ?? {}),
  patch: (p, b) => request("PATCH", p, b ?? {}),
  del: (p) => request("DELETE", p),
  login: async (username, password) => {
    const body = new URLSearchParams({ username, password });
    const resp = await fetch("/api/v1/auth/login", { method: "POST", body });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new ApiError(resp.status, data.detail || "login failed");
    setToken(data.access_token);
    return data;
  },
};

/* ---- formatting helpers ---- */
export function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

export function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function fmtMs(v) {
  if (v == null) return "—";
  return `${v < 10 ? v.toFixed(2) : Math.round(v)} ms`;
}

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* debounce for search inputs */
export function debounce(fn, ms = 300) {
  let h;
  return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
}
