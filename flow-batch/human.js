// Human-like input layer for the Google Flow page (never the ZAPI extension page's own clicks).
// Loaded once from flow_match.js via install(). It patches chromium.launchPersistentContext so every page gets:
//   - Locator.click / Page.click moved along a curved, jittery mouse path. Move time grows with distance and shrinks with target size
//     (Fitts-style, about 0.2 to 1.5 s), the speed curve is slightly front-loaded, and about 30% of long moves overshoot the target by a
//     few pixels and then correct back onto it
//   - before about 35% of clicks the cursor first goes to a random spot on the page and rests there; about 25% of clicks hover-wiggle
//     a few pixels on the target before pressing
//   - the real cursor position is tracked (also after plain clicks), so the next move never starts from a stale spot
//   - a one-time "fresh Library" step right after the Flow project loads (see fresh_library.js)
//   - a one-time "Random delay" 10-25 sec setup in the ZAPI extension panel, right after it opens (see ext_delay.js)
// One line "Human layer summary: ..." is printed when the browser closes (clicks, detours, overshoots, wiggles).
// Switch off: FLOW_HUMAN=off (everything), FLOW_FRESH_LIBRARY=off (only the library step), FLOW_RANDOM_DELAY=off (only the delay
// setup). Delay range: FLOW_DELAY_MIN / FLOW_DELAY_MAX (default 10 / 25).
const { chromium } = require("playwright");

const rand = (a, b) => a + Math.random() * (b - a);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pause = (a, b) => sleep(rand(a, b));
const isExt = (page) => /^chrome-extension:/.test(page.url());
const vp = (page) => page.viewportSize() || { width: 1440, height: 900 };

const stats = { clicks: 0, plain: 0, detours: 0, overshoots: 0, wiggles: 0, moves: 0, moveMs: 0 };
let statsLogged = false;
function logStats() {
  if (statsLogged) return;
  statsLogged = true;
  console.log("Human layer summary: humanized clicks " + stats.clicks + " | plain clicks (extension page / fixed position) " + stats.plain +
    " | detours " + stats.detours + " | overshoots " + stats.overshoots + " | hover wiggles " + stats.wiggles +
    " | mouse moves " + stats.moves + " (" + Math.round(stats.moveMs / 1000) + " s of cursor travel)");
}

// One mouse action at a time per page, so two mouse actions can never interleave between a move and its click.
function exclusive(page, fn) {
  const run = (page.__q || Promise.resolve()).then(fn);
  page.__q = run.catch(() => {});
  return run;
}

// One smooth cubic Bezier glide from a to b taking about `dur` ms. Time based, so the duration holds whatever the protocol latency is.
async function glide(page, a, b, dur, straight) {
  const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
  const nx = -dy / d, ny = dx / d; // unit normal of the line
  const curv = straight ? 0 : rand(-0.16, 0.16) * Math.min(d, 600);
  const c1 = { x: a.x + dx * rand(0.25, 0.4) + nx * curv, y: a.y + dy * rand(0.25, 0.4) + ny * curv };
  const c2 = { x: a.x + dx * rand(0.6, 0.8) + nx * curv * rand(0.3, 1), y: a.y + dy * rand(0.6, 0.8) + ny * curv * rand(0.3, 1) };
  const t0 = Date.now();
  for (;;) {
    const k = Math.min(1, (Date.now() - t0) / dur);
    const kk = Math.pow(k, 0.85); // a little faster at the start, longer slow approach at the end
    const t = kk * kk * kk * (kk * (kk * 6 - 15) + 10); // minimum-jerk easing
    const u = 1 - t;
    const j = k < 1 ? 0.7 * (1 - k) : 0; // hand tremor, fades out so the last point is exact
    const x = u * u * u * a.x + 3 * u * u * t * c1.x + 3 * u * t * t * c2.x + t * t * t * b.x + rand(-j, j);
    const y = u * u * u * a.y + 3 * u * u * t * c1.y + 3 * u * t * t * c2.y + t * t * t * b.y + rand(-j, j);
    await page.mouse.move(x, y);
    stats.moves++;
    if (k >= 1) break;
    await sleep(rand(4, 10));
  }
  stats.moveMs += Date.now() - t0;
}

// Moves the cursor from where it really is to (x, y). o.size = width of the target (bigger target = faster), o.noOvershoot = false/true.
// Not exclusive by itself: callers wrap it (moveTo below, humanClick).
async function moveRaw(page, x, y, o) {
  o = o || {};
  const v = vp(page);
  const from = page.__pos || { x: rand(100, 600), y: rand(100, 400) };
  const dx = x - from.x, dy = y - from.y;
  const dist = Math.hypot(dx, dy);
  if (dist < 1.5) { page.__pos = { x, y }; page.__lastAct = Date.now(); return; }
  const w = Math.max(10, o.size || 40);
  const dur = clamp(rand(0.85, 1.25) * (180 + 150 * Math.log2(1 + dist / w)), 160, 1500);
  const over = !o.noOvershoot && dist > 120 && Math.random() < 0.3;
  if (over) {
    const od = rand(7, 16);
    const tx = clamp(x + (dx / dist) * od + rand(-3, 3), 2, v.width - 2);
    const ty = clamp(y + (dy / dist) * od + rand(-3, 3), 2, v.height - 2);
    stats.overshoots++;
    await glide(page, from, { x: tx, y: ty }, dur * 0.92, false);
    await sleep(rand(40, 130));
    await glide(page, { x: tx, y: ty }, { x, y }, rand(120, 260), true);
  } else {
    await glide(page, from, { x, y }, dur, false);
  }
  page.__pos = { x, y };
  page.__lastAct = Date.now();
}

