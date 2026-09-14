# Spotlight Radar — daily social trend discovery

A self-contained mini-dashboard that reads three sources every day — **Google Trends,
TikTok and Instagram** — for one topic (default: **crafts**, Australia) and turns what's
trending into a handful of social content ideas you can pitch, each backed by one real
crawled post (never a fabricated example — see "Evidence" below).

Originally built inside `market_intelligence_engine` as a self-contained `radar/` folder
(no imports from that parent project, only `fastapi` + `uvicorn` as dependencies —
everything else is the Python standard library) and later extracted here as its own repo
so it can be deployed and iterated on independently.

## Run it

```bash
pip install -r requirements.txt
uvicorn radar_app:app --reload --port 8000
# open http://localhost:8000
```

With **no keys set it runs in SAMPLE mode** — realistic crafts trends so you can see the
shape of it. Nothing is presented as real live data; the header shows a `SAMPLE DATA`
badge. Add the keys below and it goes **live**.

## Configuration (all optional; env vars)

| Variable | What it does | Default |
|---|---|---|
| `APIFY_API_KEY` | Turns on live TikTok + Instagram data via Apify. | — (sample mode) |
| `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` | Turns on DataForSEO for Google Trends (paid, reliable) instead of the free scrape below. | — (free scrape) |
| `ANTHROPIC_API_KEY` | Writes the 5 ideas with Claude; without it, a grounded rule-based fallback is used. | — (rules) |
| `DATABASE_URL` | A Postgres connection string — turns on the History calendar (past days survive restarts/redeploys). | — (no history) |
| `RADAR_TOPIC` | The topic label. | `crafts` |
| `RADAR_TOPIC_MID` | Google Trends topic id. | `/m/01mrgs` (Craft) |
| `RADAR_GEO` | Region. | `AU` |
| `RADAR_KEYWORDS` | Seed hashtags/keywords to scan socially. | `crafts,craftok,diy crafts,craft ideas,handmade,craft tutorial,craft hack,easy crafts,craft diy,crafting` |
| `APIFY_TIKTOK_ACTOR` | Apify actor id for TikTok. | `sociavault~tiktok-keyword-search-scraper` |
| `APIFY_INSTAGRAM_ACTOR` | Apify actor id for Instagram. | `apify~instagram-hashtag-scraper` |

> **Note on the Apify actors:** actor input/output shapes vary between actors, so the
> parsers in `sources.py` are deliberately defensive — when you plug in your real key,
> sanity-check one live run and tweak the field mapping there if a chosen actor returns
> different keys.

**Google Trends:** DataForSEO first when `DATAFORSEO_LOGIN`/`DATAFORSEO_PASSWORD` are set
— a paid, reliable source (Basic Auth, login+password from your DataForSEO account).
Otherwise it hits the same free, unofficial endpoint pytrends uses; Google rate-limits
that hard from cloud/datacenter IPs, so a 429 trips a circuit breaker and falls back to
real Google News coverage, then to sample data only if nothing is reachable.

**On idea volume:** in live mode, an idea only ever appears once it has a real crawled
post behind it — and only TikTok/Instagram signals can carry that, Google Trends signals
never can (see "Evidence" below). So the lever for more recommendations a day is wider
TikTok/Instagram coverage (`RADAR_KEYWORDS`, and the result caps in `sources.py`), not the
Trends source — DataForSEO makes Trends data more reliable, it doesn't by itself raise
idea count.

## How it works

```
sources.py   → pulls raw signals + one real representative crawled post per signal
                (fail-soft; sample fallback; never fabricates a post/url/author/metric)
engine.py    → collect → rank_trends (define "trending") → make_ideas (Claude/rules),
                suppresses any live idea with no real example behind it → persists daily
store.py     → optional Postgres history archive (no-op without DATABASE_URL) — the
                durable copy; survives restarts/redeploys
radar_app.py → FastAPI: today's radar, history, a specific past day, health, dashboard
web/         → the dashboard (index.html, app.js, styles.css) — History as an overlay
                calendar, not a page section; no user-facing refresh/regenerate control
data/        → this instance's own same-day cache only (daily.json) — not the history
                archive; Postgres is what actually survives a redeploy
```

**"Trending" (our definition):** a sub-topic is trending when it shows recent momentum
(velocity) on a source, and it trends *more strongly* when independent sources agree — a
breakout that Trends, TikTok and Instagram all show is a stronger, earlier signal than one
channel alone. Each trend is also tagged **right-now** (memes, ~1 week) or **building**
(6–12 weeks), matching the brief's two speeds of opportunity.

**Evidence:** every idea is meant to carry one real crawled TikTok/Instagram post (url,
author, caption, engagement, timestamp) as proof the opportunity is real, not invented.
In live mode, an idea with no real post behind it is suppressed rather than shown as
ready-to-pitch. In sample mode (no Apify key), no example is fabricated either — the UI
says so explicitly instead of showing a fake post.

## Endpoints

- `GET /api/radar` — today's radar (ideas + ranked trends + raw signals), cached to one build/day.
- `GET /api/radar/history?year=&month=` — which dates in that month have a saved report (for the History calendar).
- `GET /api/radar/{date}` — a specific past day's report exactly as originally generated. Read-only, never regenerates.
- `POST /api/refresh` — force a fresh build for today. Deliberately not linked from the UI — report generation is schedule-only so end users can't trigger a paid crawl/LLM run on demand. Ops-only lever for a failed scheduled build.
- `GET /api/health` — liveness + whether Apify/AI are configured.

## Deploying

`Dockerfile` builds a standalone image (`pip install -r requirements.txt`, then
`uvicorn radar_app:app`). On Render specifically: **add a Postgres database and set
`DATABASE_URL` on this service** — Render's disk does not survive a redeploy or instance
spin-down, so without a database, History only lasts for the lifetime of a single running
instance. Render dashboard → New + → PostgreSQL → create a small database → copy its
Internal Database URL → this service's Environment → add `DATABASE_URL` → save (triggers
a redeploy). With no `DATABASE_URL` set, the app still runs — no history, one day's build
cached locally, and the calendar shows a "no history yet" message instead of erroring.
