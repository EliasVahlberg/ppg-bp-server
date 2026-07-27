"use strict";
/* Minimal SVG charts.
 *
 * Hand-rolled rather than pulling in a charting library: the page must work with
 * no CDN (the offline shell would break) and no build step (there is no
 * toolchain on the server), and what is needed here is four chart types with
 * honest axes. Plotly alone is several megabytes over Tailscale.
 *
 * Every chart carries role="img" and an aria-label summarising what it shows,
 * because an SVG scatter is invisible to a screen reader otherwise.
 */

const C = {
  green: "#1EFA8C",
  sage: "#5A7A6E",
  amber: "#F9A825",
  red: "#FF6B6B",
  outline: "#3A453F",
  fg: "#E4EAE7",
  dim: "#B7C2BC",
};

const PAD = { l: 46, r: 12, t: 12, b: 28 };

function svgEl(w, h, label) {
  return (
    '<svg viewBox="0 0 ' + w + " " + h + '" width="100%" height="' + h +
    '" role="img" aria-label="' + esc(label) + '" class="chart">'
  );
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function scale(dmin, dmax, rmin, rmax) {
  if (dmax === dmin) { dmax = dmin + 1; }
  return (v) => rmin + ((v - dmin) / (dmax - dmin)) * (rmax - rmin);
}

function niceTicks(min, max, count) {
  const span = max - min || 1;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const norm = step0 / mag;
  const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(v);
  return out;
}

function axes(w, h, xs, ys, xTicks, yTicks, xFmt, yFmt) {
  let s = "";
  yTicks.forEach((t) => {
    const y = ys(t);
    s += '<line x1="' + PAD.l + '" y1="' + y + '" x2="' + (w - PAD.r) + '" y2="' + y +
         '" stroke="' + C.outline + '" stroke-width="1"/>';
    s += '<text x="' + (PAD.l - 6) + '" y="' + (y + 4) + '" text-anchor="end" class="tick">' +
         esc(yFmt(t)) + "</text>";
  });
  xTicks.forEach((t) => {
    const x = xs(t);
    s += '<line x1="' + x + '" y1="' + PAD.t + '" x2="' + x + '" y2="' + (h - PAD.b) +
         '" stroke="' + C.outline + '" stroke-width="0.5" stroke-dasharray="2 3"/>';
    s += '<text x="' + x + '" y="' + (h - PAD.b + 16) + '" text-anchor="middle" class="tick">' +
         esc(xFmt(t)) + "</text>";
  });
  return s;
}

function legend(items) {
  return (
    '<div class="legend">' +
    items.map((i) => '<span><i style="background:' + i.color + '"></i>' + esc(i.label) + "</span>").join("") +
    "</div>"
  );
}

/* Split a series wherever the time gap exceeds maxGap, so a line is never drawn
   across a period with no data. Connecting two readings a fortnight apart implies
   a trajectory that was never measured, which is the one thing a trend chart must
   not invent. Gaps here are frequent and meaningful: this is intermittent
   self-measurement, not continuous monitoring. */
function segments(points, maxGap) {
  const out = [];
  let cur = [];
  points.forEach((p, i) => {
    if (i > 0 && p.ts - points[i - 1].ts > maxGap) {
      if (cur.length) out.push(cur);
      cur = [];
    }
    cur.push(p);
  });
  if (cur.length) out.push(cur);
  return out;
}

/* A day between cuff readings is normal; more than that is a gap in collection. */
const CUFF_GAP_S = 36 * 3600;
/* Quality is hourly, so three empty buckets means recording actually stopped. */
const QUALITY_GAP_S = 3 * 3600;

const dayFmt = (ts) =>
  new Date(ts * 1000).toLocaleDateString("sv-SE", { month: "2-digit", day: "2-digit" });

/* ---------------------------------------------------------------- BP trend */

/* Systolic and diastolic as separate series with a shaded band between them:
   the gap is pulse pressure, which is worth seeing directly rather than
   inferring from two lines. */
function bpTrend(points, h) {
  h = h || 240;
  const w = 640;
  if (!points.length) return emptyChart("No cuff readings in this window.");
  const t0 = points[0].ts, t1 = points[points.length - 1].ts;
  const lo = Math.min.apply(null, points.map((p) => p.dia)) - 8;
  const hi = Math.max.apply(null, points.map((p) => p.sys)) + 8;
  const xs = scale(t0, t1, PAD.l, w - PAD.r);
  const ys = scale(lo, hi, h - PAD.b, PAD.t);
  const yT = niceTicks(lo, hi, 5);
  const xT = niceTicks(t0, t1, 4);

  let s = svgEl(w, h, "Blood pressure over time, systolic and diastolic, from the cuff");
  s += axes(w, h, xs, ys, xT, yT, dayFmt, (v) => v.toFixed(0));

  // 90 mmHg reference: below this is the hypotension threshold used throughout
  // this project's analysis of standing readings.
  if (lo < 90 && hi > 90) {
    s += '<line x1="' + PAD.l + '" y1="' + ys(90) + '" x2="' + (w - PAD.r) + '" y2="' + ys(90) +
         '" stroke="' + C.red + '" stroke-width="1" stroke-dasharray="4 3" opacity="0.8"/>';
    s += '<text x="' + (w - PAD.r) + '" y="' + (ys(90) - 4) + '" text-anchor="end" class="tick" fill="' +
         C.red + '">90</text>';
  }

  const segs = segments(points, CUFF_GAP_S);
  segs.forEach((seg) => {
    if (seg.length > 1) {
      const band =
        seg.map((p) => xs(p.ts).toFixed(1) + "," + ys(p.sys).toFixed(1)).join(" ") + " " +
        seg.slice().reverse().map((p) => xs(p.ts).toFixed(1) + "," + ys(p.dia).toFixed(1)).join(" ");
      s += '<polygon points="' + band + '" fill="' + C.green + '" opacity="0.10"/>';
    }
    [["sys", C.green], ["dia", C.sage]].forEach(([k, col]) => {
      if (seg.length > 1) {
        s += '<polyline fill="none" stroke="' + col + '" stroke-width="1.6" points="' +
             seg.map((p) => xs(p.ts).toFixed(1) + "," + ys(p[k]).toFixed(1)).join(" ") + '"/>';
      }
      seg.forEach((p) => {
        const flag = k === "sys" && p.sys < 90;
        s += '<circle cx="' + xs(p.ts).toFixed(1) + '" cy="' + ys(p[k]).toFixed(1) +
             '" r="' + (flag ? 3.4 : 2.2) + '" fill="' + (flag ? C.red : col) + '"><title>' +
             esc(new Date(p.ts * 1000).toLocaleString("sv-SE") + "  " + p.sys + "/" + p.dia +
                 " mmHg, pulse " + p.pulse) + "</title></circle>";
      });
    });
  });
  s += "</svg>";
  return s + legend([
    { color: C.green, label: "Systolic" },
    { color: C.sage, label: "Diastolic" },
    { color: C.red, label: "Below 90 mmHg" },
    { color: C.outline, label: segs.length > 1 ? segs.length + " runs, gaps not bridged" : "" },
  ].filter((i) => i.label));
}

/* ---------------------------------------------------------------- diurnal */

/* Hour of day against systolic. This is the chart that makes the project's main
   confound visible: pump setting and time of day are entangled in the existing
   data, so a difference between settings could be a difference between morning
   and afternoon. */
function diurnal(points, h) {
  h = h || 220;
  const w = 640;
  if (!points.length) return emptyChart("No cuff readings in this window.");
  const lo = Math.min.apply(null, points.map((p) => p.dia)) - 8;
  const hi = Math.max.apply(null, points.map((p) => p.sys)) + 8;
  const xs = scale(0, 24, PAD.l, w - PAD.r);
  const ys = scale(lo, hi, h - PAD.b, PAD.t);
  let s = svgEl(w, h, "Systolic and diastolic pressure by hour of day");
  s += axes(w, h, xs, ys, [0, 6, 12, 18, 24], niceTicks(lo, hi, 5),
            (v) => String(v).padStart(2, "0"), (v) => v.toFixed(0));
  points.forEach((p) => {
    const d = new Date(p.ts * 1000);
    const hour = d.getHours() + d.getMinutes() / 60;
    s += '<circle cx="' + xs(hour).toFixed(1) + '" cy="' + ys(p.sys).toFixed(1) +
         '" r="3" fill="' + (p.sys < 90 ? C.red : C.green) + '" opacity="0.85"><title>' +
         esc(d.toLocaleString("sv-SE") + "  " + p.sys + "/" + p.dia) + "</title></circle>";
    s += '<circle cx="' + xs(hour).toFixed(1) + '" cy="' + ys(p.dia).toFixed(1) +
         '" r="2.4" fill="' + C.sage + '" opacity="0.7"/>';
  });
  s += "</svg>";
  return s + legend([
    { color: C.green, label: "Systolic" },
    { color: C.sage, label: "Diastolic" },
  ]);
}

/* --------------------------------------------------------------- coverage */

/* Recorded minutes and cuff readings per day. Gaps are the point, so days with
   no data are drawn as empty slots rather than skipped. */
function coverage(rows, days, h) {
  h = h || 200;
  const w = 640;
  const byDay = {};
  rows.forEach((r) => {
    byDay[r.day] = byDay[r.day] || { minutes: 0, cuff: 0 };
    byDay[r.day].minutes += r.recorded_minutes;
    byDay[r.day].cuff += r.cuff_count;
  });
  const labels = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today.getTime() - i * 86400000);
    labels.push(d.toISOString().slice(0, 10));
  }
  const maxMin = Math.max(10, ...labels.map((d) => (byDay[d] ? byDay[d].minutes : 0)));
  const bw = (w - PAD.l - PAD.r) / labels.length;
  const ys = scale(0, maxMin, h - PAD.b, PAD.t);
  let s = svgEl(w, h, "Recorded minutes and cuff readings per day for the last " + days + " days");
  niceTicks(0, maxMin, 4).forEach((t) => {
    s += '<line x1="' + PAD.l + '" y1="' + ys(t) + '" x2="' + (w - PAD.r) + '" y2="' + ys(t) +
         '" stroke="' + C.outline + '" stroke-width="1"/>' +
         '<text x="' + (PAD.l - 6) + '" y="' + (ys(t) + 4) + '" text-anchor="end" class="tick">' +
         t.toFixed(0) + "</text>";
  });
  labels.forEach((d, i) => {
    const x = PAD.l + i * bw;
    const e = byDay[d];
    const mins = e ? e.minutes : 0;
    /* A day with no recording gets a visible stub rather than a zero-height bar.
       The legend promises a "no data" mark, and an invisible one would make an
       empty day indistinguishable from a day that is off the chart entirely. */
    const y = mins > 0 ? ys(mins) : h - PAD.b - 2;
    s += '<rect x="' + (x + 1).toFixed(1) + '" y="' + y.toFixed(1) + '" width="' +
         Math.max(1, bw - 2).toFixed(1) + '" height="' + Math.max(2, h - PAD.b - y).toFixed(1) +
         '" fill="' + (mins > 0 ? C.green : C.outline) + '" opacity="' + (mins > 0 ? 0.85 : 0.7) +
         '"><title>' + esc(d + ": " + mins.toFixed(0) + " min recorded, " +
         (e ? e.cuff : 0) + " cuff readings") + "</title></rect>";
    if (e && e.cuff > 0) {
      s += '<circle cx="' + (x + bw / 2).toFixed(1) + '" cy="' + (PAD.t + 6) +
           '" r="' + Math.min(5, 2 + e.cuff * 0.5).toFixed(1) + '" fill="' + C.amber + '"><title>' +
           esc(d + ": " + e.cuff + " cuff readings") + "</title></circle>";
    }
    if (i % Math.ceil(labels.length / 8) === 0) {
      s += '<text x="' + (x + bw / 2).toFixed(1) + '" y="' + (h - PAD.b + 16) +
           '" text-anchor="middle" class="tick">' + esc(d.slice(5)) + "</text>";
    }
  });
  s += "</svg>";
  return s + legend([
    { color: C.green, label: "Minutes recorded" },
    { color: C.amber, label: "Cuff readings" },
    { color: C.outline, label: "No data" },
  ]);
}

