"""
radar_app.py — The Radar: a daily social-trend dashboard for one topic (crafts).

Run locally (from the radar/ folder):
    uvicorn radar_app:app --reload --port 8000
Then open http://localhost:8000

Endpoints:
    GET  /api/radar     today's radar (5 ideas + ranked trends + raw signals), cached daily
    POST /api/refresh   force a fresh build for today
    GET  /api/health    liveness + whether Apify is configured (live vs sample)

Self-contained — imports only `engine`/`sources` in this folder, so the whole
`radar/` directory ports cleanly into another dashboard.
"""
import datetime as _dt
import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import engine
import sources

app = FastAPI(title="The Radar — daily social trend discovery")
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


@app.get("/api/health")
def health():
    return {"status": "ok", "apify": sources.live(),
            "ai": bool(os.environ.get("ANTHROPIC_API_KEY")), "topic": sources.TOPIC}


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
