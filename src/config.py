"""
Configuration loaded from environment variables / .env file.
"""
import os
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
    # Avoma
    AVOMA_API_KEY: str = _optional("AVOMA_API_KEY")
    AVOMA_BASE_URL: str = "https://api.avoma.com"

    # Email sending (SMTP)
    SMTP_HOST: str = _optional("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT: int = int(_optional("SMTP_PORT", "587"))
    SMTP_USER: str = _optional("SMTP_USER")
    SMTP_PASSWORD: str = _optional("SMTP_PASSWORD")
    RECIPIENT_EMAIL: str = _optional("RECIPIENT_EMAIL")

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
    def avoma_enabled(cls) -> bool:
        return bool(cls.AVOMA_API_KEY)