const moveTo = (page, x, y, o) => exclusive(page, () => moveRaw(page, x, y, o));

async function type(page, text) {
  for (const ch of String(text)) {
    await page.keyboard.type(ch);
    await sleep(rand(60, 170) + (Math.random() < 0.08 ? rand(150, 400) : 0));
  }
  page.__lastAct = Date.now();
}

async function wander(page) {
  const v = vp(page);
  for (let i = 0; i < 2; i++) {
    await moveTo(page, rand(200, v.width - 200), rand(150, v.height - 200));
    await pause(300, 900);
  }
}

// A random spot on the page, at least 150 px away from both the cursor and the target.
function waypoint(page, from, to) {
  const v = vp(page);
  for (let i = 0; i < 8; i++) {
    const p = { x: rand(90, v.width - 90), y: rand(90, v.height - 90) };
    if (Math.hypot(p.x - from.x, p.y - from.y) > 150 && Math.hypot(p.x - to.x, p.y - to.y) > 150) return p;
  }
  return { x: clamp((from.x + to.x) / 2 + rand(-120, 120), 60, v.width - 60), y: clamp((from.y + to.y) / 2 + rand(-120, 120), 60, v.height - 60) };
}

// A few tiny moves around the target, ending exactly on it.
async function hoverWiggle(page, x, y) {
  const n = 2 + Math.floor(Math.random() * 2);
  for (let i = 0; i < n; i++) {
    await page.mouse.move(x + rand(-5, 5), y + rand(-4, 4), { steps: 2 });
    await pause(60, 220);
  }
  await page.mouse.move(x, y, { steps: 2 });
  stats.wiggles++;
}

// Move like a person, then let Playwright do the real (actionability-checked) click at the same point.
async function humanClick(origClick, loc, opts) {
  const page = loc.page();
  const plain = isExt(page) || (opts && (opts.position || opts.force || opts.trial));
  return exclusive(page, async () => {
    page.__lastAct = Date.now();
    try {
      if (plain) {
        // No extra movement (extension page / caller fixed the point), but remember where the cursor ends up.
        let b = null;
        if (!(opts && opts.trial)) { try { b = await loc.boundingBox({ timeout: 1500 }); } catch (e) {} }
        const r = await origClick.call(loc, opts);
        if (b && b.width > 0 && b.height > 0) {
          const o = opts && opts.position;
          page.__pos = o ? { x: b.x + o.x, y: b.y + o.y } : { x: b.x + b.width / 2, y: b.y + b.height / 2 };
        }
        stats.plain++;
        return r;
      }
      let args = opts;
      try {
        await loc.scrollIntoViewIfNeeded({ timeout: Math.min(5000, (opts && opts.timeout) || 5000) });
        const b = await loc.boundingBox();
        if (b && b.width > 0 && b.height > 0) {
          const tx = b.x + b.width * rand(0.3, 0.7), ty = b.y + b.height * rand(0.3, 0.7);
          const from = page.__pos || { x: rand(100, 600), y: rand(100, 400) };
          if (Math.random() < 0.35) {
            const wp = waypoint(page, from, { x: tx, y: ty });
            await moveRaw(page, wp.x, wp.y, { noOvershoot: true, size: 120 });
            await pause(150, 700);
            stats.detours++;
          }
          await pause(120, 450);
          await moveRaw(page, tx, ty, { size: Math.min(b.width, b.height) });
          if (Math.random() < 0.25) await hoverWiggle(page, tx, ty);
          await pause(60, 220);
          args = Object.assign({}, opts, { position: { x: tx - b.x, y: ty - b.y }, delay: Math.round(rand(40, 130)) });
          stats.clicks++;
        }
      } catch (e) { /* fall back to the plain click below, same errors as before */ }
      return await origClick.call(loc, args);
    } finally {
      page.__lastAct = Date.now();
    }
  });
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

const lib = { done: false };
const extDelay = { done: false };
let patched = false;
function hookPage(page) {
  if (!patched) { patched = true; patchProtos(page); }
  const goto = page.goto.bind(page);
  page.goto = async (url, o) => {
    const r = await goto(url, o);
    if (!lib.done && /labs\.google|flow\.google/.test(String(url)) && process.env.FLOW_FRESH_LIBRARY !== "off") {
      page.__noIdle = (page.__noIdle || 0) + 1;
      try {
        lib.done = await require("./fresh_library").ensureFreshLibrary(page, { moveTo, pause, type, wander, rand });
      } catch (e) {
        console.log("::warning::Fresh library step failed: " + String(e && e.message).slice(0, 160));
        lib.done = true; // never create a second library after a half-finished attempt
      } finally {
        page.__noIdle -= 1;
        page.__lastAct = Date.now();
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
    const close = ctx.close.bind(ctx);
    ctx.close = async (...a) => { logStats(); return close(...a); };
    return ctx;
  };
  process.once("exit", logStats);
  console.log("Human input layer on (FLOW_HUMAN=off to disable)");
}

module.exports = { install, moveTo, type, pause, wander, rand };
