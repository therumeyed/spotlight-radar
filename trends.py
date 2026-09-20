"""
trends.py — longitudinal trend tracking: a durable post-ID ledger plus an
append-only history of computed growth snapshots, so "emerging" is measured
from real change over time instead of one crawl's result count.

Counts only, by design: this tracks HOW MANY posts are appearing per topic
and how fast that's changing -- not the posts themselves. The ledger keeps
the bare minimum needed to compute that correctly (platform, post ID, topic,
publish time, first-seen time) and nothing else -- no url, creator, or
engagement. That minimalism is deliberate, not an oversight: a raw "how many
results did this search return" number is nearly useless once a topic is
popular enough to fill its own result cap (it just reads as a flat "50,
50, 50" forever) -- the ledger's only job is to let each new crawl tell a
genuinely NEW post (by ID) from one already counted before, so "12 new in the
last 24h, up from 4 yesterday" is a real number, not noise from re-hitting
the same pool of existing content.

Two tables, two jobs (kept separate from store.py's own radar_history table
-- that one archives daily report builds, this one tracks trend velocity):

  trend_posts      One row per (platform, post_id, topic) ever seen. Insert-
                    once (ON CONFLICT DO NOTHING -- nothing about a post
                    besides "have we seen this ID for this topic" ever needs
                    updating here). This is the ledger every windowed
                    comparison (24h/3d/7d/...) is computed from, live, at
                    query time -- so a new window size never needs a schema
                    change or a backfill.
  trend_snapshots   One row PER CRAWL RUN per topic, holding that run's
                    computed counts and classification. Append-only -- a
                    run's row is never rewritten -- which is both "don't
                    overwrite previous snapshots" and the data source for a
                    trend-line/sparkline UI.

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

# ---- configurable classification thresholds ----
MIN_SNAPSHOTS_FOR_CLASSIFICATION = int(os.environ.get("TREND_MIN_SNAPSHOTS", "2"))
EMERGING_MIN_NEW_POSTS  = int(os.environ.get("TREND_EMERGING_MIN_POSTS", "5"))
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
            PRIMARY KEY (platform, post_id, topic)
        )
    """)
    # Idempotent narrowing for a table created by an earlier version of this
    # feature (url/creator/engagement/last_seen_at) -- a no-op once applied.
    for col in ("url", "creator", "last_seen_at", "views", "likes", "comments", "shares"):
        conn.execute("ALTER TABLE trend_posts DROP COLUMN IF EXISTS %s" % col)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trend_posts_topic_pub "
                 "ON trend_posts (topic, published_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trend_snapshots (
            id BIGSERIAL PRIMARY KEY,
            topic TEXT NOT NULL,
            crawled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            new_posts_this_crawl INT NOT NULL DEFAULT 0,
            new_posts_24h INT NOT NULL, prev_posts_24h INT NOT NULL, growth_pct_24h DOUBLE PRECISION,
            new_posts_3d INT NOT NULL, prev_posts_3d INT NOT NULL, growth_pct_3d DOUBLE PRECISION,
            new_posts_7d INT NOT NULL, prev_posts_7d INT NOT NULL, growth_pct_7d DOUBLE PRECISION,
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trend_snapshots_topic_time "
                 "ON trend_snapshots (topic, crawled_at DESC)")


def _upsert_posts(conn, topic, posts):
    """Insert any post ID not already on record for this topic. Nothing about
    an existing row ever needs updating -- platform/post_id/topic/published_at
    are all immutable facts about the post -- so this is insert-or-ignore,
    and the count of rows actually inserted IS "new posts this crawl.\""""
    now = _dt.datetime.now(_dt.timezone.utc)
    inserted = 0
    for p in posts:
        pid = (p.get("post_id") or "").strip()
        if not pid:
            continue
        cur = conn.execute("""
            INSERT INTO trend_posts (platform, post_id, topic, published_at, first_seen_at)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (platform, post_id, topic) DO NOTHING
        """, (p["platform"], pid, topic, p.get("published_at"), now))
        if cur.rowcount:
            inserted += 1
    return inserted


