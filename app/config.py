import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _public_base_url_default() -> str:
    explicit = (os.getenv("PUBLIC_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    domain = (os.getenv("APP_DOMAIN", "floatfire.com") or "floatfire.com").strip()
    return "https://%s" % domain


def _as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # Public hostname for branding and absolute links (default: Floatfire production domain).
    app_domain: str = (os.getenv("APP_DOMAIN", "floatfire.com") or "floatfire.com").strip()
    public_base_url: str = _public_base_url_default()
    app_name: str = (os.getenv("APP_NAME", "Floatfire HARO") or "Floatfire HARO").strip()

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

    # Asset pipeline (conservative defaults)
    enable_asset_automation: bool = _as_bool(os.getenv("ENABLE_ASSET_AUTOMATION"), False)
    enable_ai_concept_visuals: bool = _as_bool(os.getenv("ENABLE_AI_CONCEPT_VISUALS"), False)
    enable_inline_image_previews: bool = _as_bool(os.getenv("ENABLE_INLINE_IMAGE_PREVIEWS"), False)
    max_inline_preview_images: int = int(os.getenv("MAX_INLINE_PREVIEW_IMAGES", "2"))
    max_generated_candidates: int = int(os.getenv("MAX_GENERATED_CANDIDATES", "6"))
    auto_send_concept_visuals: bool = _as_bool(os.getenv("AUTO_SEND_CONCEPT_VISUALS"), False)
    auto_send_real_assets: bool = _as_bool(os.getenv("AUTO_SEND_REAL_ASSETS"), False)
    asset_planner_use_llm: bool = _as_bool(os.getenv("ASSET_PLANNER_USE_LLM"), False)


settings = Settings()
