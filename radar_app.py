"""
radar_app.py — The Radar: a daily social-trend dashboard for one topic (crafts).

Run locally (from the radar/ folder):
    uvicorn radar_app:app --reload --port 8000
Then open http://localhost:8000

Endpoints:
    GET  /api/radar             today's radar (5 ideas + ranked trends + raw signals), cached daily
    POST /api/refresh           force a fresh build for today
    GET  /api/health            liveness + whether Apify/history/trend-tracking are configured
    GET  /api/trends            every longitudinally-tracked topic + its latest snapshot
    GET  /api/trends/{topic}    one topic's latest snapshot + history (for a trend line)
    POST /api/trends/{topic}/crawl   ops-only: run one crawl+snapshot cycle now

Self-contained — imports only `engine`/`sources`/`store`/`trends`/`tracker`/
`scheduler` in this folder, so the whole directory ports cleanly elsewhere.
"""
import datetime as _dt
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import engine
import scheduler
import sources
import store
import tracker
import trends


@asynccontextmanager
async def _lifespan(app):
    scheduler.start()   # no-op unless TREND_TRACKING_ENABLED + DATABASE_URL are both set
    yield

app = FastAPI(title="The Radar — daily social trend discovery", lifespan=_lifespan)
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


@app.get("/api/health")
def health():
    return {"status": "ok", "apify": sources.live(),
            "ai": bool(os.environ.get("ANTHROPIC_API_KEY")), "topic": sources.TOPIC,
            "history_persistent": store.enabled(),
            "trend_tracking": {"enabled": scheduler.ENABLED,
                                "currently_tracked": scheduler.select_topics() if scheduler.ENABLED else [],
                                "interval_hours": scheduler.INTERVAL_HOURS,
                                "auto_track_count": scheduler.AUTO_TRACK_COUNT,
                                "auto_track_max": scheduler.AUTO_TRACK_MAX,
                                "discovery_interval_hours": scheduler.DISCOVERY_INTERVAL_HOURS,
                                "rising_query_min_growth_pct": scheduler.RISING_QUERY_MIN_GROWTH_PCT,
                                "rising_query_candidates": len(scheduler._discovery_cache.get("rising", [])),
                                "discovery_last_run": (scheduler._discovery_cache["at"].isoformat()
                                                        if scheduler._discovery_cache["at"] else None),
                                "estimated_daily_cost_usd": (scheduler.estimate_daily_cost_usd()
                                                              if scheduler.ENABLED else 0.0),
                                "estimated_cost_note": ("Apify + DataForSEO, worst-case (assumes every "
                                                         "crawl hits its cap) — not real billing data")}}


@app.get("/api/trends")
def trends_list():
    """Every topic with at least one recorded snapshot ever, with its latest
    numbers, plus which of those are actually in this cycle's tracked set."""
    topics = trends.tracked_topics()
    current = set(scheduler.select_topics()) if scheduler.ENABLED else set()
    return {"tracking_enabled": scheduler.ENABLED, "currently_tracked": sorted(current),
            "topics": [trends.latest(t) for t in topics]}


@app.get("/api/trends/{topic}")
def trend_detail(topic: str, days: int = Query(30, ge=1, le=90)):
    """One topic's latest snapshot plus its snapshot history, for a trend line.
    `days` selects the window shown (1 for a 24h view, 7, 30, ...)."""
    snap = trends.latest(topic)
    if snap is None:
        raise HTTPException(status_code=404, detail="no snapshots recorded for that topic yet")
    return {"latest": snap, "history": trends.history(topic, days=days)}


@app.post("/api/trends/{topic}/crawl")
def trend_crawl_now(topic: str):
    """Ops-only: run one crawl+snapshot cycle for a topic right now, rather than
    waiting for the scheduler. Not linked from the UI, same reasoning as
    /api/refresh. This spends real Apify (and DataForSEO) budget if
    APIFY_API_KEY is configured — use it for the one-topic pilot test, not
    casually. 503 if trend tracking isn't configured (DATABASE_URL/APIFY_API_KEY)."""
    snap = tracker.run(topic)
    if snap is None:
        raise HTTPException(status_code=503,
                             detail="trend tracking unavailable: DATABASE_URL or APIFY_API_KEY not configured")
    return snap


@app.post("/api/trends/{topic}/purge")
def trend_purge(topic: str):
    """Ops-only: permanently delete a topic's ledger + snapshot history --
    for a discovered candidate that turned out to be noise (e.g. an
    ambiguous phrase whose corroboration was irrelevant). Not linked from
    the UI, not reversible. Add the topic to TREND_EXCLUDED_TOPICS too, or
    it can simply be re-discovered on a later crawl."""
    if not trends.enabled():
        raise HTTPException(status_code=503, detail="trend tracking unavailable: DATABASE_URL not configured")
    deleted = trends.purge_topic(topic)
    return {"topic": topic, "deleted": deleted}


@app.get("/api/radar")
def radar():
    """Today's radar — cached to one build per day."""
    return engine.daily()


@app.get("/api/radar/history")
def history(year: int = Query(...), month: int = Query(..., ge=1, le=12)):
    """Which dates in this month have a saved report — for the History calendar.
    Never downloads a whole month of reports, just the list of available dates."""
    return {"dates": engine.available_dates(year, month)}


@app.get("/api/radar/{date}")
def radar_on_date(date: str):
    """A specific past day's report, exactly as originally generated. 404 if that
    day was never built. Read-only — this never triggers a new crawl/LLM run."""
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    report = engine.get_report(date)
    if report is None:
        raise HTTPException(status_code=404, detail="no saved report for that date")
    return report


@app.post("/api/refresh")
def refresh():
    """Rebuild today's radar now (re-pulls the sources). Deliberately not linked
    from the UI: report generation is schedule-only so end users can't trigger a
    live crawl/LLM run on demand and run up API spend. This stays as an ops-only
    lever (curl it yourself) for the rare case a scheduled build failed."""
    return engine.daily(force=True)


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    with open(os.path.join(WEB, "index.html"), encoding="utf-8") as f:
        html = f.read()
    ver = int(max(os.path.getmtime(os.path.join(WEB, n)) for n in ("app.js", "styles.css")))
    html = (html.replace("/static/app.js", "/static/app.js?v=%d" % ver)
                .replace("/static/styles.css", "/static/styles.css?v=%d" % ver))
    return HTMLResponse(html)
