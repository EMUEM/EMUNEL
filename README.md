# ⚡ EMUNEL

```
███████╗███╗   ███╗██╗   ██╗███╗   ██╗███████╗██╗     
██╔════╝████╗ ████║██║   ██║████╗  ██║██╔════╝██║     
█████╗  ██╔████╔██║██║   ██║██╔██╗ ██║█████╗  ██║     
██╔══╝  ██║╚██╔╝██║██║   ██║██║╚██╗██║██╔══╝  ██║     
███████╗██║ ╚═╝ ██║╚██████╔╝██║ ╚████║███████╗███████╗
╚══════╝╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝╚══════╝
```

![Python](https://img.shields.io/badge/Python-3.11+-blue?style=flat-square&logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green?style=flat-square&logo=fastapi)
![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)
![Docker](https://img.shields.io/badge/Docker-Ready-blue?style=flat-square&logo=docker)
![PWA](https://img.shields.io/badge/PWA-Enabled-purple?style=flat-square)

> **Premium multi-protocol proxy management platform**

EMUNEL is a modern, feature-rich proxy management platform with support for VLESS, Trojan, and Shadowsocks protocols. Built with a stunning glass UI, dark mode, mobile-first design, subscription management, and real network diagnostics.

---

## ✨ Features

🔐 **Multi-Protocol Support** — VLESS, Trojan, Shadowsocks with modular plugin architecture  
🎨 **Glass UI Design** — Premium dark theme with glassmorphism, neon accents, smooth animations  
📱 **Mobile-First PWA** — Installable progressive web app, responsive on all devices  
👥 **User Management** — Role-based access, JWT auth, API keys, subscription tiers  
📊 **Real-Time Dashboard** — Live CPU, RAM, Disk, Network stats with canvas charts  
🔄 **Subscription Engine** — Traffic limits, auto-renew, auto-disable, monthly resets, device limits  
🌐 **Network Diagnostics** — Real TCP, TLS, DNS, WebSocket, and latency tests (no fakes)  
🏗️ **Node Management** — Multi-node deployment with Docker/process drivers and heartbeat  
📈 **Analytics & Logs** — Traffic analytics, audit trails, Prometheus metrics  
🌍 **i18n** — English + Persian (فارسی) with full RTL support  
🐳 **Docker Ready** — Full stack docker-compose with PostgreSQL, Nginx reverse proxy  
🔌 **Plugin System** — Extensible protocol support via plugin registry  

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────┐
│                    EMUNEL Platform                    │
├──────────┬──────────────┬──────────┬────────────────┤
│          │              │          │                │
│  Dashboard│    API       │  Core    │    Worker      │
│  (SPA)   │  (FastAPI)   │ (Proxy)  │   (Agent)      │
│          │              │          │                │
│  Glass UI│  /api/v1/*   │  VLESS   │  Docker Driver │
│  PWA     │  JWT Auth    │  Trojan  │  Process Driver│
│  i18n    │  PostgreSQL  │  SS      │  Heartbeat     │
│  Charts  │  Prometheus  │  Plugins │  Auto-deploy   │
│          │              │          │                │
├──────────┴──────────────┴──────────┴────────────────┤
│              Docker Compose / Nginx                   │
│              PostgreSQL / SQLite                      │
└─────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### Docker (Recommended)

```bash
git clone https://github.com/mehdialadi-star/EMUNEL.git
cd EMUNEL
cp .env.example .env
# Edit .env with your settings
docker compose -f docker/docker-compose.yml up -d
```

Dashboard: `http://localhost:8080`  
API Docs: `http://localhost:8000/api/docs`

### Local Development

```bash
git clone https://github.com/mehdialadi-star/EMUNEL.git
cd EMUNEL
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

### Railway One-Click Deploy

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template)

---

## 📸 Screenshots

> Screenshots coming soon — the glass UI is worth the wait ✨

| Dashboard | Nodes | Network Tests |
|-----------|-------|---------------|
| *Coming soon* | *Coming soon* | *Coming soon* |

---

## 🛠️ Tech Stack

| Layer | Technology |
|-------|------------|
| **Backend** | Python 3.11+, FastAPI, SQLAlchemy (async), Pydantic v2 |
| **Frontend** | Vanilla ES Modules, CSS Glass Design System, Canvas Charts |
| **Database** | PostgreSQL (primary), SQLite (fallback) |
| **Proxy Core** | asyncio, VLESS/Trojan/Shadowsocks relay |
| **Worker** | Docker SDK, Process management, Heartbeat |
| **Infra** | Docker Compose, Nginx, Prometheus |
| **Auth** | JWT, API Keys, RBAC |
| **i18n** | EN + FA with RTL |

---

## 📁 Project Structure

```
EMUNEL/
├── core/              # Proxy runtime (VLESS, Trojan, SS)
├── api/               # FastAPI backend (versioned)
├── dashboard/         # Frontend SPA (no build step)
├── worker/            # Node agent with drivers
├── docker/            # Docker Compose & Nginx
├── docs/              # Architecture, API, Deployment docs
├── tests/             # Test suite
├── main.py            # Single-service entry point
└── requirements.txt   # Combined dependencies
```

---

## 📖 Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [API Reference](docs/API.md)
- [Deployment Guide](docs/DEPLOYMENT.md)
- [Security](docs/SECURITY.md)
- [Development](docs/DEVELOPMENT.md)

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'feat: add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

<p align="center">
  <b>EMUNEL</b> — Premium Proxy Management, Redefined ⚡
</p>