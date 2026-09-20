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

WHICH topics get tracked is decided fresh every cycle by select_topics(),
not a fixed list set once: it's the union of

  1. TREND_TRACK_TOPICS -- an optional manual pin list, always tracked.
  2. Today's top TREND_AUTO_TRACK_COUNT QUALIFIED candidates from
     engine.discover_trends() -- broad seed searches (RADAR_KEYWORDS, which
     already includes "crafts", "diy crafts", "handmade", "craft ideas")
     clustered into recurring hashtags/phrases, corroborated by DataForSEO/
     Google Trends rising queries, same as the dashboard's own trend
     detection. A candidate "qualifies" by clearing TREND_DISCOVERY_MIN_SCORE.
     This crawl runs on its OWN cadence (TREND_DISCOVERY_INTERVAL_HOURS,
     cached in-process between calls) -- deliberately decoupled from the
     per-topic tracking cadence below, since running a full discovery crawl
     that often would roughly double the aggregate-crawl cost. Defaults to
     once/day (matching what this crawl already cost before any of this
     existed); set it equal to TREND_CRAWL_INTERVAL_HOURS for genuinely
     every-scheduled-run discovery if the extra spend is worth it to you.
  3. Anything already accumulating history within TREND_TOPIC_RETENTION_DAYS
     -- so a topic that drops out of today's top picks still gets a few
     more crawls to show its real trajectory (including cooling) instead of
     vanishing mid-story the moment it's no longer today's top pick.

...capped at TREND_AUTO_TRACK_MAX total, so cost stays bounded regardless of
how much the daily top-N churns or how long the retention window is.

Off by default. Needs TREND_TRACKING_ENABLED=true and DATABASE_URL; topics
resolve on their own from there (TREND_TRACK_TOPICS is optional, not required).
"""
import datetime as _dt
import logging
import os
import threading

import engine
import tracker
import trends

_log = logging.getLogger(__name__)

INTERVAL_HOURS        = float(os.environ.get("TREND_CRAWL_INTERVAL_HOURS", "12"))
MANUAL_TOPICS         = [t.strip() for t in os.environ.get("TREND_TRACK_TOPICS", "").split(",") if t.strip()]
AUTO_TRACK_COUNT       = int(os.environ.get("TREND_AUTO_TRACK_COUNT", "3"))
AUTO_TRACK_MAX         = int(os.environ.get("TREND_AUTO_TRACK_MAX", "5"))
TOPIC_RETENTION_DAYS  = float(os.environ.get("TREND_TOPIC_RETENTION_DAYS", "5"))
DISCOVERY_INTERVAL_HOURS = float(os.environ.get("TREND_DISCOVERY_INTERVAL_HOURS", "24"))
DISCOVERY_MIN_SCORE      = float(os.environ.get("TREND_DISCOVERY_MIN_SCORE", "0.5"))
ENABLED = os.environ.get("TREND_TRACKING_ENABLED", "").strip().lower() in ("1", "true", "yes")

_stop = threading.Event()
_discovery_cache = {"trends": [], "at": None}


def _discovery_due():
    if _discovery_cache["at"] is None:
        return True
    now = _dt.datetime.now(_dt.timezone.utc)
    return (now - _discovery_cache["at"]).total_seconds() >= DISCOVERY_INTERVAL_HOURS * 3600


def _refresh_discovery_if_due():
    """Re-run trend discovery (a real Apify/DataForSEO crawl, potentially
    slow) if the cached result is due for a refresh. ONLY ever called from
    the scheduler's own background loop -- never from a request handler.
    Render's own health checks poll /api/health, and select_topics() (below)
    is called from there; if IT triggered this, a health check could block
    for however long a live crawl takes and read as the service being down.
    In-process only: a redeploy resets this cache, so the loop's first pass
    after a restart refreshes immediately regardless of how recently one ran
    before the restart -- same restart-safe trade-off as the rest of this
    feature, worth knowing if redeploys are frequent."""
    if not _discovery_due():
        return
    try:
        _discovery_cache["trends"] = engine.discover_trends()
    except Exception:
        _log.exception("trend scheduler: discovery crawl failed")
        _discovery_cache["trends"] = []
    _discovery_cache["at"] = _dt.datetime.now(_dt.timezone.utc)


def select_topics():
    """This cycle's tracked topics -- see module docstring. Purely reads the
    last discovery result (whatever's cached, possibly empty on first call
    before the loop has run) plus a cheap DB query -- never triggers a crawl
    itself, so it's safe to call from any request handler, not just the loop."""
    auto = [t["term"] for t in _discovery_cache["trends"]
            if t.get("term") and t.get("score", 0) >= DISCOVERY_MIN_SCORE][:AUTO_TRACK_COUNT] \
        if AUTO_TRACK_COUNT > 0 else []
    retained = trends.recently_tracked_topics(TOPIC_RETENTION_DAYS)

    ordered = []
    for t in MANUAL_TOPICS + auto + retained:
        if t not in ordered:
            ordered.append(t)
    return ordered[:AUTO_TRACK_MAX]


