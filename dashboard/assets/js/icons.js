/* EMUNEL icon system — stroke-based SVG icons (24×24, stroke 1.7).
   Consistent geometry, no emojis, scales with text color. */

const P = {
  dashboard: `<path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"/>`,
  instances: `<rect x="3" y="4" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M11 7.5h.01"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 16.5h.01M11 16.5h.01"/><path d="M17 7.5v0M17 16.5v0"/>`,
  nodes: `<circle cx="12" cy="5" r="2.2"/><circle cx="5" cy="19" r="2.2"/><circle cx="19" cy="19" r="2.2"/><path d="M12 7.2 5.8 16.9M12 7.2l6.2 9.7M7.2 19h9.6"/>`,
  users: `<circle cx="9" cy="8" r="3.2"/><path d="M3.5 20c.6-3.2 2.8-5 5.5-5s4.9 1.8 5.5 5"/><circle cx="17" cy="9" r="2.4"/><path d="M16.2 15.4c2.3.3 3.9 1.9 4.3 4.6"/>`,
  subscriptions: `<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M3 10h18"/><path d="M7 15h4"/>`,
  traffic: `<path d="M3 17l4.5-6 3.5 4 4-7 6 9"/>`,
  analytics: `<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>`,
  connections: `<path d="M9.5 14.5 14.5 9.5"/><path d="M7 11 5 13a3.5 3.5 0 0 0 5 5l2-2"/><path d="M17 13l2-2a3.5 3.5 0 0 0-5-5l-2 2"/>`,
  diagnostics: `<path d="M3 12h3.5l2.5-6 4 12 2.5-6H21"/>`,
  logs: `<path d="M4 5h16M4 10h10M4 15h13M4 20h7"/>`,
  settings: `<circle cx="12" cy="12" r="3.2"/><path d="M12 2.8v3M12 18.2v3M2.8 12h3M18.2 12h3M5.5 5.5l2.1 2.1M16.4 16.4l2.1 2.1M18.5 5.5l-2.1 2.1M7.6 16.4l-2.1 2.1"/>`,
  plus: `<path d="M12 5v14M5 12h14"/>`,
  close: `<path d="M6 6l12 12M18 6L6 18"/>`,
  check: `<path d="M4 12.5l5 5L20 6.5"/>`,
  copy: `<rect x="9" y="9" width="11" height="11" rx="1.5"/><path d="M5 15H4.5A1.5 1.5 0 0 1 3 13.5v-9A1.5 1.5 0 0 1 4.5 3h9A1.5 1.5 0 0 1 15 4.5V5"/>`,
  logout: `<path d="M9 21H5.5A1.5 1.5 0 0 1 4 19.5v-15A1.5 1.5 0 0 1 5.5 3H9"/><path d="M16 17l5-5-5-5M21 12H9"/>`,
  menu: `<path d="M4 7h16M4 12h16M4 17h16"/>`,
  globe: `<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.4 4 5.6 4 9s-1.5 6.6-4 9c-2.5-2.4-4-5.6-4-9s1.5-6.6 4-9z"/>`,
  alert: `<path d="M12 3 2.5 20h19L12 3z"/><path d="M12 10v4.5M12 17.5h.01"/>`,
  clock: `<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>`,
  shield: `<path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7l8-4z"/>`,
  bolt: `<path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H13L13 2z"/>`,
};

export function icon(name, size = 16) {
  const body = P[name] || P.dashboard;
  return `<svg class="icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

/* Brand mark — the EMUNEL hexagon sigil. */
export const brandMark = `<svg width="26" height="26" viewBox="0 0 32 32" fill="none" aria-hidden="true">
  <path d="M16 2.5 27.5 9v14L16 29.5 4.5 23V9L16 2.5z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
  <path d="M11 11v10M21 11v10M11 16h10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
</svg>`;
