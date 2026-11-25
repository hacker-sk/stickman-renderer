// Flow runner WITHOUT the ZAPI extension ("direct mode").
// Instead of the extension queue, every prompt is typed into Flow's own prompt box, sent, and the run waits for that one image before it
// does the next prompt. Same login, same saved session, same settings check (applyFlowSettings), same output as run_flow.js:
//   downloads/final/scene_NN.<ext> + downloads/final/manifest.json (+ downloads/rejected, debug/), so the make job needs no change.
//
// Switch: flow-batch/runner.txt holds the script the workflow runs (run_flow_direct.js or run_flow.js). To go back to the extension
// runner put "run_flow.js" in runner.txt (or delete the file), or restore the branch backup/zapi-extension-flow.
//
// How the image is captured: no downloads. The network responses of the Flow page are watched; while a prompt is waiting, every NEW image
// response (png/jpeg/webp, at least 30 KB, requested ratio) is a candidate and the largest one is saved as scene_NN. Everything seen is
// written to debug/direct-images.json, so a run always shows what was captured.
//
// Optional env: FLOW_DIRECT_MINUTES (time budget, default 18), FLOW_PROMPT_WAIT_SECONDS (per prompt, default 150),
// FLOW_PAUSE_MIN / FLOW_PAUSE_MAX (seconds between prompts, default 5 / 12), FLOW_MIN_WIDTH (reject smaller images, default 0 = off),
// FLOW_BLOCK_RETRIES (default 2, max 2).
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const crypto = require("crypto");
const { chromium } = require("playwright");
const { applyFlowSettings, resolveAspect, matchesAspect } = require("./flow_helpers");
const { detectOverGeneration, computeBatchStatus, validateModel } = require("./flow_utils");
require("./ext_settings"); // loaded only so the same helper modules (human input layer ...) are active as in run_flow.js

const cfg = JSON.parse(fs.readFileSync(path.join(__dirname, "config.json"), "utf8"));
const FLOW_URL = process.env.FLOW_URL || cfg.flowUrl;
const DL_DIR = path.resolve(__dirname, "downloads");
const RAW_DIR = path.join(DL_DIR, "raw");
const FINAL_DIR = path.join(DL_DIR, "final");
const REJECTED_DIR = path.join(DL_DIR, "rejected");
const DEBUG_DIR = path.resolve(__dirname, "debug");
const SESS_DIR = path.resolve(__dirname, "session");
const SESS_FILE = path.join(SESS_DIR, "cookies.json");
const PROMPTS_FILE = path.resolve(__dirname, process.env.PROMPTS_FILE || "prompts/batch.json");
for (const d of [RAW_DIR, FINAL_DIR, DEBUG_DIR]) fs.mkdirSync(d, { recursive: true });

const NL = String.fromCharCode(10);
const SCRIPT_START = Date.now();
const BUDGET_MS = (parseInt(process.env.FLOW_DIRECT_MINUTES || "18", 10) || 18) * 60000;
const PROMPT_WAIT_MS = (parseInt(process.env.FLOW_PROMPT_WAIT_SECONDS || "150", 10) || 150) * 1000;
const PAUSE_MIN = parseFloat(process.env.FLOW_PAUSE_MIN || "5") || 5;
const PAUSE_MAX = Math.max(PAUSE_MIN, parseFloat(process.env.FLOW_PAUSE_MAX || "12") || 12);
const MIN_WIDTH = parseInt(process.env.FLOW_MIN_WIDTH || "0", 10) || 0;
const SETTLE_MS = 6000; // after the first candidate image: wait a little for a larger version of the same image
const BLOCK_RETRIES = (() => { const n = parseInt(process.env.FLOW_BLOCK_RETRIES || "2", 10); return Number.isNaN(n) ? 2 : Math.max(0, Math.min(n, 2)); })();
const timeLeft = () => SCRIPT_START + BUDGET_MS - Date.now();
const rand = (a, b) => a + Math.random() * (b - a);

