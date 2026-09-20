"""
tracker.py — one longitudinal-tracking crawl-and-snapshot cycle for one topic:
pull fresh posts from TikTok/Instagram, merge them into the durable ledger,
compute this run's growth numbers, classify the topic's lifecycle stage, and
record it as one immutable snapshot (trends.py).

Called by the scheduler (scheduler.py) or the manual ops trigger endpoint --
never from an ordinary page request, so loading the dashboard never sets off
a paid crawl. Same fail-soft posture as the rest of the project: any one
platform failing doesn't block the other, and a topic is simply skipped
(logged, not crashed) if DATABASE_URL isn't configured.
"""
import logging

import sources
import trends

_log = logging.getLogger(__name__)


def run(topic):
    """One full crawl+snapshot cycle for `topic`. Returns the snapshot dict,
    or None if trend tracking isn't configured (no DATABASE_URL) or the topic
    produced no live data (e.g. no APIFY_API_KEY -- sample data is never fed
    into the velocity ledger, a fabricated post would poison it forever)."""
    if not trends.enabled():
        _log.warning("trend tracker: DATABASE_URL not configured, skipping %r", topic)
        return None
    if not sources.live():
        _log.warning("trend tracker: APIFY_API_KEY not configured, skipping %r "
                      "(sample data is never written into the trend ledger)", topic)
        return None

    tiktok_posts, tiktok_capped = sources.crawl_tiktok_posts(topic)
    ig_posts, ig_capped = sources.crawl_instagram_posts(topic.replace(" ", ""))
    all_posts = tiktok_posts + ig_posts
    hit_cap = tiktok_capped or ig_capped

    try:
        gt = sources.trend_corroboration(topic)
    except Exception:
        gt = None

    snapshot = trends.record_snapshot(topic, all_posts, hit_result_cap=hit_cap, google_trends=gt)
    _log.info("trend tracker: %s -> %s (new_24h=%s prev_24h=%s creators=%s growth=%s%% "
              "tiktok=%d instagram=%d hit_cap=%s)",
              topic, snapshot["classification"], snapshot["new_posts_24h"],
              snapshot["prev_posts_24h"], snapshot["unique_creators_24h"],
              snapshot["growth_pct_24h"], len(tiktok_posts), len(ig_posts), hit_cap)
    return snapshot


def run_all(topics):
    """Run each topic in sequence (not parallel -- keeps Apify concurrency and
    spend predictable). One topic's failure doesn't stop the rest."""
    results = {}
    for topic in topics:
        try:
            results[topic] = run(topic)
        except Exception:
            _log.exception("trend tracker: crawl failed for %r", topic)
            results[topic] = None
    return results
