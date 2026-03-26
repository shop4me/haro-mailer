import logging
import threading

from flask import Flask

from app.config import settings
from app.db import init_db
from app.routes import bp

logger = logging.getLogger(__name__)

_scheduler_started = False


def _start_background_polling() -> None:
    """Run poll once at startup, then every poll_interval_minutes (default 10)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def run_poll_safe() -> None:
        try:
            from app.poll_once import main as run_poll
            run_poll()
        except Exception as e:
            logger.exception("Poll failed: %s", e)

    # First run immediately in a daemon thread so we don't block startup
    t = threading.Thread(target=run_poll_safe, daemon=True)
    t.start()

    # Then schedule every N minutes
    interval_min = getattr(settings, "poll_interval_minutes", 10) or 10
    if interval_min <= 0:
        interval_min = 10

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(run_poll_safe, "interval", minutes=interval_min, id="haro_poll")
        scheduler.start()
        logger.info("HARO auto-poll started: every %s minutes", interval_min)
    except Exception as e:
        logger.exception("Could not start scheduler: %s", e)


def _format_datetime(dt) -> str:
    """Format datetime as 'Mar 6 2026 - 11am' (used app-wide)."""
    if dt is None:
        return ""
    hour_12 = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return "%s %s %s - %s%s" % (dt.strftime("%b"), dt.day, dt.year, hour_12, ampm)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = settings.flask_secret_key
    app.jinja_env.filters["format_datetime"] = lambda dt: _format_datetime(dt)

    @app.context_processor
    def _inject_app_branding():
        return {
            "app_name": settings.app_name,
            "app_domain": settings.app_domain,
            "public_base_url": settings.public_base_url,
        }

    init_db()
    app.register_blueprint(bp)
    _start_background_polling()

    @app.errorhandler(500)
    def internal_error(e):
        import traceback
        logger.exception("Internal server error")
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        return (
            "<h1>Internal Server Error</h1><pre style='white-space:pre-wrap;font-size:12px;'>%s</pre>" % tb,
            500,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    return app