const WANT = Object.assign({ agent: "off", model: "Nano Banana Lite", aspect: "portrait", count: 1 }, cfg.flowSettings || {});
if ((process.env.FLOW_MODEL || "").trim()) WANT.model = process.env.FLOW_MODEL.trim();
if ((process.env.FLOW_ASPECT || "").trim()) WANT.aspect = process.env.FLOW_ASPECT.trim();
if (WANT.count !== 1) { console.log("Direct mode keeps one image per prompt: image count set to 1 (was " + WANT.count + ")"); WANT.count = 1; }
const ASP = resolveAspect(WANT.aspect);
if (!ASP) throw new Error("Unknown aspect '" + WANT.aspect + "'. Use portrait, landscape, square, 3:4 or 4:3 (or 9:16 / 16:9 / 1:1).");
const MODEL_CHECK = validateModel(WANT.model, cfg.allowedModels);
if (!MODEL_CHECK.ok) throw new Error("Invalid model: " + MODEL_CHECK.reason);
if ((process.env.THUMB_PROMPTS_FILE || "").trim()) console.log("::notice::THUMB_PROMPTS_FILE is ignored in direct mode (no thumbnail pass)");

const BLOCK_RE = /noticed some unusual activity/i;
const FAIL_RE = /have not been charged/gi;

const EVID = { mode: "direct", timeline: [], console: [], pageErrors: [], http: [], requestFailed: [], flow: {}, results: [] };
function mark(ev) { EVID.timeline.push({ t: new Date().toISOString(), event: ev }); }
function redact(s) { return String(s || "").replace(/[A-Za-z0-9_-]{40,}/g, "[redacted]").slice(0, 300); }
function safeUrl(u) { try { const x = new URL(u); return x.host + x.pathname; } catch (e) { return redact(u); } }
function normUrl(u) { try { const x = new URL(u); return x.origin + x.pathname; } catch (e) { return String(u).split("?")[0]; } }
function sha(buf) { return crypto.createHash("sha256").update(buf).digest("hex"); }
function attachDiagnostics(pg) {
  pg.on("console", (m) => { if (m.type() === "error" && EVID.console.length < 100) EVID.console.push({ t: new Date().toISOString(), text: redact(m.text()) }); });
  pg.on("pageerror", (e) => { if (EVID.pageErrors.length < 50) EVID.pageErrors.push(redact(e && e.message)); });
  pg.on("requestfailed", (r) => { if (EVID.requestFailed.length < 100) EVID.requestFailed.push({ t: new Date().toISOString(), method: r.method(), url: safeUrl(r.url()), reason: redact(r.failure() && r.failure().errorText) }); });
  pg.on("response", async (r) => {
    try {
      if (r.status() < 400 || EVID.http.length >= 100) return;
      const row = { t: new Date().toISOString(), status: r.status(), method: r.request().method(), url: safeUrl(r.url()) };
      if (new URL(r.url()).host.indexOf("google") !== -1) { const b = await r.text().catch(() => ""); row.body = redact(b); }
      EVID.http.push(row);
    } catch (e) {}
  });
}
async function snapshot(pg, name) {
  const t = await pg.locator("body").innerText().catch(() => "");
  fs.writeFileSync(path.join(DEBUG_DIR, name + "-text.txt"), t.slice(0, 4000));
  await pg.screenshot({ path: path.join(DEBUG_DIR, name + ".png"), timeout: 10000 }).catch(() => {});
  return { url: safeUrl(pg.url()), title: await pg.title().catch(() => "") };
}
function writeEvidence() { fs.writeFileSync(path.join(DEBUG_DIR, "flow-network.json"), JSON.stringify(EVID, null, 2)); }

// .json: array of strings or {prompt}. .txt: prompts separated by a blank line. Each prompt is flattened to one line.
function loadPrompts() {
  const text = fs.readFileSync(PROMPTS_FILE, "utf8").replace(/\r/g, "");
  let list;
  if (PROMPTS_FILE.endsWith(".txt")) list = text.split(/\n[ \t]*\n/);
  else list = JSON.parse(text).map((p) => (typeof p === "string" ? p : p.prompt));
  return list.filter(Boolean).map((p) => p.replace(/\s*\n\s*/g, " ").trim()).filter(Boolean);
}

