"""Render the public subscription page without exposing Core API credentials."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from html import escape
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import qrcode


TELEGRAM_CONFIG = ""  # reserved for operator-configured channel config (off by default)


@lru_cache(maxsize=1)
def _template() -> str:
    return Path(__file__).with_name("subscription.html").read_text(encoding="utf-8")


def _quota_row(state: dict | None) -> str:
    """Per-config real quota strip inside a config card (AHB-style):
    usage meter + limit / used / remaining, expiry, status."""
    if not state:
        return ""
    limit = int(state.get("limit_bytes") or 0)
    used = int(state.get("used_bytes") or 0)
    parts = []
    status = state.get("status") or "active"
    label = {"active": "Active", "limited": "Limited — quota reached",
             "expired": "Expired", "disabled": "Disabled"}.get(status, status)
    color = {"active": "var(--blue)", "limited": "var(--amber)",
             "expired": "var(--red)", "disabled": "var(--red)"}.get(status, "var(--blue)")
    if limit:
        pct = float(state.get("percent") or 0.0)
        bar_color = "red" if state.get("exceeded") else ("amber" if pct > 80 else "blue")
        width = max(0.0, min(100.0, pct))
        parts.append(
            f'<div class="detail-item quota"><label>Quota</label>'
            f'<span>{_fmt_bytes(used)} of {_fmt_bytes(limit)} · '
            f'{_fmt_bytes(max(0, limit - used))} left · {pct:.1f}%</span></div>'
            f'<div class="bar" style="margin:6px 0 2px"><div class="fill" '
            f'style="width:{width:.1f}%;background:var(--{bar_color})"></div></div>'
        )
    else:
        parts.append(
            f'<div class="detail-item"><label>Quota</label>'
            f'<span>{_fmt_bytes(used)} used · unlimited</span></div>'
        )
    if state.get("expires_at"):
        try:
            dt = datetime.fromisoformat(str(state["expires_at"]).replace("Z", "+00:00"))
            until = dt.strftime("%b %d, %Y")
            seconds = state.get("seconds_remaining")
            left = _fmt_left(float(seconds)) if seconds is not None else "—"
            parts.append(
                f'<div class="detail-item"><label>Valid</label>'
                f'<span>{left} · until {until}</span></div>'
            )
        except ValueError:
            pass
    speed = int(state.get("speed_limit_bytes") or 0)
    if speed:
        mbits = speed * 8 / 1024 / 1024
        parts.append(f'<div class="detail-item"><label>Speed</label><span>{mbits:g} Mbps</span></div>')
    ip_limit = int(state.get("ip_limit") or 0)
    if ip_limit:
        parts.append(f'<div class="detail-item"><label>IP limit</label><span>{ip_limit}</span></div>')
    if status != "active":
        parts.append(
            f'<div class="detail-item"><label>Status</label>'
            f'<span style="color:{color};font-weight:600">{escape(label)}</span></div>'
        )
    if not parts:
        return ""
    return f'<div class="details-grid quota-grid">{"".join(parts)}</div>'


def _config_card(config: dict, number: int, link_state: dict | None = None) -> str:
    url = str(config["share_url"])
    if url.startswith("vmess://"):
        import base64
        data = json.loads(base64.b64decode(url[8:] + "=" * (-len(url[8:]) % 4)))
        params = {"type": "ws", "security": data.get("tls", ""), "path": data.get("path", ""),
                  "sni": data.get("sni", ""), "alpn": data.get("alpn", "http/1.1")}
        parsed = urlsplit("vmess://" + str(data["id"]) + "@" + str(data["add"]) + ":" + str(data["port"]))
        vmess_name = str(data.get("ps") or config.get("label") or "VMess")
    else:
        parsed = urlsplit(url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        vmess_name = ""
    protocol = {"ss": "SS", "vless": "VLESS", "trojan": "TROJAN"}.get(
        parsed.scheme, parsed.scheme.upper()
    )
    network = params.get("type", "")
    transport = network.upper()
    if parsed.scheme == "ss":
        plugin = params.get("plugin", "").split(";")
        options = dict(part.split("=", 1) for part in plugin if "=" in part)
        network = options.get("mode", "")
        transport = "WS" if network == "websocket" else "AEAD"
        params.update({"path": options.get("path", ""),
                       "security": "tls" if "tls" in plugin else ""})
    host = parsed.hostname or ""
    port = str(parsed.port or 443)
    name = vmess_name or unquote(parsed.fragment) or str(config.get("label") or f"Config {number}")
    flags = re.findall(r"[\U0001f1e6-\U0001f1ff]{2}", name)
    flag = f'<span class="flag">{escape(flags[0])}</span>' if flags else ""
    display_name = re.sub(r"[\U0001f1e6-\U0001f1ff]{2}", "", name).strip(" |") or name
    details = [("Protocol", protocol), ("Host", host), ("Port", port),
               ("Security", params.get("security", "")), ("Network", network)]
    details.extend((label, params.get(key, "")) for label, key in (
        ("SNI", "sni"), ("FP", "fp"), ("Mode", "mode"), ("Path", "path"),
        ("ALPN", "alpn"), ("Allow Insecure", "allowInsecure")))
    detail_html = "".join(
        f'<div class="detail-item"><label>{label}</label><span>{escape(value)}</span></div>'
        for label, value in details if value
    )
    quota_html = _quota_row(link_state)
    badges = f'<span class="badge {escape(protocol.lower())}">{escape(protocol)}</span>'
    if transport and transport != protocol:
        badges += f'<span class="badge {escape(transport.lower())}">{escape(transport)}</span>'
    return f'''<div class="cfg-card tz" data-num="{number}">
     <div class="head" data-toggle="cfg">
      <span class="num">#{number}</span>{flag}
      <span class="info"><span class="name">{escape(display_name)}</span>
       <span class="host">{escape(host)}:{escape(port)}</span></span>
      <span class="badges">{badges}</span>
      <span class="arrow"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="6 9 12 15 18 9"/></svg></span>
     </div>
     <div class="acc"><div class="clip"><div class="inner cfg-inner">
      <div class="details-grid">{detail_html}</div>
      {quota_html}
      <div class="link-row">
       <input type="text" readonly value="{escape(url)}" id="link-{number}">
       <button type="button" class="copy-btn copy-single" data-num="{number}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9v3"/></svg>
        <span class="lbl">Copy</span>
       </button>
      </div>
     </div></div></div>
    </div>'''


def _fmt_bytes(n: float) -> str:
    gb = n / 1024 ** 3
    if gb >= 1:
        return f"{gb:.2f} GB"
    mb = n / 1024 ** 2
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{n / 1024:.0f} KB"


def _fmt_left(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    if days >= 1:
        return f"{days}d {hours}h left"
    minutes = int((seconds % 3600) // 60)
    if hours >= 1:
        return f"{hours}h {minutes}m left"
    return f"{minutes}m left"


def _stat_card(label: str, pct: str, fill_pct: float, color: str,
               main: str, sub: str, *, main_style: str = "") -> str:
    width = max(0.0, min(100.0, fill_pct))
    style = f" style={main_style!r}".replace("'", '"') if main_style else ""
    return (
        '<div class="stat-card tz">'
        f'<div class="label"><span>{escape(label)}</span>'
        f'<span class="pct">{escape(pct)}</span></div>'
        f'<div class="bar"><div class="fill" style="width:{width:.1f}%;'
        f'background:var(--{color})"></div></div>'
        f'<div class="values"><b{style}>{escape(main)}</b>'
        f'<span>{escape(sub)}</span></div>'
        '</div>'
    )


def _volume_card(quota: dict | None) -> str:
    """Remaining-data stat card. quota=None keeps the legacy 'not reported'
    look; a set limit renders the instance's real cap and usage."""
    if not quota or not quota.get("limit_bytes"):
        used = quota.get("used_bytes") if quota else None
        sub = "Not reported / ∞" if used is None else f"{_fmt_bytes(used)} used · unlimited"
        return _stat_card("Remaining", "∞", 100, "blue", "∞", sub)
    limit = int(quota["limit_bytes"])
    used = int(quota.get("used_bytes") or 0)
    remaining = max(0, limit - used)
    pct = float(quota.get("percent") or 0.0)
    color = "red" if quota.get("exceeded") else ("amber" if pct > 80 else "blue")
    return _stat_card("Remaining", f"{pct:.1f}%", pct, color,
                      _fmt_bytes(remaining), f"{_fmt_bytes(used)} of {_fmt_bytes(limit)}")


