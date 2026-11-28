// Flow PROJECT handling. One counter and one project link for ALL workflows that use Flow (flow-batch.yml, long_landscape.yml, shorts.yml):
// both live in flow-batch/session/project.json, the folder every one of those workflows already restores and saves (cache prefix
// flow-session-) together with the saved cookies. Flow Login Refresh restores that folder first, so it keeps project.json too.
//
// Order of everything (the repository secret/variable are FALLBACKS only, none of them has to exist):
//   cookies:  saved session (session/cookies.json)  ->  FLOW_COOKIES_B64 secret                      (run_flow.js)
//   project:  saved project (project.json)          ->  FLOW_PROJECT_URL variable  ->  a NEW project   (this file)
//   A new project is created automatically when no project opens (no saved project, no variable, or both are dead/deleted) and the
//   Flow start page shows the "New project" button (= the cookies are valid). Flow's changelog pop-ups ("Get started") are clicked away.
//
// Rotation: every 4-5 Flow runs a brand-new Flow PROJECT is created (with human mouse moves, like the fresh Library step) and used for
// the next runs.
//   - A run is counted ONCE per GitHub run id (pass 1 / pass 2 / fresh sessions of the same workflow run do not count again).
//   - state: { base, url, runs, limit, lastRun }. url = the saved project in use (null = the FLOW_PROJECT_URL project), runs = runs already
//     made in that project (this one included), limit = 4 or 5, picked at random for every project (FLOW_PROJECT_EVERY="4-5" changes the
//     range, "6" = always 6). base is only informational now: changing the variable no longer resets a saved project.
//   - When runs >= limit at the start of a run: open the Flow start page, click "New project" with human mouse moves, wait for the new
//     /project/<id> address, save it. If that fails, the old project stays in use and the next run tries again.
//   - Later page.goto(FLOW_URL) calls of run_flow.js (reloads) are redirected to the project in use.
//   - Never throws into the run: a failure only prints a warning. FLOW_PROJECT_ROTATION=off switches it off.
// Called from fresh_library.js right after the Flow page loaded. Screenshots: debug/new-project-home.png, debug/new-project-created.png.
const fs = require("fs");
const path = require("path");

const SESS_DIR = path.join(__dirname, "session");
const STATE_FILE = path.join(SESS_DIR, "project.json");
const DEBUG_DIR = path.join(__dirname, "debug");
const active = { base: null, url: null };
let handled = false;

