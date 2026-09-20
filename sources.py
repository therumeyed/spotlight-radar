"""
radar/sources.py — the three trend sources for The Radar.

Google Trends + TikTok + Instagram. Each source returns a normalised list of
trend SIGNALS for the topic (default: crafts, Australia, last 24h):

    {"term", "source", "score" (0..1 velocity), "metric" (human label), "url"}

Design rules (matching the main engine):
  * FAIL-SOFT — any error, timeout, quota or a missing key returns the built-in
    SAMPLE signals so the dashboard always renders (demo mode).
  * NEVER fabricates live data — sample signals are flagged `sample: True`, and
    engine.collect() marks the whole read as demo vs live so nothing pretends to
    be real data.
  * Self-contained — no imports from the main engine, so this folder ports cleanly
    into another dashboard.

Google Trends is free and key-less: the same unofficial endpoint pytrends uses,
with a real Google News fallback (also free) when Trends rate-limits us — which
it does hard from cloud/datacenter IPs. TikTok + Instagram go through Apify,
since neither platform has a free public trend API.

The topic is Google Trends /m/01mrgs == "Craft". Everything is overridable via
env vars, so the same code runs any topic/category later.
"""
import base64
import datetime as _dt
import http.cookiejar
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter

_BASE = "https://api.apify.com/v2"
_UA = "Mozilla/5.0 (TheRadar/1.0)"

# ---- the topic (crafts) + region; override via env to run any category ----
TOPIC       = os.environ.get("RADAR_TOPIC", "crafts")
TOPIC_MID   = os.environ.get("RADAR_TOPIC_MID", "/m/01mrgs")      # Google Trends topic id
GEO         = os.environ.get("RADAR_GEO", "AU")
TIMEFRAME   = os.environ.get("RADAR_TIMEFRAME", "now 1-d")        # last 24h — a daily read

# ---- Apify actors (TikTok + Instagram only — Google Trends is free/direct) ----
TIKTOK_ACTOR    = os.environ.get("APIFY_TIKTOK_ACTOR", "sociavault~tiktok-keyword-search-scraper")
INSTAGRAM_ACTOR = os.environ.get("APIFY_INSTAGRAM_ACTOR", "apify~instagram-hashtag-scraper")
_TIMEOUT        = float(os.environ.get("APIFY_RUN_TIMEOUT", "120"))
_TRENDS_TIMEOUT = float(os.environ.get("TRENDS_TIMEOUT", "8"))    # short: never stall a daily read

# ---- DataForSEO (optional) — a paid, reliable Google Trends source. When
# configured, it replaces the free scrape below (which Google rate-limits hard
# from cloud/datacenter IPs); without it, nothing changes. Its "live" endpoint
# genuinely takes 10-15s to answer (DataForSEO runs it synchronously against
# Google on request) — a much longer budget than the free scrape needs, so it
# gets its own timeout rather than sharing TRENDS_TIMEOUT.
DATAFORSEO_LOGIN    = os.environ.get("DATAFORSEO_LOGIN", "").strip()
DATAFORSEO_PASSWORD = os.environ.get("DATAFORSEO_PASSWORD", "").strip()
_DATAFORSEO_TIMEOUT = float(os.environ.get("DATAFORSEO_TIMEOUT", "25"))

# seed hashtags/keywords we scan on the social platforms — the widest practical
# net for one daily crawl: more keywords here means more trend clusters end up
# with a real crawled post attached, which is what actually clears the
# evidence-only bar in build_radar() (Google Trends signals alone never do).
KEYWORDS = [k.strip() for k in os.environ.get(
    "RADAR_KEYWORDS",
    "crafts,craftok,diy crafts,craft ideas,handmade,craft tutorial,"
    "craft hack,easy crafts,craft diy,crafting").split(",") if k.strip()]


def token():
    return os.environ.get("APIFY_API_KEY", "").strip()


def live():
    """True only when an Apify key is configured (otherwise we serve sample data)."""
    return bool(token())


def _run(actor, payload, timeout=_TIMEOUT):
    """Run an Apify actor synchronously, return its dataset items (list). Fail-soft:
    any error returns [] so the caller falls back to sample data."""
    tok = token()
    if not tok:
        return []
    url = "%s/acts/%s/run-sync-get-dataset-items?token=%s" % (_BASE, actor, urllib.parse.quote(tok))
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items") or data.get("data") or []
        return []
    except Exception:
        return []


