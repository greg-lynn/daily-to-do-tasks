"""
Avoma browser-based data extractor using Playwright.

Strategy: navigate to Avoma pages and INTERCEPT the network responses the
web app makes to load its data. This is far more reliable than trying to
call the public API from the browser (different host → cookies don't transfer)
or scraping the DOM (fragile CSS selectors).

When the SPA loads a page it makes authenticated XHR/fetch calls to its own
backend. We capture those responses to get clean JSON meeting/transcript data.

As a final fallback, we read the visible transcript text from each meeting
page and extract action items using keyword matching.

Session: cookies are saved to .avoma_session/cookies.json by avoma-login.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .avoma_client import (
    AvomaMeeting, AvomaAttendee, AvomaActionItem, AvomaTranscript,
    AvomaTranscriptLine,
)
from .config import Config

logger = logging.getLogger(__name__)

_APP_URL = "https://app.avoma.com"
_LOGIN_URL = f"{_APP_URL}/login"

SESSION_DIR = os.path.join(os.path.dirname(__file__), "..", ".avoma_session")
COOKIES_FILE = os.path.join(SESSION_DIR, "cookies.json")

# Keywords used to detect action items when parsing raw transcript text
_ACTION_PATTERNS = [
    r"(?i)\baction item[s]?\b[:\-]?\s*(.+)",
    r"(?i)\bnext step[s]?\b[:\-]?\s*(.+)",
    r"(?i)\bfollow.?up[s]?\b[:\-]?\s*(.+)",
    r"(?i)\btodo[s]?\b[:\-]?\s*(.+)",
    r"(?i)\bto.do[s]?\b[:\-]?\s*(.+)",
    r"(?i)^[-•*]\s*(.{10,})",            # bulleted list items
    r"(?i)\bi(?:'ll| will| need to| am going to)\s+(.{10,})",
    r"(?i)\bwe(?:'ll| will| need to| are going to)\s+(.{10,})",
    r"(?i)\byou(?:'ll| will| need to| are going to)\s+(.{10,})",
    r"(?i)\bcan you\s+(.{10,})\?",
    r"(?i)\bplease\s+(.{10,})",
    r"(?i)\bsend\s+(.{10,})",
    r"(?i)\bschedule\s+(.{10,})",
    r"(?i)\bshare\s+(.{10,})",
    r"(?i)\bset up\s+(.{10,})",
    r"(?i)\bbook\s+(.{10,})",
]

# Avoma internal API path patterns to intercept
_MEETING_API_PATTERNS = ["/meetings", "/v1/meetings", "/api/meetings"]
_TRANSCRIPT_API_PATTERNS = ["/transcription", "/transcript", "/v1/transcription"]
_NOTES_API_PATTERNS = ["/notes", "/insights", "/v1/notes", "/v1/meetings"]


class AvomaScraperError(Exception):
    pass


class AvomaLoginError(AvomaScraperError):
    pass


class AvomaSessionMissingError(AvomaScraperError):
    pass


def session_exists() -> bool:
    return Path(COOKIES_FILE).exists()


def _load_cookies() -> list[dict]:
    try:
        with open(COOKIES_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_cookies(cookies: list[dict]) -> None:
    Path(SESSION_DIR).mkdir(parents=True, exist_ok=True)
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookies, f, indent=2)


# ---------------------------------------------------------------------------
# One-time manual login
# ---------------------------------------------------------------------------

def run_manual_login() -> None:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    print("\n  Opening Avoma login page in a browser window.")
    print("  Log in with Google (or however you normally sign in).")
    print("  The window will close automatically once you are signed in.\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        print("  Waiting for you to finish logging in ...")
        for _ in range(120):
            page.wait_for_timeout(1000)
            if _is_app_url(page.url):
                break
        else:
            browser.close()
            raise AvomaLoginError("Timed out. Please try again.")
        page.wait_for_timeout(2000)
        cookies = ctx.cookies()
        _save_cookies(cookies)
        browser.close()

    print(f"\n  Login successful. {len(cookies)} cookies saved.")
    print("  You won't need to log in again unless your session expires (~30 days).\n")


# ---------------------------------------------------------------------------
# AvomaScraper
# ---------------------------------------------------------------------------

class AvomaScraper:
    """
    Playwright browser scraper. Uses network response interception so we
    capture the same JSON the web app loads — no fragile CSS selectors,
    no cross-origin API issues.
    """

    def __init__(self, email: str | None = None, password: str | None = None,
                 headless: bool = True):
        self._email = email or Config.AVOMA_EMAIL
        self._password = password or Config.AVOMA_PASSWORD
        self._headless = headless

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
        self._pw = sync_playwright().__enter__()
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._ctx = self._browser.new_context(viewport={"width": 1280, "height": 900})
        cookies = _load_cookies()
        if cookies:
            self._ctx.add_cookies(cookies)
            logger.info("Avoma scraper: loaded %d cookies", len(cookies))
        self._page = self._ctx.new_page()
        return self

    def __exit__(self, *_):
        try:
            _save_cookies(self._ctx.cookies())
        except Exception:
            pass
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._pw.__exit__(None, None, None)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Login check
    # ------------------------------------------------------------------

    def ensure_logged_in(self) -> None:
        self._page.goto(_APP_URL, wait_until="domcontentloaded", timeout=30_000)
        _wait_settle(self._page, extra_ms=1500)
        if _is_app_url(self._page.url):
            logger.info("Avoma scraper: session valid")
            return
        if self._email and self._password:
            self._do_password_login()
        else:
            raise AvomaSessionMissingError(
                "Avoma session has expired.\n"
                "Run:  python3 main.py avoma-login"
            )

    def _do_password_login(self) -> None:
        self._page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        _wait_settle(self._page)
        email_sel = 'input[type="email"], input[name="email"]'
        pwd_sel = 'input[type="password"]'
        submit_sel = 'button[type="submit"], button:has-text("Sign in"), button:has-text("Log in")'
        try:
            self._page.wait_for_selector(email_sel, timeout=10_000)
            self._page.fill(email_sel, self._email)
        except Exception as exc:
            raise AvomaLoginError("SSO-only account — run: python3 main.py avoma-login") from exc
        self._page.wait_for_selector(pwd_sel, timeout=5_000)
        self._page.fill(pwd_sel, self._password)
        self._page.click(submit_sel)
        try:
            self._page.wait_for_url(lambda u: _is_app_url(u), timeout=15_000)
        except Exception as exc:
            raise AvomaLoginError("Login timed out.") from exc
        _wait_settle(self._page)
        _save_cookies(self._ctx.cookies())

    # ------------------------------------------------------------------
    # Meeting list via network interception
    # ------------------------------------------------------------------

    def list_meetings(
        self, from_dt: datetime, to_dt: datetime, page_size: int = 50,
    ) -> list[AvomaMeeting]:
        self.ensure_logged_in()

        from_date = from_dt.strftime("%Y-%m-%d")
        to_date = to_dt.strftime("%Y-%m-%d")
        captured: list[dict] = []

        def _on_response(response):
            try:
                url = response.url
                status = response.status
                if status != 200:
                    return
                # Capture any response that looks like a meeting list
                if any(p in url for p in _MEETING_API_PATTERNS):
                    body = response.json()
                    if isinstance(body, dict) and "results" in body:
                        captured.extend(body["results"])
                        logger.info("Intercepted %d meetings from %s", len(body["results"]), url)
                    elif isinstance(body, list) and body and "uuid" in body[0]:
                        captured.extend(body)
                        logger.info("Intercepted %d meetings (list) from %s", len(body), url)
            except Exception:
                pass

        self._page.on("response", _on_response)
        try:
            url = f"{_APP_URL}/meetings?from={from_date}&to={to_date}"
            logger.info("Avoma scraper: navigating to %s", url)
            self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            # Wait generously for the SPA to finish loading and making its API calls
            _wait_settle(self._page, extra_ms=4000)
        finally:
            self._page.remove_listener("response", _on_response)

        if captured:
            from .avoma_client import _parse_meeting  # noqa: PLC0415
            # Deduplicate by uuid
            seen: set[str] = set()
            meetings = []
            for r in captured:
                uid = r.get("uuid", "")
                if uid and uid not in seen:
                    seen.add(uid)
                    meetings.append(_parse_meeting(r))
            logger.info("Avoma scraper: %d meetings captured", len(meetings))
            return meetings

        logger.warning("Avoma scraper: network interception found no meetings — trying DOM fallback")
        return self._dom_meetings_fallback()

    def _dom_meetings_fallback(self) -> list[AvomaMeeting]:
        """Read meeting links visible in the DOM as a last resort."""
        meetings: list[AvomaMeeting] = []
        try:
            # Collect all links on the page that look like meeting URLs
            links = self._page.evaluate("""
                () => {
                    return [...document.querySelectorAll('a[href]')]
                        .map(a => ({href: a.href, text: a.innerText.trim()}))
                        .filter(l => /meetings\\/[0-9a-f-]{36}/.test(l.href));
                }
            """)
            seen: set[str] = set()
            for link in (links or []):
                uuid = _extract_uuid(link.get("href", ""))
                if uuid and uuid not in seen:
                    seen.add(uuid)
                    meetings.append(AvomaMeeting(
                        uuid=uuid,
                        subject=link.get("text") or "(meeting)",
                        start_at=datetime.now(timezone.utc),
                        end_at=datetime.now(timezone.utc),
                        attendees=[],
                        state="completed",
                        transcript_ready=True,
                        notes_ready=True,
                        transcription_uuid=None,
                    ))
            logger.info("DOM fallback found %d meeting links", len(meetings))
        except Exception:
            logger.debug("DOM meetings fallback failed", exc_info=True)
        return meetings

    # ------------------------------------------------------------------
    # Action items via network interception + transcript parsing
    # ------------------------------------------------------------------

    def get_action_items(
        self, meeting_uuid: str, meeting_subject: str = ""
    ) -> list[AvomaActionItem]:
        """
        Navigate to the meeting detail page, intercept API responses for
        notes/insights, and extract action items. Also parses the visible
        transcript text as a fallback.
        """
        self.ensure_logged_in()

        captured_notes: list[dict] = []
        captured_transcript: list[dict] = []

        def _on_response(response):
            try:
                url = response.url
                if response.status != 200:
                    return
                if meeting_uuid not in url:
                    return
                body = response.json()
                if any(p in url for p in _NOTES_API_PATTERNS):
                    if "ai_notes" in body:
                        captured_notes.append(body)
                        logger.info("Intercepted notes for %s", meeting_uuid)
                if any(p in url for p in _TRANSCRIPT_API_PATTERNS):
                    captured_transcript.append(body)
                    logger.info("Intercepted transcript for %s", meeting_uuid)
            except Exception:
                pass

        self._page.on("response", _on_response)
        try:
            self._page.goto(
                f"{_APP_URL}/meetings/{meeting_uuid}",
                wait_until="domcontentloaded", timeout=30_000,
            )
            _wait_settle(self._page, extra_ms=4000)
        finally:
            self._page.remove_listener("response", _on_response)

        # 1. Try AI notes from intercepted JSON
        items = self._action_items_from_notes(captured_notes, meeting_uuid, meeting_subject)
        if items:
            return items

        # 2. Try transcript text from intercepted JSON
        items = self._action_items_from_transcript_json(
            captured_transcript, meeting_uuid, meeting_subject
        )
        if items:
            return items

        # 3. Read transcript text directly from the DOM
        return self._action_items_from_dom_transcript(meeting_uuid, meeting_subject)

    def _action_items_from_notes(
        self, notes_data: list[dict], meeting_uuid: str, meeting_subject: str
    ) -> list[AvomaActionItem]:
        items = []
        for data in notes_data:
            speakers = {
                s["id"]: s.get("name", "")
                for s in data.get("speakers", [])
            }
            for note in data.get("ai_notes", []):
                ntype = note.get("note_type", "")
                if ntype in ("action_item", "next_step", "action", "follow_up"):
                    text = note.get("text", "").strip()
                    if text:
                        items.append(AvomaActionItem(
                            text=text,
                            speaker_name=speakers.get(note.get("speaker_id", -1), ""),
                            meeting_uuid=meeting_uuid,
                            meeting_subject=meeting_subject,
                        ))
        return items

    def _action_items_from_transcript_json(
        self, transcript_data: list[dict], meeting_uuid: str, meeting_subject: str
    ) -> list[AvomaActionItem]:
        items = []
        for data in transcript_data:
            speakers = {
                s["id"]: s.get("name", "")
                for s in data.get("speakers", [])
            }
            for line in data.get("transcript", []):
                text = line.get("transcript", "").strip()
                extracted = _extract_action_items_from_text(text)
                for action in extracted:
                    spk_id = line.get("speaker_id", -1)
                    items.append(AvomaActionItem(
                        text=action,
                        speaker_name=speakers.get(spk_id, ""),
                        meeting_uuid=meeting_uuid,
                        meeting_subject=meeting_subject,
                    ))
        return items

    def _action_items_from_dom_transcript(
        self, meeting_uuid: str, meeting_subject: str
    ) -> list[AvomaActionItem]:
        """Extract all transcript text from the DOM and run keyword matching."""
        items = []
        try:
            # Grab all text that looks like transcript content
            texts = self._page.evaluate("""
                () => {
                    // Try specific transcript containers first
                    const selectors = [
                        '[class*="transcript"]',
                        '[class*="Transcript"]',
                        '[data-testid*="transcript"]',
                        '[class*="note"]',
                        '[class*="Note"]',
                        '[class*="summary"]',
                    ];
                    for (const sel of selectors) {
                        const els = document.querySelectorAll(sel);
                        if (els.length > 0) {
                            return [...els].map(e => e.innerText).join('\\n');
                        }
                    }
                    // Fallback: grab the main content area
                    const main = document.querySelector('main') ||
                                 document.querySelector('[role="main"]') ||
                                 document.body;
                    return main ? main.innerText : '';
                }
            """)
            if texts:
                extracted = _extract_action_items_from_text(texts)
                for action in extracted:
                    items.append(AvomaActionItem(
                        text=action,
                        meeting_uuid=meeting_uuid,
                        meeting_subject=meeting_subject,
                    ))
                logger.info("DOM transcript fallback: %d action items for %s",
                            len(items), meeting_uuid)
        except Exception:
            logger.debug("DOM transcript fallback failed", exc_info=True)
        return items

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    def extract_todays_action_items(
        self, from_dt: datetime, to_dt: datetime,
    ) -> list[AvomaActionItem]:
        action_items: list[AvomaActionItem] = []
        try:
            meetings = self.list_meetings(from_dt, to_dt)
        except Exception:
            logger.warning("Avoma scraper: failed to list meetings", exc_info=True)
            return action_items
        completed = [m for m in meetings if m.state in ("completed", "")]
        logger.info("Avoma scraper: processing %d completed meetings for action items",
                    len(completed))
        for m in completed:
            try:
                items = self.get_action_items(m.uuid, m.subject)
                logger.info("  %s → %d action items", m.subject[:50], len(items))
                action_items.extend(items)
            except Exception:
                logger.warning("  Failed for %s", m.uuid, exc_info=True)
        return action_items

    def get_transcript(self, meeting_uuid: str) -> AvomaTranscript | None:
        self.ensure_logged_in()
        captured: list[dict] = []

        def _on_response(response):
            try:
                if response.status == 200 and any(
                    p in response.url for p in _TRANSCRIPT_API_PATTERNS
                ):
                    captured.append(response.json())
            except Exception:
                pass

        self._page.on("response", _on_response)
        try:
            self._page.goto(
                f"{_APP_URL}/meetings/{meeting_uuid}",
                wait_until="domcontentloaded", timeout=30_000,
            )
            _wait_settle(self._page, extra_ms=3000)
        finally:
            self._page.remove_listener("response", _on_response)

        if captured:
            from .avoma_client import _parse_transcript  # noqa: PLC0415
            try:
                return _parse_transcript(captured[0])
            except Exception:
                pass
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_avoma_source():
    mode = Config.avoma_mode()
    if mode == "api":
        from .avoma_client import AvomaClient  # noqa: PLC0415
        return AvomaClient()
    if mode == "scraper":
        return AvomaScraper()
    return None


# ---------------------------------------------------------------------------
# Transcript text → action item extractor
# ---------------------------------------------------------------------------

def _extract_action_items_from_text(text: str) -> list[str]:
    """
    Scan a block of text for sentences that look like action items.
    Returns deduplicated list of action item strings.
    """
    items: list[str] = []
    seen: set[str] = set()
    lines = text.splitlines()
    for line in lines:
        line = line.strip()
        if len(line) < 10 or len(line) > 300:
            continue
        for pattern in _ACTION_PATTERNS:
            m = re.search(pattern, line)
            if m:
                # Use the capture group if present, otherwise the full line
                action = (m.group(1) if m.lastindex else line).strip()
                action = re.sub(r"\s+", " ", action).strip(" .,;:")
                if len(action) >= 10 and action.lower() not in seen:
                    seen.add(action.lower())
                    items.append(action)
                break  # only extract once per line
    return items


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_app_url(url: str) -> bool:
    return (
        _APP_URL in url
        and "login" not in url
        and "signin" not in url
        and "auth" not in url.split("?")[0]
    )


def _wait_settle(page, extra_ms: int = 500) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    if extra_ms:
        page.wait_for_timeout(extra_ms)


def _extract_uuid(url: str) -> str | None:
    m = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", url
    )
    return m.group(0) if m else None


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return text[:80]


def _fmt_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