// Same cookie handling as run_flow.js (Playwright storageState, Cookie-Editor export, plain or gzipped base64). Never logs values.
function loadCookies() {
  const b64 = (process.env.FLOW_COOKIES_B64 || "").trim();
  if (!b64) throw new Error("FLOW_COOKIES_B64 secret is missing");
  let buf = Buffer.from(b64, "base64");
  if (buf[0] === 0x1f && buf[1] === 0x8b) buf = zlib.gunzipSync(buf);
  let data;
  try { data = JSON.parse(buf.toString("utf8")); }
  catch (e) { throw new Error("FLOW_COOKIES_B64 is not valid base64 JSON (plain or gzipped)"); }
  const list = Array.isArray(data) ? data : data.cookies;
  if (!Array.isArray(list) || !list.length) throw new Error("FLOW_COOKIES_B64 contains no cookies");
  const ss = { no_restriction: "None", none: "None", lax: "Lax", strict: "Strict" };
  const out = [];
  for (const c of list) {
    if (!c || !c.name || !c.domain) continue;
    const host = String(c.domain).replace(/^\./, "");
    const secure = !!c.secure;
    let sameSite = ss[String(c.sameSite || "").toLowerCase()];
    if (sameSite === "None" && !secure) sameSite = "Lax";
    const exp = c.expires !== undefined ? c.expires : c.expirationDate;
    const cookie = { name: c.name, value: c.value === undefined ? "" : String(c.value), httpOnly: !!c.httpOnly, secure };
    if (sameSite) cookie.sameSite = sameSite;
    cookie.expires = (typeof exp === "number" && exp > 0 && !c.session) ? Math.floor(exp) : -1;
    if (c.hostOnly === true || /^__Host-/.test(c.name)) {
      cookie.url = "https://" + host + "/";
    } else {
      cookie.domain = String(c.domain);
      cookie.path = c.path || "/";
    }
    out.push(cookie);
  }
  if (!out.length) throw new Error("No usable cookies found in FLOW_COOKIES_B64");
  const names = new Set(out.map((c) => c.name));
  console.log("Cookies loaded:", out.length, "| has SID:", names.has("SID") || names.has("__Secure-1PSID"));
  return out;
}
function loadSavedCookies() {
  try {
    const a = JSON.parse(fs.readFileSync(SESS_FILE, "utf8"));
    return Array.isArray(a) && a.length ? a : null;
  } catch (e) { return null; }
}
function saveCookies(list) {
  fs.mkdirSync(SESS_DIR, { recursive: true });
  fs.writeFileSync(SESS_FILE, JSON.stringify(list.filter((c) => /google\./.test(c.domain || ""))));
}

function imgType(buf) {
  if (buf.length > 8 && buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) return ".png";
  if (buf.length > 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) return ".jpg";
  if (buf.length > 12 && buf.toString("ascii", 0, 4) === "RIFF" && buf.toString("ascii", 8, 12) === "WEBP") return ".webp";
  return null;
}
function imageSize(buf) {
  try {
    if (buf.length > 24 && buf[0] === 0x89 && buf[1] === 0x50) return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) };
    if (buf[0] === 0xff && buf[1] === 0xd8) {
      let i = 2;
      while (i + 9 < buf.length) {
        if (buf[i] !== 0xff) { i++; continue; }
        const m = buf[i + 1];
        if (m === 0xff) { i++; continue; }
        if (m === 0xd8 || m === 0x01 || (m >= 0xd0 && m <= 0xd7)) { i += 2; continue; }
        if (m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc) return { h: buf.readUInt16BE(i + 5), w: buf.readUInt16BE(i + 7) };
        i += 2 + buf.readUInt16BE(i + 2);
      }
    }
    if (buf.length > 30 && buf.toString("ascii", 0, 4) === "RIFF" && buf.toString("ascii", 8, 12) === "WEBP") {
      const fmt = buf.toString("ascii", 12, 16);
      if (fmt === "VP8X") return { w: 1 + buf.readUIntLE(24, 3), h: 1 + buf.readUIntLE(27, 3) };
      if (fmt === "VP8 ") return { w: buf.readUInt16LE(26) & 0x3fff, h: buf.readUInt16LE(28) & 0x3fff };
      if (fmt === "VP8L") { const b = buf.readUInt32LE(21); return { w: (b & 0x3fff) + 1, h: ((b >> 14) & 0x3fff) + 1 }; }
    }
  } catch (e) {}
  return null;
}
function clearDir(dir) {
  if (!fs.existsSync(dir)) return;
  for (const e of fs.readdirSync(dir)) fs.rmSync(path.join(dir, e), { recursive: true, force: true });
}

