# Daily To-Do

A Python app that:
1. Lets you manage daily tasks via a CLI
2. Connects to the **Avoma API** (using your API key — no scraping or raw login needed) to pull AI-generated action items from your call transcripts and automatically saves them as tasks
3. Sends you a **morning email** with your upcoming meetings and pending tasks
4. Sends you an **evening email** with completed meetings, extracted action items, and your task progress

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your values
```

#### Required configuration

| Variable | Description |
|---|---|
| `SMTP_USER` | Your sender email address |
| `SMTP_PASSWORD` | Gmail App Password or SMTP credential |
| `RECIPIENT_EMAIL` | Where daily emails are delivered |

#### Optional (but recommended)

| Variable | Default | Description |
|---|---|---|
| `AVOMA_API_KEY` | — | Avoma API key for transcript/action-item enrichment |
| `MORNING_TIME` | `08:00` | Time to send morning email (24-hr, local TZ) |
| `EVENING_TIME` | `18:00` | Time to send evening email |
| `TIMEZONE` | `America/New_York` | Your IANA timezone |

### 3. Get your Avoma API key

1. Log in to Avoma
2. Go to **Settings → Organization → Developer**
3. Create a new scoped key:
   - **User – full access** for your own meetings
   - **Organization – limited access** for all non-private org meetings
4. Copy the key to `AVOMA_API_KEY` in your `.env`

> No scraping or raw password sharing required — the official Avoma REST API
> ([dev.avoma.com](https://dev.avoma.com)) handles authentication securely via bearer token.

---

## CLI Reference

```
python main.py --help
```

### Task management

```bash
# Add a task
python main.py add "Prepare Q3 report" --priority high --date 2026-05-19

# List today's pending tasks
python main.py list

# List all tasks for a date (including completed)
python main.py list --all --date 2026-05-19

# Mark task #3 as done
python main.py done 3

# Re-open task #3
python main.py undo 3

# Edit a task
python main.py edit 3 --title "Updated title" --priority low

# Delete a task
python main.py delete 3

# View today's completion stats
python main.py stats
```

### Avoma integration

```bash
# Pull action items from today's completed Avoma calls and save as tasks
python main.py sync-avoma

# Sync a specific date
python main.py sync-avoma --date 2026-05-17
```

### Email

```bash
# Send the morning email right now (for testing)
python main.py send-morning

# Send the evening wrap-up right now (for testing)
python main.py send-evening
```

### Scheduler daemon

```bash
# Start the background scheduler (runs morning + evening jobs automatically)
python main.py start
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

```
Every hour (avoma_sync_job)
    └─ List completed meetings in the last 24 h
    └─ For each meeting with notes_ready=true
        └─ Call GET /v1/meetings/{uuid}/insights/
        └─ Extract ai_notes where note_type = action_item / next_step
        └─ Save each as a task (source=avoma, due_date=today)
        └─ Deduplication: skip if (meeting_uuid, title) already exists

Morning email
    └─ Today's meetings (state=scheduled) from Avoma
    └─ All pending tasks (manual + avoma-sourced)

Evening email
    └─ Today's completed meetings from Avoma
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
│   ├── config.py        # Env-var config
│   ├── avoma_client.py  # Avoma REST API wrapper
│   ├── task_manager.py  # SQLite task CRUD
│   ├── email_sender.py  # HTML email builder + SMTP sender
│   ├── scheduler.py     # APScheduler jobs
│   └── cli.py           # Click CLI commands
└── tasks.db             # SQLite database (auto-created)
```
