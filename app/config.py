import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # Set DATABASE_URL in .env. Default local dev uses SQLite file in project directory.
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///haro.db")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "changeme")
    flask_secret_key: str = os.getenv("FLASK_SECRET_KEY", "change-this-secret")

    global_auto_send: bool = _as_bool(os.getenv("GLOBAL_AUTO_SEND"), False)
    global_dry_run: bool = _as_bool(os.getenv("GLOBAL_DRY_RUN"), True)
    global_review_mode: bool = _as_bool(os.getenv("GLOBAL_REVIEW_MODE"), True)
    max_sends_per_run: int = int(os.getenv("MAX_SENDS_PER_RUN", "20"))
    lookback_hours: int = int(os.getenv("LOOKBACK_HOURS", "48"))

    # If > 0, scheduler runs poll every N minutes; else uses RUN_TIMES cron.
    poll_interval_minutes: int = int(os.getenv("POLL_INTERVAL_MINUTES", "10"))
    scheduler_times: str = os.getenv("RUN_TIMES", "08:00,13:00,18:00")
    # Optional: home/garden requests always route to this business id (set in .env).
    home_garden_business_id: Optional[int] = (
        int(os.getenv("HOME_GARDEN_BUSINESS_ID", "").strip())
        if (os.getenv("HOME_GARDEN_BUSINESS_ID") or "").strip().isdigit()
        else None
    )


settings = Settings()
