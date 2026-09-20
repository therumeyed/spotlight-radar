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
| `TREND_TRACKING_ENABLED` | Turns on the longitudinal trend tracker (see below). | — (off) |
| `TREND_TRACK_TOPICS` | Comma-separated topics/hashtags ALWAYS tracked, on top of auto-selection. Optional. | — (none) |
| `TREND_AUTO_TRACK_COUNT` | How many of today's top discovered micro-trends to auto-track. | `3` |
| `TREND_AUTO_TRACK_MAX` | Hard cap on total concurrently-tracked topics (manual + auto + retained). | `5` |
| `TREND_TOPIC_RETENTION_DAYS` | Days a topic keeps getting crawled after it drops out of today's top picks. | `5` |
| `TREND_CRAWL_INTERVAL_HOURS` | How often the tracker re-crawls each tracked topic. | `12` |
| `TREND_TIKTOK_MAX_RESULTS` / `TREND_INSTAGRAM_MAX_RESULTS` | Per-platform result cap per crawl, per topic. | `50` / `50` |
| `TREND_MIN_SNAPSHOTS` | Crawls needed before a topic can be classified (below this: "Collecting baseline"). | `2` |
| `TREND_EMERGING_MIN_POSTS` / `TREND_EMERGING_MIN_CREATORS` / `TREND_EMERGING_MIN_GROWTH_PCT` | Thresholds for the "Emerging" classification. | `5` / `3` / `50` |
| `TREND_COOLING_MAX_GROWTH_PCT` | Growth % at or below which a topic is classified "Cooling". | `-20` |

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
trends.py    → longitudinal trend tracking: a durable post-level ledger (dedup by
                platform+post ID) + an append-only history of computed growth
                snapshots per topic — 24h/3d/7d windows, velocity, acceleration,
                lifecycle classification. Off without DATABASE_URL (no non-durable
                fallback here — a velocity claim that doesn't survive a restart is
                worse than no claim)
tracker.py   → one crawl-and-snapshot cycle for one topic: pulls fresh posts,
                merges into the ledger, records a snapshot. Never called from a
                page request — only the scheduler or the manual ops trigger
scheduler.py → in-process background loop that runs tracker.py on a fixed cadence
                (default 12h) against a topic set it re-selects every cycle
                (today's top auto-discovered micro-trends + any manual pins +
                anything still in its retention window). Off by default; needs
                TREND_TRACKING_ENABLED and DATABASE_URL
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

**Trend velocity tracker (optional, off by default):** the single-crawl signals above
answer "what does today's crawl show"; the tracker answers "is this actually growing."
For each tracked topic it keeps a durable, deduplicated ledger of every post ever seen
(by platform + post ID), then on every scheduled crawl computes real growth — new posts
in the last 24h vs the previous 24h (also 3d/7d), unique creators, engagement — and
classifies the topic's lifecycle stage:

- **Collecting baseline** — not enough crawl history yet to claim anything (the first
  crawl for a topic is *always* this; it takes `TREND_MIN_SNAPSHOTS` crawls before any
  other label is possible).
- **New** — posts just started appearing where there were none before.
- **Emerging** / **Accelerating** — real growth clearing the configured thresholds
  (`TREND_EMERGING_MIN_POSTS`/`_CREATORS`/`_GROWTH_PCT`); "Accelerating" additionally
  means growth is speeding up crawl-over-crawl, not just continuing.
- **Sustained** — active, but growth has levelled off.
- **Cooling** — activity is declining (`TREND_COOLING_MAX_GROWTH_PCT`).

Google Trends (DataForSEO or the free fallback) is used here only to *corroborate* — a
"does search interest agree" check — never to supply post/view counts itself. When
Claude writes the 5 ideas, any tracked topic's real growth numbers are passed into the
prompt so "why now" can cite an actual measured growth rate instead of a single day's
score.

**Which topics get tracked** is decided fresh every crawl cycle, not fixed once: it's
the union of any manual pins (`TREND_TRACK_TOPICS`, optional), today's top
`TREND_AUTO_TRACK_COUNT` micro-trends pulled straight from the *same* daily crawl's
rank_trends() output (no separate discovery crawl — this reuses the trend detection
the app already does, so tracking "crafts" the whole category isn't the point; tracking
the specific things rank_trends() surfaces, like "punch needle kit," is), and anything
still within `TREND_TOPIC_RETENTION_DAYS` of its last crawl even if it dropped out of
today's top picks — so a fading trend gets to show "Cooling" instead of just vanishing.
The total tracked set is capped at `TREND_AUTO_TRACK_MAX` regardless, so cost stays
bounded no matter how much the daily top-N churns.

## Endpoints

- `GET /api/radar` — today's radar (ideas + ranked trends + raw signals), cached to one build/day.
- `GET /api/radar/history?year=&month=` — which dates in that month have a saved report (for the History calendar).
- `GET /api/radar/{date}` — a specific past day's report exactly as originally generated. Read-only, never regenerates.
- `POST /api/refresh` — force a fresh build for today. Deliberately not linked from the UI — report generation is schedule-only so end users can't trigger a paid crawl/LLM run on demand. Ops-only lever for a failed scheduled build.
- `GET /api/health` — liveness + whether Apify/AI/history/trend-tracking are configured.
- `GET /api/trends` — every tracked topic with its latest snapshot.
- `GET /api/trends/{topic}?days=30` — one topic's latest snapshot + snapshot history (for a trend line).
- `POST /api/trends/{topic}/crawl` — ops-only: run one crawl+snapshot cycle right now, rather than waiting for the scheduler. Spends real Apify/DataForSEO budget if configured — not linked from the UI.

## Deploying

`Dockerfile` builds a standalone image (`pip install -r requirements.txt`, then
`uvicorn radar_app:app`). On Render specifically: **add a Postgres database and set
`DATABASE_URL` on this service** — Render's disk does not survive a redeploy or instance
spin-down, so without a database, History only lasts for the lifetime of a single running
instance. Render dashboard → New + → PostgreSQL → create a small database → copy its
Internal Database URL → this service's Environment → add `DATABASE_URL` → save (triggers
a redeploy). With no `DATABASE_URL` set, the app still runs — no history, one day's build
cached locally, and the calendar shows a "no history yet" message instead of erroring.

**Trend tracker cost:** at the defaults (50 results/platform/crawl, every 12h), one
tracked topic costs roughly $0.20–0.25/crawl in worst-case Apify spend (TikTok $1.50/1k
results, Instagram ~$2.30–2.60/1k) — about $12–14/month per topic at 2 crawls/day, plus
a few cents of DataForSEO if configured. The tracked set is capped at
`TREND_AUTO_TRACK_MAX` (default `5`), so the realistic ceiling at defaults is **~$60–70/
month total**, not unbounded — it won't creep up just because the daily top-N keeps
changing. Lower `TREND_AUTO_TRACK_MAX`, `TREND_TIKTOK_MAX_RESULTS`/
`TREND_INSTAGRAM_MAX_RESULTS`, or raise `TREND_CRAWL_INTERVAL_HOURS` to trade coverage
for cost. Set `TREND_AUTO_TRACK_COUNT=0` (with one `TREND_TRACK_TOPICS` pin) to go back
to tracking a single fixed topic for a cheaper pilot.