// ---- image capture (network responses of the Flow page) ----
const SEEN_URLS = new Set();   // every image url the page has loaded; only urls first seen while a prompt waits are candidates
const SAVED_HASHES = new Set();
const CAND_LOG = [];           // debug/direct-images.json
let cur = null;                // the prompt that is waiting: { idx, cands: [], first }

function onResponse(r) {
  (async () => {
    try {
      if (r.status() !== 200) return;
      const ct = String(r.headers()["content-type"] || "").toLowerCase();
      if (!/^image\/(png|jpe?g|webp)/.test(ct)) return;
      const key = normUrl(r.url());
      const known = SEEN_URLS.has(key);
      SEEN_URLS.add(key);
      const c = cur;
      if (known || !c) return;
      const buf = await r.body();
      if (buf.length < 30000) return;
      const ext = imgType(buf);
      if (!ext) return;
      c.cands.push({ buf, ext, size: imageSize(buf), url: safeUrl(r.url()) });
      if (!c.first) c.first = Date.now();
    } catch (e) {}
  })();
}

// ---- per prompt results ----
let prompts = [];
const state = [];   // { state, ui_status, attempts }
const files = [];   // saved scene file name per prompt
const kept = [];    // manifest rows
let rejectedCount = 0;

function finalizePrompt(i, c) {
  if (!c || !c.cands.length) return "none";
  const rows = c.cands.map((x) => ({ prompt: i + 1, url: x.url, bytes: x.buf.length, width: x.size ? x.size.w : null, height: x.size ? x.size.h : null, ratio_ok: matchesAspect(x.size, ASP), chosen: false }));
  const good = [];
  c.cands.forEach((x, k) => {
    const wide = !MIN_WIDTH || (x.size && x.size.w >= MIN_WIDTH);
    if (rows[k].ratio_ok && wide && !SAVED_HASHES.has(sha(x.buf))) good.push({ x, k });
  });
  if (!good.length) {
    fs.mkdirSync(REJECTED_DIR, { recursive: true });
    c.cands.forEach((x, k) => { rejectedCount += 1; fs.writeFileSync(path.join(REJECTED_DIR, "scene_" + String(i + 1).padStart(2, "0") + "_" + (k + 1) + x.ext), x.buf); });
    CAND_LOG.push(...rows);
    return "rejected";
  }
  good.sort((a, b) => b.x.buf.length - a.x.buf.length);
  const best = good[0];
  rows[best.k].chosen = true;
  const name = "scene_" + String(i + 1).padStart(2, "0") + best.x.ext;
  fs.writeFileSync(path.join(FINAL_DIR, name), best.x.buf);
  SAVED_HASHES.add(sha(best.x.buf));
  files[i] = name;
  kept.push({ file: "final/" + name, original_name: best.x.url, scene: i + 1, matched_by: "direct", bytes: best.x.buf.length, width: best.x.size ? best.x.size.w : null, height: best.x.size ? best.x.size.h : null, matches_requested_ratio: true });
  CAND_LOG.push(...rows);
  return "saved";
}

