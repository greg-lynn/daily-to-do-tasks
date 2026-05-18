"""
HTML email builder and SMTP sender.

Two email templates:
  - Morning: today's upcoming meetings (from Avoma) + pending tasks
  - Evening: completed meetings with action items + task status summary
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from .avoma_client import AvomaMeeting, AvomaActionItem
from .config import Config
from .task_manager import Task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
_BRAND = "#4f46e5"          # indigo
_BG = "#f9fafb"
_CARD_BG = "#ffffff"
_BORDER = "#e5e7eb"
_TEXT = "#111827"
_MUTED = "#6b7280"
_GREEN = "#16a34a"
_AMBER = "#d97706"
_RED = "#dc2626"
_AVOMA_TAG = "#7c3aed"      # purple for Avoma-sourced items


def send_morning_email(
    meetings: list[AvomaMeeting],
    tasks: list[Task],
    user_name: str = "",
) -> None:
    Config.validate_email()
    subject = f"Good morning{' ' + user_name if user_name else ''}! Your day — {_today_str()}"
    html = _build_morning_html(meetings, tasks, user_name)
    _send(subject, html)
    logger.info("Morning email sent to %s", Config.RECIPIENT_EMAIL)


def send_evening_email(
    meetings: list[AvomaMeeting],
    action_items: list[AvomaActionItem],
    tasks: list[Task],
    user_name: str = "",
) -> None:
    Config.validate_email()
    subject = f"Evening wrap-up{' ' + user_name if user_name else ''} — {_today_str()}"
    html = _build_evening_html(meetings, action_items, tasks, user_name)
    _send(subject, html)
    logger.info("Evening email sent to %s", Config.RECIPIENT_EMAIL)


# ---------------------------------------------------------------------------
# Morning HTML
# ---------------------------------------------------------------------------

def _build_morning_html(
    meetings: list[AvomaMeeting],
    tasks: list[Task],
    user_name: str,
) -> str:
    pending = [t for t in tasks if not t.completed]
    avoma_tasks = [t for t in pending if t.source == "avoma"]
    manual_tasks = [t for t in pending if t.source == "manual"]

    meetings_html = _render_upcoming_meetings(meetings) if meetings else _empty_section("No meetings scheduled today.")
    manual_html = _render_task_list(manual_tasks, show_source=False) if manual_tasks else _empty_section("No manual tasks for today.")
    avoma_html = _render_task_list(avoma_tasks, show_source=True) if avoma_tasks else _empty_section("No action items carried over from calls.")

    return _wrap_email(f"""
        {_greeting_block(f"Good morning{' ' + user_name if user_name else ''}", _today_str())}
        <p style="color:{_MUTED};font-size:14px;margin:0 0 24px;">
          Here is everything lined up for your day.
        </p>

        {_section("Today's Meetings", meetings_html)}
        {_section("Your Tasks", manual_html)}
        {_section("Action Items from Recent Calls", avoma_html, tag="Avoma")}

        {_footer()}
    """)


# ---------------------------------------------------------------------------
# Evening HTML
# ---------------------------------------------------------------------------

def _build_evening_html(
    meetings: list[AvomaMeeting],
    action_items: list[AvomaActionItem],
    tasks: list[Task],
    user_name: str,
) -> str:
    done_tasks = [t for t in tasks if t.completed]
    pending_tasks = [t for t in tasks if not t.completed]
    total = len(tasks)
    pct = int(len(done_tasks) / total * 100) if total else 0

    meetings_html = _render_completed_meetings(meetings) if meetings else _empty_section("No recorded meetings today.")
    ai_html = _render_action_items(action_items) if action_items else _empty_section("No action items extracted from today's calls.")
    pending_html = _render_task_list(pending_tasks, show_source=True) if pending_tasks else _empty_section("All tasks completed — great work!")
    done_html = _render_task_list(done_tasks, show_source=False, strike=True) if done_tasks else _empty_section("No tasks completed today.")

    return _wrap_email(f"""
        {_greeting_block(f"Evening wrap-up{' ' + user_name if user_name else ''}", _today_str())}

        {_progress_bar(pct, len(done_tasks), total)}

        {_section("Meetings Today", meetings_html)}
        {_section("New Action Items from Calls", ai_html, tag="Avoma")}
        {_section("Remaining Tasks", pending_html)}
        {_section("Completed Today", done_html)}

        {_footer()}
    """)


# ---------------------------------------------------------------------------
# Component renderers
# ---------------------------------------------------------------------------

def _render_upcoming_meetings(meetings: list[AvomaMeeting]) -> str:
    rows = ""
    for m in meetings:
        time_str = m.start_local.strftime("%I:%M %p")
        attendee_names = ", ".join(a.name or a.email for a in m.attendees[:4])
        if len(m.attendees) > 4:
            attendee_names += f" +{len(m.attendees) - 4} more"
        rows += f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid {_BORDER};width:80px;
                     color:{_BRAND};font-weight:600;font-size:13px;white-space:nowrap;">
            {time_str}
          </td>
          <td style="padding:10px 0 10px 16px;border-bottom:1px solid {_BORDER};">
            <div style="font-weight:600;color:{_TEXT};">{_esc(m.subject)}</div>
            <div style="font-size:12px;color:{_MUTED};margin-top:2px;">{_esc(attendee_names)}</div>
          </td>
        </tr>"""
    return f'<table style="width:100%;border-collapse:collapse;">{rows}</table>'


