"""
trends.py — longitudinal trend tracking: a durable post-ID ledger plus an
append-only history of computed growth snapshots, so "emerging" is measured
from real change over time instead of one crawl's result count.

Counts only, by design: this tracks HOW MANY posts are appearing per topic
and how fast that's changing -- not the posts themselves. The ledger keeps
the bare minimum needed to compute that correctly and defend it against one
account gaming the count (platform, post ID, topic, publish time, first-seen
time, creator ID) -- nothing else, no url/caption/engagement. That
minimalism is deliberate: a raw "how many results did this search return"
number is nearly useless once a topic is popular enough to fill its own
result cap (it just reads as a flat "50, 50, 50" forever) -- the ledger's
job is to let each crawl tell a genuinely NEW post (by ID) from one already
counted, so "12 new in the last 24h, up from 4 yesterday" is a real number.

TikTok is the primary velocity source; Instagram's counts are supporting
evidence only, never the classification driver. Its scraper has no
newest-first sort (checked its documented input schema -- there isn't one),
so a capped Instagram crawl can't be trusted to represent "what's new" the
way a date-sorted TikTok crawl can. Swap this if a more suitable Instagram
actor is introduced.

creator_id is kept in the ledger but never surfaced in an API response or
on a trend card -- it exists purely so classification can require posts
from a minimum number of DISTINCT creators, not just a raw post count, so
one prolific account can't manufacture a false "emerging" signal on its own.

Two tables, two jobs (kept separate from store.py's own radar_history table
-- that one archives daily report builds, this one tracks trend velocity):

  trend_posts      One row per (platform, post_id, topic) ever seen. Insert-
                    once (ON CONFLICT DO NOTHING). This is the ledger every
                    windowed comparison (24h/3d/7d/...) and the creator-
                    diversity check are computed from, live, at query time.
  trend_snapshots   One row PER CRAWL RUN per topic, holding that run's
                    computed counts and classification. Append-only.

Fail-soft like the rest of this project, but with no non-durable fallback:
DATABASE_URL is required for ALL of this. A velocity claim that doesn't
survive a restart is worse than no claim -- there's no same-day local-file
equivalent here the way store.py has for the daily report cache.
"""
import datetime as _dt
import json
import logging
import os

import store

psycopg = store.psycopg
_log = logging.getLogger(__name__)

PRIMARY_PLATFORM = "tiktok"
SUPPORTING_PLATFORM = "instagram"

# ---- configurable classification thresholds ----
MIN_SNAPSHOTS_FOR_CLASSIFICATION = int(os.environ.get("TREND_MIN_SNAPSHOTS", "2"))
EMERGING_MIN_NEW_POSTS  = int(os.environ.get("TREND_EMERGING_MIN_POSTS", "5"))
EMERGING_MIN_CREATORS   = int(os.environ.get("TREND_EMERGING_MIN_CREATORS", "3"))
EMERGING_MIN_GROWTH_PCT = float(os.environ.get("TREND_EMERGING_MIN_GROWTH_PCT", "50"))
COOLING_MAX_GROWTH_PCT  = float(os.environ.get("TREND_COOLING_MAX_GROWTH_PCT", "-20"))


def enabled():
    return store.enabled()


