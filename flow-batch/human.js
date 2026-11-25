// Human-like input layer for the Google Flow page (never the ZAPI extension page's own clicks).
// Loaded once from flow_match.js via install(). It patches chromium.launchPersistentContext so every page gets:
//   - Locator.click / Page.click moved along a curved, jittery mouse path with small pauses
//   - a one-time "fresh Library" step right after the Flow project loads (see fresh_library.js)
//   - a one-time browsing-history warm-up in the same browser, before Flow opens (warmHistory below)
//   - a one-time "Random delay" 10-25 sec setup in the ZAPI extension panel, right after it opens (see ext_delay.js)
// Switch off: FLOW_HUMAN=off (everything), FLOW_FRESH_LIBRARY=off (only the library step), FLOW_HISTORY=off (only the history
// warm-up), FLOW_RANDOM_DELAY=off (only the delay setup). Delay range: FLOW_DELAY_MIN / FLOW_DELAY_MAX (default 10 / 25).
const { chromium } = require("playwright");

const rand = (a, b) => a + Math.random() * (b - a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pause = (a, b) => sleep(rand(a, b));
const isExt = (page) => /^chrome-extension:/.test(page.url());

// Cubic Bezier path with ease-in-out speed, small jitter, random step delays.
async function moveTo(page, x, y) {
  const from = page.__pos || { x: rand(100, 600), y: rand(100, 400) };
  const dx = x - from.x, dy = y - from.y;
  const steps = Math.max(12, Math.min(60, Math.round(Math.hypot(dx, dy) / 14)));
  const c1 = { x: from.x + dx * rand(0.2, 0.4) + rand(-40, 40), y: from.y + dy * rand(0.1, 0.4) + rand(-40, 40) };
  const c2 = { x: from.x + dx * rand(0.6, 0.85) + rand(-25, 25), y: from.y + dy * rand(0.6, 0.9) + rand(-25, 25) };
  for (let i = 1; i <= steps; i++) {
    const k = i / steps;
    const t = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
    const u = 1 - t;
    const j = i < steps ? 0.8 : 0;
    const px = u * u * u * from.x + 3 * u * u * t * c1.x + 3 * u * t * t * c2.x + t * t * t * x + rand(-j, j);
    const py = u * u * u * from.y + 3 * u * u * t * c1.y + 3 * u * t * t * c2.y + t * t * t * y + rand(-j, j);
    await page.mouse.move(px, py);
    await sleep(rand(3, 12));
  }
  page.__pos = { x, y };
}

async function type(page, text) {
  for (const ch of String(text)) {
    await page.keyboard.type(ch);
    await sleep(rand(60, 170) + (Math.random() < 0.08 ? rand(150, 400) : 0));
  }
}

async function wander(page) {
  const v = page.viewportSize() || { width: 1440, height: 900 };
  for (let i = 0; i < 2; i++) {
    await moveTo(page, rand(200, v.width - 200), rand(150, v.height - 200));
    await pause(300, 900);
  }
}

// Move like a person, then let Playwright do the real (actionability-checked) click at the same point.
async function humanClick(origClick, loc, opts) {
  const page = loc.page();
  if (isExt(page) || (opts && (opts.position || opts.force || opts.trial))) return origClick.call(loc, opts);
  let args = opts;
  try {
    await loc.scrollIntoViewIfNeeded({ timeout: 5000 });
    const b = await loc.boundingBox();
    if (b && b.width > 0 && b.height > 0) {
      const px = b.x + b.width * rand(0.3, 0.7), py = b.y + b.height * rand(0.3, 0.7);
      await pause(120, 450);
      await moveTo(page, px, py);
      await pause(60, 220);
      args = Object.assign({}, opts, { position: { x: px - b.x, y: py - b.y }, delay: Math.round(rand(40, 130)) });
    }
  } catch (e) { /* fall back to the plain click below, same errors as before */ }
  return origClick.call(loc, args);
}

function patchProtos(page) {
  const L = Object.getPrototypeOf(page.locator("html"));
  const oc = L.click;
  L.click = function (opts) { return humanClick(oc, this, opts); };
  const P = Object.getPrototypeOf(page);
  if (typeof P.click === "function") {
    P.click = function (sel, opts) {
      const o = Object.assign({}, opts); const loose = o.strict === false; delete o.strict;
      const l = this.locator(sel);
      return (loose ? l.first() : l).click(o);
    };
  }
}

// One-time browsing-history warm-up in the same browser, before Flow opens: a few ordinary sites with a pause and some mouse
// movement on each, so the profile is not completely empty. Runs without cookies (the Flow login is added afterwards).
// Best effort: a site that does not load is skipped, nothing here can stop the run.
async function warmHistory(ctx) {
  if (process.env.FLOW_HISTORY === "off") return;
  const sites = ["https://www.google.com/", "https://en.wikipedia.org/wiki/Main_Page", "https://www.youtube.com/"];
  let page = null;
  try {
    page = await ctx.newPage();
    for (const u of sites) {
      try {
        await page.goto(u, { waitUntil: "domcontentloaded", timeout: 20000 });
        await pause(1500, 3500);
        await wander(page);
        await pause(800, 2000);
      } catch (e) { /* skip this site */ }
    }
    console.log("Browsing history warm-up done (FLOW_HISTORY=off to skip)");
  } catch (e) {
    console.log("::warning::History warm-up skipped: " + String(e && e.message).slice(0, 120));
  }
  try { if (page && ctx.pages().length > 1) await page.close(); } catch (e) {}
}

const lib = { done: false };
const extDelay = { done: false };
let patched = false;
function hookPage(page) {
  if (!patched) { patched = true; patchProtos(page); }
  const goto = page.goto.bind(page);
  page.goto = async (url, o) => {
    const r = await goto(url, o);
    if (!lib.done && /labs\.google|flow\.google/.test(String(url)) && process.env.FLOW_FRESH_LIBRARY !== "off") {
      try {
        lib.done = await require("./fresh_library").ensureFreshLibrary(page, { moveTo, pause, type, wander, rand });
      } catch (e) {
        console.log("::warning::Fresh library step failed: " + String(e && e.message).slice(0, 160));
        lib.done = true; // never create a second library after a half-finished attempt
      }
    }
    // The ZAPI extension panel: set the Random delay fields once, before the queue is started.
    if (!extDelay.done && /^chrome-extension:/.test(String(url)) && process.env.FLOW_RANDOM_DELAY !== "off") {
      extDelay.done = true;
      try {
        await require("./ext_delay").applyRandomDelay(page, { moveTo, pause, rand });
      } catch (e) {
        console.log("::warning::Random delay step failed: " + String(e && e.message).slice(0, 160));
      }
    }
    return r;
  };
}

let installed = false;
function install() {
  if (installed || process.env.FLOW_HUMAN === "off") return;
  installed = true;
  const launch = chromium.launchPersistentContext.bind(chromium);
  chromium.launchPersistentContext = async (...args) => {
    const ctx = await launch(...args);
    ctx.pages().forEach(hookPage);
    ctx.on("page", hookPage);
    await warmHistory(ctx);
    return ctx;
  };
  console.log("Human input layer on (FLOW_HUMAN=off to disable)");
}

module.exports = { install, moveTo, type, pause, wander, rand };
