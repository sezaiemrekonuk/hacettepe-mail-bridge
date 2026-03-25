# Hacettepe Mail Bridge

> Because Hacettepe disabled IMAP forwarding and you refuse to check two inboxes like some kind of animal.

A headless Playwright bot that logs into your Hacettepe University email (OWA), scrapes new messages, and forwards them straight to your Gmail. Runs in a Docker container on a cheap VPS. Set it and forget it.

---

## The Problem

Hacettepe University runs an on-prem Exchange server with Outlook Web App. There's no IMAP, no POP3, no native forwarding, no API access. You either check `posta.hacettepe.edu.tr` manually or you miss that one critical email about your midterm schedule that was sent at 2 AM on a Sunday.

## The Solution

A tiny robot that:

1. Logs into OWA through Microsoft SSO like a real human (it even has a Chrome user-agent to prove it)
2. Reads your inbox every 15 minutes
3. Forwards anything new to your Gmail
4. Remembers what it already sent (SQLite) so you don't get 2,048 duplicate emails

```
posta.hacettepe.edu.tr
        |
        |  Playwright + Chromium (headless)
        |  ADFS SSO login
        v
   Scrape inbox (every 15 min)
        |
        |  SQLite dedup (seen.db)
        v
   smtplib -> smtp.gmail.com:587
        |
        v
   Your Gmail inbox (finally)
```

---

## What's Inside

```
hacettepe-mail-bridge/
├── src/
│   ├── main.py        # Entry point + poll loop
│   ├── auth.py        # ADFS SSO login automation
│   ├── scraper.py     # OWA inbox scraper (premium + basic view)
│   ├── forwarder.py   # Gmail SMTP forwarder
│   └── db.py          # SQLite seen-message tracker
├── Dockerfile         # Python 3.11-slim + Chromium
├── docker-compose.yml # One container, two volumes, zero drama
├── deploy.sh          # One-command VPS deployment
├── requirements.txt
└── .env.example
```

### Scraper Superpowers

The scraper auto-detects which OWA version it's looking at:

- **Premium view** (OWA 2016+ SPA): Parses `aria-label` metadata from the virtualised list, clicks into the reading pane for message bodies. Extracts sender email from persona photo URLs because Microsoft hides it there for some reason.
- **Basic view** (OWA 2010/2013 table layout): Falls back to table row scraping with page-by-page navigation. Handles the classic blue Outlook vibes.

If something goes wrong, it takes a screenshot and dumps the page HTML to the data volume. Debugging a headless browser on a remote VPS has never been this painless (it's still painful, just less).

---

## Quick Start

### 1. Configure

```bash
cp .env.example .env
# Fill in your credentials (see table below)
```

### 2. First-Time Login (local, one time only)

You need to log in once with a visible browser to handle any MFA prompts:

```bash
pip install -r requirements.txt
playwright install chromium
python -m src.main --auth
```

A Chromium window opens -> log in -> complete MFA if prompted -> press **Enter** when you see your inbox. Session cookies are saved to `user_data/` and reused forever (or until they expire, whichever comes first).

### 3. Deploy

```bash
./deploy.sh root@YOUR_VPS_IP
```

That's it. The script handles everything:
- Installs Docker if missing (Debian-aware, handles Trixie)
- Rsyncs project files + `.env`
- Builds the image and starts the container
- Uses SSH ControlMaster so you only type your password once

> **Pro tip:** Run `ssh-copy-id root@YOUR_VPS_IP` first and never type a password again.

---

## Operations

```bash
# Watch it work (or not)
ssh root@YOUR_VPS_IP 'docker compose -f /opt/hu-mail-bridge/docker-compose.yml logs -f'

# Pull the plug
ssh root@YOUR_VPS_IP 'docker compose -f /opt/hu-mail-bridge/docker-compose.yml down'

# Push new code
./deploy.sh root@YOUR_VPS_IP

# Session expired? Re-auth locally, then redeploy
python -m src.main --auth
./deploy.sh root@YOUR_VPS_IP
```

### When Things Go Wrong

The scraper saves debug artifacts to the `data/` volume on failure:
- `inbox_timeout.png` — screenshot of what OWA looks like when it can't find the inbox
- `msg_error_*.png` + `.html` — per-message failure snapshots

Pull them for inspection:
```bash
scp root@YOUR_VPS_IP:/opt/hu-mail-bridge/data/*.png .
```

---

## Gmail App Password

Gmail won't let you use your regular password for SMTP (reasonable). You need an App Password:

1. Go to [myaccount.google.com](https://myaccount.google.com)
2. **Security** -> enable **2-Step Verification**
3. Search for **"App passwords"**
4. Create one (call it `hu-bridge` or `mail-goblin` or whatever)
5. Paste the 16-character password into `.env`

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `HU_EMAIL` | yes | — | Your `@hacettepe.edu.tr` email |
| `HU_PASSWORD` | yes | — | Your Hacettepe password |
| `GMAIL_SENDER` | yes | — | Gmail address that sends the forwarded mail |
| `GMAIL_APP_PASSWORD` | yes | — | Gmail App Password (16 chars) |
| `GMAIL_TARGET` | yes | — | Where forwarded mail lands (can be same as sender) |
| `POLL_INTERVAL` | no | `900` | Seconds between inbox checks (900 = 15 min) |
| `HEADLESS` | no | `1` | `0` to show the browser window (for local debugging) |
| `USER_DATA_DIR` | no | `.playwright/user_data` | Browser session persistence directory |
| `DB_PATH` | no | `/app/data/seen.db` | SQLite database path |

---

## Tech Stack

- **Python 3.11** — because life's too short for older Pythons
- **Playwright** — browser automation that actually works
- **SQLite** — the database you don't have to think about
- **Docker** — so it runs the same everywhere
- **Gmail SMTP** — the most reliable free email relay

---

*Built out of spite towards university IT departments that disable email forwarding. Vibe coded with [Cursor](https://cursor.com).*
