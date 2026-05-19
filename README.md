# Daily To-Do

A Python app that:
1. Lets you manage daily tasks via a CLI
2. Connects to **Avoma** to pull AI-generated action items from your call transcripts and saves them as tasks
3. Sends you a **morning email** with today's upcoming meetings and pending tasks
4. Sends you an **evening email** with completed meetings, extracted action items, and your task progress

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
python3 -m playwright install chromium   # installs the headless browser (~120 MB, one-time)
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — fill in SMTP settings at minimum
```

#### Required

| Variable | Description |
|---|---|
| `SMTP_USER` | Your sender email address |
| `SMTP_PASSWORD` | Gmail App Password or SMTP credential |
| `RECIPIENT_EMAIL` | Where daily emails are delivered (default: `glynn@rocketlane.com`) |

#### Optional — scheduling

| Variable | Default | Description |
|---|---|---|
| `MORNING_TIME` | `08:00` | Time to send morning email (24-hr, local TZ) |
| `EVENING_TIME` | `18:00` | Time to send evening email |
| `TIMEZONE` | `America/New_York` | Your IANA timezone |

### 3. Connect Avoma — pick the easiest option

#### Option A: One-time browser login (recommended — works with Google/SSO)

Run this **once**:

```bash
python3 main.py avoma-login
```

A real browser window opens. Click **Sign in with Google** exactly as you normally would in Avoma. Once you're past the login page, the window closes automatically and the session is saved. Every run after that is fully automated — nothing else to configure, no passwords stored anywhere.

> Sessions typically stay valid for 30+ days. If it ever expires, just run `avoma-login` again.

#### Option B: Ask your admin for a user-scoped API key

You don't need admin access to *use* a key — only to *create* one. Ask your Avoma admin to:
1. Go to **Avoma → Settings → Organization → Developer**
2. Create a **"User – full access"** key assigned to your account and send it to you

Then set it in `.env`:
```env
AVOMA_API_KEY=<the key>
```

#### Option C: Email + standalone Avoma password

Only if your Avoma account has a direct password (not Google/SSO):
```env
AVOMA_EMAIL=glynn@rocketlane.com
AVOMA_PASSWORD=your_avoma_password
```

---

## CLI Reference

```
python3 main.py --help
```

### Task management

```bash
# Add a task
python3 main.py add "Prepare Q3 report" --priority high --date 2026-05-19

# List today's pending tasks
python3 main.py list

# List all tasks for a date (including completed)
python3 main.py list --all --date 2026-05-19

# Mark task #3 as done
python3 main.py done 3

# Re-open task #3
python3 main.py undo 3

# Edit a task
python3 main.py edit 3 --title "Updated title" --priority low

# Delete a task
python3 main.py delete 3

# View today's completion stats
python3 main.py stats
```

### Avoma integration

```bash
# One-time login via browser (Google/SSO supported)
python3 main.py avoma-login

# Pull action items from today's completed Avoma calls and save as tasks
python3 main.py sync-avoma

# Sync a specific date
python3 main.py sync-avoma --date 2026-05-17
```

### Email

```bash
# Send the morning email right now (for testing)
python3 main.py send-morning

# Send the evening wrap-up right now (for testing)
python3 main.py send-evening
```

### Scheduler daemon

```bash
# Start the background scheduler (runs morning + evening jobs automatically)
python3 main.py start
```

Run `start` in a terminal multiplexer (tmux/screen) or as a systemd service so it persists after you close your shell.

#### systemd service example

Create `/etc/systemd/system/daily-todo.service`:

```ini
[Unit]
Description=Daily To-Do Scheduler
After=network.target

[Service]
WorkingDirectory=/path/to/daily-to-do
EnvironmentFile=/path/to/daily-to-do/.env
ExecStart=/usr/bin/python3 main.py start
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now daily-todo
sudo systemctl status daily-todo
```

---

## How Avoma integration works

The app auto-selects the mode — no manual config needed beyond the one-time setup:

| Mode | Triggered by | How it works |
|---|---|---|
| **API** | `AVOMA_API_KEY` set | Direct REST calls to `api.avoma.com` |
| **Scraper** | Saved session OR email+password | Headless Chrome reuses your browser session |
| **Disabled** | None of the above | Emails sent without Avoma data |

```
Every hour (avoma_sync_job)
    └─ List completed meetings for today
    └─ For each meeting with AI notes ready
        └─ Fetch action items (via API or browser session cookies)
        └─ Save each as a task (source=avoma, due_date=today)
        └─ Deduplication: skip if (meeting_uuid, title) already exists

Morning email
    └─ Today's meetings from Avoma
    └─ All pending tasks (manual + avoma-sourced)

Evening email
    └─ Today's completed meetings
    └─ Fresh action-item extraction for the email summary
    └─ Full task list with completion status + progress bar
```

---

## Gmail App Password setup

1. Enable 2-Factor Authentication on your Google account
2. Go to **Google Account → Security → App Passwords**
3. Create a new app password (any name, e.g. "Daily To-Do")
4. Use that 16-character password as `SMTP_PASSWORD`

---

## Project structure

```
daily-to-do/
├── main.py              # Entry point
├── requirements.txt
├── .env.example         # Config template
├── src/
│   ├── config.py        # Config + Avoma mode auto-detection
│   ├── avoma_client.py  # Avoma REST API wrapper (Option B)
│   ├── avoma_scraper.py # Playwright browser scraper (Options A & C)
│   ├── task_manager.py  # SQLite task CRUD
│   ├── email_sender.py  # HTML email builder + SMTP sender
│   ├── scheduler.py     # APScheduler jobs
│   └── cli.py           # Click CLI commands
├── .avoma_session/      # Saved browser session (auto-created, git-ignored)
└── tasks.db             # SQLite database (auto-created)
```