def _sat(x, scale):
    """Map an unbounded non-negative count onto 0..1, saturating at ~scale."""
    return round(1.0 - math.exp(-max(0.0, x) / scale), 3) if scale > 0 else 0.0


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _iso_from_ts(ts):
    """Best-effort ISO-8601 timestamp from an epoch number (seconds or ms) or an
    already-ISO string. Returns None rather than guessing when it can't parse —
    a missing publish date must never be silently invented."""
    if not ts:
        return None
    if isinstance(ts, str):
        try:
            _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return ts
        except ValueError:
            return None
    try:
        val = float(ts)
        if val > 1e12:   # milliseconds
            val /= 1000.0
        return _dt.datetime.utcfromtimestamp(val).isoformat(timespec="seconds") + "Z"
    except (TypeError, ValueError, OSError):
        return None


def _example(source, url, title, author, published_at, metric_label):
    """A single real evidence record for one crawled post. Never call this with a
    fabricated field — omit the whole example instead (see the two callers below)."""
    if not url:
        return None
    return {"example_source": source, "example_url": url, "example_title": (title or "")[:160],
            "example_author": author or "", "example_published_at": published_at,
            "example_metric": metric_label or ""}


# ======================================================================= TRENDS
# Free Google Trends: the same unofficial endpoint pytrends uses (explore ->
# RELATED_QUERIES widget -> widgetdata/relatedsearches), no key, no Apify cost.
# Google rate-limits this hard from cloud/datacenter IPs, so a 429 trips a
# process-wide circuit breaker and we fall back to real Google News coverage
# instead of hammering a blocked endpoint. Sample data is the last resort.
# DataForSEO, when configured (see below), takes priority over all of this.
_TRENDS_API = "https://trends.google.com/trends/api"
_trends_blocked = False


def _consent_cookie():
    return http.cookiejar.Cookie(
        version=0, name="CONSENT", value="YES+", port=None, port_specified=False,
        domain=".google.com", domain_specified=True, domain_initial_dot=True,
        path="/", path_specified=True, secure=True, expires=None, discard=False,
        comment=None, comment_url=None, rest={})


def _strip_xssi(text):
    i = text.find("{")
    return json.loads(text[i:]) if i != -1 else None


def _trends_rising(query, geo=GEO, timeframe=TIMEFRAME):
    """Rising related queries for `query`, straight from Google Trends. None on
    any failure; trips the circuit breaker on HTTP 429."""
    global _trends_blocked
    if _trends_blocked:
        return None
    try:
        cj = http.cookiejar.CookieJar()
        cj.set_cookie(_consent_cookie())
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        opener.addheaders = [("User-Agent", _UA)]

        req = json.dumps({"comparisonItem": [{"keyword": query, "geo": geo, "time": timeframe}],
                          "category": 0, "property": ""})
        ex_url = "%s/explore?hl=en-US&tz=0&req=%s" % (_TRENDS_API, urllib.parse.quote(req))
        ex = opener.open(ex_url, timeout=_TRENDS_TIMEOUT).read().decode("utf-8", "ignore")
        widgets = (_strip_xssi(ex) or {}).get("widgets", [])
        rq_widget = next((w for w in widgets if w.get("id") == "RELATED_QUERIES"), None)
        if not rq_widget:
            return None
        wreq = json.dumps(rq_widget["request"])
        rs_url = ("%s/widgetdata/relatedsearches?hl=en-US&tz=0&req=%s&token=%s"
                  % (_TRENDS_API, urllib.parse.quote(wreq), rq_widget["token"]))
        rs = opener.open(rs_url, timeout=_TRENDS_TIMEOUT).read().decode("utf-8", "ignore")
        ranked = (_strip_xssi(rs) or {}).get("default", {}).get("rankedList", [])
        rising = ranked[1]["rankedKeyword"] if len(ranked) > 1 else []
        return rising or None
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _trends_blocked = True    # stop hammering a blocked endpoint
        return None
    except Exception:
        return None


_STOPWORDS = {"and", "for", "the", "with", "your", "into", "from", "this", "that",
              "are", "was", "were", "have", "has", "you", "our", "how", "why",
              "what", "new", "top", "best", "get", "can", "will", "all", "out",
              "these", "those", "its", "amid", "over", "off", "via"}


