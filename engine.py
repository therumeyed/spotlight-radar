"""
radar/engine.py — The Radar's brain: collect -> rank -> 5 ideas, cached daily.

Pipeline (a daily read):
  1. collect()      pull signals from the 3 sources (Google Trends, TikTok, Instagram)
  2. rank_trends()  DEFINE "trending" = velocity + cross-source agreement, then rank
  3. make_ideas()   turn the top trends into exactly 5 social content-idea cards
                    (Claude Haiku when ANTHROPIC_API_KEY is set, else a grounded
                    rule-based fallback — either way grounded in the real trends)

"Trending" (our chosen definition, per the brief): a sub-topic is trending when it
shows recent momentum (velocity) on at least one source, and it is trending MORE
strongly when independent sources agree — a breakout that TikTok, Instagram and
Google Trends all show is a stronger, earlier signal than one channel alone.
"""
import datetime as _dt
import json
import os
import re
import urllib.request

import sources
import store
import trends as trend_tracker   # aliased: every function below already uses `trends` as a local
                                  # parameter name for the ranked-trend-cluster list (unrelated concept)

MODEL = os.environ.get("RADAR_MODEL", os.environ.get("ASSISTANT_MODEL", "claude-haiku-4-5-20251001"))
_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CACHE = os.path.join(_DATA, "daily.json")


# --------------------------------------------------------------- collect -------
def collect():
    """Gather raw signals from all three sources. Marks the read live vs sample."""
    g, t, i = sources.google_trends(), sources.tiktok(), sources.instagram()
    is_sample = any(s.get("sample") for s in (g + t + i))
    return {
        "live": sources.live() and not is_sample,
        "sample": is_sample or not sources.live(),
        "topic": sources.TOPIC,
        "geo": sources.GEO,
        "sources": {"google_trends": g, "tiktok": t, "instagram": i},
    }


# ------------------------------------------------- rank (define "trending") ----
_SUFFIXES = ["ofinstagram", "tiktok", "australia", "aus", "ideas", "idea", "kits", "kit",
             "patterns", "pattern", "projects", "project", "making", "tutorial",
             "aesthetic", "art", "diy", "ers", "ing", "s"]


def _canon(term):
    """Collapse hashtag/keyword variants to a shared root so the same trend on
    different sources clusters together (punch needle kit / punchneedle /
    punchneedleart -> punchneedle-ish)."""
    t = re.sub(r"[^a-z]", "", (term or "").lower())
    changed = True
    while changed and len(t) > 5:
        changed = False
        for suf in _SUFFIXES:
            if t.endswith(suf) and len(t) - len(suf) >= 4:
                t = t[:-len(suf)]; changed = True; break
    return t


def _same(a, b):
    if not a or not b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return short == long or (len(short) >= 5 and long.startswith(short)) or a[:6] == b[:6]


def rank_trends(collected, top=6):
    """Cluster signals across sources into ranked trends. Score = strongest velocity
    seen, boosted when independent sources corroborate."""
    signals = []
    for src, arr in collected["sources"].items():
        signals.extend(arr)
    signals.sort(key=lambda s: -s.get("score", 0))

    clusters = []
    for s in signals:
        c = next((c for c in clusters if _same(c["_canon"], _canon(s["term"]))), None)
        if c is None:
            c = {"_canon": _canon(s["term"]), "term": s["term"], "signals": [],
                 "sources": set(), "metrics": [], "links": [], "example": None}
            clusters.append(c)
        c["signals"].append(s)
        c["sources"].add(s["source"])
        c["metrics"].append(s["metric"])
        c["links"].append({"source": s["source"], "url": s["url"]})
        # keep one real crawled post for this trend: signals arrive sorted by score
        # descending, so the first example seen already belongs to the strongest signal
        if s.get("example") and c["example"] is None:
            c["example"] = s["example"]
        # prefer a readable, spaced label (Google Trends queries read best)
        if s["source"] == "google_trends" and " " in s["term"]:
            c["term"] = s["term"]

    trends = []
    for c in clusters:
        base = max(s["score"] for s in c["signals"])
        agree = len(c["sources"])
        score = min(1.0, round(base + 0.12 * (agree - 1), 3))
        # two-speed classification (per the brief)
        fast = ("tiktok" in c["sources"] or "instagram" in c["sources"]) and base >= 0.8
        trends.append({
            "term": c["term"],
            "score": score,
            "base": round(base, 3),
            "agreement": agree,
            "sources": sorted(c["sources"]),
            "speed": "right-now" if fast else "building",
            "window": "about a week" if fast else "6-12 weeks",
            "metrics": c["metrics"][:3],
            "links": c["links"][:3],
            "example": c["example"],
        })
    trends.sort(key=lambda t: (-t["score"], -t["agreement"], -t["base"]))
    return trends[:top]


