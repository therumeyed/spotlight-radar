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
  2. Layer 1: today's top TREND_AUTO_TRACK_COUNT QUALIFIED candidates from
     sources.rising_query_candidates() -- Google's own Rising Queries (via
     DataForSEO), pulled once per RADAR_KEYWORDS seed theme, past_day
     window. Near-free (one DataForSEO task per seed theme) and it's
     Google's own trend detection doing the wide-net work, not a hand-
     maintained seed list. A candidate qualifies by being flagged Breakout,
     or clearing TREND_RISING_QUERY_MIN_GROWTH_PCT. This is the PRIMARY
     discovery channel -- tried first, fills as many of the
     TREND_AUTO_TRACK_COUNT slots as it can.
  2b. Layer 1b (fallback/supplement): engine.discover_trends() -- the
     broader social-hashtag-clustering discovery (same RADAR_KEYWORDS,
     clustered TikTok/Instagram signals). Fills any TREND_AUTO_TRACK_COUNT
     slots the rising-query channel didn't -- catches something trending
     socially before it shows up in Google search volume. A candidate
     qualifies by clearing TREND_DISCOVERY_MIN_SCORE.
     Both channels refresh together on the SAME cadence
     (TREND_DISCOVERY_INTERVAL_HOURS, cached in-process between calls) --
     deliberately decoupled from the per-topic tracking cadence below,
     since running the social discovery crawl that often would roughly
     double the aggregate-crawl cost (the rising-query channel itself is
     cheap enough that its own cadence barely matters). Defaults to
     once/day; set it equal to TREND_CRAWL_INTERVAL_HOURS for genuinely
     every-scheduled-run discovery if the extra spend is worth it to you.
  3. Anything already accumulating history within TREND_TOPIC_RETENTION_DAYS
     -- so a topic that drops out of today's top picks still gets a few
     more crawls to show its real trajectory (including cooling) instead of
     vanishing mid-story the moment it's no longer today's top pick.

Layer 2, where a candidate actually gets confirmed, is unchanged: whatever
gets selected here goes through the same real TikTok/Instagram crawl,
post-ID dedup, creator-diversity gate and classification as any other
tracked topic -- this module only ever decides WHAT to check, never
substitutes for actually checking it.

...capped at TREND_AUTO_TRACK_MAX total, so cost stays bounded regardless of
how much the daily candidates churn or how long the retention window is.

Off by default. Needs TREND_TRACKING_ENABLED=true and DATABASE_URL; topics
resolve on their own from there (TREND_TRACK_TOPICS is optional, not required).
"""
import datetime as _dt
import logging
import os
import threading

import engine
import sources
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
RISING_QUERY_MIN_GROWTH_PCT = float(os.environ.get("TREND_RISING_QUERY_MIN_GROWTH_PCT", "20"))
ENABLED = os.environ.get("TREND_TRACKING_ENABLED", "").strip().lower() in ("1", "true", "yes")

# Topics that must never be auto-selected (discovered OR retained), by exact
# (case-insensitive) name -- for a candidate that turned out to be noise
# after the fact. "craft online" surfaced ambiguous, off-topic corroboration
# (crossword-clue pages) despite clearing the score threshold; excluded here
# rather than relying on BLOCKED_SEARCH_INTENT, since the term itself
# doesn't contain a blocked word -- only its related queries did. A manual
# TREND_TRACK_TOPICS pin still overrides this (an explicit operator choice).
EXCLUDED_TOPICS = {t.strip().lower() for t in
                   os.environ.get("TREND_EXCLUDED_TOPICS", "craft online").split(",") if t.strip()}

_stop = threading.Event()
_discovery_cache = {"rising": [], "trends": [], "at": None}


def _discovery_due():
    if _discovery_cache["at"] is None:
        return True
    now = _dt.datetime.now(_dt.timezone.utc)
    return (now - _discovery_cache["at"]).total_seconds() >= DISCOVERY_INTERVAL_HOURS * 3600


def _refresh_discovery_if_due():
    """Re-run both discovery channels (real Apify/DataForSEO spend,
    potentially slow) if the cached result is due for a refresh. ONLY ever
    called from the scheduler's own background loop -- never from a request
    handler. Render's own health checks poll /api/health, and select_topics()
    (below) is called from there; if IT triggered this, a health check could
    block for however long a live crawl takes and read as the service being
    down. In-process only: a redeploy resets this cache, so the loop's first
    pass after a restart refreshes immediately regardless of how recently one
    ran before the restart -- same restart-safe trade-off as the rest of this
    feature, worth knowing if redeploys are frequent."""
    if not _discovery_due():
        return
    try:
        _discovery_cache["rising"] = sources.rising_query_candidates(sources.KEYWORDS)
    except Exception:
        _log.exception("trend scheduler: rising-query discovery failed")
        _discovery_cache["rising"] = []
    try:
        _discovery_cache["trends"] = engine.discover_trends()
    except Exception:
        _log.exception("trend scheduler: social discovery crawl failed")
        _discovery_cache["trends"] = []
    _discovery_cache["at"] = _dt.datetime.now(_dt.timezone.utc)


def select_topics():
    """This cycle's tracked topics -- see module docstring. Purely reads the
    last discovery result (whatever's cached, possibly empty on first call
    before the loop has run) plus a cheap DB query -- never triggers a crawl
    itself, so it's safe to call from any request handler, not just the loop.

    EXCLUDED_TOPICS and BLOCKED_SEARCH_INTENT are applied to auto-discovered
    and retained candidates, never to MANUAL_TOPICS -- an explicit pin is an
    operator's deliberate choice and overrides both. The rising-query channel
    (Layer 1, primary) fills slots first; engine.discover_trends() (Layer 1b)
    fills whatever's left -- see module docstring for why both exist."""
    auto = []
    if AUTO_TRACK_COUNT > 0:
        rising_qualified = [c["term"] for c in _discovery_cache["rising"]
                            if c["term"] not in EXCLUDED_TOPICS
                            and (c["breakout"] or (c["growth_pct"] or 0) >= RISING_QUERY_MIN_GROWTH_PCT)]
        social_qualified = [t["term"] for t in _discovery_cache["trends"] if t.get("term")
                            and t.get("score", 0) >= DISCOVERY_MIN_SCORE
                            and not sources.is_blocked_search_intent(t["term"])
                            and t["term"].strip().lower() not in EXCLUDED_TOPICS]
        merged = []
        for t in rising_qualified + social_qualified:
            if t not in merged:
                merged.append(t)
        auto = merged[:AUTO_TRACK_COUNT]
    retained = [t for t in trends.recently_tracked_topics(TOPIC_RETENTION_DAYS)
                if t.strip().lower() not in EXCLUDED_TOPICS]

    ordered = []
    for t in MANUAL_TOPICS + auto + retained:
        if t not in ordered:
            ordered.append(t)
    return ordered[:AUTO_TRACK_MAX]


