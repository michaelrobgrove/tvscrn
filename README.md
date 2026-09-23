# tvscrn — Remote ADB Management for Android TV Boxes

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Release](https://img.shields.io/badge/Release-v2.0.0-blue.svg)](https://github.com/tvscrn/tvscrn/releases)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8+-yellow.svg)](https://python.org)

> Open-source remote management and monitoring for Android TV boxes. Control any ADB-enabled device from your browser — tap, swipe, type, navigate — with near-instant WebRTC video.

**Free for personal and commercial use.** Modifications require attribution ("Powered by tvscrn" with a link to the GitHub repository).

---

<p align="center">
  <img src="images/list.png" alt="tvscrn Dashboard" width="800">
</p>

---

## Why tvscrn?

Most remote management tools for Android TV are either commercial (per-device licensing), require a native client (TeamViewer, AnyDesk), or don't support Android TV at all.

**tvscrn is different:**
- **Self-hosted** — your data stays on your server
- **Browser-based** — no native client needed, works on any device
- **Lightweight** — runs on a $5/mo VPS with 1GB RAM
- **Multi-device** — manage unlimited boxes from one dashboard
- **Reseller-ready** — multi-user roles, audit logging, Docker deployment

Built for IPTV support teams, resellers, and anyone managing streaming devices at scale.

---

## Quick Start

### Docker (30 seconds)

```bash
git clone https://github.com/tvscrn/tvscrn.git
cd tvscrn
docker compose up -d
```

Open `http://localhost:5000` — the setup wizard creates your admin account.

### Manual Install

```bash
# Prerequisites
apt-get install -y python3 python3-venv ffmpeg scrcpy adb xvfb xauth

# Clone and setup
git clone https://github.com/tvscrn/tvscrn.git
cd tvscrn
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Start services
Xvfb :99 -screen 0 1920x1080x24 -ac &
export DISPLAY=:99
python3 app.py
```

Open `http://localhost:5000` and follow the setup wizard.

---

## Features

<p align="center">
  <img src="images/users.png" alt="User List" width="800">
</p>

| Feature | Description |
|---------|-------------|
| **Live Screen Sharing** | Near-instant WebRTC video of the box's display |
| **Remote Control** | Tap, swipe, type, navigate via ADB input injection |
| **One-Click Login** | Send usernames/passwords to devices with a button |
| **Multi-User** | Admin and Operator roles with granular permissions |
| **Audit Trail** | Every action logged with user, timestamp, and details |
| **Subscription Tracking** | Expiration dates with automatic email alerts |
| **Docker Ready** | Single container deployment with SQLite or MariaDB |
| **Rebrandable** | Easy to customize for your organization |

---

## Connecting Your First Box

### 1. Enable ADB on the Box

On your Android TV box:
- Go to **Settings → Device Preferences → Developer Options**
- Enable **USB Debugging** (or **Network Debugging**)
- Note the box's IP address (Settings → Network → Wi-Fi → IP Address)

### 2. Verify Connectivity

From the tvscrn server:
```bash
adb connect <box-ip>:5555
adb devices  # Should show "connected"
```

### 3. Add the Device

- Open tvscrn → Click **Add Device**
- Enter: Customer Name, IP Address, Device Type
- Optionally enter IPTV credentials (for one-click login)
- Click **Save**

### 4. Connect

Click the green **Connect** button on the device row. The VNC modal opens with live video and remote control toolbar.

<p align="center">
  <img src="images/settings.png" alt="Settings" width="800">
</p>

### Toolbar

| Button | Action |
|--------|--------|
| Home / Back / OK | Android navigation |
| ↑ ↓ ← → | D-pad navigation |
| V+ / V- | Volume control |
| **User** | Type the device's IPTV username |
| **Pass** | Type the device's IPTV password |
| Disconnect | End the session |

---

## How It Works

```
Your Browser ←→ WebRTC ←→ mediamtx ←→ RTSP ←→ ffmpeg ←→ Xvfb ←→ scrcpy ←→ Android Box
```

1. **scrcpy** mirrors the Android screen via ADB
2. **Xvfb** provides a virtual display for scrcpy
3. **ffmpeg** captures and encodes to H.264
4. **mediamtx** serves via WebRTC (browser-native, no plugins)
5. **ADB commands** handle input injection (tap, key, text, swipe)

---

## Configuration

### Environment Variables

```bash
# Database (default: SQLite, set for MariaDB)
DB_URL=mariadb+pymysql://user:password@localhost:3306/tvscrn

# Flask secret key (generate for production)
SECRET_KEY=your-random-secret-key

# SQLite path (used when DB_URL is empty)
DB_PATH=/app/data/devices.db
```

### MariaDB (Optional)

For multi-user or production deployments:

```bash
# Install MariaDB
apt-get install -y mariadb-server
systemctl enable --now mariadb

# Create database
mysql -e "CREATE DATABASE tvscrn; CREATE USER 'tvscrn'@'localhost' IDENTIFIED BY 'password'; GRANT ALL ON tvscrn.* TO 'tvscrn'@'localhost';"

# Set environment
export DB_URL="mariadb+pymysql://tvscrn:password@localhost:3306/tvscrn"
```

### Email Alerts

Configure in **Settings → Email**:
1. Enter SMTP details (Gmail: `smtp.gmail.com:587`)
2. Set notification email
3. Click **Test Email**
4. tvscrn checks daily for expiring subscriptions

---

## Networking

### Same Network (Simplest)

```
tvscrn server ←→ Router ←→ Android Box
```

### Different Locations (Recommended)

Use [Tailscale](https://tailscale.com) or [Headscale](https://headscale.net):

```bash
# Install Tailscale on server
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up

# Use Tailscale IPs in tvscrn
```

Benefits:
- No port forwarding needed
- Encrypted traffic
- Works behind NAT/firewalls
- Access boxes from anywhere

### Required Ports

| Port | Purpose |
|------|---------|
| 5000 | Web UI |
| 8555 | RTSP (internal) |
| 8889 | WebRTC (internal) |
| 5555 | ADB (to each box) |

---

## User Roles

| Role | Permissions |
|------|------------|
| **Admin** | Full access — users, devices, settings, audit logs |
| **Operator** | View devices, connect/control, type credentials |

---

## Troubleshooting

### "Can't connect to box"

```bash
# 1. Check network
ping <box-ip>

# 2. Check ADB
adb connect <box-ip>:5555
adb devices

# 3. Check firewall
nc -zv <box-ip> 5555
```

### "Black screen / no video"

```bash
# Check scrcpy
ps aux | grep scrcpy

# Check Xvfb
ps aux | grep Xvfb
# Restart if needed:
Xvfb :99 -screen 0 1920x1080x24 -ac &
```

### "Choppy stream"

1. Reduce resolution: edit `app.py`, change `--max-size=1920` to `--max-size=1280`
2. Lower bitrate: change `-b:v 3000k` to `-b:v 1500k`
3. Check network stability

### "Database locked" (SQLite)

Switch to MariaDB — see Configuration section above.

### "App won't start"

```bash
journalctl -u tvscrn -n 50
# Common: port 5000 in use, missing dependencies, DB permissions
```

### Known Limitations

- **Hardware video overlays** (YouTube, Netflix) don't appear in the stream — this is a scrcpy limitation
- **Audio** is not captured (disabled for performance/privacy)
- **Session timeout** is 30 minutes (configurable in `app.py`)

---

## FAQ

**Q: Does this work with Fire TV Stick / Roku / Apple TV?**
A: Android TV boxes with ADB support. Fire TV Stick works. Roku and Apple TV are not supported.

**Q: Can the box user see I'm connected?**
A: No. ADB connections are invisible to the end user.

**Q: How many boxes can I manage?**
A: ~5-10 simultaneous sessions on a 4GB RAM server. Each session uses ~100-200MB.

**Q: Is this secure?**
A: Use a VPN (Tailscale/Headscale), strong passwords, and audit logging. Don't expose port 5000 to the internet without HTTPS.

**Q: Can I rebrand this?**
A: Yes. See [CONTRIBUTING.md](CONTRIBUTING.md) for the rebranding guide. Keep the "Powered by tvscrn" attribution.

---

## Rebranding

tvscrn is designed for easy customization. Search for `tvscrn` in all files to find branding points:

| File | What to Change |
|------|---------------|
| `templates/login.html` | Login page title |
| `templates/dashboard.html` | Navbar, footer |
| `templates/settings.html` | Settings title |
| `app.py` | Email templates, startup banner |
| `docker-compose.yml` | Container name |

Keep the footer attribution: `Powered by <a href="https://github.com/tvscrn/tvscrn">tvscrn</a>`

---

## Audit Logging

<p align="center">
  <img src="images/log.png" alt="Audit Log" width="800">
</p>

Every action is logged in the audit trail:
- Logins and logouts
- Device connections and disconnections
- ADB input (keys, taps, text, swipes)
- Device reboots
- Password changes
- User management actions

View audit logs from **Settings → Audit Log**. Filter by user, action type, or date range.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/devices/<id>/connect` | Start VNC session |
| POST | `/api/devices/<id>/disconnect` | End session |
| POST | `/api/devices/<id>/input/key` | Send Android keycode |
| POST | `/api/devices/<id>/input/text` | Type text |
| GET | `/api/devices/<id>/credentials` | Get device credentials |
| GET | `/api/users` | List users (admin) |
| GET | `/api/audit-log` | Query audit logs (admin) |

---

## License

MIT License — see [LICENSE](LICENSE) for details.

**Powered by [tvscrn](https://github.com/tvscrn/tvscrn)**
