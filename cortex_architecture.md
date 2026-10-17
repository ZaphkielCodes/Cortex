# Cortex — Personal Cross-Device Agent

## 1. High-Level Concept
A central "brain" (hub) that holds context/state, plus lightweight agents on each device (phone, tablet, laptop 1, laptop 2) that execute commands and report status back. Devices are **clients**. The hub is the single source of truth for context, permissions, and logs.

## 2. Core Components

**A. Hub (central server)**
- Small always-on service: cheap cloud VM or free-tier managed DB (Supabase/Postgres, or Redis for hot state)
- Stores: device status, context/state, permission tiers, action logs, user location/presence, calendar data
- Exposes an API (REST or WebSocket) that all devices talk to

**B. Device Agents (one per device)**
- Lightweight background service/app installed on each device
- Responsibilities:
  - Report status (on/off/idle/active) to hub
  - Listen for commands from hub
  - Execute local actions (screenshot, open app, run command, adjust settings)
  - Local "light" monitoring (active window, app usage, URL domain) — NOT constant video capture
  - Escalate to full screen/vision analysis only when triggered

**C. Presence / Context Layer**
- GPS (phone) + Wi-Fi network detection (home vs. campus)
- Calendar integration (class schedule)
- Optional: fitness app API (gym attendance)
- Feeds a simple state machine: HOME / CAMPUS / GYM / UNKNOWN → triggers automations

**D. Permission System (3 tiers)**
- **Tier 1 – Free rein:** read files, screenshots, open apps, move data between own devices
- **Tier 2 – Ask first (low stakes):** install app, change setting, send message/email
- **Tier 3 – Always confirm w/ specifics shown:** delete, wipe, remote shutdown, terminal commands
- Every action logged with timestamp + device + outcome (for undo/audit trail)

**E. Wake / Power Layer**
- Wake-on-LAN for devices in sleep/low-power state (not full power-off)
- Trigger conditions: only "armed" when presence layer = HOME (disabled on campus)

**F. Suggestion / "Digital Conscience" Engine**
- Pattern tracking across categories: study, soccer, gym, gaming (time logs from device agents + calendar)
- Rule-based nudges first (e.g. "no gym in 6 days" → suggest), ML-based pattern detection later
- Lightweight local checks (window titles, app usage, domains) as first pass; heavier AI vision/analysis only triggered on a suspicious pattern (e.g. flag a possible scam email)

## 4. Secure Network Layer (Phase 3)

**Problem:** The hub binds to `0.0.0.0:8000` so both localhost and Tailscale peers can connect. This means any IP on any interface could potentially reach the hub.

**Solution: Default-deny peer filtering + WireGuard tunnel.**

### Peer-Address Filtering (`PeerFilterMiddleware`)

Every incoming HTTP/WebSocket request is checked **before any authentication logic**:

```
allowed = peer_ip is localhost (127.0.0.0/8, ::1)
       OR peer_ip falls within 100.64.0.0/10  (Tailscale CGNAT range)
```

All other sources (LAN peers, public IPs) are rejected with `403 Forbidden`.

Configurable via environment variables:
- `CORTEX_TAILSCALE_CIDR=100.64.0.0/10` — trust range
- `CORTEX_TAILSCALE_ADDR=100.100.50.3` — trust only one specific IP
- `CORTEX_ALLOW_LAN=true` — temporarily trust RFC-1918 ranges for local dev

### Authentication Model

| Client | Authentication |
|---|---|
| **Agent** (`/ws/{device_id}`) | Challenge + secret (bcrypt-verified) |
| **CLI** (`/ws-cli`) | Challenge + secret (same flow as agents) |
| **HTTP read** (`/devices`, `/logs`, `/pending`) | `X-Cortex-Device` + `X-Cortex-Secret` headers |
| **HTTP `/bootstrap`** | Localhost-only restriction |
| **HTTP `/enroll`** | Admin auth headers required |
| **HTTP `/rotate-secret`** | Admin auth headers required |

### Command Resilience

- **Duplicate protection:** Every `command_id` is persisted in SQLite's `command_history` table. Duplicates are rejected. Survives hub restarts.
- **Command expiry:** Commands older than `COMMAND_TTL` (default 300s) are rejected. Uses hub-generated `received_at` timestamps (never client clocks).
- **History pruning:** Old `command_history` rows (>24h) are deleted on startup and every hour.

### Secret Rotation

Admin can rotate any device's secret via `rotate <device_id>` in the CLI. The old secret is invalidated immediately. The new secret is shown once and must be manually delivered to the target device.

---

## 5. Data Flow Example
1. You're gaming on Laptop 1, say "pull up build guide on tablet"
2. Laptop 1 agent sends command to Hub
3. Hub checks: is tablet online? permission tier for this action? (Tier 1, free rein)
4. Hub forwards command to Tablet agent
5. Tablet agent opens browser/app, displays reference
6. Hub logs the action

## 6. Suggested Tech Stack (student-friendly / free-tier)
- **Hub backend:** Python (FastAPI) or Node.js, hosted on a free-tier VM (Oracle Cloud free tier / Fly.io / Railway)
- **Database:** Supabase (Postgres, free tier) or SQLite for a pure local-first version
- **Messaging (hub ↔ devices):** WebSockets (real-time) or MQTT (lightweight, great for IoT-style device messaging)
- **Device agents:** Python scripts (laptops), or a small native app (tablet/phone) — can start laptop-only as MVP
- **Wake-on-LAN:** standard WoL magic packet, triggered by hub when presence = HOME
- **Calendar:** Google Calendar API
- **Location/presence:** phone GPS via a simple companion app, or just Wi-Fi SSID detection (simpler MVP)
- **Networking:** Tailscale (free tier, WireGuard-based) for cross-network device connectivity

## 7. Development Phases
- ~~**Phase 1: Core CLI**~~ ✅
  ~~Basic hub/agent communication, commands, device registration, admin/sub-admin roles.~~
- ~~**Phase 2: Networking + Persistence**~~ ✅
  ~~WebSockets, device state, database, reconnects, cross-device communication.~~
- ~~**Phase 3: Security Layer**~~ ✅
  ~~Authentication, secrets, Tailscale/WireGuard support, secure cross-network access, log scrubbing.~~
- ~~**Phase 4: Control Center**~~ ✅
  ~~GUI, dynamic capabilities, permissions, approvals, background agents, live device/activity view.~~
- **Phase 5: Memory + Tasks + Integrations**
  Persistent memory, task engine, scheduled/background jobs, Gmail/calendar integrations, cross-device task continuation.
- **Phase 6: AI Conversation Layer**
  Natural-language control, conversation continuity, context manager, coding/assignment workflows, command interpretation.
- **Phase 7: Voice + Ambient Cortex**
  Push-to-talk, wake word, voice responses, floating orb, edge glow, smart popups, proactive reminders.
- **Phase 8: Social + Relationship Intelligence**
  Remember people/relationships, understand who is talking, adapt between friend/mentor/assistant modes, participate in conversations appropriately.
- **Phase 9: Advanced Personal Operations**
  Finance monitoring, broader service integrations, richer automations, multi-step autonomous workflows with strict permissions.
- **Phase 10: Mature Platform**
  Plugin SDK, third-party device support, remote deployment, better service management, recovery/failover, scaling, maybe multi-user support if you ever want it.