(async () => {
  prompts = loadPrompts();
  if (!prompts.length) throw new Error("No prompts found in " + PROMPTS_FILE);
  prompts.forEach(() => state.push({ state: "PROMPT_QUEUED", ui_status: "", attempts: 0 }));
  console.log("Direct mode (no extension). Prompts loaded:", prompts.length, "| budget:", Math.round(BUDGET_MS / 60000), "min");
  console.log("Wanted Flow settings:", JSON.stringify(WANT), "-> ratio", ASP.label, "| model check:", JSON.stringify(MODEL_CHECK));
  clearDir(FINAL_DIR);
  clearDir(REJECTED_DIR);

  const context = await chromium.launchPersistentContext(path.join(__dirname, "profile"), {
    headless: false,
    viewport: { width: 1440, height: 900 },
    acceptDownloads: true,
    downloadsPath: RAW_DIR,
    args: ["--no-sandbox"]
  });
  context.on("response", onResponse);

  const flow = await context.newPage();
  attachDiagnostics(flow);
  mark("flow_page_opened");

  // Saved session first, then the FLOW_COOKIES_B64 secret. A logged-in project URL must stay on /project/...
  const candidates = [];
  const saved = loadSavedCookies();
  if (saved) candidates.push(["saved-session", saved]);
  if (process.env.FLOW_COOKIES_B64) candidates.push(["FLOW_COOKIES_B64", loadCookies()]);
  if (!candidates.length) throw new Error("FLOW_COOKIES_B64 secret is missing and no saved session exists");
  let authed = false;
  for (const [label, cookies] of candidates) {
    await context.clearCookies();
    await context.addCookies(cookies);
    await flow.goto(FLOW_URL, { waitUntil: "domcontentloaded" });
    await flow.waitForTimeout(12000);
    const onProject = /\/project\//.test(flow.url()) && flow.url().indexOf("accounts.google.com") === -1;
    mark("auth_attempt_" + label + "_" + (onProject ? "ok" : "rejected"));
    console.log("Auth attempt:", label, "->", safeUrl(flow.url()), "| on project:", onProject);
    if (onProject) { authed = true; break; }
  }
  EVID.flow.afterLoad = await snapshot(flow, "01b-flow-load");
  console.log("Flow after load:", JSON.stringify(EVID.flow.afterLoad));
  if (!authed) throw new Error("AUTH FAILED: Flow did not open the project (ended on " + safeUrl(flow.url()) + "). Google rejected the cookies (expired, rotated or blocked for this server) - re-export fresh cookies from a private/incognito window and update FLOW_COOKIES_B64");
  try { saveCookies(await context.cookies()); } catch (e) {}

  async function applySceneSettings() {
    await flow.bringToFront().catch(() => {});
    EVID.flow.settings = await applyFlowSettings(flow, WANT, DEBUG_DIR);
    console.log("Flow settings verified:", EVID.flow.settings.verified, "| verdict:", EVID.flow.settings.verdict);
    if (EVID.flow.settings.verdict === "wrong" && cfg.abortOnSettingsMismatch !== false) {
      throw new Error("SETTINGS MISMATCH: Flow shows '" + EVID.flow.settings.chipAfter + "' but wanted " + WANT.model + " / " + ASP.label + " / x" + WANT.count + ". Nothing was submitted. See debug/02b-settings-*.png and debug/02b-popover.html");
    }
    if (!EVID.flow.settings.verified && EVID.flow.settings.verdict === "unknown") {
      console.log("::warning::Flow settings could NOT be verified (chip unreadable). Batch cannot be reported as SUCCESS.");
      if (cfg.abortOnUnverifiedSettings === true) throw new Error("SETTINGS UNVERIFIED: could not read the Flow settings chip and abortOnUnverifiedSettings is true. Nothing was submitted.");
    }
  }

  // ---- Flow's own prompt box ----
  async function findBox() {
    const makers = [
      () => flow.getByRole("textbox", { name: /what do you want to create/i }),
      () => flow.getByPlaceholder(/what do you want to create/i),
      () => flow.locator('[contenteditable="true"]'),
      () => flow.locator("textarea")
    ];
    for (const mk of makers) {
      try {
        const l = mk().last();
        await l.waitFor({ state: "visible", timeout: 2500 });
        return l;
      } catch (e) {}
    }
    return null;
  }
  async function boxText(box) {
    return box.evaluate((el) => String((el.tagName !== "DIV" && el.value !== undefined && el.value !== null) ? el.value : (el.innerText || el.textContent || ""))).catch(() => "");
  }
  async function sendPrompt(text, first) {
    const box = await findBox();
    if (!box) {
      await snapshot(flow, "d00-no-box");
      const e = new Error("PROMPT BOX NOT FOUND: could not find Flow's prompt box on the page (see debug/d00-no-box.png). Flow's page layout may have changed.");
      e.fatal = true;
      throw e;
    }
    if (first) {
      try {
        const html = await box.evaluate((el) => { let p = el; for (let k = 0; k < 4 && p.parentElement; k++) p = p.parentElement; return p.outerHTML.slice(0, 8000); });
        fs.writeFileSync(path.join(DEBUG_DIR, "direct-box.html"), html);
      } catch (e) {}
    }
    await box.click({ timeout: 10000 });
    await flow.keyboard.press("Control+A");
    await flow.keyboard.press("Backspace");
    await flow.keyboard.type(text, { delay: Math.round(rand(10, 30)) });
    await flow.waitForTimeout(rand(500, 1500));
    if (first) await snapshot(flow, "d01-typed");
    const btn = flow.locator('button:has-text("arrow_forward")').last();
    let clicked = false;
    try {
      await btn.waitFor({ state: "visible", timeout: 3000 });
      if (await btn.isEnabled()) { await btn.click({ timeout: 5000 }); clicked = true; }
    } catch (e) {}
    if (!clicked) await flow.keyboard.press("Enter");
    await flow.waitForTimeout(3000);
    let left = (await boxText(box)).trim();
    if (left.length > 20) {
      await flow.keyboard.press("Enter");
      await flow.waitForTimeout(3000);
      left = (await boxText(box)).trim();
      if (left.length > 20) throw new Error("SUBMIT FAILED: the prompt is still in the box after the send button and Enter");
    }
    if (first) await snapshot(flow, "d02-submitted");
  }

  let blocked = false;
  let blockRetriesUsed = 0;
  let submitFails = 0;

  // One prompt: type + send, then wait for its image. Returns done | rejected | failed | timeout | blocked.
  async function attemptPrompt(i) {
    const st = state[i];
    st.attempts += 1;
    cur = { idx: i, cands: [], first: 0 };
    const pre = await flow.locator("body").innerText().catch(() => "");
    if (BLOCK_RE.test(pre)) { st.ui_status = "blocked"; return "blocked"; }
    const failBefore = (pre.match(FAIL_RE) || []).length;
    const t0 = Date.now();
    try {
      await sendPrompt(prompts[i], i === 0 && st.attempts === 1);
      submitFails = 0;
    } catch (e) {
      if (e && e.fatal) throw e;
      submitFails += 1;
      console.log("::warning::Prompt " + (i + 1) + " could not be sent: " + String(e && e.message).slice(0, 160));
      st.state = "GENERATION_FAILED"; st.ui_status = "send failed";
      cur = null;
      if (submitFails >= 3) throw new Error("SUBMIT FAILED 3 times in a row - stopping. See debug/ for the page state.");
      return "failed";
    }
    mark("prompt_" + (i + 1) + "_sent");
    st.state = "GENERATION_IN_PROGRESS";
    const until = Math.min(t0 + PROMPT_WAIT_MS, SCRIPT_START + BUDGET_MS);
    let res = "timeout";
    while (Date.now() < until) {
      const c = cur;
      if (c.cands.length && Date.now() - c.first > SETTLE_MS) { res = "done"; break; }
      const t = await flow.locator("body").innerText().catch(() => "");
      if (BLOCK_RE.test(t)) { res = "blocked"; break; }
      if (!c.cands.length && (t.match(FAIL_RE) || []).length > failBefore) { res = "failed"; break; }
      await flow.waitForTimeout(2000);
    }
    if (res === "blocked") {
      st.ui_status = "blocked";
      console.log("::warning::Flow shows 'We noticed some unusual activity' at prompt " + (i + 1));
      await flow.screenshot({ path: path.join(DEBUG_DIR, "blocked.png") }).catch(() => {});
      return "blocked"; // cur stays set: an image that still arrives late is taken by the backoff
    }
    const c = cur;
    cur = null;
    if (c && c.cands.length) {
      const r = finalizePrompt(i, c);
      st.state = "GENERATION_COMPLETED";
      st.ui_status = r;
      console.log("Prompt " + (i + 1) + ": " + r + " (" + c.cands.length + " candidate image(s))");
      return r === "saved" ? "done" : "rejected";
    }
    st.state = res === "failed" ? "GENERATION_FAILED" : "GENERATION_IN_PROGRESS";
    st.ui_status = res;
    console.log("Prompt " + (i + 1) + ": " + res);
    return res;
  }

  // Flow blocked the session: back off, reload in the SAME session, re-apply the settings and try the same prompt again.
  // At most BLOCK_RETRIES (max 2) in the whole run; the second wait is longer; stops when the time budget is nearly used.
  async function recoverFromBlock(i) {
    while (blockRetriesUsed < BLOCK_RETRIES) {
      const n = ++blockRetriesUsed;
      const waitMs = Math.round((n === 1 ? 90 + Math.random() * 60 : 240 + Math.random() * 120) * 1000);
      if (waitMs + 150000 > timeLeft()) { console.log("::warning::Block retry " + n + " skipped: not enough time left"); break; }
      console.log("::warning::Flow block retry " + n + " of " + BLOCK_RETRIES + ": waiting " + Math.round(waitMs / 1000) + "s, then reloading Flow");
      mark("block_retry_" + n + "_wait");
      await flow.waitForTimeout(waitMs);
      const late = cur;
      if (late && late.cands.length && finalizePrompt(i, late) === "saved") {
        state[i].state = "GENERATION_COMPLETED"; state[i].ui_status = "saved (late)";
        cur = null;
        console.log("Prompt " + (i + 1) + ": image arrived while waiting");
        return "done";
      }
      try {
        await flow.goto(FLOW_URL, { waitUntil: "domcontentloaded" });
        await flow.waitForTimeout(12000);
        const t = await flow.locator("body").innerText().catch(() => "");
        if (BLOCK_RE.test(t)) {
          console.log("Flow still shows the unusual-activity notice after the wait");
          await flow.screenshot({ path: path.join(DEBUG_DIR, "blocked-retry" + n + ".png") }).catch(() => {});
          continue;
        }
        await applySceneSettings();
        const r = await attemptPrompt(i);
        if (r !== "blocked") return r;
      } catch (e) {
        if (e && e.fatal) throw e;
        console.log("::warning::Block retry " + n + " failed to run:", String(e && e.message).slice(0, 160));
        break;
      }
    }
    return "blocked";
  }

  await applySceneSettings();
  await snapshot(flow, "03d-flow-before-start");
  mark("before_first_prompt");

  // ---- main loop: one prompt at a time ----
  for (let i = 0; i < prompts.length && !blocked; i++) {
    if (timeLeft() < 60000) { console.log("::warning::Time budget used up - the remaining scenes use the normal image providers"); break; }
    let r = await attemptPrompt(i);
    if (r === "blocked") r = await recoverFromBlock(i);
    if (r === "blocked") { blocked = true; break; }
    if (i < prompts.length - 1) await flow.waitForTimeout(Math.round(rand(PAUSE_MIN, PAUSE_MAX) * 1000));
  }

  // ---- one retry pass for prompts that failed or timed out (not after a block) ----
  if (!blocked) {
    const redo = state.map((s, i) => i).filter((i) => !files[i] && state[i].state !== "PROMPT_QUEUED" && state[i].ui_status !== "rejected");
    for (const i of redo) {
      if (timeLeft() < PROMPT_WAIT_MS + 30000) break;
      console.log("Retry pass for prompt " + (i + 1));
      const r = await attemptPrompt(i);
      if (r === "blocked") { blocked = true; break; }
      await flow.waitForTimeout(Math.round(rand(PAUSE_MIN, PAUSE_MAX) * 1000));
    }
  }

  if (blocked) mark("blocked_unusual_activity");
  await snapshot(flow, "04b-flow-end").then((s) => { EVID.flow.end = s; }).catch(() => {});
  try { if (/\/project\//.test(flow.url())) saveCookies(await context.cookies()); } catch (e) {}
  await context.close();

  // ---- result, same shape as run_flow.js ----
  fs.writeFileSync(path.join(DEBUG_DIR, "direct-images.json"), JSON.stringify(CAND_LOG, null, 2));
  EVID.results = prompts.map((p, i) => ({ index: i + 1, prompt: p.slice(0, 80), state: state[i].state, ui_status: state[i].ui_status, attempts: state[i].attempts }));
  const count = (s) => EVID.results.filter((r) => r.state === s).length;
  const accepted = kept.length;
  const completed = count("GENERATION_COMPLETED");
  const failedPrompts = count("GENERATION_FAILED");
  const unfinished = prompts.length - completed - failedPrompts;
  const expectedImages = prompts.length;
  const over = detectOverGeneration({ found: accepted + rejectedCount, expected: expectedImages });
  const verdict = computeBatchStatus({
    imagesAccepted: accepted, expectedImages, imagesWrongRatio: rejectedCount,
    failedPrompts, unfinishedPrompts: unfinished, overGenerated: over.over, extraImages: over.extra,
    extensionDone: unfinished === 0, settingsVerified: !!(EVID.flow.settings && EVID.flow.settings.verified)
  });
  const missingScenes = prompts.map((p, i) => i + 1).filter((n) => !files[n - 1]);
  if (missingScenes.length) verdict.reasons.push("scenes without an image: " + missingScenes.join(","));
  const sv = EVID.flow.settings || {};
  EVID.summary = {
    mode: "direct", batch_status: verdict.status, status_reasons: verdict.reasons,
    prompts: prompts.length, wanted: { model: WANT.model, ratio: ASP.label, per_prompt: 1 },
    settings_verified: sv.verified, settings_verdict: sv.verdict,
    extension_done: unfinished === 0, completion_via: "direct",
    generation_completed: completed, generation_failed: failedPrompts, generation_unfinished: unfinished,
    expected_images: expectedImages, images_accepted: accepted, images_wrong_ratio: rejectedCount,
    over_generated: over.over, extra_images: over.extra,
    mapping_available: true, missing_scenes: missingScenes,
    thumbnail: { requested: false }
  };
  writeEvidence();
  const scenes = prompts.map((p, i) => ({ scene: i + 1, file: files[i] ? "final/" + files[i] : null, uuid: files[i] ? "direct" : null }));
  fs.writeFileSync(path.join(FINAL_DIR, "manifest.json"), JSON.stringify({ generated_at: new Date().toISOString(), batch_status: verdict.status, status_reasons: verdict.reasons, summary: EVID.summary, prompts: EVID.results, scenes, missing_scenes: missingScenes, images: kept, note: "Direct mode (no ZAPI extension): scene_NN.* = the image Flow made for prompt NN, saved straight from the page's network responses. Scenes listed in missing_scenes need the Cloudflare fallback." }, null, 2));
  console.log("PROMPT_RESULTS", JSON.stringify(EVID.results.map((r) => r.index + ":" + r.state)));
  console.log("EVIDENCE_SUMMARY", JSON.stringify(EVID.summary), "http_errors=" + EVID.http.length, "console_errors=" + EVID.console.length, "failed_requests=" + EVID.requestFailed.length);
  console.log("RESULT prompts=" + prompts.length + " expected_images=" + expectedImages + " accepted=" + accepted + " wrong_ratio=" + rejectedCount + " extra=" + over.extra + " mode=direct");
  console.log("BATCH_STATUS " + verdict.status + (verdict.reasons.length ? " | " + verdict.reasons.join("; ") : ""));

  if (process.env.GITHUB_STEP_SUMMARY) {
    const s = EVID.summary;
    const md = ["### Flow batch result (direct mode, no extension): " + s.batch_status, "", verdict.reasons.length ? "Reasons: " + verdict.reasons.join("; ") : "All prompts completed and verified.", "", "| Item | Value |", "|---|---|",
      "| Prompts | " + s.prompts + " |", "| Wanted | " + s.wanted.model + ", " + s.wanted.ratio + " |",
      "| Settings verified | " + s.settings_verified + " (" + s.settings_verdict + ") |", "| Completed / failed / unfinished | " + s.generation_completed + " / " + s.generation_failed + " / " + s.generation_unfinished + " |",
      "| Images kept | " + s.images_accepted + " of " + s.expected_images + " |", "| Images rejected (wrong ratio / size) | " + s.images_wrong_ratio + " |", ""].join(NL);
    try { fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, md + NL); } catch (e) {}
  }
  if (blocked) {
    console.log("::warning::Flow was blocked ('unusual activity'). Kept " + accepted + " image(s); the remaining scenes use the normal image providers.");
    process.exit(0);
  }
  if (verdict.status === "FAILED") { console.log("::error::Batch FAILED - " + verdict.reasons.join("; ")); process.exit(1); }
  if (verdict.status === "PARTIAL_SUCCESS") console.log("::warning::PARTIAL_SUCCESS - kept " + accepted + " image(s); " + verdict.reasons.join("; ") + ". See manifest.json, debug/direct-images.json and downloads/rejected");
  process.exit(0);
})().catch((e) => {
  console.error(e.message || e);
  try { EVID.error = redact(e && e.message); writeEvidence(); fs.writeFileSync(path.join(DEBUG_DIR, "direct-images.json"), JSON.stringify(CAND_LOG, null, 2)); } catch (_) {}
  process.exit(1);
});
