"""
store.py — persistent history for The Radar, in Postgres.

Optional and fail-soft by design, matching the rest of this project: with no
DATABASE_URL set, every function here is a no-op (returns None/[]), so the app
still runs — it just has no history across restarts. Set DATABASE_URL (a
Render Postgres, or any Postgres) to make daily builds durable: Render's
free-tier disk does not survive redeploys or periodic restarts, so this is
the only thing that actually keeps the History calendar populated in
production.

One table, one job: radar_history(date, topic, geo, data). "date" + "topic" +
"geo" together identify a day's build, so switching RADAR_TOPIC/RADAR_GEO
later doesn't collide with older history under a different topic.
"""
import json
import os

try:
    import psycopg
except ImportError:
    psycopg = None

_URL = os.environ.get("DATABASE_URL", "").strip()


def enabled():
    """True only when a database is configured and the driver is installed."""
    return bool(_URL and psycopg)


def _connect():
    # Render's DATABASE_URL sometimes uses the (deprecated) postgres:// scheme;
    # psycopg only accepts postgresql://.
    url = _URL.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url, connect_timeout=5)


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS radar_history (
            date TEXT NOT NULL,
            topic TEXT NOT NULL,
            geo TEXT NOT NULL,
            data JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (date, topic, geo)
        )
    """)


def save(date, topic, geo, data):
    """Persist one day's build. Fail-soft: any error is swallowed — history is
    a nice-to-have, never a reason for the daily read itself to break."""
    if not enabled():
        return
    try:
        with _connect() as conn:
            _ensure_table(conn)
            conn.execute("""
                INSERT INTO radar_history (date, topic, geo, data)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (date, topic, geo) DO UPDATE SET data = EXCLUDED.data
            """, (date, topic, geo, json.dumps(data)))
    except Exception:
        pass


def load(date, topic, geo):
    """One day's stored build, or None if not found / not configured."""
    if not enabled():
        return None
    try:
        with _connect() as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT data FROM radar_history WHERE date = %s AND topic = %s AND geo = %s",
                (date, topic, geo),
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def list_dates(topic, geo):
    """All dates with a stored build for this topic/geo, newest first."""
    if not enabled():
        return []
    try:
        with _connect() as conn:
            _ensure_table(conn)
            rows = conn.execute(
                "SELECT date FROM radar_history WHERE topic = %s AND geo = %s ORDER BY date DESC",
                (topic, geo),
            ).fetchall()
            return [r[0] for r in rows]
    except Exception:
        return []
