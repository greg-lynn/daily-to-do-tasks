"""
Configuration loaded from environment variables / .env file.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            "Copy .env.example to .env and fill in the values."
        )
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


class Config:
    # Avoma — API key (admin-generated) OR email+password (credential-based scraper)
    AVOMA_API_KEY: str = _optional("AVOMA_API_KEY")
    AVOMA_BASE_URL: str = "https://api.avoma.com"
    # Used when AVOMA_API_KEY is not available (non-admin workaround)
    AVOMA_EMAIL: str = _optional("AVOMA_EMAIL")
    AVOMA_PASSWORD: str = _optional("AVOMA_PASSWORD")

    # Email sending (SMTP)
    SMTP_HOST: str = _optional("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT: int = int(_optional("SMTP_PORT", "587"))
    SMTP_USER: str = _optional("SMTP_USER")
    SMTP_PASSWORD: str = _optional("SMTP_PASSWORD")
    RECIPIENT_EMAIL: str = _optional("RECIPIENT_EMAIL", "glynn@rocketlane.com")

    # Scheduling
    MORNING_TIME: str = _optional("MORNING_TIME", "08:00")   # HH:MM in local TZ
    EVENING_TIME: str = _optional("EVENING_TIME", "18:00")   # HH:MM in local TZ
    TIMEZONE: str = _optional("TIMEZONE", "America/New_York")

    # Database path
    DB_PATH: str = _optional("DB_PATH", "tasks.db")

    # How many days back to pull Avoma transcripts for the evening email
    AVOMA_LOOKBACK_HOURS: int = int(_optional("AVOMA_LOOKBACK_HOURS", "24"))

    @classmethod
    def validate_email(cls) -> None:
        missing = [k for k in ("SMTP_USER", "SMTP_PASSWORD", "RECIPIENT_EMAIL")
                   if not getattr(cls, k)]
        if missing:
            raise EnvironmentError(
                f"Missing email config: {', '.join(missing)}. "
                "Set them in your .env file."
            )

    @classmethod
    def _avoma_session_exists(cls) -> bool:
        """True when `avoma-login` has been run and a browser session is saved."""
        from .avoma_scraper import SESSION_DIR, session_exists  # noqa: PLC0415
        return session_exists()

    @classmethod
    def avoma_enabled(cls) -> bool:
        return bool(cls.AVOMA_API_KEY) or cls.avoma_mode() == "scraper"

    @classmethod
    def avoma_mode(cls) -> str:
        """
        Returns 'api', 'scraper', or 'disabled'.

        Scraper mode activates when ANY of the following is true:
          - AVOMA_EMAIL + AVOMA_PASSWORD are both set (auto-login on first run)
          - A saved browser session exists from a previous `avoma-login` run
        """
        if cls.AVOMA_API_KEY:
            return "api"
        if cls.AVOMA_EMAIL and cls.AVOMA_PASSWORD:
            return "scraper"
        # Check for a persisted browser session (set by `avoma-login`)
        try:
            if cls._avoma_session_exists():
                return "scraper"
        except Exception:
            pass
        return "disabled"
