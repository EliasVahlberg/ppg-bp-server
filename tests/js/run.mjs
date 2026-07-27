/*
 * Tests for the browser code, run headlessly under node.
 *
 * The charts are hand-rolled SVG with no build step, so until now they were only
 * ever verified by loading the page on a phone. That is not a check that can run
 * while the phone is somewhere else, and it is not a check that runs before a
 * deploy. These tests load the real files in a sandbox with the smallest stubs
 * that let them execute, then assert on the SVG string they produce.
 *
 * Run: node tests/js/run.mjs
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const STATIC = path.join(import.meta.dirname, "..", "..", "src", "ppg_pi_server", "static");

let failures = 0;
let passes = 0;

function test(name, fn) {
  try {
    fn();
    passes += 1;
  } catch (e) {
    failures += 1;
    console.error("FAIL " + name + "\n  " + e.message);
  }
}

/* A sandbox with just enough of a browser for these files to evaluate. Anything
   the code actually depends on is stubbed explicitly rather than mocked broadly,
   so a new dependency on the DOM shows up here as a failure instead of passing
   silently. */
function stubEl() {
  return {
    addEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    querySelectorAll: () => [],
    querySelector: () => stubEl(),
    closest: () => stubEl(),
    classList: { add() {}, remove() {}, toggle() {} },
    style: {},
    dataset: {},
    hidden: false,
    value: "",
    textContent: "",
    innerHTML: "",
  };
}

function load(files, { innerWidth = 900 } = {}) {
  const listeners = {};
  const ctx = {
    console,
    Math,
    Date,
    Number,
    JSON,
    Set,
    Map,
    isFinite,
    parseFloat,
    parseInt,
    setInterval: () => 0,
    setTimeout: () => 0,
    fetch: () => Promise.reject(new Error("no network in tests")),
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    document: {
      readyState: "complete",
      addEventListener: (k, f) => (listeners[k] = f),
      /* app.js wires its listeners at load time, so every id it asks for has to
         answer. Returning null instead just proves the stub is incomplete. */
      getElementById: () => stubEl(),
      querySelectorAll: () => [],
      createElementNS: () => ({ setAttribute() {}, appendChild() {} }),
    },
    navigator: { serviceWorker: { register: () => Promise.resolve() } },
    location: { protocol: "http:", host: "x" },
  };
  ctx.window = ctx;
  ctx.window.innerWidth = innerWidth;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  for (const f of files) {
    vm.runInContext(fs.readFileSync(path.join(STATIC, f), "utf8"), ctx, { filename: f });
  }
  return ctx;
}

// ---------------------------------------------------------------------------
// charts.js
// ---------------------------------------------------------------------------

const day = 86400;
const t0 = 1785000000;

function cuff(n, { step = day / 3, sys = 120 } = {}) {
  return Array.from({ length: n }, (_, i) => ({
    ts: t0 + i * step,
    sys: sys + (i % 5) - 2,
    dia: 78 + (i % 3),
    pulse: 70,
    subject_id: "s1",
  }));
}

test("charts.js exposes every chart the page asks for", () => {
  const { Charts } = load(["charts.js"]);
  for (const k of ["bpTrend", "diurnal", "coverage", "quality", "pairs"]) {
    assert.equal(typeof Charts[k], "function", k + " missing");
  }
});

test("a short window plots individual readings", () => {
  const { Charts } = load(["charts.js"]);
  const svg = Charts.bpTrend(cuff(12));
  assert.match(svg, /<svg/);
  assert.match(svg, /Systolic</);
  assert.doesNotMatch(svg, /daily median/);
});

test("a long window summarises per day instead of per reading", () => {
  /* The reason this exists: three readings a day for two months drew 180 spikes
     into 300 pixels and was unreadable on a phone. */
  const { Charts } = load(["charts.js"]);
  const svg = Charts.bpTrend(cuff(180));
  assert.match(svg, /daily median/);
  assert.match(svg, /Daily range/);
});

test("aggregation never invents or drops a day", () => {
  const { Charts } = load(["charts.js"]);
  const pts = cuff(60);
  const days = new Set(pts.map((p) => new Date(p.ts * 1000).toDateString())).size;
  const svg = Charts.bpTrend(pts);
  const circles = (svg.match(/<circle/g) || []).length;
  // Two series, systolic and diastolic, one point each per distinct local day.
  assert.equal(circles, days * 2, "expected " + days + " days x 2 series, got " + circles);
});