# ------------------------------------------------------- 5 ideas (Haiku) -------
_PLAYS = ["Organic social", "Creator campaign", "Publisher partnership"]
_SRC_NAME = {"google_trends": "Google Trends", "tiktok": "TikTok", "instagram": "Instagram"}


def _srcs(sources):
    return ", ".join(_SRC_NAME.get(s, s) for s in sources)


def _velocity_note(term):
    """A short, real clause of longitudinal growth for `term`, from the
    trend-velocity tracker (trends.py) -- if that topic is being tracked and
    has cleared the baseline-collection period. "" (never fabricated) when
    tracking isn't configured, the term isn't tracked, or it's still
    establishing a baseline -- a single-crawl idea must never claim a growth
    rate it doesn't actually have on record."""
    if not trend_tracker.enabled():
        return ""
    try:
        snap = trend_tracker.latest(term)
    except Exception:
        return ""
    if not snap or snap["classification"] == "collecting_baseline":
        return ""
    growth = snap.get("growth_pct_24h")
    growth_txt = ("%+.0f%% vs the previous 24h" % growth) if growth is not None else "new activity"
    return (" Longitudinal tracking: %s (%s, %d new posts/%d creators in the last 24h)."
            % (snap["classification"].replace("_", " "), growth_txt,
               snap["new_posts_24h"], snap["unique_creators_24h"]))


def _llm_ideas(topic, trends):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    facts = "\n".join(
        "- %s (score %.2f, %s, on %s) — %s%s" % (
            t["term"], t["score"], t["speed"], _srcs(t["sources"]), t["metrics"][0],
            _velocity_note(t["term"]))
        for t in trends)
    system = (
        "You are the content strategist behind 'The Radar' at a social media agency, briefing a "
        "creator who is filming TODAY. From TODAY'S micro-trends within '%s' (each is already a "
        "specific, niche signal from Google Trends, TikTok or Instagram — NOT the parent category), "
        "write EXACTLY 5 concrete, immediately-postable pieces of content for organic Meta/TikTok.\n"
        "Hard rules:\n"
        "1. Each idea is ONE specific piece of content someone could film or design today — never a "
        "content pillar, theme, or roundup. Reject anything as broad as 'craft ideas for everyone' "
        "or 'trends this week'.\n"
        "2. The exact micro-trend term must appear in the title, verbatim.\n"
        "3. Name the platform + format in the title (e.g. '15-sec TikTok Reel', 'Instagram carousel', "
        "'duet/stitch').\n"
        "4. 'why_now' must end with the literal opening hook or caption line to use, in quotes.\n"
        "5. When a trend's line below includes 'Longitudinal tracking' data (real measured growth "
        "over time, not just today's snapshot), cite the actual number — the growth percentage or "
        "new-post count — in why_now instead of a vague 'trending' claim.\n"
        "Ground everything in the trends provided — never invent a trend, never generalize a "
        "micro-trend back up to the parent topic. Return ONLY a JSON array of 5 objects with keys: "
        "\"title\" (max 12 words; names the platform/format AND the exact micro-trend term), "
        "\"signal\" (what's trending, one line), \"why_now\" (the right-now moment or building-trend "
        "window, plus the literal hook/caption line in quotes), \"play\" (one of: Organic social, "
        "Creator campaign, Publisher partnership). No prose outside the JSON." % topic)
    body = json.dumps({
        "model": MODEL, "max_tokens": 900, "system": system,
        "messages": [{"role": "user", "content": "TODAY'S TRENDS:\n%s" % facts}],
    }).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        m = re.search(r"\[.*\]", text, re.S)
        ideas = json.loads(m.group(0) if m else text)
        return ideas[:5] if isinstance(ideas, list) else None
    except Exception:
        return None


_HOOKS_FAST = [
    "Open cold on the finished “%s” in the first second, then rewind to show how.",
    "Text-on-screen hook: “%s is trending — here's how in 60 seconds.”",
    "Duet/stitch a top “%s” post and react with your own version.",
]
_HOOKS_BUILD = [
    "Carousel: slide 1 teases “%s”, slides 2–5 walk through the how-to.",
    "Save-this-for-later caption anchored on “%s”, posted as a Reel with materials pinned in the comments.",
]