def _render_completed_meetings(meetings: list[AvomaMeeting]) -> str:
    rows = ""
    for m in meetings:
        time_str = m.start_local.strftime("%I:%M %p")
        dur = f"{m.duration_minutes} min" if m.duration_minutes else ""
        note_badge = _badge("Notes ready", _GREEN) if m.notes_ready else ""
        rows += f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid {_BORDER};width:80px;
                     color:{_MUTED};font-size:13px;white-space:nowrap;">
            {time_str}
          </td>
          <td style="padding:10px 0 10px 16px;border-bottom:1px solid {_BORDER};">
            <div style="font-weight:600;color:{_TEXT};">{_esc(m.subject)}</div>
            <div style="font-size:12px;color:{_MUTED};margin-top:2px;">
              {dur} {note_badge}
            </div>
          </td>
        </tr>"""
    return f'<table style="width:100%;border-collapse:collapse;">{rows}</table>'


def _render_task_list(tasks: list[Task], show_source: bool, strike: bool = False) -> str:
    items = ""
    for t in tasks:
        style = f"text-decoration:line-through;color:{_MUTED};" if strike else f"color:{_TEXT};"
        source_badge = ""
        if show_source and t.source == "avoma":
            source_badge = _badge("Avoma", _AVOMA_TAG)
            if t.avoma_meeting_subject:
                source_badge += f' <span style="font-size:11px;color:{_MUTED};">{_esc(t.avoma_meeting_subject)}</span>'
        prio_color = {"high": _RED, "medium": _AMBER, "low": _MUTED}.get(t.priority, _MUTED)
        prio_dot = f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{prio_color};margin-right:8px;"></span>'
        desc_html = f'<div style="font-size:12px;color:{_MUTED};margin-top:3px;">{_esc(t.description)}</div>' if t.description else ""
        items += f"""
        <div style="display:flex;align-items:flex-start;padding:10px 0;border-bottom:1px solid {_BORDER};">
          <div style="flex:1;">
            <div style="{style}font-weight:500;">{prio_dot}{_esc(t.title)}</div>
            {desc_html}
            <div style="margin-top:4px;">{source_badge}</div>
          </div>
        </div>"""
    return items


def _render_action_items(items: list[AvomaActionItem]) -> str:
    html = ""
    for ai in items:
        speaker = f" — <em>{_esc(ai.speaker_name)}</em>" if ai.speaker_name else ""
        meeting = f'<div style="font-size:11px;color:{_MUTED};margin-top:2px;">{_esc(ai.meeting_subject)}</div>' if ai.meeting_subject else ""
        html += f"""
        <div style="padding:10px 0;border-bottom:1px solid {_BORDER};">
          <div style="color:{_TEXT};font-weight:500;">{_esc(ai.text)}{speaker}</div>
          {meeting}
        </div>"""
    return html


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _wrap_email(body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily To-Do</title></head>
<body style="margin:0;padding:0;background:{_BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" style="background:{_BG};padding:32px 16px;">
    <tr><td align="center">
      <table width="600" style="max-width:600px;width:100%;">
        <tr><td style="background:{_CARD_BG};border-radius:12px;border:1px solid {_BORDER};
                       padding:32px;box-shadow:0 1px 3px rgba(0,0,0,.07);">
          {body}
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def _greeting_block(greeting: str, date_str: str) -> str:
    return f"""
    <div style="border-bottom:2px solid {_BRAND};padding-bottom:20px;margin-bottom:28px;">
      <div style="display:flex;align-items:center;margin-bottom:8px;">
        <div style="width:4px;height:28px;background:{_BRAND};border-radius:2px;margin-right:12px;"></div>
        <h1 style="margin:0;font-size:22px;font-weight:700;color:{_TEXT};">{greeting}</h1>
      </div>
      <p style="margin:0;color:{_MUTED};font-size:14px;">{date_str}</p>
    </div>"""


def _section(title: str, content: str, tag: str = "") -> str:
    tag_html = f' {_badge(tag, _AVOMA_TAG)}' if tag else ""
    return f"""
    <div style="margin-bottom:28px;">
      <h2 style="font-size:14px;font-weight:700;color:{_MUTED};text-transform:uppercase;
                 letter-spacing:.08em;margin:0 0 12px;">{title}{tag_html}</h2>
      {content}
    </div>"""


def _empty_section(msg: str) -> str:
    return f'<p style="color:{_MUTED};font-size:14px;font-style:italic;margin:0;">{msg}</p>'


def _progress_bar(pct: int, done: int, total: int) -> str:
    return f"""
    <div style="background:#f3f4f6;border-radius:8px;padding:16px;margin-bottom:28px;">
      <div style="display:flex;justify-content:space-between;margin-bottom:8px;">
        <span style="font-size:13px;font-weight:600;color:{_TEXT};">Task Progress</span>
        <span style="font-size:13px;color:{_MUTED};">{done} / {total} completed</span>
      </div>
      <div style="background:#e5e7eb;border-radius:99px;height:8px;overflow:hidden;">
        <div style="width:{pct}%;background:{_BRAND};height:8px;border-radius:99px;
                    transition:width .3s;"></div>
      </div>
    </div>"""


def _badge(text: str, color: str) -> str:
    return (f'<span style="display:inline-block;background:{color}1a;color:{color};'
            f'font-size:10px;font-weight:700;padding:2px 7px;border-radius:99px;'
            f'text-transform:uppercase;letter-spacing:.06em;">{text}</span>')


def _footer() -> str:
    return f"""
    <div style="border-top:1px solid {_BORDER};margin-top:32px;padding-top:20px;
                text-align:center;color:{_MUTED};font-size:12px;">
      Daily To-Do &bull; Powered by Avoma API &bull; {_today_str()}
    </div>"""


def _today_str() -> str:
    return datetime.now().strftime("%A, %B %-d, %Y")


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# SMTP sender
# ---------------------------------------------------------------------------

def _send(subject: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = Config.SMTP_USER
    msg["To"] = Config.RECIPIENT_EMAIL

    # Plain-text fallback
    plain = f"{subject}\n\n(This email is best viewed in an HTML-capable client.)"
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
        server.sendmail(Config.SMTP_USER, Config.RECIPIENT_EMAIL, msg.as_string())
