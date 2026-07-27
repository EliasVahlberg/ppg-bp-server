"use strict";
/* Status page. Polls /api/v1/status and renders it.
   No framework: the payload is small and the page is a handful of tables. */

const el = (id) => document.getElementById(id);

/* Cached payload, so a failed refresh shows the last known state marked stale
   rather than an empty page. Losing sight of the numbers is worse than showing
   slightly old ones, as long as the staleness is visible. */
let last = null;
let lastAt = null;

function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 90) return Math.round(seconds) + " s ago";
  const m = seconds / 60;
  if (m < 90) return Math.round(m) + " min ago";
  const h = m / 60;
  if (h < 48) return Math.round(h) + " h ago";
  return Math.round(h / 24) + " d ago";
}

function fmtInt(n) {
  return n === null || n === undefined ? "-" : n.toLocaleString("en-GB");
}

function fmtClock(epoch) {
  if (!epoch) return "-";
  return new Date(epoch * 1000).toLocaleString("sv-SE", { dateStyle: "short", timeStyle: "short" });
}

function tile(key, value, sub, state) {
  const cls = state ? " is-" + state : "";
  return (
    '<div class="tile' + cls + '"><span class="k">' + key + "</span>" +
    '<span class="v">' + value + (sub ? " <small>" + sub + "</small>" : "") + "</span></div>"
  );
}

const DAY = 86400;

function renderSubject(s) {
  /* Four tiles, not six. Each one is something that can prompt an action:
     is data still arriving, is the cuff about to overwrite, is there enough
     calibration data. Thresholds mirror status.py so the two cannot disagree. */
  const recAge = s.last_session_age_s;
  const recState = recAge === null ? "warn" : recAge > 2 * DAY ? "error" : "ok";
  const cuffAge = s.last_cuff_transfer_age_s;
  const cuffState = cuffAge === null ? "warn" : cuffAge > 14 * DAY ? "error" : "ok";
  const est = s.estimated_unsynced_cuff;
  const estState = est === null ? "" : est >= 100 ? "error" : est >= 70 ? "warn" : "ok";
  const pairState = s.pairs === 0 ? "warn" : s.pairs < 20 ? "" : "ok";

  return (
    '<div class="subject">' +
    (s.subject_id ? '<h3><span class="who">' + s.subject_id + "</span></h3>" : "") +
    '<div class="tiles">' +
    tile("Last recording", fmtAge(recAge), "", recState) +
    tile("Last cuff sync", fmtAge(cuffAge), "", cuffState) +
    tile("Cuff buffer", est === null ? "-" : "~" + Math.round(est), "of 100", estState) +
    tile("Pairs", fmtInt(s.pairs), "of 20", pairState) +
    "</div>" +
    '<p class="sub-detail">' + fmtInt(s.sessions) + " recordings, " + s.recorded_hours +
    " h. " + fmtInt(s.cuff_readings) + " cuff readings" +
    (s.cuff_per_day ? " at " + s.cuff_per_day + "/day" : "") + ".</p>" +
    "</div>"
  );
}

function renderWarnings(ws) {
  if (!ws || ws.length === 0) {
    return '<div class="all-clear">No warnings. Collection looks healthy.</div>';
  }
  return ws
    .map(
      (w) =>
        '<div class="w w-' + w.level + '"><span class="lvl">' + w.level + "</span> " +
        '<span class="msg">' + (w.subject_id ? w.subject_id + ": " : "") + w.message + "</span>" +
        (w.detail ? '<div class="det">' + w.detail + "</div>" : "") +
        "</div>"
    )
    .join("");
}

