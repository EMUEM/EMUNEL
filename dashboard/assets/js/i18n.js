/* EMUNEL i18n — English + Persian (فارسی) with RTL. */

const dict = {
  en: {
    "nav.dashboard": "Dashboard", "nav.instances": "Instances", "nav.nodes": "Nodes",
    "nav.users": "Users", "nav.subscriptions": "Subscriptions", "nav.traffic": "Traffic",
    "nav.analytics": "Analytics", "nav.connections": "Online", "nav.diagnostics": "Diagnostics",
    "nav.logs": "Logs", "nav.settings": "Settings", "nav.logout": "Sign out",
    "login.subtitle": "Sign in to your console", "login.username": "Username",
    "login.password": "Password", "login.submit": "Sign in", "login.failed": "Invalid username or password",
    "common.loading": "Loading…", "common.empty": "Nothing here yet",
    "common.emptySub": "Data will appear once the platform starts producing it",
    "common.error": "Something went wrong", "common.retry": "Retry",
    "common.create": "Create", "common.save": "Save", "common.cancel": "Cancel",
    "common.delete": "Delete", "common.confirm": "Confirm", "common.actions": "Actions",
    "common.status": "Status", "common.name": "Name", "common.close": "Close",
    "common.search": "Search…", "common.refresh": "Refresh", "common.copy": "Copy",
    "common.copied": "Copied to clipboard", "common.never": "Never",
    "common.unlimited": "Unlimited", "common.expired": "Expired", "common.disabled": "Disabled",
    "common.active": "Active", "common.running": "Running", "common.stopped": "Stopped",
    "common.failed": "Failed", "common.degraded": "Degraded",
    "dash.title": "Dashboard", "dash.overview": "Platform overview",
    "dash.totalTraffic": "Total traffic", "dash.activeConnections": "Active connections",
    "dash.runningInstances": "Running instances", "dash.activeLinks": "Active links",
    "dash.users": "Users", "dash.subscriptions": "Subscriptions",
    "dash.health": "System health", "dash.traffic24h": "Traffic (last hours)",
    "dash.degradedNote": "One or more subsystems are degraded — check Health.",
    "inst.title": "Instances", "inst.create": "New instance",
    "inst.name": "Instance name", "inst.region": "Region", "inst.protocols": "Protocols",
    "inst.cpu": "CPU limit", "inst.memory": "Memory (MB)", "inst.publicHost": "Public host (for share links)",
    "inst.start": "Start", "inst.stop": "Stop", "inst.restart": "Restart",
    "inst.links": "Links", "inst.stats": "Live stats", "inst.noInstances": "No instances yet",
    "inst.noInstancesSub": "Create your first isolated proxy runtime",
    "inst.stoppedHint": "Instance is stopped — start it to provision links",
    "inst.addLink": "Add link", "inst.shareLinks": "Get share links",
    "inst.shareHostPrompt": "Host to render (e.g. proxy.example.com)",
    "inst.linkLabel": "Label", "inst.linkProtocol": "Protocol", "inst.linkQuota": "Quota (bytes, 0 = unlimited)",
    "inst.linkExpiry": "Expiry (optional)", "inst.usage": "Usage", "inst.connections": "Connections",
    "nodes.title": "Nodes", "nodes.noNodes": "No nodes registered",
    "nodes.noNodesSub": "Nodes are host entries for distributed deployments",
    "users.title": "Users", "users.create": "New user", "users.noUsers": "No users",
    "users.username": "Username", "users.email": "Email", "users.role": "Role",
    "subs.title": "Subscriptions", "subs.create": "New subscription",
    "subs.noSubs": "No subscriptions yet", "subs.noSubsSub": "Create a plan and assign it to a user",
    "subs.user": "User", "subs.plan": "Plan name", "subs.trafficLimit": "Traffic limit (GB, empty = unlimited)",
    "subs.duration": "Duration", "subs.custom": "Custom", "subs.days": "days",
    "subs.targetInstance": "Target instance", "subs.protocol": "Protocol",
    "subs.used": "Used", "subs.limit": "Limit", "subs.expires": "Expires",
    "subs.extend": "Extend", "subs.renew": "Renew", "subs.revoke": "Revoke",
    "subs.resetTraffic": "Reset traffic", "subs.extendDays": "Days to add",
    "subs.quotaExceeded": "Quota exceeded", "subs.autoRenew": "Auto renew",
    "subs.deviceLimit": "Device limit", "subs.activeDevices": "Active devices",
    "subs.subURL": "Subscription URL", "subs.noLinks": "No links provisioned",
    "traffic.title": "Traffic", "traffic.byInstance": "By instance",
    "traffic.bySubscription": "By subscription", "traffic.topLinks": "Top links",
    "traffic.noData": "No traffic recorded yet",
    "analytics.title": "Analytics", "analytics.overview": "Overview",
    "analytics.protocolDist": "Protocol distribution", "analytics.subStatus": "Subscription status",
    "analytics.hourly": "Hourly traffic (live)", "analytics.recentEvents": "Recent events",
    "conn.title": "Online connections", "conn.noConn": "No clients connected right now",
    "conn.noConnSub": "Connections appear here in real time as clients connect",
    "conn.ip": "Client IP", "conn.sessions": "Sessions", "conn.bytes": "Bytes",
    "conn.transports": "Transports", "conn.lastSeen": "Last seen",
    "diag.title": "Network diagnostics", "diag.intro": "Real measurements only — every latency is an actual timed operation.",
    "diag.target": "Target host", "diag.port": "Port", "diag.run": "Run test",
    "diag.dns": "DNS", "diag.tcp": "TCP connect", "diag.tls": "TLS handshake",
    "diag.http": "HTTP", "diag.ws": "WebSocket", "diag.xhttp": "xHTTP",
    "diag.latency": "Latency", "diag.result": "Result", "diag.chain": "Full chain",
    "diag.limits": "Probes are on-demand, rate limited, and concurrency bounded",
    "diag.serverLatency": "Server (API) latency",
    "diag.wsTunnelLatency": "Tunnel establishment",
    "logs.title": "Logs", "logs.audit": "Audit trail", "logs.core": "Core runtime logs",
    "logs.pickInstance": "Pick a running instance", "logs.noLogs": "No log entries",
    "settings.title": "Settings", "settings.platform": "Platform",
    "settings.about": "About", "settings.note": "Settings are read from environment configuration.",
  },
  fa: {
    "nav.dashboard": "داشبورد", "nav.instances": "اینستنس‌ها", "nav.nodes": "نودها",
    "nav.users": "کاربران", "nav.subscriptions": "اشتراک‌ها", "nav.traffic": "ترافیک",
    "nav.analytics": "تحلیل", "nav.connections": "آنلاین", "nav.diagnostics": "تشخیص شبکه",
    "nav.logs": "لاگ‌ها", "nav.settings": "تنظیمات", "nav.logout": "خروج",
    "login.subtitle": "ورود به کنسول مدیریت", "login.username": "نام کاربری",
    "login.password": "رمز عبور", "login.submit": "ورود", "login.failed": "نام کاربری یا رمز عبور اشتباه است",
    "common.loading": "در حال بارگذاری…", "common.empty": "موردی وجود ندارد",
    "common.emptySub": "با فعالیت پلتفرم، داده‌ها در این بخش نمایش داده می‌شوند",
    "common.error": "خطایی رخ داد", "common.retry": "تلاش مجدد",
    "common.create": "ایجاد", "common.save": "ذخیره", "common.cancel": "انصراف",
    "common.delete": "حذف", "common.confirm": "تأیید", "common.actions": "عملیات",
    "common.status": "وضعیت", "common.name": "نام", "common.close": "بستن",
    "common.search": "جستجو…", "common.refresh": "بازنشانی", "common.copy": "کپی",
    "common.copied": "کپی شد", "common.never": "هرگز",
    "common.unlimited": "نامحدود", "common.expired": "منقضی", "common.disabled": "غیرفعال",
    "common.active": "فعال", "common.running": "در حال اجرا", "common.stopped": "متوقف",
    "common.failed": "ناموفق", "common.degraded": "ناقص",
    "dash.title": "داشبورد", "dash.overview": "نمای کلی پلتفرم",
    "dash.totalTraffic": "ترافیک کل", "dash.activeConnections": "اتصالات فعال",
    "dash.runningInstances": "اینستنس‌های فعال", "dash.activeLinks": "لینک‌های فعال",
    "dash.users": "کاربران", "dash.subscriptions": "اشتراک‌ها",
    "dash.health": "سلامت سیستم", "dash.traffic24h": "ترافیک (ساعات اخیر)",
    "dash.degradedNote": "یک یا چند زیرسیستم دچار اختلال است — بخش سلامت را بررسی کنید.",
    "inst.title": "اینستنس‌ها", "inst.create": "اینستنس جدید",
    "inst.name": "نام اینستنس", "inst.region": "منطقه", "inst.protocols": "پروتکل‌ها",
    "inst.cpu": "محدودیت CPU", "inst.memory": "حافظه (مگابایت)", "inst.publicHost": "دامنه عمومی (برای لینک‌های اشتراک)",
    "inst.start": "اجرا", "inst.stop": "توقف", "inst.restart": "ری‌استارت",
    "inst.links": "لینک‌ها", "inst.stats": "آمار زنده", "inst.noInstances": "هنوز اینستنسی وجود ندارد",
    "inst.noInstancesSub": "اولین هسته پروکسی ایزوله خود را بسازید",
    "inst.stoppedHint": "اینستنس متوقف است — برای ساخت لینک ابتدا آن را اجرا کنید",
    "inst.addLink": "افزودن لینک", "inst.shareLinks": "دریافت لینک‌ها",
    "inst.shareHostPrompt": "دامنه (مثلاً proxy.example.com)",
    "inst.linkLabel": "برچسب", "inst.linkProtocol": "پروتکل", "inst.linkQuota": "سهمیه (بایت، ۰ = نامحدود)",
    "inst.linkExpiry": "انقضا (اختیاری)", "inst.usage": "مصرف", "inst.connections": "اتصالات",
    "nodes.title": "نودها", "nodes.noNodes": "نودی ثبت نشده است",
    "nodes.noNodesSub": "نودها، میزبان‌های استقرار توزیع‌شده هستند",
    "users.title": "کاربران", "users.create": "کاربر جدید", "users.noUsers": "کاربری وجود ندارد",
    "users.username": "نام کاربری", "users.email": "ایمیل", "users.role": "نقش",
    "subs.title": "اشتراک‌ها", "subs.create": "اشتراک جدید",
    "subs.noSubs": "هنوز اشتراکی وجود ندارد", "subs.noSubsSub": "یک پلن بسازید و به کاربر اختصاص دهید",
    "subs.user": "کاربر", "subs.plan": "نام پلن", "subs.trafficLimit": "سقف ترافیک (گیگابایت، خالی = نامحدود)",
    "subs.duration": "مدت", "subs.custom": "سفارشی", "subs.days": "روز",
    "subs.targetInstance": "اینستنس هدف", "subs.protocol": "پروتکل",
    "subs.used": "مصرف", "subs.limit": "سقف", "subs.expires": "انقضا",
    "subs.extend": "تمدید", "subs.renew": "شارژ مجدد", "subs.revoke": "لغو",
    "subs.resetTraffic": "ریست ترافیک", "subs.extendDays": "روز‌های افزوده",
    "subs.quotaExceeded": "سقف ترافیک پر شده", "subs.autoRenew": "تمدید خودکار",
    "subs.deviceLimit": "سقف دستگاه", "subs.activeDevices": "دستگاه‌های فعال",
    "subs.subURL": "آدرس اشتراک", "subs.noLinks": "لینکی ساخته نشده",
    "traffic.title": "ترافیک", "traffic.byInstance": "بر اساس اینستنس",
    "traffic.bySubscription": "بر اساس اشتراک", "traffic.topLinks": "پرمصرف‌ترین لینک‌ها",
    "traffic.noData": "هنوز ترافیکی ثبت نشده",
    "analytics.title": "تحلیل", "analytics.overview": "نمای کلی",
    "analytics.protocolDist": "توزیع پروتکل‌ها", "analytics.subStatus": "وضعیت اشتراک‌ها",
    "analytics.hourly": "ترافیک ساعتی (زنده)", "analytics.recentEvents": "رویدادهای اخیر",
    "conn.title": "اتصالات آنلاین", "conn.noConn": "الان کلاینت متصلی وجود ندارد",
    "conn.noConnSub": "با اتصال کلاینت‌ها، اطلاعات اینجا به‌صورت زنده نمایش داده می‌شود",
    "conn.ip": "آی‌پی کلاینت", "conn.sessions": "نشست‌ها", "conn.bytes": "بایت",
    "conn.transports": "ترنسپورت‌ها", "conn.lastSeen": "آخرین دیدار",
    "diag.title": "تشخیص شبکه", "diag.intro": "فقط اندازه‌گیری واقعی — هر تأخیر عدد یک عملیات زمان‌گیری‌شده است.",
    "diag.target": "مقصد", "diag.port": "پورت", "diag.run": "اجرای تست",
    "diag.dns": "DNS", "diag.tcp": "اتصال TCP", "diag.tls": "دست‌دادن TLS",
    "diag.http": "HTTP", "diag.ws": "وب‌سوکت", "diag.xhttp": "xHTTP",
    "diag.latency": "تأخیر", "diag.result": "نتیجه", "diag.chain": "زنجیره کامل",
    "diag.limits": "تست‌ها فقط در لحظه، با محدودیت نرخ و همزمانی اجرا می‌شوند",
    "diag.serverLatency": "تأخیر سرور (API)",
    "diag.wsTunnelLatency": "برقراری تونل",
    "logs.title": "لاگ‌ها", "logs.audit": "رد حسابرسی", "logs.core": "لاگ هسته",
    "logs.pickInstance": "یک اینستنس در حال اجرا انتخاب کنید", "logs.noLogs": "لاگی وجود ندارد",
    "settings.title": "تنظیمات", "settings.platform": "پلتفرم",
    "settings.about": "درباره", "settings.note": "تنظیمات از پیکربندی محیطی خوانده می‌شوند.",
  },
};

let current = localStorage.getItem("emunel.lang") || "en";

export function t(key) {
  return (dict[current] && dict[current][key]) || dict.en[key] || key;
}

export function lang() {
  return current;
}

export function setLang(l) {
  if (!dict[l]) return;
  current = l;
  localStorage.setItem("emunel.lang", l);
  applyDirection();
}

export function toggleLang() {
  setLang(current === "en" ? "fa" : "en");
}

export function applyDirection() {
  document.documentElement.dir = current === "fa" ? "rtl" : "ltr";
  document.documentElement.lang = current;
}

export function applyTranslations(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
}