def _news_rising(topic, keywords, k=5):
    """Fallback when Trends is blocked: real (not fabricated) candidate terms
    mined from recent Google News coverage of the topic — how often a phrase
    recurs across fresh headlines stands in for 'rising query' velocity.

    Phrases that just restate the parent topic (e.g. "arts and crafts" when the
    topic IS crafts) are dropped: a micro-trend has to name something specific
    — a technique, product or format — not echo the category itself."""
    topic_words = set(topic.lower().split())
    counts = Counter()
    for q in [topic] + list(keywords[:3]):
        url = ("https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en"
               % urllib.parse.quote(q))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=_TRENDS_TIMEOUT) as r:
                raw = r.read()
            root = ET.fromstring(raw)
        except Exception:
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").lower()
            title = re.sub(r"[^a-z0-9 ]", " ", title)
            words = title.split()
            for n in (2, 3):
                for i in range(len(words) - n + 1):
                    phrase = " ".join(words[i:i + n])
                    if phrase in (topic, q) or len(phrase) <= 6:
                        continue
                    phrase_words = phrase.split()
                    content = set(phrase_words) - _STOPWORDS - topic_words
                    if len(content) / len(phrase_words) <= 0.5:
                        continue    # mostly topic/filler words, not a micro-trend
                    counts[phrase] += 1
    if not counts:
        return None
    top = counts.most_common(k)
    peak = top[0][1]
    return [{"term": term, "source": "google_trends",
             "score": round(max(0.35, min(1.0, 0.4 + 0.6 * n / peak)), 3),
             "metric": "Google News: mentioned in %d recent headlines" % n,
             "url": "https://news.google.com/search?q=%s" % urllib.parse.quote(term)}
            for term, n in top]


# DataForSEO location_name lookup for the geos this project has actually used;
# extend as needed. Falls back to "Australia" (today's only market) rather
# than guessing at an unmapped ISO code.
_DATAFORSEO_LOCATIONS = {"AU": "Australia", "US": "United States", "GB": "United Kingdom"}