function renderTables(d) {
  const sessions = d.recent_sessions
    .map(
      (s) =>
        "<tr><td>" + s.id + "</td><td>" + fmtClock(s.start_time) + "</td>" +
        '<td class="num">' + (s.duration_s ? Math.round(s.duration_s / 60) + " min" : "-") + "</td>" +
        "<td>" + (s.subject_id || "-") + "</td><td>" + (s.status || "-") + "</td>" +
        '<td class="num">' + s.notes + "</td></tr>"
    )
    .join("");

  const cuff = d.recent_cuff
    .map(
      (c) =>
        "<tr><td>" + c.taken_at + "</td>" +
        '<td class="num">' + c.sys + "/" + c.dia + "</td>" +
        '<td class="num">' + c.pulse + "</td><td>" + c.subject_id + "</td>" +
        '<td class="num">' + (c.clock_offset_s === null ? "-" : c.clock_offset_s + " s") + "</td>" +
        "<td>" +
        (c.clock_valid ? "" : '<span class="flag">clock</span> ') +
        (c.clock_suspect ? '<span class="flag">suspect</span> ' : "") +
        (c.irregular ? "irregular " : "") +
        (c.movement ? "movement" : "") +
        "</td></tr>"
    )
    .join("");

  const markers = d.markers.length
    ? d.markers
        .map(
          (m) =>
            "<tr><td>" + fmtClock(m.ts) + "</td><td>" + m.event.replace("calibration_", "") +
            "</td><td>" + (m.name || "-") + "</td><td>" + (m.tags || "-") +
            '</td><td class="num">' + m.session_id + "</td></tr>"
        )
        .join("")
    : '<tr><td colspan="5"><em>No calibration markers yet</em></td></tr>';

  return (
    "<table><caption>Recent recordings</caption><tr><th>ID</th><th>Started</th><th>Length</th>" +
    "<th>Subject</th><th>Status</th><th>Notes</th></tr>" + sessions + "</table>" +
    "<table><caption>Recent cuff readings</caption><tr><th>Taken (cuff clock)</th><th>mmHg</th>" +
    "<th>Pulse</th><th>Subject</th><th>Offset</th><th>Flags</th></tr>" + cuff + "</table>" +
    "<table><caption>Calibration markers</caption><tr><th>Time</th><th>Event</th><th>Name</th>" +
    "<th>Tags</th><th>Session</th></tr>" + markers + "</table>"
  );
}

/* Charts are fetched separately from status: a slow series query must not delay
   the warnings, which are the part that matters when something is wrong. */
