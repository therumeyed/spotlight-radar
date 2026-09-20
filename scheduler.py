"""
scheduler.py — an in-process background loop that runs the longitudinal
trend-tracking crawl (tracker.py) on a fixed cadence, so loading the
dashboard is never what triggers a paid crawl.

Deliberately NOT a separate Render Cron Job / worker service: this app runs
as a single process (Render's own boot log shows "Setting WEB_CONCURRENCY=1"),
so one daemon thread inside that process is enough, with no new
infrastructure to provision. Restart-safe: each wake checks the most recent
snapshot actually on record in Postgres rather than timing from process
start, so a redeploy doesn't reset the clock or force an immediate re-crawl.

Off by default. Three env vars gate it on:
    TREND_TRACKING_ENABLED=true
    TREND_TRACK_TOPICS=crafts               (comma-separated; start with ONE for the pilot)
    TREND_CRAWL_INTERVAL_HOURS=12           (default 12)
...plus DATABASE_URL, since trends.py has no non-durable fallback.
"""
import datetime as _dt
import logging
import os
import threading

import tracker
import trends

_log = logging.getLogger(__name__)

INTERVAL_HOURS = float(os.environ.get("TREND_CRAWL_INTERVAL_HOURS", "12"))
TRACKED_TOPICS = [t.strip() for t in os.environ.get("TREND_TRACK_TOPICS", "").split(",") if t.strip()]
ENABLED = os.environ.get("TREND_TRACKING_ENABLED", "").strip().lower() in ("1", "true", "yes")

_stop = threading.Event()


def _seconds_until_next_run():
    """Seconds to wait before the next crawl, based on the most recent
    snapshot actually on record across all tracked topics -- not process
    uptime, so a redeploy doesn't restart the clock or force an immediate run."""
    last = None
    for topic in TRACKED_TOPICS:
        snap = trends.latest(topic)
        if snap:
            t = _dt.datetime.fromisoformat(snap["crawled_at"])
            if last is None or t > last:
                last = t
    if last is None:
        return 0.0   # never crawled for any tracked topic -- run now to establish a baseline
    due = last + _dt.timedelta(hours=INTERVAL_HOURS)
    now = _dt.datetime.now(due.tzinfo)
    return max(0.0, (due - now).total_seconds())


def _loop():
    while not _stop.is_set():
        wait_s = _seconds_until_next_run()
        if wait_s > 0:
            _log.info("trend scheduler: next crawl in %.1fh", wait_s / 3600.0)
            # re-check hourly (not just once at wait_s) so stop() is noticed promptly
            if _stop.wait(timeout=min(wait_s, 3600)):
                break
            continue
        _log.info("trend scheduler: running crawl for %s", TRACKED_TOPICS)
        try:
            tracker.run_all(TRACKED_TOPICS)
        except Exception:
            _log.exception("trend scheduler: crawl cycle failed")
        _stop.wait(timeout=5)   # avoid a tight loop if something keeps returning wait_s == 0


def start():
    """Start the background loop if fully configured. Safe to call more than
    once -- only the first call actually starts a thread."""
    if not ENABLED:
        _log.info("trend scheduler: TREND_TRACKING_ENABLED not set, staying off")
        return
    if not TRACKED_TOPICS:
        _log.warning("trend scheduler: enabled but TREND_TRACK_TOPICS is empty, staying off")
        return
    if not trends.enabled():
        _log.warning("trend scheduler: enabled but DATABASE_URL is not configured, staying off")
        return
    if getattr(start, "_started", False):
        return
    start._started = True
    th = threading.Thread(target=_loop, name="trend-scheduler", daemon=True)
    th.start()
    _log.info("trend scheduler: started, tracking %s every %gh", TRACKED_TOPICS, INTERVAL_HOURS)


def stop():
    """Signal the loop to exit (used by tests; a live process just exits)."""
    _stop.set()