/* ---------------------------------------------------------------- quality */

/* Signal quality and PPG heart rate. Heart rate is here because it is checkable
   against the cuff's pulse; PPG-derived pressure is deliberately absent. */
function quality(rows, h) {
  h = h || 200;
  const w = 640;
  if (!rows.length) return emptyChart("No processed PPG minutes yet.");
  const t0 = rows[0].ts, t1 = rows[rows.length - 1].ts;
  const xs = scale(t0, t1, PAD.l, w - PAD.r);
  const ys = scale(0, 1, h - PAD.b, PAD.t);
  let s = svgEl(w, h, "PPG signal quality index over time, with the 0.8 usability threshold");
  s += axes(w, h, xs, ys, niceTicks(t0, t1, 4), [0, 0.25, 0.5, 0.75, 1],
            dayFmt, (v) => v.toFixed(2));
  s += '<line x1="' + PAD.l + '" y1="' + ys(0.8) + '" x2="' + (w - PAD.r) + '" y2="' + ys(0.8) +
       '" stroke="' + C.amber + '" stroke-width="1" stroke-dasharray="4 3"/>';
  const pts = rows.filter((r) => r.sqi !== null);
  segments(pts, QUALITY_GAP_S).forEach((seg) => {
    if (seg.length > 1) {
      s += '<polyline fill="none" stroke="' + C.green + '" stroke-width="1.4" points="' +
           seg.map((r) => xs(r.ts).toFixed(1) + "," + ys(r.sqi).toFixed(1)).join(" ") + '"/>';
    }
  });
  pts.forEach((r) => {
    s += '<circle cx="' + xs(r.ts).toFixed(1) + '" cy="' + ys(r.sqi).toFixed(1) +
         '" r="2" fill="' + (r.sqi >= 0.8 ? C.green : C.amber) + '"><title>' +
         esc(new Date(r.ts * 1000).toLocaleString("sv-SE") + "  SQI " + r.sqi +
             (r.hr ? ", HR " + r.hr : "") + ", " + r.minutes + " min") + "</title></circle>";
  });
  s += "</svg>";
  return s + legend([
    { color: C.green, label: "SQI (hourly mean)" },
    { color: C.amber, label: "0.8 usability threshold" },
  ]);
}