def _time_card(quota: dict | None) -> str:
    """Time stat card. No expiry set → the legacy Unlimited look."""
    expires_at = quota.get("expires_at") if quota else None
    if not expires_at:
        return _stat_card("Time", "∞", 100, "blue", "Unlimited", "—",
                          main_style="color:var(--blue)")
    try:
        dt = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return _stat_card("Time", "∞", 100, "blue", "Unlimited", "—",
                          main_style="color:var(--blue)")
    until = dt.strftime("%b %d, %Y")
    seconds = quota.get("seconds_remaining")
    seconds = float(seconds) if seconds is not None else None
    if seconds is not None and seconds <= 0:
        return _stat_card("Time", "0%", 0, "red", "Expired",
                          f"ended {until}", main_style="color:var(--red)")
    days = quota.get("time_limit_days")
    if seconds is not None and days:
        total = float(days) * 86400.0
        pct = max(0.0, min(100.0, seconds / total * 100.0))
        color = "amber" if pct < 20 else "blue"
        return _stat_card("Time", f"{pct:.0f}%", pct, color,
                          _fmt_left(seconds), f"until {until}",
                          main_style="color:var(--blue)")
    return _stat_card("Time", "—", 100, "blue", _fmt_left(seconds) if seconds is not None else "—",
                      f"until {until}", main_style="color:var(--blue)")