def _ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trend_posts (
            platform TEXT NOT NULL,
            post_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            published_at TIMESTAMPTZ,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            creator_id TEXT,
            PRIMARY KEY (platform, post_id, topic)
        )
    """)
    # Idempotent narrowing for a table created by an earlier version of this
    # feature (url/creator/engagement/last_seen_at) -- a no-op once applied.
    for col in ("url", "creator", "last_seen_at", "views", "likes", "comments", "shares"):
        conn.execute("ALTER TABLE trend_posts DROP COLUMN IF EXISTS %s" % col)
    conn.execute("ALTER TABLE trend_posts ADD COLUMN IF NOT EXISTS creator_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trend_posts_topic_pub "
                 "ON trend_posts (topic, published_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trend_posts_topic_platform_creator "
                 "ON trend_posts (topic, platform, creator_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trend_snapshots (
            id BIGSERIAL PRIMARY KEY,
            topic TEXT NOT NULL,
            crawled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            new_posts_this_crawl INT NOT NULL DEFAULT 0,
            new_posts_24h INT NOT NULL, prev_posts_24h INT NOT NULL, growth_pct_24h DOUBLE PRECISION,
            new_posts_3d INT NOT NULL, prev_posts_3d INT NOT NULL, growth_pct_3d DOUBLE PRECISION,
            new_posts_7d INT NOT NULL, prev_posts_7d INT NOT NULL, growth_pct_7d DOUBLE PRECISION,
            instagram_new_24h INT NOT NULL DEFAULT 0,
            velocity DOUBLE PRECISION,
            acceleration DOUBLE PRECISION,
            classification TEXT NOT NULL,
            earliest_published_at TIMESTAMPTZ,
            latest_published_at TIMESTAMPTZ,
            hit_result_cap BOOLEAN NOT NULL DEFAULT false,
            google_trends JSONB
        )
    """)
    for col in ("unique_creators_24h", "engagement_24h", "prev_engagement_24h", "sample_posts"):
        conn.execute("ALTER TABLE trend_snapshots DROP COLUMN IF EXISTS %s" % col)
    conn.execute("ALTER TABLE trend_snapshots ADD COLUMN IF NOT EXISTS "
                 "new_posts_this_crawl INT NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE trend_snapshots ADD COLUMN IF NOT EXISTS "
                 "instagram_new_24h INT NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trend_snapshots_topic_time "
                 "ON trend_snapshots (topic, crawled_at DESC)")


def _upsert_posts(conn, topic, posts):
    """Insert any post ID not already on record for this topic. Nothing about
    an existing row ever needs updating -- platform/post_id/topic/published_at/
    creator_id are all immutable facts about the post -- so this is
    insert-or-ignore. Returns {"tiktok": n_inserted, "instagram": n_inserted}."""
    now = _dt.datetime.now(_dt.timezone.utc)
    inserted = {"tiktok": 0, "instagram": 0}
    for p in posts:
        pid = (p.get("post_id") or "").strip()
        platform = p.get("platform")
        if not pid or platform not in inserted:
            continue
        cur = conn.execute("""
            INSERT INTO trend_posts (platform, post_id, topic, published_at, first_seen_at, creator_id)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (platform, post_id, topic) DO NOTHING
        """, (platform, pid, topic, p.get("published_at"), now, p.get("creator_id")))
        if cur.rowcount:
            inserted[platform] += 1
    return inserted


def _window(conn, topic, hours, platform):
    now = _dt.datetime.now(_dt.timezone.utc)
    t0 = now - _dt.timedelta(hours=hours)
    tprev = now - _dt.timedelta(hours=2 * hours)
    row = conn.execute("""
        SELECT
          count(*) FILTER (WHERE published_at >= %(t0)s AND published_at < %(now)s),
          count(*) FILTER (WHERE published_at >= %(tprev)s AND published_at < %(t0)s),
          MIN(published_at), MAX(published_at)
        FROM trend_posts WHERE topic = %(topic)s AND platform = %(platform)s
              AND published_at IS NOT NULL
    """, {"t0": t0, "now": now, "tprev": tprev, "topic": topic, "platform": platform}).fetchone()
    latest, prev, earliest, latest_pub = row
    growth = None if prev == 0 else round((latest - prev) / prev * 100.0, 1)
    return {"new": latest, "prev": prev, "growth_pct": growth,
            "earliest": earliest, "latest_pub": latest_pub}


def _creator_diversity(conn, topic, hours, platform):
    """Distinct creators behind the primary platform's posts in the last
    `hours` -- used ONLY to gate classification (never returned to an API
    caller or shown on a card). One account posting 10 times isn't 10
    creators' worth of momentum."""
    now = _dt.datetime.now(_dt.timezone.utc)
    t0 = now - _dt.timedelta(hours=hours)
    row = conn.execute("""
        SELECT count(DISTINCT creator_id) FROM trend_posts
        WHERE topic=%s AND platform=%s AND published_at >= %s AND published_at < %s
              AND creator_id IS NOT NULL AND creator_id != ''
    """, (topic, platform, t0, now)).fetchone()
    return row[0] or 0