# Apify per-1000-result pricing, from the actors' own published listings
# (checked against apify.com directly -- sociavault/tiktok-keyword-search-
# scraper and apify/instagram-hashtag-scraper). Instagram's is a range
# ($2.30-2.60 depending on plan); using the midpoint for the estimate.
_TIKTOK_PRICE_PER_1K = 1.50
_INSTAGRAM_PRICE_PER_1K = 2.45
# Discovery crawl size -- must match sources.py's tiktok()/instagram()
# (KEYWORDS[:6] @ 30 results, KEYWORDS[:5] @ 60 results). Not read from
# there directly to avoid coupling the estimate to internals; keep in sync
# if those numbers change.
_DISCOVERY_TIKTOK_RESULTS = 6 * 30
_DISCOVERY_INSTAGRAM_RESULTS = 5 * 60
# DataForSEO: one task per call regardless of result count within a task.
_DATAFORSEO_PRICE_PER_CALL = 0.0012


def estimate_daily_cost_usd():
    """Rough daily Apify cost from current config -- NOT real billing data
    (no Apify billing API access here), just caps x cadence x published
    pricing. Assumes every crawl hits its cap, so this is a conservative/
    high estimate, consistent with how hit_result_cap is treated everywhere
    else in this feature."""
    topics = select_topics()
    crawls_per_day = 24.0 / INTERVAL_HOURS if INTERVAL_HOURS > 0 else 0.0
    per_crawl_cost = 0.0
    for topic in topics:
        tiktok_max = tracker.TIKTOK_CAP_BUMPED_MAX \
            if trends.recent_hit_cap_streak(topic) >= tracker.TIKTOK_CAP_BUMP_THRESHOLD \
            else sources.TREND_TIKTOK_MAX_RESULTS
        per_crawl_cost += (tiktok_max / 1000.0) * _TIKTOK_PRICE_PER_1K
        per_crawl_cost += (sources.TREND_INSTAGRAM_MAX_RESULTS / 1000.0) * _INSTAGRAM_PRICE_PER_1K
    tracking_cost = per_crawl_cost * crawls_per_day

    discovery_crawls_per_day = 24.0 / DISCOVERY_INTERVAL_HOURS if DISCOVERY_INTERVAL_HOURS > 0 else 0.0
    discovery_cost_per_crawl = ((_DISCOVERY_TIKTOK_RESULTS / 1000.0) * _TIKTOK_PRICE_PER_1K
                                + (_DISCOVERY_INSTAGRAM_RESULTS / 1000.0) * _INSTAGRAM_PRICE_PER_1K)
    discovery_cost = discovery_cost_per_crawl * discovery_crawls_per_day

    rising_query_cost = len(sources.KEYWORDS) * _DATAFORSEO_PRICE_PER_CALL * discovery_crawls_per_day

    return round(tracking_cost + discovery_cost + rising_query_cost, 2)


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
    _log.info("trend scheduler: started (auto-track top %d, max %d concurrent -- "
              "Layer 1 rising-query breakout/growth>=%.0f%%, Layer 1b social score>=%.2f, "
              "%gd retention, crawl every %gh, discovery every %gh)",
              AUTO_TRACK_COUNT, AUTO_TRACK_MAX, RISING_QUERY_MIN_GROWTH_PCT, DISCOVERY_MIN_SCORE,
              TOPIC_RETENTION_DAYS, INTERVAL_HOURS, DISCOVERY_INTERVAL_HOURS)


def stop():
    """Signal the loop to exit (used by tests; a live process just exits)."""
    _stop.set()
