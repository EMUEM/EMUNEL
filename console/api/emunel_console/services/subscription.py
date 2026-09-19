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


def _config_card(config: dict, number: int) -> str:
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
      <div class="link-row">
       <input type="text" readonly value="{escape(url)}" id="link-{number}">
       <button type="button" class="copy-btn copy-single" data-num="{number}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9v3"/></svg>
        <span class="lbl">Copy</span>
       </button>
      </div>
     </div></div></div>
    </div>'''


def render_subscription(title: str, configs: list, host: str, sub_path: str,
                        qr_path: str = "") -> str:
    """Keep browser-announced hosts when clients import the copied subscription."""
    sub_url = f"https://{host}{sub_path}?{urlencode({'host': host})}"
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(sub_url)
    qr.make(fit=True)
    now = datetime.now(timezone.utc)
    cards = ([{"share_url": TELEGRAM_CONFIG}] if TELEGRAM_CONFIG else []) + list(configs)
    values = {
        "BRAND": "EMUNEL",
        "TITLE": escape(title),
        "SUB_URL": escape(sub_url),
        "SUB_URL_JSON": json.dumps(sub_url).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"),
        "QR_MATRIX": json.dumps(qr.get_matrix(), separators=(",", ":")),
        "COUNT": str(len(configs)),
        "CONFIG_CARDS": "\n".join(_config_card(c, i) for i, c in enumerate(cards, 1))
            + ('' if configs else '<div class="empty-configs">No configurations available</div>'),
        "YEAR": str(now.year),
        "UPDATED": now.strftime("%Y/%m/%d %H:%M:%S UTC"),
    }
    return re.sub(r"@@([A-Z_]+)@@", lambda match: values[match[1]], _template())
