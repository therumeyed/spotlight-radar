/* The Radar — dashboard logic (vanilla JS, no build step) */
(() => {
  const $ = (id) => document.getElementById(id);
  const SRC_LABEL = { google_trends: "Google Trends", tiktok: "TikTok", instagram: "Instagram" };
  const SRC_ICON = { tiktok: "TT", instagram: "IG" };

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const todayIso = () => new Date().toISOString().slice(0, 10);

  function fmtDate(iso) {
    try {
      return new Date(iso + "T00:00:00").toLocaleDateString(undefined,
        { weekday: "long", day: "numeric", month: "long" });
    } catch (e) { return iso; }
  }

  function fmtTime(iso) {
    try { return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }); }
    catch (e) { return ""; }
  }

  function timeAgo(iso) {
    if (!iso) return "";
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "";
    const days = Math.floor((Date.now() - then) / 86400000);
    if (days <= 0) return "Today";
    if (days === 1) return "Yesterday";
    return `${days}d ago`;
  }

  // ---------------------------------------------------------------- state --
  const state = { report: null, visibleYear: null, visibleMonth: null, activeFilter: "All" };

  // -------------------------------------------------------------- evidence --
  function proofPanel(idea, sample) {
    const ex = idea.example;
    if (ex) {
      const icon = SRC_ICON[ex.example_source] || ex.example_source.slice(0, 2).toUpperCase();
      return `<aside class="proof">
        <div class="proof-label"><span class="eyebrow">Example from the crawl</span><span class="muted">${esc(timeAgo(ex.example_published_at))}</span></div>
        <div class="proof-source">
          <div class="source-icon ${esc(ex.example_source)}">${esc(icon)}</div>
          <div style="min-width:0">
            <div class="proof-title">${esc(ex.example_title || "(no caption)")}</div>
            <div class="proof-meta">${esc(ex.example_author ? "@" + ex.example_author : "")}${ex.example_author ? " · " : ""}${esc(ex.example_metric || "")}</div>
          </div>
        </div>
        <a class="proof-link" href="${esc(ex.example_url)}" target="_blank" rel="noopener noreferrer" aria-label="Open the ${esc(ex.example_source)} example">
          View ${esc(SRC_LABEL[ex.example_source] || ex.example_source)} example →
        </a>
      </aside>`;
    }
    if (sample) {
      return `<aside class="proof proof--muted">
        <div class="proof-label"><span class="eyebrow">Sample mode</span></div>
        <p class="muted">Connect an Apify key to see a real crawled example here — nothing fabricated is shown as evidence.</p>
      </aside>`;
    }
    return `<aside class="proof proof--muted">
      <div class="proof-label"><span class="eyebrow">No strong example found</span></div>
      <p class="muted">Not ready to pitch — no crawled post cleared the confidence bar for this idea today.</p>
    </aside>`;
  }

  function ideaRow(idea, i, sample) {
    return `<article class="idea" data-channel="${esc(idea.channel)}">
      <div class="number">${String(i + 1).padStart(2, "0")}</div>
      <div>
        <div class="idea-head"><span class="format">${esc(idea.channel)}</span><h3>${esc(idea.title)}</h3></div>
        <p>${esc(idea.signal)}</p>
        <div class="why"><strong>Why now</strong><span>${esc(idea.why_now)}</span></div>
        <div class="sources"><span class="muted">Evidence:</span><span class="source-chip">${esc(idea.source_of_signal || "")}</span>
          ${idea.play ? `<span class="source-chip">${esc(idea.play)}</span>` : ""}</div>
      </div>
      ${proofPanel(idea, sample)}
    </article>`;
  }

  function signalRow(t) {
    const status = t.speed === "right-now" ? "Peaking now" : "Building";
    const detail = t.agreement > 1 ? `${t.agreement} sources agree` : `${SRC_LABEL[t.sources[0]] || t.sources[0]} only`;
    return `<div class="signal"><strong>${esc(t.term)}</strong><span>${esc(status)} · ${esc(detail)}</span>
      <div class="meter"><i style="width:${Math.round((t.score || 0) * 100)}%"></i></div></div>`;
  }

  // -------------------------------------------------------------- render --
  function applyFilter() {
    const items = [...$("ideaList").querySelectorAll(".idea")];
    let visible = 0;
    items.forEach((el) => {
      const show = state.activeFilter === "All" || el.dataset.channel === state.activeFilter;
      el.style.display = show ? "" : "none";
      if (show) visible += 1;
    });
    $("emptyState").hidden = visible !== 0;
  }

  function render(report) {
    state.report = report;
    const sample = !!report.sample;

    $("brandSub").textContent = `Daily social trend discovery · ${report.topic || "crafts"} · ${report.geo || "AU"}`;
    const badge = $("modeBadge");
    if (sample) { badge.textContent = "SAMPLE DATA"; badge.className = "badge badge--sample"; }
    else if (report.live) { badge.textContent = "LIVE · APIFY"; badge.className = "badge badge--live"; }
    else { badge.textContent = "OFFLINE"; badge.className = "badge"; }

    const isToday = report.date === todayIso();
    $("updatedAt").textContent = isToday
      ? `Updated today, ${fmtTime(report.generated_at)}`
      : `Generated ${fmtDate(report.date)}, ${fmtTime(report.generated_at)}`;
    $("reportDate").textContent = fmtDate(report.date);
    $("backToToday").hidden = isToday;

    const ideas = report.ideas || [];
    const trends = report.trends || [];
    const sourcesChecked = new Set();
    Object.entries(report.raw || {}).forEach(([src, arr]) => { if ((arr || []).length) sourcesChecked.add(src); });

    $("briefHeadline").textContent = trends.length
      ? `${trends[0].term} is ${trends[0].speed === "right-now" ? "peaking" : "building"} right now`
      : "No strong pattern detected today";
    $("briefBody").textContent = sample
      ? "Showing sample crafts trends so you can see the shape of it — connect an Apify key for a live read."
      : `"Trending" here means recent velocity on a source, boosted when independent sources agree.`;
    $("statIdeas").textContent = ideas.length;
    $("statSources").textContent = sourcesChecked.size || (sample ? 3 : 0);
    $("statSignals").textContent = trends.length;

    $("ideaList").innerHTML = ideas.map((idea, i) => ideaRow(idea, i, sample)).join("") ||
      `<p class="muted">No ideas cleared the bar today.</p>`;

    const channels = [...new Set(ideas.map((i) => i.channel))];
    const filterset = $("filterset");
    if (channels.length > 1) {
      state.activeFilter = "All";
      filterset.hidden = false;
      filterset.innerHTML = [`<button type="button" class="filter" data-filter="All" aria-pressed="true">All ${ideas.length}</button>`]
        .concat(channels.map((c) => `<button type="button" class="filter" data-filter="${esc(c)}" aria-pressed="false">${esc(c)}</button>`))
        .join("");
      [...filterset.querySelectorAll(".filter")].forEach((btn) => btn.addEventListener("click", () => {
        filterset.querySelectorAll(".filter").forEach((b) => b.setAttribute("aria-pressed", "false"));
        btn.setAttribute("aria-pressed", "true");
        state.activeFilter = btn.dataset.filter;
        applyFilter();
      }));
    } else {
      filterset.hidden = true;
      state.activeFilter = "All";
    }
    applyFilter();

    const top3 = trends.slice(0, 3);
    $("signalStrip").hidden = top3.length === 0;
    $("signalPreview").innerHTML = top3.map(signalRow).join("");
  }

  // -------------------------------------------------------------- fetch --
  function fetchToday() {
    return fetch("/api/radar").then((r) => r.json()).then(render).catch(() => {
      $("ideaList").innerHTML = `<p class="muted">Could not load the radar. Is the server running?</p>`;
    });
  }

  function fetchDate(date) {
    return fetch(`/api/radar/${date}`).then((r) => {
      if (!r.ok) throw new Error("no report");
      return r.json();
    }).then(render);
  }

  $("backToToday").addEventListener("click", () => { fetchToday(); });

  // -------------------------------------------------------- history popover --
  const historyButton = $("historyButton");
  const historyPopover = $("historyPopover");
  const scrim = $("scrim");
  const closeButton = $("historyClose");
  const prevButton = $("historyPrev");
  const nextButton = $("historyNext");
  const monthLabel = $("historyMonth");
  const calendar = $("historyCalendar");
  const noteEl = historyPopover.querySelector(".history-note");
  const NOTE_DEFAULT = noteEl.textContent;

  function isoDate(year, month, day) {
    return `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  }

  function renderCalendar(availableDates) {
    const year = state.visibleYear, month = state.visibleMonth;
    monthLabel.textContent = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" })
      .format(new Date(year, month, 1));
    const mondayOffset = (new Date(year, month, 1).getDay() + 6) % 7;
    const days = new Date(year, month + 1, 0).getDate();
    const available = new Set(availableDates || []);
    const selected = state.report ? state.report.date : null;
    const blanks = Array.from({ length: mondayOffset }, () => `<span class="day blank" aria-hidden="true"></span>`).join("");
    const buttons = Array.from({ length: days }, (_, idx) => {
      const day = idx + 1, iso = isoDate(year, month, day);
      const isAvail = available.has(iso), isSel = iso === selected;
      const label = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" })
        .format(new Date(year, month, day));
      return `<button type="button" class="day${isAvail ? " available" : ""}${isSel ? " selected" : ""}" data-date="${iso}"
        aria-label="${isAvail ? "Load report for " : "No report available for "}${label}"${isAvail ? "" : " disabled"}>${day}</button>`;
    }).join("");
    calendar.innerHTML = blanks + buttons;
    calendar.querySelectorAll(".day.available").forEach((btn) => btn.addEventListener("click", () => {
      const date = btn.dataset.date;
      fetchDate(date).then(() => setHistory(false)).catch(() => {
        noteEl.textContent = "Couldn't load that report — try again.";
        setTimeout(() => { noteEl.textContent = NOTE_DEFAULT; }, 4000);
      });
    }));
  }

  function loadMonth() {
    return fetch(`/api/radar/history?year=${state.visibleYear}&month=${state.visibleMonth + 1}`)
      .then((r) => r.json()).then((d) => renderCalendar(d.dates)).catch(() => renderCalendar([]));
  }

  function positionOverlay() {
    const top = `${document.querySelector(".topbar").getBoundingClientRect().height}px`;
    scrim.style.top = top;
    historyPopover.style.top = top;
  }

  function setHistory(open, restoreFocus = true) {
    historyPopover.classList.toggle("open", open);
    scrim.classList.toggle("open", open);
    historyButton.setAttribute("aria-expanded", String(open));
    if (open) {
      positionOverlay();
      const base = state.report ? new Date(state.report.date + "T00:00:00") : new Date();
      state.visibleYear = base.getFullYear();
      state.visibleMonth = base.getMonth();
      loadMonth().then(() => {
        requestAnimationFrame(() => {
          const sel = calendar.querySelector(".day.selected");
          (sel || closeButton).focus();
        });
      });
    } else if (restoreFocus) {
      historyButton.focus();
    }
  }

  historyButton.addEventListener("click", () => setHistory(!historyPopover.classList.contains("open")));
  closeButton.addEventListener("click", () => setHistory(false));
  scrim.addEventListener("click", () => setHistory(false));
  prevButton.addEventListener("click", () => {
    state.visibleMonth -= 1;
    if (state.visibleMonth < 0) { state.visibleMonth = 11; state.visibleYear -= 1; }
    loadMonth();
  });
  nextButton.addEventListener("click", () => {
    state.visibleMonth += 1;
    if (state.visibleMonth > 11) { state.visibleMonth = 0; state.visibleYear += 1; }
    loadMonth();
  });
  historyPopover.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); setHistory(false); return; }
    if (event.key === "Tab") {
      const focusable = [...historyPopover.querySelectorAll("button:not(:disabled)")];
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });

  fetchToday();
})();