def _status_line(quota: dict | None) -> tuple[str, str]:
    if quota and quota.get("expired"):
        return "s-expired", "Expired"
    if quota and quota.get("exceeded"):
        return "s-limited", "Limited"
    return "s-active", "Active"


def render_subscription(title: str, configs: list, host: str, sub_path: str,
                        qr_path: str = "", quota: dict | None = None,
                        link_states: dict | None = None) -> str:
    """Keep browser-announced hosts when clients import the copied subscription.

    quota is the effective quota state (instance envelope, or the aggregate
    of the per-config caps when no envelope is set); it renders the
    Remaining/Time stat cards and the status dot. None keeps the legacy
    unlimited/not-reported presentation. link_states (uuid → state) adds the
    per-config real quota strip inside each config card."""
    sub_url = f"https://{host}{sub_path}?{urlencode({'host': host})}"
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(sub_url)
    qr.make(fit=True)
    now = datetime.now(timezone.utc)
    link_states = link_states or {}
    cards = ([{"share_url": TELEGRAM_CONFIG}] if TELEGRAM_CONFIG else []) + list(configs)
    status_class, status_label = _status_line(quota)
    values = {
        "BRAND": "EMUNEL",
        "TITLE": escape(title),
        "STATUS_CLASS": status_class,
        "STATUS_LABEL": escape(status_label),
        "VOLUME_CARD": _volume_card(quota),
        "TIME_CARD": _time_card(quota),
        "SUB_URL": escape(sub_url),
        "SUB_URL_JSON": json.dumps(sub_url).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"),
        "QR_MATRIX": json.dumps(qr.get_matrix(), separators=(",", ":")),
        "COUNT": str(len(configs)),
        "CONFIG_CARDS": "\n".join(
            _config_card(c, i, link_states.get(c.get("uuid"))) for i, c in enumerate(cards, 1)
        )
            + ('' if configs else '<div class="empty-configs">No configurations available</div>'),
        "YEAR": str(now.year),
        "UPDATED": now.strftime("%Y/%m/%d %H:%M:%S UTC"),
    }
    return re.sub(r"@@([A-Z_]+)@@", lambda match: values[match[1]], _template())
