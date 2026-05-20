"""
Avoma browser-based data extractor using Playwright.

Session approach: saves cookies to .avoma_session/cookies.json after login.
The scraper then loads those cookies into a fresh headless browser context.
This avoids the browser-profile binary incompatibility between the headed
(full Chromium) login browser and the headless shell used for automation.

Two auth sub-modes:
  1. SESSION mode (recommended — works with Google/Microsoft SSO)
       python3 main.py avoma-login
     A visible browser opens. Log in via Google SSO. Cookies are saved.
     Every subsequent run is fully headless and automatic.

  2. PASSWORD mode (email + password Avoma accounts only)
     Set AVOMA_EMAIL + AVOMA_PASSWORD in .env. Auto-logs in on first run.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .avoma_client import (
    AvomaMeeting, AvomaAttendee, AvomaActionItem, AvomaTranscript,
    AvomaTranscriptLine,
)
from .config import Config

logger = logging.getLogger(__name__)

_APP_URL = "https://app.avoma.com"
_LOGIN_URL = f"{_APP_URL}/login"

# Directory and file where the session cookies are persisted
SESSION_DIR = os.path.join(os.path.dirname(__file__), "..", ".avoma_session")
COOKIES_FILE = os.path.join(SESSION_DIR, "cookies.json")


class AvomaScraperError(Exception):
    pass


class AvomaLoginError(AvomaScraperError):
    pass


class AvomaSessionMissingError(AvomaScraperError):
    pass


def session_exists() -> bool:
    """True if a cookies.json has been saved by avoma-login."""
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
# One-time manual login (called by `python3 main.py avoma-login`)
# ---------------------------------------------------------------------------

def run_manual_login() -> None:
    """
    Open a VISIBLE browser so the user can log in (Google SSO, etc.).
    Saves cookies to .avoma_session/cookies.json when done.
    """
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

        # Poll until we land inside the app (not on login/auth pages)
        for _ in range(120):
            page.wait_for_timeout(1000)
            if _is_app_url(page.url):
                break
        else:
            browser.close()
            raise AvomaLoginError("Timed out waiting for login. Please try again.")

        # Brief pause so all auth cookies are fully written
        page.wait_for_timeout(2000)

        # Save cookies
        cookies = ctx.cookies()
        _save_cookies(cookies)
        browser.close()

    print(f"\n  Login successful. {len(cookies)} cookies saved to .avoma_session/cookies.json")
    print("  You won't need to log in again unless your session expires (~30 days).\n")


# ---------------------------------------------------------------------------
# AvomaScraper
# ---------------------------------------------------------------------------

class AvomaScraper:
    """
    Playwright-based Avoma data extractor.

    Uses a standard headless browser context loaded with saved cookies —
    no persistent profile needed, no binary compatibility issues.

    Context-manager usage:
        with AvomaScraper() as scraper:
            meetings = scraper.list_meetings(from_dt, to_dt)
    """

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        headless: bool = True,
    ):
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

        # Load saved cookies if available
        cookies = _load_cookies()
        if cookies:
            self._ctx.add_cookies(cookies)
            logger.info("Avoma scraper: loaded %d cookies from session file", len(cookies))

        self._page = self._ctx.new_page()
        return self

    def __exit__(self, *_):
        # Persist any updated cookies before closing
        try:
            updated = self._ctx.cookies()
            if updated:
                _save_cookies(updated)
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
    # Login check / auto-login
    # ------------------------------------------------------------------

    def ensure_logged_in(self) -> None:
        """Navigate to Avoma. Re-authenticate if the session has expired."""
        self._page.goto(_APP_URL, wait_until="domcontentloaded", timeout=30_000)
        _wait_settle(self._page)

        if _is_app_url(self._page.url):
            logger.info("Avoma scraper: session valid")
            return

        # Session expired or missing
        if self._email and self._password:
            logger.info("Avoma scraper: session expired, logging in with password ...")
            self._do_password_login()
        else:
            raise AvomaSessionMissingError(
                "Avoma session has expired or is missing.\n"
                "Run:  python3 main.py avoma-login\n"
                "A browser window will open — log in with Google/SSO once "
                "and the session will be refreshed automatically."
            )

    def _do_password_login(self) -> None:
        self._page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        _wait_settle(self._page)

        email_sel = 'input[type="email"], input[name="email"], input[placeholder*="email" i]'
        pwd_sel = 'input[type="password"], input[name="password"]'
        submit_sel = (
            'button[type="submit"], button:has-text("Sign in"), '
            'button:has-text("Log in"), button:has-text("Continue")'
        )
        try:
            self._page.wait_for_selector(email_sel, timeout=10_000)
            self._page.fill(email_sel, self._email)
        except Exception as exc:
            raise AvomaLoginError(
                "Could not find the email field — your account may be SSO-only.\n"
                "Run:  python3 main.py avoma-login"
            ) from exc

        self._page.wait_for_selector(pwd_sel, timeout=5_000)
        self._page.fill(pwd_sel, self._password)
        self._page.click(submit_sel)

        try:
            self._page.wait_for_url(lambda u: _is_app_url(u), timeout=15_000)
        except Exception as exc:
            body = self._page.inner_text("body")
            if any(w in body.lower() for w in ("invalid", "incorrect", "wrong", "error")):
                raise AvomaLoginError("Avoma login failed: incorrect email or password.")
            raise AvomaLoginError("Avoma login timed out.") from exc

        _wait_settle(self._page)
        # Save refreshed cookies
        _save_cookies(self._ctx.cookies())
        logger.info("Avoma scraper: password login successful, cookies saved")

    # ------------------------------------------------------------------
    # Meeting list
    # ------------------------------------------------------------------

    def list_meetings(
        self,
        from_dt: datetime,
        to_dt: datetime,
        page_size: int = 50,
    ) -> list[AvomaMeeting]:
        self.ensure_logged_in()
        from_str = _fmt_dt(from_dt)
        to_str = _fmt_dt(to_dt)

        meetings = self._browser_fetch_meetings(from_str, to_str, page_size)
        if meetings is not None:
            return meetings

        logger.warning("Avoma scraper: falling back to DOM meeting list")
        return self._dom_meetings(from_dt, to_dt)

    def _browser_fetch_meetings(
        self, from_str: str, to_str: str, page_size: int
    ) -> list[AvomaMeeting] | None:
        try:
            result = self._page.evaluate(f"""
                async () => {{
                    try {{
                        const r = await fetch(
                            '/v1/meetings/?from_date={from_str}&to_date={to_str}&page_size={page_size}',
                            {{credentials: 'include'}}
                        );
                        if (!r.ok) return null;
                        return await r.json();
                    }} catch(e) {{ return null; }}
                }}
            """)
            if result and "results" in result:
                logger.info("Avoma scraper: fetched %d meetings via API", len(result["results"]))
                from .avoma_client import _parse_meeting  # noqa: PLC0415
                return [_parse_meeting(r) for r in result["results"]]
        except Exception:
            logger.debug("In-browser meetings fetch failed", exc_info=True)
        return None

    def _dom_meetings(self, from_dt: datetime, to_dt: datetime) -> list[AvomaMeeting]:
        from_date = from_dt.strftime("%Y-%m-%d")
        to_date = to_dt.strftime("%Y-%m-%d")
        self._page.goto(
            f"{_APP_URL}/meetings?from={from_date}&to={to_date}",
            wait_until="domcontentloaded", timeout=30_000,
        )
        _wait_settle(self._page, extra_ms=2500)
        meetings: list[AvomaMeeting] = []
        for sel in (
            '[data-testid*="meeting"]', '.meeting-row', '.meeting-item',
            '[class*="MeetingRow"]', '[class*="meeting-card"]',
        ):
            cards = self._page.query_selector_all(sel)
            if cards:
                for card in cards[:50]:
                    try:
                        text = card.inner_text()
                        link = card.query_selector("a")
                        href = link.get_attribute("href") if link else ""
                        uuid = _extract_uuid(href or "")
                        subject = _first_line(text)
                        meetings.append(AvomaMeeting(
                            uuid=uuid or f"dom-{abs(hash(subject))}",
                            subject=subject,
                            start_at=datetime.now(timezone.utc),
                            end_at=datetime.now(timezone.utc),
                            attendees=[],
                            state="completed",
                            transcript_ready=True,
                            notes_ready=True,
                            transcription_uuid=None,
                        ))
                    except Exception:
                        continue
                break
        return meetings

    # ------------------------------------------------------------------
    # Action items
    # ------------------------------------------------------------------

    def get_action_items(
        self, meeting_uuid: str, meeting_subject: str = ""
    ) -> list[AvomaActionItem]:
        self.ensure_logged_in()
        items = self._browser_fetch_insights(meeting_uuid, meeting_subject)
        if items is not None:
            return items
        return self._dom_action_items(meeting_uuid, meeting_subject)

    def _browser_fetch_insights(
        self, meeting_uuid: str, meeting_subject: str
    ) -> list[AvomaActionItem] | None:
        try:
            result = self._page.evaluate(f"""
                async () => {{
                    try {{
                        const r = await fetch(
                            '/v1/meetings/{meeting_uuid}/insights/',
                            {{credentials: 'include'}}
                        );
                        if (!r.ok) return null;
                        return await r.json();
                    }} catch(e) {{ return null; }}
                }}
            """)
            if not result:
                return None
            speakers = {
                s["id"]: s.get("name", "")
                for s in result.get("speakers", [])
            }
            items = []
            for note in result.get("ai_notes", []):
                if note.get("note_type") in ("action_item", "next_step", "action"):
                    items.append(AvomaActionItem(
                        text=note.get("text", "").strip(),
                        speaker_name=speakers.get(note.get("speaker_id", -1), ""),
                        meeting_uuid=meeting_uuid,
                        meeting_subject=meeting_subject,
                    ))
            return items
        except Exception:
            logger.debug("In-browser insights fetch failed for %s", meeting_uuid, exc_info=True)
            return None

    def _dom_action_items(
        self, meeting_uuid: str, meeting_subject: str
    ) -> list[AvomaActionItem]:
        self._page.goto(
            f"{_APP_URL}/meetings/{meeting_uuid}",
            wait_until="domcontentloaded", timeout=30_000,
        )
        _wait_settle(self._page, extra_ms=2500)
        items: list[AvomaActionItem] = []
        for sel in (
            '[data-note-type="action_item"]', '[data-category*="action"]',
            '.action-item', '[class*="ActionItem"]',
        ):
            els = self._page.query_selector_all(sel)
            for el in els:
                text = el.inner_text().strip()
                if text and len(text) > 5:
                    items.append(AvomaActionItem(
                        text=text, meeting_uuid=meeting_uuid,
                        meeting_subject=meeting_subject,
                    ))
            if items:
                return items

        for heading in ("Action Items", "Next Steps", "Follow-up"):
            try:
                result = self._page.evaluate(f"""
                    () => {{
                        const hdrs = [...document.querySelectorAll(
                            'h1,h2,h3,h4,h5,strong,[class*="heading"],[class*="title"]'
                        )];
                        const hdr = hdrs.find(
                            el => el.innerText && el.innerText.trim().toLowerCase()
                                     .includes('{heading.lower()}')
                        );
                        if (!hdr) return [];
                        const out = [];
                        let el = (hdr.parentElement || hdr).nextElementSibling;
                        for (let i = 0; i < 20 && el; i++, el = el.nextElementSibling) {{
                            const t = (el.innerText || '').trim();
                            if (t) out.push(t);
                        }}
                        return out;
                    }}
                """)
                for text in (result or []):
                    text = text.strip()
                    if text and len(text) > 5:
                        items.append(AvomaActionItem(
                            text=text, meeting_uuid=meeting_uuid,
                            meeting_subject=meeting_subject,
                        ))
                if items:
                    break
            except Exception:
                continue
        return items

    # ------------------------------------------------------------------
    # High-level helpers (same signature as AvomaClient)
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
        for m in meetings:
            if not m.notes_ready:
                continue
            try:
                items = self.get_action_items(m.uuid, m.subject)
                action_items.extend(items)
            except Exception:
                logger.warning("Avoma scraper: insights failed for %s", m.uuid, exc_info=True)
        return action_items

    def get_transcript(self, meeting_uuid: str) -> AvomaTranscript | None:
        self.ensure_logged_in()
        try:
            result = self._page.evaluate(f"""
                async () => {{
                    const r = await fetch(
                        '/v1/transcriptions/?meeting_uuid={meeting_uuid}',
                        {{credentials: 'include'}}
                    );
                    if (!r.ok) return null;
                    const d = await r.json();
                    return Array.isArray(d) ? d[0] : d;
                }}
            """)
            if result:
                from .avoma_client import _parse_transcript  # noqa: PLC0415
                return _parse_transcript(result)
        except Exception:
            logger.debug("Scraper transcript fetch failed for %s", meeting_uuid, exc_info=True)
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
        page.wait_for_load_state("networkidle", timeout=8_000)
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