def _classify(conn, topic, w24, creators24):
    prior_count = conn.execute(
        "SELECT count(*) FROM trend_snapshots WHERE topic=%s", (topic,)).fetchone()[0]
    if prior_count < MIN_SNAPSHOTS_FOR_CLASSIFICATION or w24["earliest"] is None:
        return "collecting_baseline", float(w24["new"]), None

    velocity = float(w24["new"])
    prev_row = conn.execute(
        "SELECT velocity FROM trend_snapshots WHERE topic=%s ORDER BY crawled_at DESC LIMIT 1",
        (topic,)).fetchone()
    prev_velocity = prev_row[0] if prev_row and prev_row[0] is not None else None
    acceleration = (velocity - prev_velocity) if prev_velocity is not None else None

    if w24["prev"] == 0 and w24["new"] > 0:
        return "new", velocity, acceleration

    growth = w24["growth_pct"] if w24["growth_pct"] is not None else 0.0
    # Both a post-count floor AND a creator-diversity floor: a growth spike
    # driven by one prolific account posting repeatedly must not read as an
    # organic emerging trend just because the raw count cleared a threshold.
    if (w24["new"] >= EMERGING_MIN_NEW_POSTS and creators24 >= EMERGING_MIN_CREATORS
            and growth >= EMERGING_MIN_GROWTH_PCT):
        return ("accelerating" if (acceleration or 0) > 0 else "emerging"), velocity, acceleration
    if growth <= COOLING_MAX_GROWTH_PCT:
        return "cooling", velocity, acceleration
    return "sustained", velocity, acceleration


def record_snapshot(topic, posts, hit_result_cap=False, google_trends=None):
    """Upsert this crawl's post IDs into the ledger, compute this run's
    windowed counts (TikTok-primary; Instagram as a supporting figure only),
    classify the topic, and append one immutable snapshot row. Returns the
    snapshot as a plain dict. Requires DATABASE_URL -- raises if not
    configured (callers are expected to check enabled() first; this isn't
    called from any request path, only from the scheduler/manual trigger)."""
    if not enabled():
        raise RuntimeError("trends.record_snapshot called without DATABASE_URL configured")

    with store._connect() as conn:
        _ensure_tables(conn)
        inserted = _upsert_posts(conn, topic, posts)

        w24 = _window(conn, topic, 24, PRIMARY_PLATFORM)
        w72 = _window(conn, topic, 72, PRIMARY_PLATFORM)
        w168 = _window(conn, topic, 168, PRIMARY_PLATFORM)
        ig24 = _window(conn, topic, 24, SUPPORTING_PLATFORM)
        creators24 = _creator_diversity(conn, topic, 24, PRIMARY_PLATFORM)
        classification, velocity, acceleration = _classify(conn, topic, w24, creators24)

        row = conn.execute("""
            INSERT INTO trend_snapshots (
                topic, new_posts_this_crawl,
                new_posts_24h, prev_posts_24h, growth_pct_24h,
                new_posts_3d, prev_posts_3d, growth_pct_3d,
                new_posts_7d, prev_posts_7d, growth_pct_7d,
                instagram_new_24h,
                velocity, acceleration, classification,
                earliest_published_at, latest_published_at, hit_result_cap,
                google_trends)
            VALUES (%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s, %s,%s,%s, %s,%s,%s, %s)
            RETURNING id, crawled_at
        """, (topic, inserted[PRIMARY_PLATFORM],
              w24["new"], w24["prev"], w24["growth_pct"],
              w72["new"], w72["prev"], w72["growth_pct"],
              w168["new"], w168["prev"], w168["growth_pct"],
              ig24["new"],
              velocity, acceleration, classification,
              w24["earliest"], w24["latest_pub"], hit_result_cap,
              json.dumps(google_trends) if google_trends is not None else None)).fetchone()

    return {
        "topic": topic, "id": row[0], "crawled_at": row[1].isoformat(),
        "new_posts_this_crawl": inserted[PRIMARY_PLATFORM],
        "new_posts_24h": w24["new"], "prev_posts_24h": w24["prev"], "growth_pct_24h": w24["growth_pct"],
        "new_posts_3d": w72["new"], "prev_posts_3d": w72["prev"], "growth_pct_3d": w72["growth_pct"],
        "new_posts_7d": w168["new"], "prev_posts_7d": w168["prev"], "growth_pct_7d": w168["growth_pct"],
        "instagram_new_24h": ig24["new"],
        "velocity": velocity, "acceleration": acceleration, "classification": classification,
        "hit_result_cap": hit_result_cap, "google_trends": google_trends,
    }


