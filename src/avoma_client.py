"""
Avoma REST API client.

Docs: https://dev.avoma.com/
Auth: Bearer token via AVOMA_API_KEY env variable.

Key resources used:
  GET /v1/meetings/           - list meetings in a date range
  GET /v1/meetings/{uuid}/insights/ - AI notes + action items
  GET /v1/transcriptions/     - full transcripts
  GET /v1/notes/              - AI-generated summaries
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from .config import Config

logger = logging.getLogger(__name__)

_BASE = "https://api.avoma.com"


@dataclass
class AvomaAttendee:
    email: str
    name: str


@dataclass
class AvomaActionItem:
    text: str
    speaker_name: str = ""
    meeting_uuid: str = ""
    meeting_subject: str = ""


@dataclass
class AvomaTranscriptLine:
    speaker_id: int
    text: str
    timestamps: list[float] = field(default_factory=list)


@dataclass
class AvomaTranscript:
    uuid: str
    meeting_uuid: str
    speakers: list[dict]
    lines: list[AvomaTranscriptLine]
    vtt_url: str = ""

    def speaker_name(self, speaker_id: int) -> str:
        for s in self.speakers:
            if s.get("id") == speaker_id:
                return s.get("name", f"Speaker {speaker_id}")
        return f"Speaker {speaker_id}"

    def as_text(self, max_chars: int = 4000) -> str:
        parts: list[str] = []
        for line in self.lines:
            name = self.speaker_name(line.speaker_id)
            parts.append(f"{name}: {line.text}")
        full = "\n".join(parts)
        return full[:max_chars] + ("…" if len(full) > max_chars else "")


@dataclass
class AvomaNote:
    meeting_uuid: str
    text: str


@dataclass
class AvomaInsight:
    meeting_uuid: str
    action_items: list[AvomaActionItem]
    speakers: list[dict]
    raw: dict


@dataclass
class AvomaMeeting:
    uuid: str
    subject: str
    start_at: datetime
    end_at: datetime
    attendees: list[AvomaAttendee]
    state: str                       # scheduled | completed | cancelled
    transcript_ready: bool
    notes_ready: bool
    transcription_uuid: Optional[str]
    duration_seconds: float = 0.0

    @property
    def start_local(self) -> datetime:
        return self.start_at.astimezone()

    @property
    def duration_minutes(self) -> int:
        return int(self.duration_seconds / 60)


class AvomaClient:
    """Thin wrapper around the Avoma REST API."""

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or Config.AVOMA_API_KEY
        if not self._api_key:
            raise ValueError(
                "AVOMA_API_KEY is not set. Add it to your .env file."
            )
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {self._api_key}",
             "Content-Type": "application/json"}
        )

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{_BASE}{path}"
        resp = self._session.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            raise RuntimeError("Avoma rate limit hit (60 req/min). Slow down.")
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Meetings
    # ------------------------------------------------------------------

    def list_meetings(
        self,
        from_dt: datetime,
        to_dt: datetime,
        page_size: int = 50,
    ) -> list[AvomaMeeting]:
        """Return all meetings in [from_dt, to_dt] (UTC)."""
        from_str = _fmt_dt(from_dt)
        to_str = _fmt_dt(to_dt)
        params: dict[str, Any] = {
            "from_date": from_str,
            "to_date": to_str,
            "page_size": page_size,
            "o": "-start_at",
        }
        meetings: list[AvomaMeeting] = []
        while True:
            data = self._get("/v1/meetings/", params=params)
            results = data.get("results", [])
            for r in results:
                meetings.append(_parse_meeting(r))
            if not data.get("next"):
                break
            # cursor pagination: the next field contains a full URL with cursor
            params["cursor"] = _extract_cursor(data["next"])
        return meetings

    def get_meeting(self, uuid: str) -> AvomaMeeting | None:
        try:
            r = self._get(f"/v1/meetings/{uuid}/")
            return _parse_meeting(r)
        except requests.HTTPError as e:
            if e.response and e.response.status_code == 404:
                return None
            raise

    # ------------------------------------------------------------------
    # Insights / action items
    # ------------------------------------------------------------------

    def get_insights(self, meeting_uuid: str) -> AvomaInsight | None:
        """Return AI notes (including action items) for a completed meeting."""
        try:
            data = self._get(f"/v1/meetings/{meeting_uuid}/insights/")
        except requests.HTTPError as e:
            if e.response and e.response.status_code in (404, 400):
                return None
            raise
        action_items: list[AvomaActionItem] = []
        speakers = data.get("speakers", [])
        speaker_map = {s["id"]: s.get("name", "") for s in speakers}
        for note in data.get("ai_notes", []):
            if note.get("note_type") in ("action_item", "next_step", "action"):
                action_items.append(
                    AvomaActionItem(
                        text=note.get("text", "").strip(),
                        speaker_name=speaker_map.get(note.get("speaker_id", -1), ""),
                        meeting_uuid=meeting_uuid,
                    )
                )
        return AvomaInsight(
            meeting_uuid=meeting_uuid,
            action_items=action_items,
            speakers=speakers,
            raw=data,
        )

    # ------------------------------------------------------------------
    # Transcriptions
    # ------------------------------------------------------------------

    def get_transcript(self, meeting_uuid: str) -> AvomaTranscript | None:
        """Fetch the full transcript for a meeting."""
        try:
            data = self._get("/v1/transcriptions/", params={"meeting_uuid": meeting_uuid})
        except requests.HTTPError as e:
            if e.response and e.response.status_code in (404, 400):
                return None
            raise
        # When meeting_uuid is given, the API returns a single object (not a list)
        if isinstance(data, list):
            if not data:
                return None
            data = data[0]
        return _parse_transcript(data)

    def get_transcript_by_uuid(self, uuid: str) -> AvomaTranscript | None:
        try:
            data = self._get(f"/v1/transcriptions/{uuid}/")
            return _parse_transcript(data)
        except requests.HTTPError as e:
            if e.response and e.response.status_code == 404:
                return None
            raise

    # ------------------------------------------------------------------
    # Notes (AI summaries)
    # ------------------------------------------------------------------

    def list_notes(
        self,
        from_dt: datetime,
        to_dt: datetime,
        page_size: int = 50,
    ) -> list[AvomaNote]:
        from_str = _fmt_dt(from_dt)
        to_str = _fmt_dt(to_dt)
        params: dict[str, Any] = {
            "from_date": from_str,
            "to_date": to_str,
            "page_size": page_size,
        }
        notes: list[AvomaNote] = []
        try:
            data = self._get("/v1/notes/", params=params)
            for r in data.get("results", []):
                notes.append(
                    AvomaNote(
                        meeting_uuid=r.get("meeting_uuid", ""),
                        text=r.get("text", ""),
                    )
                )
        except requests.HTTPError:
            logger.warning("Could not fetch Avoma notes", exc_info=True)
        return notes

    # ------------------------------------------------------------------
    # High-level helper: action items from today's completed meetings
    # ------------------------------------------------------------------

    def extract_todays_action_items(
        self,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[AvomaActionItem]:
        """
        Fetch all completed meetings in [from_dt, to_dt] that have notes
        ready, pull their AI insights, and return action items.
        """
        action_items: list[AvomaActionItem] = []
        try:
            meetings = self.list_meetings(from_dt, to_dt)
        except Exception:
            logger.warning("Failed to list Avoma meetings", exc_info=True)
            return action_items

        for m in meetings:
            if m.state != "completed" or not m.notes_ready:
                continue
            try:
                insight = self.get_insights(m.uuid)
            except Exception:
                logger.warning("Failed to get insights for %s", m.uuid, exc_info=True)
                continue
            if insight:
                for ai in insight.action_items:
                    ai.meeting_subject = m.subject
                action_items.extend(insight.action_items)

        return action_items


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_cursor(next_url: str) -> str:
    """Pull the 'cursor' query param from a pagination URL."""
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(next_url).query)
    cursors = qs.get("cursor", [])
    return cursors[0] if cursors else ""


def _parse_meeting(r: dict) -> AvomaMeeting:
    attendees = [
        AvomaAttendee(email=a.get("email", ""), name=a.get("name", ""))
        for a in r.get("attendees", [])
    ]
    return AvomaMeeting(
        uuid=r["uuid"],
        subject=r.get("subject", "(no subject)"),
        start_at=_parse_dt(r.get("start_at", "")),
        end_at=_parse_dt(r.get("end_at", "")),
        attendees=attendees,
        state=r.get("state", ""),
        transcript_ready=r.get("transcript_ready", False),
        notes_ready=r.get("notes_ready", False),
        transcription_uuid=r.get("transcription_uuid"),
        duration_seconds=float(r.get("duration") or 0),
    )


def _parse_dt(s: str) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _parse_transcript(data: dict) -> AvomaTranscript:
    lines = [
        AvomaTranscriptLine(
            speaker_id=line.get("speaker_id", 0),
            text=line.get("transcript", ""),
            timestamps=line.get("timestamps", []),
        )
        for line in data.get("transcript", [])
    ]
    return AvomaTranscript(
        uuid=data.get("uuid", ""),
        meeting_uuid=data.get("meeting_uuid", ""),
        speakers=data.get("speakers", []),
        lines=lines,
        vtt_url=data.get("transcription_vtt_url", ""),
    )