test("a day whose readings straddle 90 is flagged, not averaged away", () => {
  /* A day containing a sub-90 reading has to stay visible even when its median is
     above 90, because the low readings are the clinically interesting ones. */
  const { Charts } = load(["charts.js"]);
  const pts = [];
  for (let d = 0; d < 20; d += 1) {
    for (const [h, sys] of [[8, 130], [13, 128], [19, 85]]) {
      pts.push({ ts: t0 + d * day + h * 3600, sys, dia: 80, pulse: 70, subject_id: "s1" });
    }
  }
  const svg = Charts.bpTrend(pts);
  assert.match(svg, /daily median/);
  assert.match(svg, /Below 90/);
  // The red flag colour must actually appear on a point.
  assert.match(svg, /#FF6B6B/);
});

test("no data produces an empty state rather than a broken chart", () => {
  const { Charts } = load(["charts.js"]);
  assert.match(Charts.bpTrend([]), /class="empty"/);
  assert.match(Charts.bpTrend([]), /^(?!.*<svg)/s);
  assert.match(Charts.pairs([]), /class="empty"/);
});

test("gaps in collection are not bridged by a line", () => {
  const { Charts } = load(["charts.js"]);
  const near = cuff(6);
  const far = cuff(6, { step: day / 3 }).map((p) => ({ ...p, ts: p.ts + 30 * day }));
  const svg = Charts.bpTrend(near.concat(far));
  // More than one polyline per series means the run was split at the gap.
  const lines = (svg.match(/<polyline/g) || []).length;
  assert.ok(lines >= 4, "expected split runs, got " + lines + " polylines");
});

test("a narrow screen draws fewer gridlines than a wide one", () => {
  const wide = load(["charts.js"], { innerWidth: 1200 }).Charts.bpTrend(cuff(12));
  const narrow = load(["charts.js"], { innerWidth: 400 }).Charts.bpTrend(cuff(12));
  // Gridlines are dashed lines; ticks are the text labels beside them.
  const count = (s) => (s.match(/stroke-dasharray="2 3"/g) || []).length +
    (s.match(/<text/g) || []).length;
  assert.ok(count(narrow) < count(wide), "narrow " + count(narrow) + " vs wide " + count(wide));
});

test("values are escaped, so a device name cannot inject markup", () => {
  const { Charts } = load(["charts.js"]);
  const pts = cuff(4).map((p) => ({ ...p, subject_id: '<img src=x onerror="alert(1)">' }));
  const svg = Charts.bpTrend(pts);
  assert.doesNotMatch(svg, /<img/);
});

test("NaN in a series does not produce a broken path", () => {
  /* Real derived rows carry NaN where a minute had no usable pulse. The server
     maps those to null; the chart still has to survive one arriving. */
  const { Charts } = load(["charts.js"]);
  const q = Array.from({ length: 10 }, (_, i) => ({
    ts: t0 + i * 3600,
    sqi: i === 4 ? null : 0.9,
    n: 60,
  }));
  const svg = Charts.quality(q);
  assert.doesNotMatch(svg, /NaN/);
});

// ---------------------------------------------------------------------------
// app.js
// ---------------------------------------------------------------------------

test("app.js loads without a DOM present", () => {
  const ctx = load(["charts.js", "app.js"]);
  assert.equal(typeof ctx.unpairedLine, "function");
});

test("the unpaired line names each reason it is given", () => {
  const { unpairedLine } = load(["charts.js", "app.js"]);
  const line = unpairedLine({
    unpaired: { no_overlap: 2, clock_never_read: 1, clock_invalid: 0, clock_suspect: 0 },
    nearest_miss_s: 480,
  });
  assert.match(line, /2 outside a recording/);
  assert.match(line, /1 with no cuff clock/);
  assert.match(line, /8 min/);
});

test("a whole-hour miss is reported in hours, a small one in minutes", () => {
  /* The magnitude is the diagnosis: minutes means a mistimed measurement, hours
     means a clock or timezone fault. Reporting 7200 s as "120 min" hides that. */
  const { unpairedLine } = load(["charts.js", "app.js"]);
  const hours = unpairedLine({ unpaired: { no_overlap: 1 }, nearest_miss_s: 7200 });
  assert.match(hours, /2\.0 h/);
  const mins = unpairedLine({ unpaired: { no_overlap: 1 }, nearest_miss_s: 300 });
  assert.match(mins, /5 min/);
});

test("nothing unpaired means no line at all", () => {
  const { unpairedLine } = load(["charts.js", "app.js"]);
  assert.equal(unpairedLine({ unpaired: {}, nearest_miss_s: null }), "");
  assert.equal(unpairedLine({}), "");
});

console.log((failures ? "FAILED" : "ok") + ": " + passes + " passed, " + failures + " failed");
process.exit(failures ? 1 : 0);