async function loadCharts() {
  const days = parseInt(el("range").value, 10);
  try {
    const r = await fetch("/api/v1/series?days=" + days, { credentials: "same-origin" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    renderCharts(await r.json(), days);
  } catch (e) {
    el("charts").innerHTML = '<p class="empty">Charts unavailable: ' + e.message + "</p>";
  }
}

function card(title, body, why) {
  /* Explanations live behind a per-card toggle rather than above every chart.
     On a phone, three lines of preamble push the chart itself off the screen, and
     a chart that needs a paragraph to be read is usually the wrong chart. */
  return (
    '<div class="card"><div class="card-head"><h3>' + title + "</h3>" +
    (why ? '<button class="why" type="button" aria-expanded="false" ' +
           'aria-label="About this chart">?</button>' : "") +
    "</div>" + body +
    (why ? '<p class="note" hidden>' + why + "</p>" : "") +
    "</div>"
  );
}

function renderCharts(d, days) {
  /* One set per subject. Merging subjects would draw a plausible line that means
     nothing, and a chart is where that mistake stops being visible. */
  const subjects = [...new Set(d.cuff.map((p) => p.subject_id))];
  if (!subjects.length) subjects.push(null);

  let html = "";
  subjects.forEach((sid) => {
    const cuff = sid ? d.cuff.filter((p) => p.subject_id === sid) : [];
    const pairs = sid ? d.pairs.filter((p) => p.subject_id === sid) : d.pairs;
    const cov = sid ? d.coverage.filter((p) => p.subject_id === sid) : d.coverage;
    if (subjects.length > 1) html += "<h2>" + sid + "</h2>";
    html += card("Blood pressure", Charts.bpTrend(cuff),
      "Cuff readings only. The cuff is the sole pressure reference here; no value " +
      "on this page is estimated from PPG.");
    html += card("By hour", Charts.diurnal(cuff),
      "Pump setting and time of day are entangled in this data, so a difference " +
      "between settings may really be morning versus afternoon.");
    html += card("Coverage", Charts.coverage(cov, Math.min(days, 30)),
      "Minutes recorded per day, cuff readings marked above. Empty days are shown " +
      "deliberately.");
    html += card("Pairs", Charts.pairs(pairs),
      "PPG heart rate against cuff pulse for readings taken during a recording. A " +
      "check that both instruments saw the same person at the same time, not a " +
      "calibration curve.");
  });
  if (d.quality.length) {
    html += card("Signal quality", Charts.quality(d.quality),
      "Hourly mean signal quality index. Above 0.8 is usable for analysis.");
  }
  el("charts").innerHTML = html;

  el("charts").querySelectorAll("button.why").forEach((b) => {
    b.addEventListener("click", () => {
      const note = b.closest(".card").querySelector(".note");
      const open = !note.hidden;
      note.hidden = open;
      b.setAttribute("aria-expanded", String(!open));
    });
  });
}

function render(d, stale) {
  el("warnings").innerHTML = renderWarnings(d.warnings);
  el("subjects").innerHTML = d.subjects.length
    ? d.subjects.map(renderSubject).join("")
    : "<p>No data uploaded yet.</p>";
  el("tables").innerHTML = renderTables(d);
  const c = d.clock || {};
  const legacy = c.no_provenance
    ? " " + c.no_provenance + " cuff readings predate clock provenance."
    : "";
  const t = d.totals;
  el("totals").innerHTML =
    "Store: " + fmtInt(t.sessions) + " recordings, " + fmtInt(t.ppg_samples) + " PPG samples, " +
    fmtInt(t.cuff_readings) + " cuff readings, " + fmtInt(t.notes) + " notes." + legacy +
    (d.viewer ? " Signed in as " + d.viewer + "." : "");
  const scope = d.scope && d.scope[0] !== "*" ? d.scope.join(", ") : null;
  el("scope").textContent = scope ? "Showing " + scope : "";
  el("updated").innerHTML = stale
    ? '<span class="stale">Stale — last update ' + fmtClock(lastAt / 1000) + "</span>"
    : "Updated " + fmtClock(Date.now() / 1000);
  el("main").hidden = false;
  el("login").hidden = true;
  el("signout").hidden = false;
}

function showLogin(message) {
  el("login").hidden = false;
  el("main").hidden = true;
  el("signout").hidden = true;
  el("login-error").textContent = message || "";
}

async function load() {
  try {
    const r = await fetch("/api/v1/status", { credentials: "same-origin" });
    if (r.status === 401) return showLogin("");
    /* 503 means the store is locked by an analysis refresh, not that anything is
       wrong. Keep the previous numbers on screen, marked stale. */
    if (!r.ok) throw new Error(r.status === 503 ? "store busy" : "HTTP " + r.status);
    last = await r.json();
    lastAt = Date.now();
    render(last, false);
    if (!el("charts").innerHTML) loadCharts();
  } catch (e) {
    if (last) render(last, true);
    else showLogin("Could not reach the server: " + e.message);
  }
}

el("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const token = el("token").value.trim();
  try {
    const r = await fetch("/app/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token }),
      credentials: "same-origin",
    });
    if (!r.ok) return showLogin("That token was not accepted.");
    el("token").value = "";
    await load();
    await loadCharts();
  } catch (e) {
    showLogin("Sign-in failed: " + e.message);
  }
});

el("refresh").addEventListener("click", () => { load(); loadCharts(); });
el("range").addEventListener("change", loadCharts);
el("signout").addEventListener("click", async () => {
  await fetch("/app/logout", { method: "POST", credentials: "same-origin" });
  last = null;
  showLogin("Signed out.");
});

/* Only in a secure context: a service worker cannot register over plain HTTP,
   and attempting it throws a console error that looks like a bug. */
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

load();
setInterval(load, 60000);