function pid(u) {
  const m = /\/project\/([0-9a-zA-Z-]{8,})/.exec(String(u || ""));
  return m ? m[1].toLowerCase() : null;
}
function projectUrl(u) {
  const m = /^(https?:\/\/[^?#]*?\/project\/[0-9a-zA-Z-]+)/.exec(String(u || ""));
  return m ? m[1] : String(u || "");
}
function homeUrl() {
  try { return JSON.parse(fs.readFileSync(path.join(__dirname, "config.json"), "utf8")).flowUrl || "https://labs.google/fx/tools/flow"; }
  catch (e) { return "https://labs.google/fx/tools/flow"; }
}
function everyRange() {
  const m = /^\s*(\d+)\s*(?:-\s*(\d+))?\s*$/.exec(process.env.FLOW_PROJECT_EVERY || "4-5");
  let lo = m ? parseInt(m[1], 10) : 4;
  let hi = m && m[2] ? parseInt(m[2], 10) : lo;
  lo = Math.max(1, lo);
  hi = Math.max(lo, hi);
  return [lo, hi];
}
function pickLimit() {
  const r = everyRange();
  return r[0] + Math.floor(Math.random() * (r[1] - r[0] + 1));
}
function readState() {
  try {
    const j = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
    return j && typeof j === "object" ? j : null;
  } catch (e) { return null; }
}
function writeState(s) {
  try {
    fs.mkdirSync(SESS_DIR, { recursive: true });
    fs.writeFileSync(STATE_FILE, JSON.stringify(s));
  } catch (e) { console.log("::warning::Project rotation: state not saved: " + String(e && e.message).slice(0, 100)); }
}
// The saved project did not open (deleted or unreachable): forget its link, keep the counters.
function forgetSaved() {
  const s = readState();
  if (s && s.url) { s.url = null; writeState(s); }
}

// A goto of the FLOW_PROJECT_URL address (or of the Flow start page) goes to the project in use instead.
function redirect(url) {
  if (!active.url) return url;
  const u = String(url || "");
  if (!/labs\.google|flow\.google/.test(u)) return url;
  // the FLOW_PROJECT_URL address, or the plain Flow start page (no variable set): both go to the project in use
  if (!pid(u) || (active.base && pid(u) === pid(active.base))) return active.url;
  return url;
}
function installRedirect(page) {
  if (page.__projRedirect) return;
  page.__projRedirect = true;
  const prev = page.goto.bind(page);
  page.goto = (u, o) => prev(redirect(u), o);
}

// The "New project" control on Flow's start page: the smallest visible button/link whose label says new/create/start project.
function findNewProject(page) {
  return page.evaluate(() => {
    const RE = /new project|create (a )?(new )?project|start (a )?(new )?project/i;
    let best = null;
    for (const el of document.querySelectorAll('button,[role="button"],a,[tabindex]')) {
      const b = el.getBoundingClientRect();
      if (!b.width || !b.height || b.bottom < 0 || b.top > window.innerHeight) continue;
      const t = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).replace(/\s+/g, " ").trim();
      if (t.length > 80 || !RE.test(t)) continue;
      if (!best || t.length < best.len) best = { x: b.x + b.width / 2, y: b.y + b.height / 2, w: b.width, h: b.height, len: t.length, text: t.slice(0, 40) };
    }
    return best;
  });
}

// Flow's "what's new" pop-ups that cover a freshly loaded project ("A faster, easier-to-reach Flow Agent" -> arrow -> "Smarter search"
// -> "Get started"; the Get started button stays grey until the last page). They sit on top of the sidebar and the prompt bar, so they
// are clicked away: Get started when it is usable, else the forward arrow, else Escape. Never throws, at most 8 clicks.
async function dismissDialogs(page, h) {
  let n = 0;
  let lastWasStart = false;
  await page.waitForTimeout(2500).catch(() => {});
  for (let i = 0; i < 8; i++) {
    const t = await page.evaluate((skipStart) => {
      const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
      const leaf = [...document.querySelectorAll("*")].find((e) => !e.children.length && /view all changelogs/i.test(e.textContent || "") && vis(e));
      if (!leaf) return null;
      let box = leaf;
      for (let k = 0; k < 8 && box.parentElement; k++) { box = box.parentElement; if (/get started/i.test(box.textContent || "")) break; }
      const btns = [...box.querySelectorAll('button,[role="button"]')].filter(vis);
      const label = (e) => ((e.getAttribute("aria-label") || "") + " " + (e.textContent || "")).replace(/\s+/g, " ").trim();
      const pt = (e, what) => { const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2, what }; };
      const gs = btns.find((e) => /^get started$/i.test(label(e)));
      if (gs && !skipStart && !gs.disabled && gs.getAttribute("aria-disabled") !== "true") return pt(gs, "get started");
      const next = btns.find((e) => /forward|next|chevron_right|arrow_right/i.test(label(e)));
      if (next) return pt(next, "next");
      if (gs && !gs.disabled) return pt(gs, "get started");
      const other = btns.find((e) => !/changelog|get started|back|previous|arrow_back|close/i.test(label(e)));
      if (other) return pt(other, "other");
      return { esc: true };
    }, lastWasStart).catch(() => null);
    if (!t) break;
    if (t.esc) { await page.keyboard.press("Escape").catch(() => {}); n++; await page.waitForTimeout(900).catch(() => {}); lastWasStart = false; continue; }
    try {
      if (h && h.moveTo) await h.moveTo(page, t.x + (h.rand ? h.rand(-2, 2) : 0), t.y + (h.rand ? h.rand(-2, 2) : 0));
      await page.mouse.click(t.x, t.y);
    } catch (e) { break; }
    n++;
    lastWasStart = t.what === "get started";
    await page.waitForTimeout(1000).catch(() => {});
  }
  if (n) console.log("Flow pop-ups: " + n + " click(s) to close the 'what's new' dialog");
  return n;
}

