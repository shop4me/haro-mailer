from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from app.config import settings
from app.poll_once import main as run_poll


def start_scheduler() -> None:
    scheduler = BlockingScheduler()
    if settings.poll_interval_minutes > 0:
        scheduler.add_job(
            run_poll,
            "interval",
            minutes=settings.poll_interval_minutes,
            id="haro_poll_interval",
        )
        print(f"Polling every {settings.poll_interval_minutes} minutes")
    else:
        times = [t.strip() for t in settings.scheduler_times.split(",") if t.strip()]
        for idx, value in enumerate(times):
            hour, minute = value.split(":")
            scheduler.add_job(run_poll, "cron", hour=int(hour), minute=int(minute), id=f"haro_job_{idx}")
        print("Polling at: %s" % ", ".join(times))
    scheduler.start()


if __name__ == "__main__":
    print("Starting HARO scheduler at %s" % datetime.utcnow().isoformat() + "Z")
    start_scheduler()