def _rule_ideas(trends):
    """Grounded fallback when no AI key: one concrete, platform-specific post per
    top trend — a single filmable/postable concept naming the exact micro-trend,
    not a category-level statement."""
    ideas = []
    for i, t in enumerate(trends[:5]):
        fast = t["speed"] == "right-now"
        play = "Organic social" if fast else ("Creator campaign" if t["score"] >= 0.85 else "Publisher partnership")
        platform = "TikTok" if "tiktok" in t["sources"] else ("Instagram" if "instagram" in t["sources"] else "Google-driven Reel")
        hook = (_HOOKS_FAST[i % len(_HOOKS_FAST)] if fast else _HOOKS_BUILD[i % len(_HOOKS_BUILD)]) % t["term"]
        title = ("%s Reel: “%s” in 15 seconds" % (platform, t["term"])) if fast else \
                ("%s carousel: the “%s” how-to" % (platform, t["term"]))
        why = ("Right-now moment — %s across %s. Post within %s or it's stale. Hook: %s" %
               ("breakout" if t["base"] >= 0.9 else "rising", _srcs(t["sources"]), t["window"], hook)) if fast else \
              ("Building trend on %s — get ahead before it's common knowledge. Window: %s. Hook: %s" %
               (_srcs(t["sources"]), t["window"], hook))
        why += _velocity_note(t["term"])
        ideas.append({"title": title, "signal": t["metrics"][0], "why_now": why, "play": play})
    return ideas


def _match_trend(idea, trends, used):
    """Which trend is this idea actually about? Match by content (the idea's
    title/signal text) rather than list position — an LLM idea's order isn't
    guaranteed to follow `trends`, so a positional pairing can attach the
    wrong link/source to an idea."""
    text = _canon((idea.get("title") or "") + " " + (idea.get("signal") or ""))
    for t in trends:
        if t["term"] not in used and _canon(t["term"]) and _canon(t["term"]) in text:
            return t
    return next((t for t in trends if t["term"] not in used), trends[0] if trends else None)


def make_ideas(topic, trends):
    ideas = _llm_ideas(topic, trends) or _rule_ideas(trends)
    used = set()
    for idea in ideas:
        t = _match_trend(idea, trends, used)
        if t is None:
            continue
        used.add(t["term"])
        idea["link"] = (t["links"][0] if t.get("links") else {}).get("url", "")
        idea["source_of_signal"] = _srcs(t["sources"])
        idea["channel"] = "TikTok" if "tiktok" in t["sources"] else \
            ("Instagram" if "instagram" in t["sources"] else "Google Trends")
        idea["example"] = t.get("example")
        if idea["example"]:
            idea["example"]["example_reason"] = (
                "Highest-engagement real %s post currently using this trend, out of the posts crawled today."
                % idea["example"]["example_source"].title())
    return ideas[:5]


# --------------------------------------------------------- daily orchestration -
def build_radar():
    collected = collect()
    trends = rank_trends(collected)
    ideas = make_ideas(collected["topic"], trends)
    if not collected["sample"]:
        # Live mode: never present an idea as ready-to-pitch without a real,
        # clickable example behind it — suppress rather than show a gap.
        ideas = [i for i in ideas if i.get("example")]
    return {
        "date": _dt.date.today().isoformat(),
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "topic": collected["topic"],
        "geo": collected["geo"],
        "live": collected["live"],
        "sample": collected["sample"],
        "ideas": ideas,
        "trends": trends,
        "raw": collected["sources"],
        "ai": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }


def _load_today_cache():
    """This process's own same-day cache — NOT the history archive. Purely to
    avoid rebuilding (and re-billing Apify) on every request within one day
    when no database is configured; overwritten daily, never read for a past
    date. The durable, cross-restart archive is store.py (Postgres)."""
    try:
        with open(_CACHE) as f:
            cached = json.load(f)
        if cached.get("date") == _dt.date.today().isoformat():
            return cached
    except (OSError, ValueError):
        pass
    return None


def _save_today_cache(result):
    try:
        os.makedirs(_DATA, exist_ok=True)
        with open(_CACHE, "w") as f:
            json.dump(result, f, indent=1)
    except OSError:
        pass


def get_report(date_str):
    """A specific past day's report, exactly as generated — never regenerates,
    so a historical read's ideas and examples stay stable. Backed by Postgres
    (store.py) — the only durable copy, since Render's free-tier disk does not
    survive redeploys. None if not saved, or if no database is configured."""
    return store.load(date_str, sources.TOPIC, sources.GEO)


def available_dates(year, month):
    """ISO dates in this year/month that have a saved report, for the history
    calendar. Empty (not an error) when DATABASE_URL isn't configured."""
    prefix = "%04d-%02d-" % (year, month)
    return sorted(d for d in store.list_dates(sources.TOPIC, sources.GEO) if d.startswith(prefix))


def daily(force=False):
    """Today's radar. Checked in order: Postgres (survives restarts/redeploys,
    when configured), then this process's own same-day cache, then a fresh
    build — saved to both so today's read doesn't get rebuilt (and re-billed)
    on every request even before the database copy exists."""
    today = _dt.date.today().isoformat()
    if not force:
        stored = store.load(today, sources.TOPIC, sources.GEO)
        if stored:
            return stored
        cached = _load_today_cache()
        if cached:
            return cached
    result = build_radar()
    store.save(today, sources.TOPIC, sources.GEO, result)
    _save_today_cache(result)
    return result
