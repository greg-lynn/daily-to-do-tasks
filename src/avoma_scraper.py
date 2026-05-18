"""
Avoma credential-based scraper using Playwright.

This is the fallback path when AVOMA_API_KEY is not available (i.e. the user
does not have Avoma admin access to generate an API key).

It logs in to app.avoma.com with AVOMA_EMAIL + AVOMA_PASSWORD, then:
  1. Lists recently-completed meetings from the Meetings page
  2. For each meeting, opens the detail page and extracts:
     - AI action items from the Notes panel
     - The meeting subject, attendees, start time, duration

Returns the same dataclasses as avoma_client.py so the rest of the app is
agnostic about which data source is in use.

Limitations vs. the API path:
  - Slower (real browser, ~5-30 s per scrape run)
  - Requires a persistent login state (stored in a browser profile directory)
  - May break if Avoma changes their UI
  - Does not support SSO-only accounts (Google/Microsoft login — see note below)

SSO note:
  If your Avoma account was created via Google or Microsoft SSO you will not
  have a standalone Avoma password. In that case the only options are:
    (a) Ask your Avoma admin for a user-scoped API key, or
    (b) Set a standalone Avoma password via the "Forgot password" flow at
        https://app.avoma.com/login and use that here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from .avoma_client import (
    AvomaMeeting, AvomaAttendee, AvomaActionItem, AvomaTranscript,
    AvomaTranscriptLine,
)
from .config import Config

logger = logging.getLogger(__name__)

_APP_URL = "https://app.avoma.com"
_LOGIN_URL = f"{_APP_URL}/login"
_MEETINGS_URL = f"{_APP_URL}/meetings"

# Where to persist the browser login session between runs
_SESSION_DIR = os.path.join(os.path.dirname(__file__), "..", ".avoma_session")


class AvomaScraperError(Exception):
    pass


class AvomaLoginError(AvomaScraperError):
    pass


class AvomaScraper:
    """
    Playwright-based Avoma data extractor.
    Mirrors the public interface of AvomaClient for the methods we need.
    """

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        headless: bool = True,
        session_dir: str | None = None,
    ):
        self._email = email or Config.AVOMA_EMAIL
        self._password = password or Config.AVOMA_PASSWORD
        self._headless = headless
        self._session_dir = session_dir or _SESSION_DIR
        if not self._email or not self._password:
            raise ValueError(
                "AVOMA_EMAIL and AVOMA_PASSWORD must be set to use the scraper."
            )

    # ------------------------------------------------------------------
    # Context-manager interface (manages browser lifecycle)
    # ------------------------------------------------------------------

    def __enter__(self):
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
        self._pw = sync_playwright().__enter__()
        Path(self._session_dir).mkdir(parents=True, exist_ok=True)
        self._browser = self._pw.chromium.launch_persistent_context(
            user_data_dir=self._session_dir,
            headless=self._headless,
            viewport={"width": 1280, "height": 900},
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._page = self._browser.pages[0] if self._browser.pages else self._browser.new_page()
        return self

    def __exit__(self, *_):
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._pw.__exit__(None, None, None)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def ensure_logged_in(self) -> None:
        """
        Navigate to Avoma. If we land on the login page, authenticate.
        Uses persistent context so subsequent runs skip login.
        """
        self._page.goto(_APP_URL, wait_until="domcontentloaded", timeout=30_000)
        _wait_settle(self._page)

        if self._is_logged_in():
            logger.info("Avoma: already logged in (session cached)")
            return

        logger.info("Avoma: logging in as %s ...", self._email)
        self._do_login()

    def _is_logged_in(self) -> bool:
        url = self._page.url
        return (
            "login" not in url
            and "signin" not in url
            and _APP_URL in url
        )

    def _do_login(self) -> None:
        self._page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        _wait_settle(self._page)

        # Fill email
        email_sel = 'input[type="email"], input[name="email"], input[placeholder*="email" i]'
        try:
            self._page.wait_for_selector(email_sel, timeout=10_000)
            self._page.fill(email_sel, self._email)
        except Exception as e:
            raise AvomaLoginError(
                "Could not find the email field on the Avoma login page. "
                "Your account may use SSO (Google/Microsoft). "
                "See the SSO note in avoma_scraper.py for options."
            ) from e

        # Fill password
        pwd_sel = 'input[type="password"], input[name="password"]'
        try:
            self._page.wait_for_selector(pwd_sel, timeout=5_000)
            self._page.fill(pwd_sel, self._password)
        except Exception as e:
            raise AvomaLoginError(
                "Could not find the password field. Your account may require SSO."
            ) from e

        # Submit
        submit_sel = 'button[type="submit"], button:has-text("Sign in"), button:has-text("Log in"), button:has-text("Continue")'
        self._page.click(submit_sel)

        # Wait for navigation away from login page
        try:
            self._page.wait_for_url(
                lambda url: "login" not in url and "signin" not in url,
                timeout=15_000,
            )
        except Exception:
            # Check for error messages
            err_text = self._page.inner_text("body")
            if any(w in err_text.lower() for w in ("invalid", "incorrect", "wrong", "error")):
                raise AvomaLoginError(
                    "Avoma login failed: incorrect email or password."
                )
            raise AvomaLoginError(
                "Avoma login timed out. The page did not redirect after submitting credentials."
            )

        _wait_settle(self._page)

        if not self._is_logged_in():
            raise AvomaLoginError(
                "Login appeared to succeed but we are still on a login/auth page."
            )
        logger.info("Avoma: login successful")

    # ------------------------------------------------------------------
    # Meeting list
    # ------------------------------------------------------------------

    def list_meetings(
        self,
        from_dt: datetime,
        to_dt: datetime,
        page_size: int = 50,
    ) -> list[AvomaMeeting]:
        """
        Navigate to the Meetings page and collect meetings that fall in [from_dt, to_dt].
        """
        self.ensure_logged_in()

        # Build date-filtered URL
        from_date = from_dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        to_date = to_dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        url = f"{_MEETINGS_URL}?from={from_date}&to={to_date}"

        logger.info("Avoma scraper: loading meetings page ...")
        self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        _wait_settle(self._page, extra_ms=2000)

        # Try to intercept the API response the SPA makes for its meetings list.
        # Avoma's React app calls its own backend; we can capture the JSON.
        meetings = self._intercept_meetings_api(from_dt, to_dt)
        if meetings is not None:
            return meetings

        # Fallback: DOM scraping
        return self._scrape_meetings_from_dom(from_dt, to_dt)

    def _intercept_meetings_api(
        self, from_dt: datetime, to_dt: datetime
    ) -> list[AvomaMeeting] | None:
        """
        Re-use any network response already cached in window.__INITIAL_DATA__
        or execute a fetch against the same API the SPA uses with the browser's
        authenticated session cookies.
        """
        from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_str = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            result = self._page.evaluate(f"""
                async () => {{
                    const resp = await fetch(
                        '/v1/meetings/?from_date={from_str}&to_date={to_str}&page_size=50',
                        {{credentials: 'include'}}
                    );
                    if (!resp.ok) return null;
                    return await resp.json();
                }}
            """)
            if result and "results" in result:
                logger.info("Avoma scraper: intercepted %d meetings via in-browser fetch",
                            len(result["results"]))
                from .avoma_client import _parse_meeting  # noqa: PLC0415
                return [_parse_meeting(r) for r in result["results"]]
        except Exception:
            logger.debug("In-browser fetch did not work", exc_info=True)
        return None

    def _scrape_meetings_from_dom(
        self, from_dt: datetime, to_dt: datetime
    ) -> list[AvomaMeeting]:
        """
        DOM fallback: extract meeting cards visible on the page.
        Returns a best-effort list; less reliable than the API path.
        """
        meetings: list[AvomaMeeting] = []
        try:
            # Wait for meeting rows / cards to appear
            self._page.wait_for_selector(
                '[data-testid*="meeting"], .meeting-row, .meeting-item, [class*="MeetingRow"], [class*="meeting-card"]',
                timeout=8_000,
            )
        except Exception:
            logger.warning("Avoma scraper: no meeting elements found in DOM")
            return meetings

        cards = self._page.query_selector_all(
            '[data-testid*="meeting"], .meeting-row, .meeting-item, [class*="MeetingRow"]'
        )
        for card in cards[:50]:
            try:
                text = card.inner_text()
                link_el = card.query_selector("a")
                href = link_el.get_attribute("href") if link_el else None
                uuid = _extract_uuid_from_url(href or "")
                subject = _first_line(text)
                m = AvomaMeeting(
                    uuid=uuid or f"dom-{hash(subject)}",
                    subject=subject,
                    start_at=datetime.now(timezone.utc),  # approximate
                    end_at=datetime.now(timezone.utc),
                    attendees=[],
                    state="completed",
                    transcript_ready=True,
                    notes_ready=True,
                    transcription_uuid=None,
                )
                meetings.append(m)
            except Exception:
                continue
        return meetings

    # ------------------------------------------------------------------
    # Action items for a meeting
    # ------------------------------------------------------------------

    def get_action_items(self, meeting_uuid: str, meeting_subject: str = "") -> list[AvomaActionItem]:
        """
        Open a meeting detail page and extract action items from the Notes panel.
        First tries an in-browser API fetch; falls back to DOM scraping.
        """
        self.ensure_logged_in()

        # Try in-browser API fetch first (same session cookies)
        items = self._fetch_action_items_via_api(meeting_uuid, meeting_subject)
        if items is not None:
            return items

        # DOM fallback
        return self._scrape_action_items_from_page(meeting_uuid, meeting_subject)

    def _fetch_action_items_via_api(
        self, meeting_uuid: str, meeting_subject: str
    ) -> list[AvomaActionItem] | None:
        try:
            result = self._page.evaluate(f"""
                async () => {{
                    const resp = await fetch(
                        '/v1/meetings/{meeting_uuid}/insights/',
                        {{credentials: 'include'}}
                    );
                    if (!resp.ok) return null;
                    return await resp.json();
                }}
            """)
            if not result:
                return None
            speakers = {s["id"]: s.get("name", "") for s in result.get("speakers", [])}
            items: list[AvomaActionItem] = []
            for note in result.get("ai_notes", []):
                if note.get("note_type") in ("action_item", "next_step", "action"):
                    items.append(AvomaActionItem(
                        text=note.get("text", "").strip(),
                        speaker_name=speakers.get(note.get("speaker_id", -1), ""),
                        meeting_uuid=meeting_uuid,
                        meeting_subject=meeting_subject,
                    ))
            logger.debug("Avoma scraper: %d action items for %s", len(items), meeting_uuid)
            return items
        except Exception:
            logger.debug("In-browser insights fetch failed for %s", meeting_uuid, exc_info=True)
            return None

    def _scrape_action_items_from_page(
        self, meeting_uuid: str, meeting_subject: str
    ) -> list[AvomaActionItem]:
        """Navigate to the meeting page and scrape action items from the DOM."""
        meeting_url = f"{_APP_URL}/meetings/{meeting_uuid}"
        self._page.goto(meeting_url, wait_until="domcontentloaded", timeout=30_000)
        _wait_settle(self._page, extra_ms=2500)

        items: list[AvomaActionItem] = []
        # Avoma's Notes panel commonly renders action items under an "Action Items"
        # heading or with a checkbox / bullet. Try several selector strategies.
        selectors = [
            '[data-note-type="action_item"]',
            '[data-category*="action"]',
            '.action-item',
            '[class*="ActionItem"]',
            '[class*="action_item"]',
        ]
        for sel in selectors:
            els = self._page.query_selector_all(sel)
            if els:
                for el in els:
                    text = el.inner_text().strip()
                    if text and len(text) > 5:
                        items.append(AvomaActionItem(
                            text=text,
                            meeting_uuid=meeting_uuid,
                            meeting_subject=meeting_subject,
                        ))
                if items:
                    break

        # Broader fallback: find a section heading "Action Items" and grab its siblings
        if not items:
            items = self._scrape_under_heading(
                headings=["Action Items", "Next Steps", "Follow-up"],
                meeting_uuid=meeting_uuid,
                meeting_subject=meeting_subject,
            )

        logger.debug("Avoma DOM scraper: %d action items for %s", len(items), meeting_uuid)
        return items

    def _scrape_under_heading(
        self, headings: list[str], meeting_uuid: str, meeting_subject: str
    ) -> list[AvomaActionItem]:
        items: list[AvomaActionItem] = []
        for heading in headings:
            try:
                result = self._page.evaluate(f"""
                    () => {{
                        const all = [...document.querySelectorAll('h1,h2,h3,h4,h5,strong,b,[class*="heading"],[class*="title"]')];
                        const hdr = all.find(el => el.innerText && el.innerText.trim().toLowerCase().includes('{heading.lower()}'));
                        if (!hdr) return [];
                        const results = [];
                        let el = hdr.parentElement ? hdr.parentElement.nextElementSibling : hdr.nextElementSibling;
                        for (let i = 0; i < 30 && el; i++) {{
                            const t = el.innerText ? el.innerText.trim() : '';
                            if (t) results.push(t);
                            el = el.nextElementSibling;
                        }}
                        return results;
                    }}
                """)
                for text in (result or []):
                    text = text.strip()
                    if text and len(text) > 5:
                        items.append(AvomaActionItem(
                            text=text,
                            meeting_uuid=meeting_uuid,
                            meeting_subject=meeting_subject,
                        ))
                if items:
                    break
            except Exception:
                continue
        return items

    # ------------------------------------------------------------------
    # High-level helper matching AvomaClient.extract_todays_action_items
    # ------------------------------------------------------------------

    def extract_todays_action_items(
        self,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[AvomaActionItem]:
        action_items: list[AvomaActionItem] = []
        try:
            meetings = self.list_meetings(from_dt, to_dt)
        except Exception:
            logger.warning("Avoma scraper: failed to list meetings", exc_info=True)
            return action_items

        for m in meetings:
            if m.state not in ("completed", "") or not m.notes_ready:
                continue
            try:
                items = self.get_action_items(m.uuid, m.subject)
                action_items.extend(items)
            except Exception:
                logger.warning("Avoma scraper: failed to get action items for %s", m.uuid,
                               exc_info=True)
        return action_items

    # ------------------------------------------------------------------
    # Transcript (best-effort)
    # ------------------------------------------------------------------

    def get_transcript(self, meeting_uuid: str) -> AvomaTranscript | None:
        """Fetch transcript using in-browser API fetch (same session cookies)."""
        self.ensure_logged_in()
        try:
            result = self._page.evaluate(f"""
                async () => {{
                    const resp = await fetch(
                        '/v1/transcriptions/?meeting_uuid={meeting_uuid}',
                        {{credentials: 'include'}}
                    );
                    if (!resp.ok) return null;
                    const data = await resp.json();
                    return Array.isArray(data) ? data[0] : data;
                }}
            """)
            if result:
                from .avoma_client import _parse_transcript  # noqa: PLC0415
                return _parse_transcript(result)
        except Exception:
            logger.debug("Scraper transcript fetch failed for %s", meeting_uuid, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_settle(page, extra_ms: int = 500) -> None:
    """Wait for network to be idle and optionally sleep a bit more."""
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass
    if extra_ms:
        page.wait_for_timeout(extra_ms)


def _extract_uuid_from_url(url: str) -> str | None:
    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", url)
    return m.group(0) if m else None


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return text[:80]


# ---------------------------------------------------------------------------
# Factory function used by the rest of the app
# ---------------------------------------------------------------------------

def get_avoma_source():
    """
    Returns the appropriate Avoma data source based on available credentials.

    - If AVOMA_API_KEY is set    → returns AvomaClient (API, best)
    - If AVOMA_EMAIL + PASSWORD  → returns AvomaScraper (browser, fallback)
    - Otherwise                  → returns None
    """
    mode = Config.avoma_mode()
    if mode == "api":
        from .avoma_client import AvomaClient  # noqa: PLC0415
        return AvomaClient()
    if mode == "scraper":
        return AvomaScraper()
    return None
