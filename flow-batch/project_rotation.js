// Every 4-5 Flow runs a brand-new Flow PROJECT is created (with human mouse moves, like the fresh Library step) and used for the next runs.
// One counter for ALL workflows that use Flow (flow-batch.yml, long_landscape.yml, shorts.yml): the counter and the link of the current
// project live in flow-batch/session/project.json, the folder every one of those workflows already restores and saves (cache prefix
// flow-session-). No workflow file had to change.
//
// How it works (called from fresh_library.js right after the Flow project loaded and the sidebar is visible):
//   - A run is counted ONCE per GitHub run id (pass 1 / pass 2 / fresh sessions of the same workflow run do not count again).
//   - state: { base, url, runs, limit, lastRun }. base = the FLOW_PROJECT_URL the state belongs to (a changed variable starts over),
//     url = the project in use (null = the base project), runs = runs already made in that project (this one included),
//     limit = 4 or 5, picked at random for every project (FLOW_PROJECT_EVERY="4-5" changes the range, "6" = always 6).
//   - When runs >= limit at the start of a run: open the Flow start page, click "New project" with human mouse moves, wait for the new
//     /project/<id> address, save it. If that fails, the old project stays in use and the next run tries again.
//   - When the project in use is not the one FLOW_URL points to, the browser goes to the project in use first. Later page.goto(FLOW_URL)
//     calls of run_flow.js (reloads) are redirected to it too, so a reload never drops back into the old project.
//   - Never throws into the run: a failure only prints a warning. FLOW_PROJECT_ROTATION=off switches it off.
// The new project keeps Flow's default name. Screenshots: debug/new-project-home.png and debug/new-project-created.png.
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

// A goto of the base project address goes to the project in use instead.
function redirect(url) {
  if (active.url && active.base && pid(url) && pid(url) === pid(active.base)) return active.url;
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

// Returns false when the Flow sidebar is not there yet (not logged in): the caller tries again after the next page load.
// Returns true once the step is handled (also when nothing had to be done or when it failed).
async function maybeRotate(page, h) {
  if (handled || process.env.FLOW_PROJECT_ROTATION === "off") return true;
  if (!(await sidebarVisible(page, 25000))) { console.log("Project rotation: sidebar not visible yet, skipped for now"); return false; }
  handled = true;

  const base = process.env.FLOW_URL || homeUrl();
  const runId = String(process.env.GITHUB_RUN_ID || "local");
  let st = readState();
  let due = false;
  if (!st || st.base !== base) {
    st = { base, url: null, runs: 1, limit: pickLimit(), lastRun: runId };
    console.log("Project rotation: no saved counter for this project, starting at run 1 of " + st.limit);
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
    }
  } else {
    const want = st.url || base;
    if (pid(want) && pid(page.url()) !== pid(want)) {
      console.log("Project rotation: opening the project in use (" + String(pid(want)).slice(0, 8) + ")");
      try {
        await page.mainFrame().goto(want, { waitUntil: "domcontentloaded" });
        await h.pause(7000, 11000);
        if (!(await sidebarVisible(page, 20000)) && st.url) {
          console.log("::warning::Project rotation: the saved project did not open, back to the base project");
          st.url = null;
          await page.mainFrame().goto(base, { waitUntil: "domcontentloaded" });
          await h.pause(7000, 11000);
        }
      } catch (e) {
        console.log("::warning::Project rotation: could not open the project in use: " + String(e && e.message).slice(0, 120));
      }
    }
    console.log("Project rotation: run " + st.runs + " of " + st.limit + " in project " + String(pid(st.url || base)).slice(0, 8));
  }

  writeState(st);
  active.base = base;
  active.url = st.url || null;
  installRedirect(page);
  return true;
}

module.exports = { maybeRotate };