def _seconds_until_next_run(topics):
    """Seconds to wait before the next crawl of this whole topic batch --
    not process uptime, so a redeploy doesn't restart the clock or force an
    immediate run. Governed by the OLDEST last-crawl among the given topics,
    since the batch runs together: if even one topic has never been crawled
    (brand new, just auto-selected), that alone means the batch is due now --
    a topic newly added to the tracked set must not sit unwatched just
    because some OTHER topic in the same batch happens to be fresh."""
    oldest = None
    for topic in topics:
        snap = trends.latest(topic)
        if not snap:
            return 0.0   # this topic has never been crawled -- the batch is due now
        t = _dt.datetime.fromisoformat(snap["crawled_at"])
        if oldest is None or t < oldest:
            oldest = t
    if oldest is None:
        return 0.0   # empty topic list -- nothing to wait on, re-check soon
    due = oldest + _dt.timedelta(hours=INTERVAL_HOURS)
    now = _dt.datetime.now(due.tzinfo)
    return max(0.0, (due - now).total_seconds())


def _loop():
    while not _stop.is_set():
        _refresh_discovery_if_due()   # the ONLY place this runs -- see its docstring
        topics = select_topics()
        wait_s = _seconds_until_next_run(topics)
        if wait_s > 0:
            _log.info("trend scheduler: next crawl in %.1fh (topics: %s)", wait_s / 3600.0, topics)
            # re-check hourly (not just once at wait_s) so stop() and topic changes are noticed
            if _stop.wait(timeout=min(wait_s, 3600)):
                break
            continue
        _log.info("trend scheduler: running crawl for %s", topics)
        try:
            tracker.run_all(topics)
        except Exception:
            _log.exception("trend scheduler: crawl cycle failed")
        _stop.wait(timeout=5)   # avoid a tight loop if something keeps returning wait_s == 0


def start():
    """Start the background loop if configured. Safe to call more than once
    -- only the first call actually starts a thread. Topics are resolved
    inside the loop, not here -- an empty TREND_TRACK_TOPICS is fine as long
    as auto-tracking or retention will eventually find something to do."""
    if not ENABLED:
        _log.info("trend scheduler: TREND_TRACKING_ENABLED not set, staying off")
        return
    if not trends.enabled():
        _log.warning("trend scheduler: enabled but DATABASE_URL is not configured, staying off")
        return
    if getattr(start, "_started", False):
        return
    start._started = True
    th = threading.Thread(target=_loop, name="trend-scheduler", daemon=True)
    th.start()
    _log.info("trend scheduler: started (auto-track top %d @ score>=%.2f, max %d concurrent, "
              "%gd retention, crawl every %gh, discovery every %gh)",
              AUTO_TRACK_COUNT, DISCOVERY_MIN_SCORE, AUTO_TRACK_MAX,
              TOPIC_RETENTION_DAYS, INTERVAL_HOURS, DISCOVERY_INTERVAL_HOURS)


def stop():
    """Signal the loop to exit (used by tests; a live process just exits)."""
    _stop.set()