/* ------------------------------------------------------------------ pairs */

/* Cuff systolic against the PPG heart rate measured at the same moment. This is
   not a calibration curve and must not be read as one: it is the check that the
   two instruments were pointed at the same person at the same time. */
function pairs(rows, h) {
  h = h || 220;
  const w = 640;
  if (!rows.length) {
    return emptyChart(
      "No calibration pairs yet. A pair needs a cuff reading taken while a " +
      "recording is running, on the same subject, with a trustworthy cuff clock."
    );
  }
  const withHr = rows.filter((r) => r.hr_ppg !== null);
  if (!withHr.length) {
    return emptyChart(rows.length + " pairs, but no processed PPG minutes overlap them yet.");
  }
  const xlo = Math.min.apply(null, withHr.map((r) => r.hr_ppg)) - 5;
  const xhi = Math.max.apply(null, withHr.map((r) => r.hr_ppg)) + 5;
  const ylo = Math.min.apply(null, withHr.map((r) => r.pulse)) - 5;
  const yhi = Math.max.apply(null, withHr.map((r) => r.pulse)) + 5;
  const lo = Math.min(xlo, ylo), hi = Math.max(xhi, yhi);
  const xs = scale(lo, hi, PAD.l, w - PAD.r);
  const ys = scale(lo, hi, h - PAD.b, PAD.t);
  let s = svgEl(w, h, "PPG heart rate against cuff pulse for paired measurements");
  s += axes(w, h, xs, ys, niceTicks(lo, hi, 4), niceTicks(lo, hi, 4),
            (v) => v.toFixed(0), (v) => v.toFixed(0));
  // Identity line: agreement means points sit on it.
  s += '<line x1="' + xs(lo) + '" y1="' + ys(lo) + '" x2="' + xs(hi) + '" y2="' + ys(hi) +
       '" stroke="' + C.sage + '" stroke-width="1" stroke-dasharray="4 3"/>';
  withHr.forEach((r) => {
    s += '<circle cx="' + xs(r.hr_ppg).toFixed(1) + '" cy="' + ys(r.pulse).toFixed(1) +
         '" r="4" fill="' + C.green + '" opacity="0.85"><title>' +
         esc("PPG " + r.hr_ppg + " bpm vs cuff " + r.pulse + " bpm, " + r.sys + "/" + r.dia +
             " mmHg, session " + r.session_id) + "</title></circle>";
  });
  s += "</svg>";
  return s + legend([
    { color: C.green, label: "Paired measurement" },
    { color: C.sage, label: "Perfect agreement" },
  ]);
}

function emptyChart(msg) {
  return '<p class="empty">' + esc(msg) + "</p>";
}

window.Charts = { bpTrend, diurnal, coverage, quality, pairs };