// Creates the project. Returns { ok, url }. On any failure the browser goes back to backUrl and { ok: false } is returned.
async function createProject(page, h, backUrl) {
  const oldId = pid(page.url());
  const fr = page.mainFrame(); // Frame.goto: does not go through the patched page.goto, so no second Library step is triggered
  try {
    fs.mkdirSync(DEBUG_DIR, { recursive: true });
    await h.pause(2500, 6000);
    await fr.goto(homeUrl(), { waitUntil: "domcontentloaded" });
    await h.pause(6000, 10000);
    await h.wander(page);
    let btn = null;
    for (let i = 0; i < 12 && !btn; i++) {
      btn = await findNewProject(page).catch(() => null);
      if (!btn) await h.pause(900, 1500);
    }
    await page.screenshot({ path: path.join(DEBUG_DIR, "new-project-home.png") }).catch(() => {});
    if (!btn) throw new Error("no 'New project' button found on " + String(page.url()).slice(0, 80));
    console.log("Project rotation: clicking '" + btn.text + "'");
    await h.pause(500, 1400);
    await h.moveTo(page, btn.x + h.rand(-3, 3), btn.y + h.rand(-2, 2), { size: Math.min(btn.w, btn.h) });
    await h.pause(80, 250);
    await page.mouse.down();
    await h.pause(40, 110);
    await page.mouse.up();
    let id = null;
    for (let i = 0; i < 25 && !id; i++) {
      await h.pause(800, 1200);
      const cur = pid(page.url());
      if (cur && cur !== oldId) { id = cur; break; }
      if (i === 6) {
        // some versions ask for a confirmation first
        const ok = page.locator('[role="dialog"]').locator("visible=true").getByRole("button", { name: /create|continue|start|confirm|done|ok/i }).first();
        if (await ok.count().catch(() => 0)) await ok.click().catch(() => {});
      }
    }
    if (!id) throw new Error("the new project address did not appear after the click");
    await h.pause(5000, 8000);
    await page.screenshot({ path: path.join(DEBUG_DIR, "new-project-created.png") }).catch(() => {});
    return { ok: true, url: projectUrl(page.url()) };
  } catch (e) {
    console.log("::warning::Project rotation: new project not created (" + String(e && e.message).slice(0, 140) + ") - staying in the current project, trying again next run");
    try {
      await fr.goto(backUrl, { waitUntil: "domcontentloaded" });
      await h.pause(7000, 11000);
    } catch (e2) {}
    return { ok: false };
  }
}

async function sidebarVisible(page, ms) {
  try {
    await page.getByText("Library", { exact: true }).locator("visible=true").first().waitFor({ state: "visible", timeout: ms });
    return true;
  } catch (e) { return false; }
}

