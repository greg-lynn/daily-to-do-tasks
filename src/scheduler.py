"""
APScheduler-based daemon.

Jobs:
  morning_job  - runs at MORNING_TIME in the user's timezone
    1. Sync today's Avoma meetings (state=scheduled)
    2. Import any outstanding action items as tasks
    3. Send morning email: upcoming meetings + pending tasks

  evening_job  - runs at EVENING_TIME in the user's timezone
    1. Pull today's completed meetings from Avoma
    2. Extract action items from AI notes
    3. Save new action items as tasks (source=avoma)
    4. Send evening email: meeting summary + action items + task status

  avoma_sync   - runs every hour (if Avoma is enabled) to auto-import
                 action items from newly completed meetings as tasks.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config
from .email_sender import send_morning_email, send_evening_email
from .task_manager import TaskManager

logger = logging.getLogger(__name__)


def _get_avoma_source():
    """
    Return the appropriate Avoma data source:
      - AvomaClient  if AVOMA_API_KEY is set  (API key path)
      - AvomaScraper if AVOMA_EMAIL+PASSWORD are set (credential path)
      - None         if Avoma is not configured
    """
    if not Config.avoma_enabled():
        return None
    from .avoma_scraper import get_avoma_source  # noqa: PLC0415
    return get_avoma_source()


def _today_range_utc() -> tuple[datetime, datetime]:
    """Return start-of-today and end-of-today in UTC based on user timezone."""
    tz = pytz.timezone(Config.TIMEZONE)
    today = date.today()
    local_start = tz.localize(datetime.combine(today, time.min))
    local_end = tz.localize(datetime.combine(today, time.max))
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _yesterday_range_utc() -> tuple[datetime, datetime]:
    tz = pytz.timezone(Config.TIMEZONE)
    yesterday = date.today() - timedelta(days=1)
    local_start = tz.localize(datetime.combine(yesterday, time.min))
    local_end = tz.localize(datetime.combine(yesterday, time.max))
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

def morning_job() -> None:
    logger.info("Running morning job...")
    tm = TaskManager()
    avoma = _get_avoma_source()
    today_iso = date.today().isoformat()

    # Pull today's meetings from Avoma (failures here must never block the email)
    meetings = []
    if avoma:
        try:
            with _avoma_ctx(avoma) as src:
                try:
                    from_dt, to_dt = _today_range_utc()
                    meetings = src.list_meetings(from_dt, to_dt)
                    logger.info("Fetched %d meetings from Avoma", len(meetings))
                except Exception:
                    logger.warning("Failed to fetch Avoma meetings", exc_info=True)
                try:
                    _import_avoma_action_items(src, tm, *_yesterday_range_utc(), tag_date=today_iso)
                except Exception:
                    logger.warning("Failed to import Avoma action items", exc_info=True)
        except Exception:
            logger.warning(
                "Avoma browser session could not be opened — skipping Avoma sync. "
                "Run 'python3 main.py avoma-login' to refresh the session.",
                exc_info=True,
            )

    # Fetch today's tasks
    tasks = tm.list_tasks(due_date=today_iso, include_completed=False)

    try:
        send_morning_email(meetings=meetings, tasks=tasks)
        logger.info("Morning email sent.")
    except Exception:
        logger.error("Failed to send morning email", exc_info=True)


def evening_job() -> None:
    logger.info("Running evening job...")
    tm = TaskManager()
    avoma = _get_avoma_source()
    today_iso = date.today().isoformat()

    # Pull today's completed meetings and action items (failures must never block the email)
    meetings = []
    action_items = []
    if avoma:
        try:
            with _avoma_ctx(avoma) as src:
                try:
                    from_dt, to_dt = _today_range_utc()
                    meetings = src.list_meetings(from_dt, to_dt)
                    logger.info("Fetched %d meetings from Avoma", len(meetings))
                    new_count = _import_avoma_action_items(src, tm, from_dt, to_dt, tag_date=today_iso)
                    logger.info("Imported %d new Avoma action items", new_count)
                    action_items = src.extract_todays_action_items(from_dt, to_dt)
                except Exception:
                    logger.warning("Failed to fetch Avoma data", exc_info=True)
        except Exception:
            logger.warning(
                "Avoma browser session could not be opened — skipping Avoma sync. "
                "Run 'python3 main.py avoma-login' to refresh the session.",
                exc_info=True,
            )

    # All tasks for today (including completed)
    tasks = tm.list_tasks(due_date=today_iso, include_completed=True)

    try:
        send_evening_email(meetings=meetings, action_items=action_items, tasks=tasks)
        logger.info("Evening email sent.")
    except Exception:
        logger.error("Failed to send evening email", exc_info=True)


def avoma_sync_job() -> None:
    """Hourly job: import new action items from completed Avoma calls."""
    if not Config.avoma_enabled():
        return
    logger.info("Running hourly Avoma sync (mode=%s)...", Config.avoma_mode())
    tm = TaskManager()
    from .avoma_scraper import get_avoma_source  # noqa: PLC0415
    avoma = get_avoma_source()
    if avoma is None:
        return
    today_iso = date.today().isoformat()
    from_dt, to_dt = _today_range_utc()
    with _avoma_ctx(avoma) as src:
        try:
            count = _import_avoma_action_items(src, tm, from_dt, to_dt, tag_date=today_iso)
            if count:
                logger.info("Avoma sync: imported %d new action items", count)
        except Exception:
            logger.warning("Avoma sync failed", exc_info=True)


# ---------------------------------------------------------------------------
# Avoma → Task importer
# ---------------------------------------------------------------------------

def _import_avoma_action_items(avoma, tm: TaskManager, from_dt, to_dt, tag_date: str) -> int:
    """
    Extract action items from completed Avoma meetings in [from_dt, to_dt]
    and save them as tasks (source=avoma, due_date=tag_date).
    Skips duplicates by checking existing avoma_meeting_uuid + title combos.
    Returns count of newly added tasks.
    """
    action_items = avoma.extract_todays_action_items(from_dt, to_dt)
    if not action_items:
        return 0

    # Build a set of existing (meeting_uuid, title) pairs to avoid duplicates
    existing = tm.list_tasks(due_date=tag_date, include_completed=True, source="avoma")
    existing_keys = {(t.avoma_meeting_uuid, t.title.strip()) for t in existing}

    added = 0
    for ai in action_items:
        key = (ai.meeting_uuid, ai.text.strip())
        if key in existing_keys:
            continue
        if not ai.text.strip():
            continue
        desc = f"Speaker: {ai.speaker_name}" if ai.speaker_name else ""
        tm.add_task(
            title=ai.text.strip(),
            description=desc,
            due_date=tag_date,
            priority="medium",
            source="avoma",
            avoma_meeting_uuid=ai.meeting_uuid,
            avoma_meeting_subject=ai.meeting_subject,
        )
        existing_keys.add(key)
        added += 1
    return added


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def run_scheduler() -> None:
    """Start the blocking APScheduler daemon."""
    tz = Config.TIMEZONE
    morning_h, morning_m = _parse_time(Config.MORNING_TIME)
    evening_h, evening_m = _parse_time(Config.EVENING_TIME)

    scheduler = BlockingScheduler(timezone=tz)

    scheduler.add_job(
        morning_job,
        trigger=CronTrigger(hour=morning_h, minute=morning_m, timezone=tz),
        id="morning_email",
        name="Morning Email",
        replace_existing=True,
        misfire_grace_time=300,
    )

    scheduler.add_job(
        evening_job,
        trigger=CronTrigger(hour=evening_h, minute=evening_m, timezone=tz),
        id="evening_email",
        name="Evening Email",
        replace_existing=True,
        misfire_grace_time=300,
    )

    if Config.avoma_enabled():
        scheduler.add_job(
            avoma_sync_job,
            trigger=CronTrigger(minute=0, timezone=tz),  # top of every hour
            id="avoma_sync",
            name="Avoma Sync",
            replace_existing=True,
            misfire_grace_time=120,
        )

    logger.info(
        "Scheduler started. Morning=%s, Evening=%s, TZ=%s",
        Config.MORNING_TIME,
        Config.EVENING_TIME,
        tz,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


def _parse_time(t: str) -> tuple[int, int]:
    h, m = t.strip().split(":")
    return int(h), int(m)


from contextlib import contextmanager  # noqa: E402


@contextmanager
def _avoma_ctx(source):
    """
    Unified context manager for both AvomaClient (no-op CM) and
    AvomaScraper (needs browser open/close).
    """
    from .avoma_scraper import AvomaScraper  # noqa: PLC0415
    if isinstance(source, AvomaScraper):
        with source as s:
            yield s
    else:
        # AvomaClient is a plain object — no context management needed
        yield source