def _dataforseo_rising(query, geo=GEO):
    """Rising related queries for `query`, via DataForSEO's Google Trends Explore
    (live) endpoint — a paid, reliable stand-in for the free endpoint below, which
    Google rate-limits hard from cloud/datacenter IPs. None if not configured or
    on any failure, so the caller falls through to the free path unaffected."""
    if not (DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD):
        return None
    auth = base64.b64encode(("%s:%s" % (DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD)).encode()).decode()
    body = json.dumps([{
        "keywords": [query],
        "location_name": _DATAFORSEO_LOCATIONS.get(geo, "Australia"),
        "time_range": "past_7_days",
        "item_types": ["google_trends_queries_list"],
    }]).encode("utf-8")
    req = urllib.request.Request(
        "https://api.dataforseo.com/v3/keywords_data/google_trends/explore/live",
        data=body, headers={"Authorization": "Basic %s" % auth, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_DATAFORSEO_TIMEOUT) as r:
            data = json.loads(r.read())
        result = (data.get("tasks") or [{}])[0].get("result") or [{}]
        items = (result[0] or {}).get("items") or []
        queries = next((it for it in items if it.get("type") == "google_trends_queries_list"), None)
        rising = (queries or {}).get("data", {}).get("rising") or []
        return [{"query": rq.get("query"), "value": rq.get("value"), "formattedValue": None}
                for rq in rising if rq.get("query")] or None
    except Exception:
        return None


def google_trends():
    """Rising related queries for the topic. DataForSEO first when configured
    (paid, reliable); else the free Google Trends endpoint (no Apify, no cost);
    falls back to real Google News coverage when Trends is rate-limited; sample
    data only if all of the above are unreachable."""
    rising = _dataforseo_rising(TOPIC) or _trends_rising(TOPIC)
    out = []
    for rq in rising or []:
        term = rq.get("query") or ""
        if not term:
            continue
        val = _num(rq.get("value") or 0)
        formatted = rq.get("formattedValue") or ("Breakout" if val >= 5000 else "+%d%%" % val)
        score = 1.0 if (isinstance(formatted, str) and "reak" in formatted) else min(1.0, val / 500.0)
        out.append({"term": term.lower().strip(), "source": "google_trends",
                    "score": round(max(0.35, score), 3), "metric": "Google Trends: %s" % formatted,
                    "url": "https://trends.google.com/trends/explore?q=%s&geo=%s"
                           % (urllib.parse.quote(term), GEO)})
    if out:
        return out
    return _news_rising(TOPIC, KEYWORDS) or _sample("google_trends")


# ======================================================================= TIKTOK
def tiktok():
    """Recent TikTok videos for the topic; aggregate the hashtags that show the most
    recent reach into per-tag velocity signals. Falls back to sample."""
    seen = {}
    if live():
        for kw in KEYWORDS[:6]:
            for it in _run(TIKTOK_ACTOR, {"query": kw, "region": GEO, "max_results": 30,
                                          "sort_by": "relevance"}):
                info = it.get("aweme_info") or it
                stats = info.get("statistics") or info.get("stats") or {}
                plays = int(_num(stats.get("play_count") or stats.get("playCount") or 0))
                created = info.get("create_time") or info.get("createTime") or 0
                age_days = (time.time() - created) / 86400.0 if created else 1e9
                if age_days > 30:
                    continue
                desc_text = (info.get("desc") or it.get("desc") or "").strip()
                author = ((info.get("author") or {}).get("nickname")
                          or (info.get("author") or {}).get("unique_id")
                          or (it.get("authorMeta") or {}).get("name") or "")
                url = (info.get("share_url") or it.get("webVideoUrl") or it.get("shareUrl") or "")
                for tag in _hashtags(desc_text) or [kw.replace(" ", "")]:
                    d = seen.setdefault(tag, {"plays": [], "n": 0, "best": None})
                    d["plays"].append(plays); d["n"] += 1
                    if url and (d["best"] is None or plays > d["best"]["plays"]):
                        d["best"] = {"plays": plays, "author": author, "url": url,
                                     "title": desc_text, "created": created}
    if not seen:
        return _sample("tiktok")
    out = []
    for tag, d in seen.items():
        d["plays"].sort()
        median = d["plays"][len(d["plays"]) // 2] if d["plays"] else 0
        best = d["best"]
        example = _example("tiktok", best["url"], best["title"], best["author"],
                            _iso_from_ts(best["created"]),
                            "%s plays" % _compact(best["plays"])) if best else None
        out.append({"term": tag, "source": "tiktok", "score": round(0.5 * _sat(d["n"], 6.0) + 0.5 * _sat(median, 60_000.0), 3),
                    "metric": "TikTok: %d recent videos, %s median plays" % (d["n"], _compact(median)),
                    "url": "https://www.tiktok.com/tag/%s" % urllib.parse.quote(tag),
                    "example": example})
    out.sort(key=lambda s: -s["score"])
    return out[:8]


# ==================================================================== INSTAGRAM
def instagram():
    """Recent Instagram posts under the topic hashtag; aggregate the co-occurring
    hashtags by recent engagement into velocity signals. Falls back to sample."""
    seen = {}
    if live():
        for tag in [k.replace(" ", "") for k in KEYWORDS[:5]]:
            for it in _run(INSTAGRAM_ACTOR, {"hashtags": [tag], "resultsLimit": 60}):
                likes = int(_num(it.get("likesCount") or it.get("likes") or 0))
                comments = int(_num(it.get("commentsCount") or it.get("comments") or 0))
                eng = likes + 3 * comments
                caption = (it.get("caption") or "").strip()
                author = it.get("ownerUsername") or it.get("username") or ""
                url = it.get("url") or it.get("postUrl") or it.get("permalink") or ""
                ts = it.get("timestamp") or it.get("takenAt") or it.get("takenAtTimestamp")
                for h in (it.get("hashtags") or _hashtags(caption)):
                    h = h.lstrip("#").lower()
                    d = seen.setdefault(h, {"eng": [], "n": 0, "best": None})
                    d["eng"].append(eng); d["n"] += 1
                    if url and (d["best"] is None or eng > d["best"]["eng"]):
                        d["best"] = {"eng": eng, "likes": likes, "comments": comments,
                                     "author": author, "url": url, "caption": caption, "ts": ts}
    if not seen:
        return _sample("instagram")
    out = []
    for tag, d in seen.items():
        d["eng"].sort()
        median = d["eng"][len(d["eng"]) // 2] if d["eng"] else 0
        best = d["best"]
        example = _example("instagram", best["url"], best["caption"], best["author"],
                            _iso_from_ts(best["ts"]),
                            "%s likes, %s comments" % (_compact(best["likes"]), _compact(best["comments"]))
                            ) if best else None
        out.append({"term": tag, "source": "instagram", "score": round(0.5 * _sat(d["n"], 8.0) + 0.5 * _sat(median, 3_000.0), 3),
                    "metric": "Instagram: %d recent posts, %s median engagement" % (d["n"], _compact(median)),
                    "url": "https://www.instagram.com/explore/tags/%s/" % urllib.parse.quote(tag),
                    "example": example})
    out.sort(key=lambda s: -s["score"])
    return out[:8]


# ---------------------------------------------------------------- small helpers
def _hashtags(text):
    return [w[1:].lower() for w in (text or "").split() if w.startswith("#") and len(w) > 2]


def _compact(n):
    n = float(n or 0)
    return "%.1fM" % (n / 1e6) if n >= 1e6 else "%dk" % round(n / 1e3) if n >= 1e3 else str(int(n))


# --------------------------------------------------------- SAMPLE (demo) DATA --
# Realistic crafts trends so the dashboard is fully populated with NO key set.
# Clearly returned only as a fallback; engine.collect() flags the read as `sample`.
_SAMPLE = {
    "google_trends": [
        {"term": "punch needle kit", "score": 1.0, "metric": "Google Trends: Breakout"},
        {"term": "junk journal ideas", "score": 0.92, "metric": "Google Trends: +420%"},
        {"term": "resin coasters", "score": 0.8, "metric": "Google Trends: +180%"},
        {"term": "crochet bag pattern", "score": 0.7, "metric": "Google Trends: +90%"},
        {"term": "air dry clay", "score": 0.6, "metric": "Google Trends: +60%"},
    ],
    "tiktok": [
        {"term": "craftok", "score": 0.94, "metric": "TikTok: 14 recent videos, 320k median plays"},
        {"term": "punchneedle", "score": 0.88, "metric": "TikTok: 9 recent videos, 210k median plays"},
        {"term": "junkjournal", "score": 0.83, "metric": "TikTok: 11 recent videos, 140k median plays"},
        {"term": "resinart", "score": 0.72, "metric": "TikTok: 7 recent videos, 95k median plays"},
        {"term": "crochettiktok", "score": 0.66, "metric": "TikTok: 8 recent videos, 70k median plays"},
    ],
    "instagram": [
        {"term": "junkjournaling", "score": 0.9, "metric": "Instagram: 22 recent posts, 1.8k median engagement"},
        {"term": "punchneedleart", "score": 0.82, "metric": "Instagram: 17 recent posts, 1.3k median engagement"},
        {"term": "crochetersofinstagram", "score": 0.75, "metric": "Instagram: 26 recent posts, 900 median engagement"},
        {"term": "resinart", "score": 0.7, "metric": "Instagram: 15 recent posts, 1.1k median engagement"},
        {"term": "handmadeaustralia", "score": 0.58, "metric": "Instagram: 12 recent posts, 640 median engagement"},
    ],
}
_TAG_URL = {
    "google_trends": lambda t: "https://trends.google.com/trends/explore?q=%s&geo=%s" % (urllib.parse.quote(t), GEO),
    "tiktok": lambda t: "https://www.tiktok.com/tag/%s" % urllib.parse.quote(t.replace(" ", "")),
    "instagram": lambda t: "https://www.instagram.com/explore/tags/%s/" % urllib.parse.quote(t.replace(" ", "")),
}


def _sample(source):
    out = []
    for s in _SAMPLE[source]:
        out.append(dict(s, source=source, sample=True, url=_TAG_URL[source](s["term"])))
    return out


# ============================================================ TREND TRACKING ==
# Post-level crawls for the longitudinal tracker (tracker.py/trends.py) --
# distinct from tiktok()/instagram() above, which only ever aggregate signals
# for a single build and never persist individual posts. Velocity/growth needs
# a STABLE ID per post so the same post seen on day 2 doesn't get double-counted
# as "new" -- tiktok()/instagram() never captured one because they didn't need
# to. These do, and return [] (not sample data) when not live, since a velocity
# series salted with fabricated posts would be worse than a gap in the record.
TREND_TIKTOK_MAX_RESULTS    = int(os.environ.get("TREND_TIKTOK_MAX_RESULTS", "100"))
TREND_INSTAGRAM_MAX_RESULTS = int(os.environ.get("TREND_INSTAGRAM_MAX_RESULTS", "50"))
# Instagram stays at 50 deliberately -- it's supporting evidence only (no
# recency sort on this actor), so there's no case for spending more on it
# than the primary TikTok source.


def crawl_tiktok_posts(keyword, max_results=None):
    """Minimal structured TikTok posts for the longitudinal tracker -- just
    enough to dedupe/date a post and identify its creator (platform, post_id,
    published_at, creator_id). No url/caption/engagement: the tracker only
    needs counts, not post content. creator_id is kept ONLY to detect one
    account dominating a topic's post count (a false emerging signal) -- it's
    never surfaced on a trend card, just used internally to gate classification.
    Returns (posts, hit_cap) -- hit_cap is True when the actor returned >= the
    requested cap, i.e. there were likely more matching posts than we asked
    for, so any count from this crawl should be read as "at least N," not N.

    sort_by="date" (not the aggregate build_radar() path's "relevance"):
    velocity needs the newest posts, not the most engaging ones -- otherwise
    a search capped at N results could fill up entirely with old high-
    engagement posts and never surface what's actually new today.

    Field mapping note (same caveat the README already gives for the aggregate
    path): this actor's docs don't pin down exact field names for the video ID
    or the author's handle, only that they exist ("Video ID... Author profile
    information... Complete metadata from TikTok API"). The names below match
    TikTok's own native aweme-object shape, which this actor is understood to
    pass through -- sanity-check against one real live run before trusting
    this at volume."""
    cap = max_results or TREND_TIKTOK_MAX_RESULTS
    if not live():
        return [], False
    items = _run(TIKTOK_ACTOR, {"query": keyword, "region": GEO, "max_results": cap,
                                "sort_by": "date"})
    posts = []
    for it in items:
        info = it.get("aweme_info") or it
        post_id = str(info.get("aweme_id") or info.get("id") or it.get("id") or "").strip()
        if not post_id:
            continue    # can't dedupe/track without a stable id -- skip rather than guess one
        published_at = _iso_from_ts(info.get("create_time") or info.get("createTime"))
        creator_id = ((info.get("author") or {}).get("uid")
                      or (info.get("author") or {}).get("unique_id")
                      or (it.get("authorMeta") or {}).get("id") or "") or None
        posts.append({"platform": "tiktok", "post_id": post_id, "published_at": published_at,
                      "creator_id": creator_id})
    return posts, len(items) >= cap


def crawl_instagram_posts(hashtag, max_results=None):
    """Minimal structured Instagram posts for the longitudinal tracker -- see
    crawl_tiktok_posts. Returns (posts, hit_cap).

    No sort-by-recency input exists on this actor (checked its documented
    input schema: hashtags, keywordSearch, resultsType, resultsLimit -- no
    order/date field) -- whatever order it returns is what we get. Per the
    same reasoning, its counts aren't used as the primary velocity signal --
    see trends.py."""
    cap = max_results or TREND_INSTAGRAM_MAX_RESULTS
    if not live():
        return [], False
    items = _run(INSTAGRAM_ACTOR, {"hashtags": [hashtag], "resultsLimit": cap})
    posts = []
    for it in items:
        post_id = str(it.get("id") or it.get("shortCode") or "").strip()
        if not post_id:
            continue
        published_at = _iso_from_ts(it.get("timestamp") or it.get("takenAt") or it.get("takenAtTimestamp"))
        creator_id = it.get("ownerId") or it.get("ownerUsername") or it.get("username") or None
        posts.append({"platform": "instagram", "post_id": post_id, "published_at": published_at,
                      "creator_id": creator_id})
    return posts, len(items) >= cap


# Reference-lookup/dictionary-site search intent -- a term whose Google
# results are dominated by this isn't telling us anything about a craft
# trend, it's telling us the phrase is ambiguous with an unrelated genre of
# content (crossword-clue sites, dictionary definition pages, etc). Used to
# both filter corroboration noise and screen discovery candidates.
BLOCKED_SEARCH_INTENT = ("crossword", "puzzle", "dictionary", "clue", "definition",
                         "synonym", "wordle")


def is_blocked_search_intent(text):
    t = (text or "").lower()
    return any(w in t for w in BLOCKED_SEARCH_INTENT)


def trend_corroboration(topic):
    """DataForSEO (or the free fallback) rising-query check for `topic` --
    validation only, per the brief: this never supplies a post/view count,
    it only says whether Google search interest agrees with what the social
    crawl found. None on total failure (never fabricated). Rising queries
    matching BLOCKED_SEARCH_INTENT are dropped before corroboration is
    computed -- a topic whose "agreement" is a pile of crossword-clue pages
    isn't actually corroborated by anything."""
    rising = _dataforseo_rising(topic) or _trends_rising(topic)
    if rising is None:
        return None
    terms = [(rq.get("query") or "").lower() for rq in rising if rq.get("query")]
    terms = [t for t in terms if not is_blocked_search_intent(t)]
    canon_topic = re.sub(r"[^a-z]", "", topic.lower())
    corroborated = any(canon_topic and canon_topic in re.sub(r"[^a-z]", "", t) for t in terms)
    return {"corroborated": corroborated, "rising_terms": terms[:10]}