// No working project is open (no sidebar): try the saved project, then the FLOW_PROJECT_URL project, and when neither opens create a
// NEW project on the Flow start page. Returns true when the browser ends up inside a working project, false when it could not
// (not signed in, or the "New project" button is missing) - the caller then reports "not ready" like before.
async function recover(page, h) {
  if (/accounts\.google\.com|\/about(\/|\?|$)/.test(page.url())) return false; // signed out: cookies were rejected, nothing to create
  const fr = page.mainFrame();
  let here = pid(page.url());
  const st0 = readState();
  const tries = [];
  if (st0 && pid(st0.url)) tries.push({ label: "saved project", url: st0.url, saved: true });
  if (pid(process.env.FLOW_URL)) tries.push({ label: "FLOW_PROJECT_URL project", url: process.env.FLOW_URL, saved: false });
  for (const t of tries) {
    if (pid(t.url) === here) { if (t.saved) forgetSaved(); continue; } // already here and it showed no sidebar: not usable
    try {
      await fr.goto(t.url, { waitUntil: "domcontentloaded" });
      await h.pause(7000, 11000);
    } catch (e) {}
    if (await sidebarVisible(page, 20000)) {
      console.log("Project rotation: using the " + t.label + " " + String(pid(t.url)).slice(0, 8));
      return true;
    }
    console.log("::warning::Project rotation: the " + t.label + " did not open");
    if (t.saved) forgetSaved();
    here = pid(page.url());
  }
  console.log("Project rotation: no working project - creating a new one");
  const res = await createProject(page, h, homeUrl());
  if (!res.ok) return false;
  writeState({ base: process.env.FLOW_URL || homeUrl(), url: res.url, runs: 1, limit: pickLimit(), lastRun: String(process.env.GITHUB_RUN_ID || "local") });
  console.log("Project rotation: new project " + String(pid(res.url)).slice(0, 8) + " saved (no project was available)");
  return true;
}

// Returns false when the Flow sidebar is not there yet (not logged in): the caller tries again after the next page load.
// Returns true once the step is handled (also when nothing had to be done or when it failed).
async function maybeRotate(page, h) {
  if (handled || process.env.FLOW_PROJECT_ROTATION === "off") return true;
  if (!(await sidebarVisible(page, 25000))) {
    console.log("Project rotation: sidebar not visible - looking for a working project");
    let ok = false;
    try { ok = await recover(page, h); }
    catch (e) { console.log("::warning::Project rotation: recovery failed: " + String(e && e.message).slice(0, 140)); }
    if (!ok) { console.log("Project rotation: no project ready yet, skipped for now"); return false; }
  }
  handled = true;
  try { await dismissDialogs(page, h); } catch (e) {}

  const base = process.env.FLOW_URL || homeUrl();
  const runId = String(process.env.GITHUB_RUN_ID || "local");
  let st = readState();
  let due = false;
  if (!st) {
    st = { base, url: null, runs: 1, limit: pickLimit(), lastRun: runId };
    console.log("Project rotation: no saved counter, starting at run 1 of " + st.limit);
  } else if (st.lastRun !== runId) {
    st.lastRun = runId;
    if (st.runs >= st.limit) due = true; else st.runs += 1;
  }

  if (due) {
    console.log("Project rotation: " + st.runs + " runs done in this project (limit " + st.limit + ") - creating a new project");
    const res = await createProject(page, h, st.url || base);
    if (res.ok) {
      st.url = res.url;
      st.runs = 1;
      st.limit = pickLimit();
      console.log("Project rotation: new project " + String(pid(st.url)).slice(0, 8) + " is in use, the next one after " + st.limit + " runs");
      try { await dismissDialogs(page, h); } catch (e) {}
    }
  } else {
    const want = st.url || base;
    if (pid(want) && pid(page.url()) !== pid(want)) {
      console.log("Project rotation: opening the project in use (" + String(pid(want)).slice(0, 8) + ")");
      try {
        await page.mainFrame().goto(want, { waitUntil: "domcontentloaded" });
        await h.pause(7000, 11000);
        if (!(await sidebarVisible(page, 20000)) && st.url) {
          console.log("::warning::Project rotation: the saved project did not open, back to the FLOW_PROJECT_URL project");
          st.url = null;
          await page.mainFrame().goto(base, { waitUntil: "domcontentloaded" });
          await h.pause(7000, 11000);
        }
      } catch (e) {
        console.log("::warning::Project rotation: could not open the project in use: " + String(e && e.message).slice(0, 120));
      }
    }
    console.log("Project rotation: run " + st.runs + " of " + st.limit + " in project " + String(pid(st.url || page.url() || base)).slice(0, 8));
  }

  st.base = base;
  writeState(st);
  active.base = base;
  active.url = st.url || null;
  installRedirect(page);
  return true;
}

module.exports = { maybeRotate, dismissDialogs };
