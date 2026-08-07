"""
In-process cron replacement for vm-manager. Runs as its own long-running
systemd service (vm-manager-scheduler.service) rather than inside the main
FastAPI process, so a bug in a scheduled job (or a job blocking) can never
affect request handling -- same isolation reasoning as
sip_reject_watch.py/phone_service.py's dedicated process/threads.

Jobs are plain no-arg callables registered in JOBS below with a fixed
time-of-day (local, config.YEALINK_TIME_ZONE) they should run at. A job
fires once it's at-or-past its scheduled time and hasn't already run today
(app_db.get/set_job_last_run_date) -- "at or past" rather than an exact
minute match means a job that was due while the service was restarting (or
the whole box was down) still catches up the same day it comes back,
instead of silently skipping to tomorrow.

To add a new scheduled task: write a module with a no-arg entry point (see
daily_digest.main for the pattern) and add one line to JOBS.
"""
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from . import app_db, config, daily_digest

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [scheduler] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# Same zone email_template.py formats voicemail timestamps in -- recipients
# and the schedule should agree on what "7am" means.
_LOCAL_TZ = ZoneInfo(config.YEALINK_TIME_ZONE)

# How often the loop wakes up to check for due jobs. Coarser risks a job's
# fire time drifting noticeably late; finer buys nothing since nothing here
# needs sub-minute precision.
_POLL_SECONDS = 30


@dataclass(frozen=True)
class Job:
    name: str
    hour: int
    minute: int
    func: Callable[[], None]


JOBS = [
    Job(name="daily_digest", hour=7, minute=0, func=daily_digest.main),
]


def _due(job: Job, now: datetime) -> bool:
    scheduled_today = now.replace(hour=job.hour, minute=job.minute, second=0, microsecond=0)
    if now < scheduled_today:
        return False
    return app_db.get_job_last_run_date(job.name) != now.date().isoformat()


def _run_job(job: Job, now: datetime) -> None:
    logger.info("Running job %s", job.name)
    try:
        job.func()
    except Exception:
        logger.exception("Job %s raised", job.name)
    # Marked as run for today regardless of success -- a failing job (e.g.
    # SMTP down) retries at tomorrow's scheduled time, not every poll tick
    # for the rest of today.
    app_db.set_job_last_run_date(job.name, now.date().isoformat())


def run_forever() -> None:
    app_db.init_db()
    logger.info("Scheduler started with %d job(s): %s", len(JOBS), ", ".join(j.name for j in JOBS))
    while True:
        now = datetime.now(_LOCAL_TZ)
        for job in JOBS:
            if _due(job, now):
                _run_job(job, now)
        time.sleep(_POLL_SECONDS)


if __name__ == "__main__":
    run_forever()