def _window(conn, topic, hours):
    now = _dt.datetime.now(_dt.timezone.utc)
    t0 = now - _dt.timedelta(hours=hours)
    tprev = now - _dt.timedelta(hours=2 * hours)
    row = conn.execute("""
        SELECT
          count(*) FILTER (WHERE published_at >= %(t0)s AND published_at < %(now)s),
          count(*) FILTER (WHERE published_at >= %(tprev)s AND published_at < %(t0)s),
          MIN(published_at), MAX(published_at)
        FROM trend_posts WHERE topic = %(topic)s AND published_at IS NOT NULL
    """, {"t0": t0, "now": now, "tprev": tprev, "topic": topic}).fetchone()
    latest, prev, earliest, latest_pub = row
    growth = None if prev == 0 else round((latest - prev) / prev * 100.0, 1)
    return {"new": latest, "prev": prev, "growth_pct": growth,
            "earliest": earliest, "latest_pub": latest_pub}


def _classify(conn, topic, w24):
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
    if w24["new"] >= EMERGING_MIN_NEW_POSTS and growth >= EMERGING_MIN_GROWTH_PCT:
        return ("accelerating" if (acceleration or 0) > 0 else "emerging"), velocity, acceleration
    if growth <= COOLING_MAX_GROWTH_PCT:
        return "cooling", velocity, acceleration
    return "sustained", velocity, acceleration


def record_snapshot(topic, posts, hit_result_cap=False, google_trends=None):
    """Upsert this crawl's post IDs into the ledger, compute this run's
    windowed counts, classify the topic, and append one immutable snapshot
    row. Returns the snapshot as a plain dict. Requires DATABASE_URL --
    raises if not configured (callers are expected to check enabled() first;
    this isn't called from any request path, only from the scheduler/manual
    trigger)."""
    if not enabled():
        raise RuntimeError("trends.record_snapshot called without DATABASE_URL configured")

    with store._connect() as conn:
        _ensure_tables(conn)
        new_this_crawl = _upsert_posts(conn, topic, posts)

        w24 = _window(conn, topic, 24)
        w72 = _window(conn, topic, 72)
        w168 = _window(conn, topic, 168)
        classification, velocity, acceleration = _classify(conn, topic, w24)

        row = conn.execute("""
            INSERT INTO trend_snapshots (
                topic, new_posts_this_crawl,
                new_posts_24h, prev_posts_24h, growth_pct_24h,
                new_posts_3d, prev_posts_3d, growth_pct_3d,
                new_posts_7d, prev_posts_7d, growth_pct_7d,
                velocity, acceleration, classification,
                earliest_published_at, latest_published_at, hit_result_cap,
                google_trends)
            VALUES (%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s)
            RETURNING id, crawled_at
        """, (topic, new_this_crawl,
              w24["new"], w24["prev"], w24["growth_pct"],
              w72["new"], w72["prev"], w72["growth_pct"],
              w168["new"], w168["prev"], w168["growth_pct"],
              velocity, acceleration, classification,
              w24["earliest"], w24["latest_pub"], hit_result_cap,
              json.dumps(google_trends) if google_trends is not None else None)).fetchone()

    return {
        "topic": topic, "id": row[0], "crawled_at": row[1].isoformat(),
        "new_posts_this_crawl": new_this_crawl,
        "new_posts_24h": w24["new"], "prev_posts_24h": w24["prev"], "growth_pct_24h": w24["growth_pct"],
        "new_posts_3d": w72["new"], "prev_posts_3d": w72["prev"], "growth_pct_3d": w72["growth_pct"],
        "new_posts_7d": w168["new"], "prev_posts_7d": w168["prev"], "growth_pct_7d": w168["growth_pct"],
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