def latest(topic):
    """The most recent snapshot for a topic, or None if never crawled."""
    if not enabled():
        return None
    with store._connect() as conn:
        _ensure_tables(conn)
        row = conn.execute("""
            SELECT topic, crawled_at, new_posts_this_crawl,
                   new_posts_24h, prev_posts_24h, growth_pct_24h,
                   new_posts_3d, prev_posts_3d, growth_pct_3d,
                   new_posts_7d, prev_posts_7d, growth_pct_7d,
                   instagram_new_24h,
                   velocity, acceleration, classification, hit_result_cap,
                   google_trends
            FROM trend_snapshots WHERE topic=%s ORDER BY crawled_at DESC LIMIT 1
        """, (topic,)).fetchone()
    if not row:
        return None
    cols = ["topic", "crawled_at", "new_posts_this_crawl",
            "new_posts_24h", "prev_posts_24h", "growth_pct_24h",
            "new_posts_3d", "prev_posts_3d", "growth_pct_3d",
            "new_posts_7d", "prev_posts_7d", "growth_pct_7d",
            "instagram_new_24h",
            "velocity", "acceleration", "classification", "hit_result_cap",
            "google_trends"]
    d = dict(zip(cols, row))
    d["crawled_at"] = d["crawled_at"].isoformat()
    return d


def history(topic, days=30):
    """Snapshot history for a topic's trend line, newest first, for the last
    `days` days (24h/7d/30d views are just different `days` values)."""
    if not enabled():
        return []
    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
    with store._connect() as conn:
        _ensure_tables(conn)
        rows = conn.execute("""
            SELECT crawled_at, new_posts_24h, growth_pct_24h, velocity, acceleration, classification
            FROM trend_snapshots WHERE topic=%s AND crawled_at >= %s ORDER BY crawled_at ASC
        """, (topic, since)).fetchall()
    return [{"crawled_at": r[0].isoformat(), "new_posts_24h": r[1], "growth_pct_24h": r[2],
             "velocity": r[3], "acceleration": r[4], "classification": r[5]} for r in rows]


def tracked_topics():
    """Every topic that has at least one snapshot ever, for a summary listing."""
    if not enabled():
        return []
    with store._connect() as conn:
        _ensure_tables(conn)
        rows = conn.execute("SELECT DISTINCT topic FROM trend_snapshots ORDER BY topic").fetchall()
    return [r[0] for r in rows]


def recently_tracked_topics(days):
    """Topics crawled within the last `days` days -- the scheduler's retention
    window, so a topic that drops out of today's auto-selected top picks still
    gets a few more crawls to show its actual trajectory (including cooling)
    instead of vanishing mid-story the moment it's no longer today's top pick."""
    if not enabled() or days <= 0:
        return []
    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
    with store._connect() as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT DISTINCT topic FROM trend_snapshots WHERE crawled_at >= %s", (since,)).fetchall()
    return [r[0] for r in rows]


def recent_hit_cap_streak(topic):
    """How many of the topic's most recent consecutive crawls hit their
    result cap, counting back from the newest and stopping at the first one
    that didn't. Used to auto-bump a topic's crawl size when it keeps maxing
    out -- see tracker.py."""
    if not enabled():
        return 0
    with store._connect() as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT hit_result_cap FROM trend_snapshots WHERE topic=%s ORDER BY crawled_at DESC LIMIT 10",
            (topic,)).fetchall()
    streak = 0
    for (hit,) in rows:
        if hit:
            streak += 1
        else:
            break
    return streak


def purge_topic(topic):
    """Permanently delete all ledger + snapshot rows for a topic. Ops use
    only -- e.g. removing a discovered candidate that turned out to be noise
    (ambiguous phrase, irrelevant corroboration). Not reversible; the topic
    can be re-discovered later unless also added to a scheduler exclusion list."""
    if not enabled():
        return {"posts": 0, "snapshots": 0}
    with store._connect() as conn:
        _ensure_tables(conn)
        p = conn.execute("DELETE FROM trend_posts WHERE topic=%s", (topic,))
        s = conn.execute("DELETE FROM trend_snapshots WHERE topic=%s", (topic,))
        return {"posts": p.rowcount, "snapshots": s.rowcount}
