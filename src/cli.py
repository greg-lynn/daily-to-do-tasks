"""
Command-line interface for the Daily To-Do app.

Usage examples:
  python main.py add "Write Q3 report" --priority high --date 2026-05-19
  python main.py list
  python main.py done 3
  python main.py delete 3
  python main.py sync-avoma
  python main.py send-morning
  python main.py send-evening
  python main.py start          # run the scheduler daemon
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta

import click
from tabulate import tabulate

from .config import Config
from .task_manager import TaskManager, Task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _tm() -> TaskManager:
    return TaskManager()


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """Daily To-Do: task management + Avoma transcript enrichment + daily emails."""


# ---------------------------------------------------------------------------
# Task commands
# ---------------------------------------------------------------------------

@cli.command("add")
@click.argument("title")
@click.option("--desc", "-d", default="", help="Optional description.")
@click.option(
    "--date", "due_date",
    default=None,
    help="Due date as YYYY-MM-DD. Defaults to today.",
)
@click.option(
    "--priority", "-p",
    type=click.Choice(["high", "medium", "low"]),
    default="medium",
    help="Task priority.",
)
def add_task(title: str, desc: str, due_date: str | None, priority: str) -> None:
    """Add a new task."""
    d = due_date or date.today().isoformat()
    task = _tm().add_task(title=title, description=desc, due_date=d, priority=priority)
    click.echo(f"  Added task #{task.id}: {task.title} [{task.priority}] due {task.due_date}")


@cli.command("list")
@click.option("--all", "show_all", is_flag=True, help="Include completed tasks.")
@click.option(
    "--date", "due_date",
    default=None,
    help="Filter by due date (YYYY-MM-DD). Defaults to today.",
)
@click.option("--source", default=None, type=click.Choice(["manual", "avoma"]))
def list_tasks(show_all: bool, due_date: str | None, source: str | None) -> None:
    """List tasks (default: today's pending tasks)."""
    d = due_date or date.today().isoformat()
    tasks = _tm().list_tasks(due_date=d, include_completed=show_all, source=source)
    if not tasks:
        click.echo("No tasks found.")
        return
    _print_tasks(tasks)


@cli.command("done")
@click.argument("task_id", type=int)
def complete_task(task_id: int) -> None:
    """Mark a task as complete."""
    task = _tm().complete_task(task_id)
    click.echo(f"  Completed task #{task.id}: {task.title}")


@cli.command("undo")
@click.argument("task_id", type=int)
def uncomplete_task(task_id: int) -> None:
    """Mark a task as incomplete."""
    task = _tm().uncomplete_task(task_id)
    click.echo(f"  Re-opened task #{task.id}: {task.title}")


@cli.command("edit")
@click.argument("task_id", type=int)
@click.option("--title", "-t", default=None)
@click.option("--desc", "-d", default=None)
@click.option("--date", "due_date", default=None)
@click.option("--priority", "-p",
              type=click.Choice(["high", "medium", "low"]), default=None)
def edit_task(task_id: int, title: str | None, desc: str | None,
              due_date: str | None, priority: str | None) -> None:
    """Edit a task's fields."""
    task = _tm().update_task(task_id, title=title, description=desc,
                              due_date=due_date, priority=priority)
    click.echo(f"  Updated task #{task.id}: {task.title}")


@cli.command("delete")
@click.argument("task_id", type=int)
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def delete_task(task_id: int, yes: bool) -> None:
    """Delete a task permanently."""
    tm = _tm()
    task = tm.get_task(task_id)
    if not yes:
        click.confirm(f"Delete task #{task.id}: '{task.title}'?", abort=True)
    tm.delete_task(task_id)
    click.echo(f"  Deleted task #{task_id}.")


@cli.command("stats")
def stats() -> None:
    """Show today's task completion stats."""
    s = _tm().today_stats()
    click.echo(f"\nToday ({date.today().isoformat()})")
    click.echo(f"  Total   : {s['total']}")
    click.echo(f"  Done    : {s['completed']}")
    click.echo(f"  Pending : {s['pending']}")
    if s["total"]:
        pct = int(s["completed"] / s["total"] * 100)
        bar = "#" * (pct // 5) + "." * (20 - pct // 5)
        click.echo(f"  Progress: [{bar}] {pct}%\n")


# ---------------------------------------------------------------------------
# Avoma commands
# ---------------------------------------------------------------------------

@cli.command("sync-avoma")
@click.option(
    "--date", "sync_date",
    default=None,
    help="Date to sync (YYYY-MM-DD). Defaults to today.",
)
def sync_avoma(sync_date: str | None) -> None:
    """Pull action items from completed Avoma meetings and save as tasks."""
    if not Config.avoma_enabled():
        click.echo(
            "Avoma is not configured.\n"
            "Set AVOMA_API_KEY (preferred) or AVOMA_EMAIL + AVOMA_PASSWORD in your .env file."
        )
        return

    from .avoma_scraper import get_avoma_source  # noqa: PLC0415
    from .avoma_scraper import AvomaScraper       # noqa: PLC0415
    from .scheduler import _import_avoma_action_items, _today_range_utc, _avoma_ctx  # noqa: PLC0415
    from datetime import datetime, time, timezone  # noqa: PLC0415
    import pytz  # noqa: PLC0415

    tag_date = sync_date or date.today().isoformat()
    tz = pytz.timezone(Config.TIMEZONE)

    if sync_date:
        d = date.fromisoformat(sync_date)
        from_dt = tz.localize(datetime.combine(d, time.min)).astimezone(timezone.utc)
        to_dt = tz.localize(datetime.combine(d, time.max)).astimezone(timezone.utc)
    else:
        from_dt, to_dt = _today_range_utc()

    mode = Config.avoma_mode()
    click.echo(f"  Avoma mode: {mode}")
    if mode == "scraper":
        click.echo("  Using credential-based browser scraper (no API key) ...")

    avoma = get_avoma_source()
    tm = _tm()
    try:
        with _avoma_ctx(avoma) as src:
            count = _import_avoma_action_items(src, tm, from_dt, to_dt, tag_date=tag_date)
        click.echo(f"  Synced {count} new action items from Avoma for {tag_date}.")
    except Exception as e:
        click.echo(f"  Avoma sync failed: {e}", err=True)
        click.echo("  If your session expired, run: python3 main.py avoma-login", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Email commands
# ---------------------------------------------------------------------------

@cli.command("send-morning")
def send_morning() -> None:
    """Send the morning summary email right now."""
    from .scheduler import morning_job  # noqa: PLC0415
    click.echo("Sending morning email...")
    morning_job()
    click.echo("Done.")


@cli.command("send-evening")
def send_evening() -> None:
    """Send the evening wrap-up email right now."""
    from .scheduler import evening_job  # noqa: PLC0415
    click.echo("Sending evening email...")
    evening_job()
    click.echo("Done.")


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

@cli.command("avoma-login")
def avoma_login() -> None:
    """One-time Avoma login via a visible browser window (supports Google/SSO).

    Opens a real browser so you can sign in with Google (or any SSO).
    The session is saved and reused automatically — you only need to do
    this once (or again if your session expires, typically after 30+ days).
    """
    from .avoma_scraper import run_manual_login  # noqa: PLC0415
    try:
        run_manual_login()
        click.echo("  Avoma session saved. Run 'sync-avoma' or 'start' to use it.")
    except Exception as e:
        click.echo(f"  Login failed: {e}", err=True)
        raise SystemExit(1)


@cli.command("start")
def start_daemon() -> None:
    """Start the scheduler daemon (blocks until stopped with Ctrl+C)."""
    from .scheduler import run_scheduler  # noqa: PLC0415
    click.echo(
        f"Starting scheduler — morning={Config.MORNING_TIME}, "
        f"evening={Config.EVENING_TIME}, tz={Config.TIMEZONE}"
    )
    mode = Config.avoma_mode()
    if mode == "api":
        click.echo("  Avoma: API key mode")
    elif mode == "scraper":
        click.echo("  Avoma: credential / browser scraper mode")
    else:
        click.echo("  Avoma: disabled (set AVOMA_API_KEY or AVOMA_EMAIL+AVOMA_PASSWORD)")
    run_scheduler()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_tasks(tasks: list[Task]) -> None:
    PRIO_LABEL = {"high": "HIGH", "medium": "MED ", "low": "LOW "}
    rows = []
    for t in tasks:
        status = "DONE" if t.completed else "    "
        prio = PRIO_LABEL.get(t.priority, "    ")
        source_tag = "[Avoma]" if t.source == "avoma" else ""
        title = t.title + (f"  {source_tag}" if source_tag else "")
        rows.append([t.id, status, prio, t.due_date or "", title])
    click.echo(
        tabulate(rows, headers=["#", "Done", "Prio", "Due", "Title"], tablefmt="simple")
    )
