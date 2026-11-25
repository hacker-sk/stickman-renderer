const fs = require("fs");
const os = require("os");
const path = require("path");
const zlib = require("zlib");
const crypto = require("crypto");
const { chromium } = require("playwright");
const { applyFlowSettings, resolveAspect, matchesAspect } = require("./flow_helpers");
const { parseDoneMessage, isQueueComplete, detectOverGeneration, computeBatchStatus, validateModel, uniqueName } = require("./flow_utils");
const { turnOffSerial } = require("./ext_settings");
const { createMediaTracker } = require("./flow_match");

const cfg = JSON.parse(fs.readFileSync(path.join(__dirname, "config.json"), "utf8"));
const FLOW_URL = process.env.FLOW_URL || cfg.flowUrl;
const EXT_DIR = path.resolve(__dirname, "extension", "zapi-flow");
const DL_DIR = path.resolve(__dirname, "downloads");
const RAW_DIR = path.join(DL_DIR, "raw");
const FINAL_DIR = path.join(DL_DIR, "final");
const REJECTED_DIR = path.join(DL_DIR, "rejected");
const DEBUG_DIR = path.resolve(__dirname, "debug");
const SESS_DIR = path.resolve(__dirname, "session");
const SESS_FILE = path.join(SESS_DIR, "cookies.json");
const PROMPTS_FILE = path.resolve(__dirname, process.env.PROMPTS_FILE || "prompts/batch.json");
fs.mkdirSync(RAW_DIR, { recursive: true });
fs.mkdirSync(FINAL_DIR, { recursive: true });
fs.mkdirSync(DEBUG_DIR, { recursive: true });

const NL = String.fromCharCode(10);
let TRACKER = null; // prompt -> Flow media UUID tracker (set once the prompts are loaded)
let CONTEXT = null; // the browser context (set once it is launched)
const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;

// ---------------------------------------------------------------------------------------------------------------------------------
// Download names.
// In a normal Chrome the ZAPI Flow extension decides the file name itself (the first words of the prompt, so "[s01] ..." gives a file
// that starts with [s01]). Under Playwright every download is intercepted and Playwright only reports the name the SERVER suggests, which
// for Flow images is "unnamed.webp" - the extension's own name is lost, so no image could be matched to its scene.
// Fix, in layers (each one is optional, none can stop the run):
//   1. zdlPatch() is put in front of the extension's service worker (before the extension runs) and into every extension page. It records
//      what the extension asks Chrome to download (chrome.downloads.download: url + filename) and which name it suggests in
//      chrome.downloads.onDeterminingFilename. Those records are read back here and matched to each Playwright download by URL.
//   2. Any media UUID found in the download URL is added to the saved file name (the media tracker matches UUID -> scene).
//   3. collectImages() can still give scenes by download ORDER when nothing else matched (see orderScenes there).
// Everything seen is written to debug/download-names.json so a run always shows which layer worked.
// ---------------------------------------------------------------------------------------------------------------------------------
function zdlPatch() {
  try {
    var g = typeof self !== "undefined" ? self : globalThis;
    if (!g.chrome || !g.chrome.downloads || g.__zdlPatched) return;
    g.__zdlPatched = true;
    g.__zdl = g.__zdl || [];
    var rec = function (r) {
      try {
        r.t = Date.now();
        g.__zdl.push(r);
        if (typeof g.__zapiDl === "function") g.__zapiDl(r);
      } catch (e) {}
    };
    var d = g.chrome.downloads;
    var od = d.download;
    if (typeof od === "function") {
      d.download = function (o, cb) {
        try { rec({ kind: "download", url: String((o && o.url) || ""), filename: String((o && o.filename) || "") }); } catch (e) {}
        return od.apply(this, arguments);
      };
    }
    var ev = d.onDeterminingFilename;
    if (ev && typeof ev.addListener === "function") {
      var oa = ev.addListener;
      ev.addListener = function (fn) {
        var a = Array.prototype.slice.call(arguments);
        if (typeof fn === "function") {
          a[0] = function (item, suggest) {
            var s2 = function (s) {
              try { rec({ kind: "suggest", url: String((item && (item.url || item.finalUrl)) || ""), filename: String((s && s.filename) || "") }); } catch (e) {}
              return suggest.apply(this, arguments);
            };
            return fn.call(this, item, s2);
          };
        }
        return oa.apply(this, a);
      };
    }
  } catch (e) {}
}

// Puts zdlPatch() in front of the extension's service worker file. Runs before the browser starts; safe to run twice.
function patchExtensionWorker() {
  try {
    const mf = JSON.parse(fs.readFileSync(path.join(EXT_DIR, "manifest.json"), "utf8"));
    const rel = mf.background && mf.background.service_worker;
    if (!rel) { console.log("Download-name capture: the extension has no service worker file, page hook only"); return; }
    const file = path.join(EXT_DIR, rel);
    const src = fs.readFileSync(file, "utf8");
    if (src.indexOf("__zdlPatched") !== -1) { console.log("Download-name capture: service worker already patched (" + rel + ")"); return; }
    fs.writeFileSync(file, "(" + zdlPatch.toString() + ")();" + NL + src);
    console.log("Download-name capture: installed in front of " + rel);
  } catch (e) {
    console.log("Download-name capture NOT installed in the service worker:", String(e && e.message).slice(0, 140));
  }
}

const DL_LOG = [];            // what the extension asked Chrome to download: { t, kind, url, filename, claimed }
const DL_KEYS = new Set();
const DL_NAMES = [];          // one row per Playwright download (written to debug/download-names.json)
function addRec(r) {
  if (!r) return;
  const key = [r.t, r.kind, r.url, r.filename].join("|");
  if (DL_KEYS.has(key)) return;
  DL_KEYS.add(key);
  DL_LOG.push({ t: r.t, kind: r.kind, url: String(r.url || ""), filename: String(r.filename || ""), claimed: false });
}
async function pullSwLog() {
  if (!CONTEXT) return;
  for (const sw of CONTEXT.serviceWorkers()) {
    try {
      const rows = await sw.evaluate(() => (globalThis.__zdl || []).slice());
      for (const r of rows) addRec(r);
    } catch (e) {}
  }
}
function normUrl(u) { try { const x = new URL(u); return x.origin + x.pathname; } catch (e) { return String(u).split("?")[0]; } }
// The records of ONE download: same URL (exact, else without the query string). The suggested name wins over the requested one.
function pickRecord(url) {
  let m = DL_LOG.filter((r) => !r.claimed && r.url === url);
  if (!m.length) { const n = normUrl(url); m = DL_LOG.filter((r) => !r.claimed && normUrl(r.url) === n); }
  if (!m.length) return null;
  m.forEach((r) => { r.claimed = true; });
  const named = m.filter((r) => r.filename);
  const sug = named.filter((r) => r.kind === "suggest");
  return (sug.length ? sug : named).slice(-1)[0] || m[m.length - 1];
}
async function recoverName(url) {
  const end = Date.now() + 3000;
  for (;;) {
    await pullSwLog();
    const rec = pickRecord(url);
    if (rec) return rec;
    if (Date.now() >= end) return null;
    await new Promise((r) => setTimeout(r, 300));
  }
}
function cleanName(n) {
  let b = String(n || "").replace(/\\/g, "/").split("/").pop();
  b = b.replace(/[<>:"|?*\u0000-\u001f]/g, "_").trim();
  return b.slice(0, 120);
}

// Flow settings: config.json defaults, overridden by FLOW_MODEL / FLOW_ASPECT / FLOW_COUNT (set from workflow inputs).
const WANT = Object.assign({ agent: "off", model: "Nano Banana Lite", aspect: "portrait", count: 1 }, cfg.flowSettings || {});
if ((process.env.FLOW_MODEL || "").trim()) WANT.model = process.env.FLOW_MODEL.trim();
if ((process.env.FLOW_ASPECT || "").trim()) WANT.aspect = process.env.FLOW_ASPECT.trim();
if ((process.env.FLOW_COUNT || "").trim()) WANT.count = parseInt(process.env.FLOW_COUNT, 10);
const ASP = resolveAspect(WANT.aspect);
if (!ASP) throw new Error("Unknown aspect '" + WANT.aspect + "'. Use portrait, landscape, square, 3:4 or 4:3 (or 9:16 / 16:9 / 1:1).");
if (!(WANT.count >= 1 && WANT.count <= 4)) throw new Error("Image count must be 1-4, got " + WANT.count);
const MODEL_CHECK = validateModel(WANT.model, cfg.allowedModels);
if (!MODEL_CHECK.ok) throw new Error("Invalid model: " + MODEL_CHECK.reason);
const RETRY_PASSES = cfg.retryFailedPasses === undefined ? 1 : cfg.retryFailedPasses;

// Backoff + retry when Flow shows "We noticed some unusual activity": at most 2 retries (FLOW_BLOCK_RETRIES can lower it, never raise it; 0 = off).
// The workflow step is capped at 22 minutes, so retries stop early when less than the time they need is left (STEP_LIMIT_MS).
const BLOCK_RETRIES = (() => { const n = parseInt(process.env.FLOW_BLOCK_RETRIES || "2", 10); return Number.isNaN(n) ? 2 : Math.max(0, Math.min(n, 2)); })();
const SCRIPT_START = Date.now();
const STEP_LIMIT_MS = 20 * 60000;

// Optional thumbnail pass, in the SAME browser session: it runs first with its own settings (portrait by default), the image is
// copied to THUMB_OUT_DIR, then Flow is switched back to the scene settings (and verified) before the scenes are submitted.
// Set THUMB_PROMPTS_FILE to switch it on; leave it empty for the old single-batch behaviour.
const THUMB_FILE = (process.env.THUMB_PROMPTS_FILE || "").trim();
const THUMB_OUT = path.resolve(__dirname, process.env.THUMB_OUT_DIR || "../flow-thumb");
const THUMB_ASPECT = (process.env.THUMB_ASPECT || "portrait").trim();
const THUMB_MINUTES = Math.min(parseInt(process.env.THUMB_MINUTES || "5", 10) || 5, 10);
const THUMB_ASP = resolveAspect(THUMB_ASPECT);
if (THUMB_FILE && !THUMB_ASP) throw new Error("Unknown thumbnail aspect '" + THUMB_ASPECT + "'. Use portrait, landscape, square, 3:4 or 4:3.");

const EVID = { timeline: [], console: [], pageErrors: [], http: [], requestFailed: [], flow: {}, results: [] };
function mark(ev) { EVID.timeline.push({ t: new Date().toISOString(), event: ev }); }
function redact(s) { return String(s || '').replace(/[A-Za-z0-9_-]{40,}/g, '[redacted]').slice(0, 300); }
function safeUrl(u) { try { const x = new URL(u); return x.host + x.pathname; } catch (e) { return redact(u); } }
function attachDiagnostics(pg) {
  pg.on('console', (m) => { if (m.type() === 'error' && EVID.console.length < 100) EVID.console.push({ t: new Date().toISOString(), text: redact(m.text()) }); });
  pg.on('pageerror', (e) => { if (EVID.pageErrors.length < 50) EVID.pageErrors.push(redact(e && e.message)); });
  pg.on('requestfailed', (r) => { if (EVID.requestFailed.length < 100) EVID.requestFailed.push({ t: new Date().toISOString(), method: r.method(), url: safeUrl(r.url()), reason: redact(r.failure() && r.failure().errorText) }); });
  pg.on('response', async (r) => {
    try {
      if (r.status() < 400 || EVID.http.length >= 100) return;
      const row = { t: new Date().toISOString(), status: r.status(), method: r.request().method(), url: safeUrl(r.url()) };
      if (new URL(r.url()).host.indexOf('google') !== -1) { const b = await r.text().catch(() => ''); row.body = redact(b); }
      EVID.http.push(row);
    } catch (e) {}
  });
}
async function snapshot(pg, name) {
  const t = await pg.locator('body').innerText().catch(() => '');
  fs.writeFileSync(path.join(DEBUG_DIR, name + '-text.txt'), t.slice(0, 4000));
  await pg.screenshot({ path: path.join(DEBUG_DIR, name + '.png'), timeout: 10000 }).catch(() => {});
  return { url: safeUrl(pg.url()), title: await pg.title().catch(() => '') };
}
function writeEvidence() { fs.writeFileSync(path.join(DEBUG_DIR, 'flow-network.json'), JSON.stringify(EVID, null, 2)); }
function writeDownloadNames() {
  try {
    fs.writeFileSync(path.join(DEBUG_DIR, 'download-names.json'), JSON.stringify({
      downloads: DL_NAMES,
      extension_calls: DL_LOG.map((r) => ({ t: r.t, kind: r.kind, url: safeUrl(r.url), filename: r.filename, claimed: r.claimed }))
    }, null, 2));
  } catch (e) {}
}
function parseQueue(text, prompts) {
  const lines = text.split(NL).map((s) => s.trim());
  const start = lines.findIndex((l) => l.toUpperCase() === 'QUEUE');
  const map = { PENDING: 'PROMPT_QUEUED', GENERATING: 'GENERATION_IN_PROGRESS', FAILED: 'GENERATION_FAILED', DONE: 'GENERATION_COMPLETED' };
  const toks = Object.keys(map);
  const res = [];
  if (start < 0) return res;
  for (let i = start + 1; i < lines.length; i++) {
    const u = lines[i].toUpperCase();
    if (u.indexOf('AUTO DOWNLOAD') === 0) break;
    if (u.length > 20) continue;
    const tok = toks.find((x) => u.indexOf(x) === 0);
    if (!tok) continue;
    res.push({ index: res.length + 1, prompt: String(prompts[res.length] || '').slice(0, 80), ui_status: lines[i], state: map[tok] });
  }
  return res;
}

// .json: array of strings or {prompt}. .txt: prompts separated by a blank line.
// Each prompt is flattened to ONE line because ZAPI FLOW queues one prompt per line.
// `file` is optional (relative to flow-batch); without it the main PROMPTS_FILE is read.
function loadPrompts(file) {
  const src = file ? path.resolve(__dirname, file) : PROMPTS_FILE;
  const text = fs.readFileSync(src, "utf8").replace(/\r/g, "");
  let list;
  if (src.endsWith(".txt")) list = text.split(/\n[ \t]*\n/);
  else list = JSON.parse(text).map((p) => (typeof p === "string" ? p : p.prompt));
  return list.filter(Boolean).map((p) => p.replace(/\s*\n\s*/g, " ").trim()).filter(Boolean);
}

// Accepts: Playwright storageState ({cookies:[...]}) or array, Cookie-Editor / EditThisCookie export (array).
// Base64 of plain JSON or of gzipped JSON. Never logs cookie values.
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

// Session carried over from a previous successful run (workflow cache, if enabled). Google rotates session cookies,
// so the freshest copy is the one that still works. Never logged, never uploaded as an artifact.
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

// Width/height from the file header (PNG, JPEG, WebP). Returns null when it cannot be read.
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

function listFiles(dir, out) {
  if (!fs.existsSync(dir)) return out;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) { if (full !== FINAL_DIR && full !== REJECTED_DIR && full !== RAW_DIR) listFiles(full, out); }
    else out.push(full);
  }
  return out;
}

function clearDir(dir) {
  if (!fs.existsSync(dir)) return;
  for (const e of fs.readdirSync(dir)) fs.rmSync(path.join(dir, e), { recursive: true, force: true });
}

// Collect complete, valid images from every place Chrome may have written them (subfolders included). Safe to call more than once.
// Images with the requested shape go to downloads/final; wrong-shaped ones go to downloads/rejected and the rest of the batch is kept.
// When the prompt->media mapping is available, each image is matched to its scene: first by a Flow media UUID in the file name, then by
// the [sNN] scene tag in the file name (see flow_match.js); it is saved as scene_NN.<ext> (NN = prompt number). Any other accepted image
// is saved as unmatched_<n> so it can never be mistaken for a scene.
// Last resort, only when `orderScenes` is given: the scenes (1-based prompt numbers, ascending) whose prompts finished DONE in the first
// and only attempt. If exactly as many images are still unmatched as there are such scenes still without an image, the images are given
// to those scenes in download order (the extension queue is strictly one prompt after the other). Any other count: nothing is guessed.
// Without a mapping the old behaviour applies (descriptive names, capture timestamp prefix removed).
function collectImages(orderScenes) {
  clearDir(FINAL_DIR);
  clearDir(REJECTED_DIR);
  const files = [];
  listFiles(DL_DIR, files);
  listFiles(path.join(os.homedir(), "Downloads"), files);
  files.sort((a, b) => fs.statSync(a).mtimeMs - fs.statSync(b).mtimeMs);
  const seen = new Set();
  const recs = [];
  const skipped = [];
  const sceneFile = {};
  const mapped = TRACKER ? TRACKER.hasMapping() : false;
  for (const full of files) {
    const f = path.basename(full);
    if (/\.(crdownload|tmp|part)$/i.test(f)) { skipped.push(f + " (incomplete)"); continue; }
    const buf = fs.readFileSync(full);
    const ext = imgType(buf);
    if (!ext || buf.length < 10000) { skipped.push(f + " (not a valid image / too small)"); continue; }
    const hash = crypto.createHash("sha256").update(buf).digest("hex");
    if (seen.has(hash)) continue;
    seen.add(hash);
    const size = imageSize(buf);
    const ok = matchesAspect(size, ASP);
    const r = { full, f, buf, ext, size, ok, scene: null, how: null };
    if (ok && mapped) {
      const hit = TRACKER.matchFile(f);
      if (hit && !sceneFile[hit.index]) {
        r.scene = hit.index;
        r.how = String(hit.uuid).indexOf("tag:") === 0 ? "tag" : "uuid";
        sceneFile[hit.index] = { file: null, uuid: hit.uuid };
      }
    }
    recs.push(r);
  }

  if (mapped && Array.isArray(orderScenes) && orderScenes.length) {
    const free = recs.filter((r) => r.ok && !r.scene);
    const cand = orderScenes.filter((n) => !sceneFile[n]);
    if (free.length && free.length === cand.length) {
      free.forEach((r, i) => { r.scene = cand[i]; r.how = "order"; sceneFile[cand[i]] = { file: null, uuid: "order" }; });
      console.log("::notice::Scenes given by download order for " + free.length + " image(s) (names carried no scene): " + cand.join(","));
    } else if (free.length) {
      console.log("Order matching skipped: " + free.length + " unmatched image(s) but " + cand.length + " scene(s) still without an image");
    }
  }

  const usedFinal = new Set();
  const usedRejected = new Set();
  const items = [];
  let accepted = 0;
  let rejected = 0;
  for (const r of recs) {
    let name;
    if (r.ok) {
      accepted += 1;
      if (r.scene) {
        name = "scene_" + String(r.scene).padStart(2, "0") + r.ext;
        sceneFile[r.scene].file = "final/" + name;
      } else if (mapped) {
        name = "unmatched_" + uniqueName(r.f, r.ext, usedFinal);
      } else {
        name = uniqueName(r.f, r.ext, usedFinal);
      }
      fs.copyFileSync(r.full, path.join(FINAL_DIR, name));
    } else {
      rejected += 1;
      fs.mkdirSync(REJECTED_DIR, { recursive: true });
      name = uniqueName(r.f, r.ext, usedRejected);
      fs.copyFileSync(r.full, path.join(REJECTED_DIR, name));
    }
    items.push({ file: (r.ok ? "final/" : "rejected/") + name, original_name: r.f.replace(/^\d{10,}-/, ""), scene: r.scene, matched_by: r.how, bytes: r.buf.length, width: r.size ? r.size.w : null, height: r.size ? r.size.h : null, matches_requested_ratio: r.size ? r.ok : null });
  }
  const total = TRACKER ? TRACKER.count : 0;
  const scenes = [];
  for (let i = 1; i <= total; i++) scenes.push({ scene: i, file: sceneFile[i] ? sceneFile[i].file : null, uuid: sceneFile[i] ? sceneFile[i].uuid : null });
  const missingScenes = mapped ? scenes.filter((s) => !s.file).map((s) => s.scene) : [];
  fs.writeFileSync(path.join(DEBUG_DIR, "downloads-listing.json"), JSON.stringify({ items, skipped }, null, 2));
  console.log("Valid images:", accepted + rejected, "| accepted:", accepted, "| wrong ratio:", rejected, "| skipped:", skipped.length);
  if (mapped) {
    const by = (h) => recs.filter((r) => r.how === h).length;
    console.log("Scene matching: " + (total - missingScenes.length) + " of " + total + " scenes matched (UUID " + by("uuid") + ", tag " + by("tag") + ", order " + by("order") + ") | missing: " + (missingScenes.join(",") || "none"));
  } else console.log("::warning::No prompt->media mapping captured; images keep Flow's names and cannot be matched to scenes (see debug/media-map.json and media-samples.json)");
  return { accepted, rejected, items, skipped, scenes, missingScenes, mapped };
}

// The scenes (1-based) whose prompts finished DONE, for the download-order fallback of collectImages. Only valid while every prompt
// was submitted exactly once (a retried prompt's image arrives late, so the order would no longer follow the prompt numbers).
function orderScenesFromState(state, parsedOk) {
  if (!parsedOk) return null;
  if (!state.every((s) => s.attempts === 1)) return null;
  const list = [];
  state.forEach((s, i) => { if (s.state === "GENERATION_COMPLETED") list.push(i + 1); });
  return list;
}

// Takes the thumbnail out of the download folders: the first valid image with the thumbnail's shape is copied to THUMB_OUT as
// thumbnail.<ext>, then EVERY downloaded file is removed so the thumbnail can never be collected as a scene image later.
function takeThumbnail() {
  const files = [];
  listFiles(DL_DIR, files);
  listFiles(path.join(os.homedir(), "Downloads"), files);
  files.sort((a, b) => { try { return fs.statSync(a).mtimeMs - fs.statSync(b).mtimeMs; } catch (e) { return 0; } });
  let saved = null;
  const notes = [];
  for (const full of files) {
    const f = path.basename(full);
    if (/\.(crdownload|tmp|part)$/i.test(f)) { notes.push({ file: f, skipped: "incomplete" }); continue; }
    let buf;
    try { buf = fs.readFileSync(full); } catch (e) { continue; }
    const ext = imgType(buf);
    if (!ext || buf.length < 10000) { notes.push({ file: f, skipped: "not a valid image / too small" }); continue; }
    const size = imageSize(buf);
    const ok = matchesAspect(size, THUMB_ASP);
    notes.push({ file: f.replace(/^\d{10,}-/, ""), width: size ? size.w : null, height: size ? size.h : null, ratio_ok: ok });
    if (ok && !saved) {
      fs.mkdirSync(THUMB_OUT, { recursive: true });
      saved = "thumbnail" + ext;
      fs.copyFileSync(full, path.join(THUMB_OUT, saved));
    }
  }
  for (const full of files) { try { fs.rmSync(full, { force: true }); } catch (e) {} }
  clearDir(RAW_DIR);
  try {
    fs.mkdirSync(path.join(DEBUG_DIR, "thumb"), { recursive: true });
    fs.writeFileSync(path.join(DEBUG_DIR, "thumb", "thumb-files.json"), JSON.stringify({ saved, notes }, null, 2));
  } catch (e) {}
  return { saved, notes };
}

async function dumpUiText(ui, name) {
  const t = await ui.locator("body").innerText().catch(() => "");
  fs.writeFileSync(path.join(DEBUG_DIR, name), t);
  return t;
}

(async () => {
  if (!fs.existsSync(path.join(EXT_DIR, "manifest.json"))) throw new Error("ZAPI Flow extension not found in " + EXT_DIR);
  const prompts = loadPrompts();
  if (!prompts.length) throw new Error("No prompts found in " + PROMPTS_FILE);
  console.log("Prompts loaded:", prompts.length);
  console.log("Wanted Flow settings:", JSON.stringify(WANT), "-> ratio", ASP.label, "| model check:", JSON.stringify(MODEL_CHECK));
  if (THUMB_FILE) console.log("Thumbnail pass on (same session):", THUMB_FILE, "->", THUMB_ASP.label, "then back to", ASP.label);
  TRACKER = createMediaTracker(prompts, DEBUG_DIR);

  patchExtensionWorker();

  const context = await chromium.launchPersistentContext(path.join(__dirname, "profile"), {
    headless: false,
    viewport: { width: 1440, height: 900 },
    acceptDownloads: true,
    downloadsPath: RAW_DIR,
    args: ["--no-sandbox", "--disable-extensions-except=" + EXT_DIR, "--load-extension=" + EXT_DIR]
  });
  CONTEXT = context;
  context.on("response", TRACKER.onResponse);

  // Records what the extension pages ask Chrome to download (the service worker is covered by patchExtensionWorker).
  try { await context.exposeFunction("__zapiDl", (r) => { try { addRec(r); } catch (e) {} }); }
  catch (e) { console.log("Download-name capture: page binding not installed:", String(e && e.message).slice(0, 100)); }
  try { await context.addInitScript(zdlPatch); }
  catch (e) { console.log("Download-name capture: page script not installed:", String(e && e.message).slice(0, 100)); }

  const hook = (pg) => pg.on("download", async (d) => {
    let rec = null;
    try { rec = await recoverName(d.url()); } catch (e) {}
    const sugg = d.suggestedFilename();
    const own = rec && rec.filename ? cleanName(rec.filename) : "";
    let name = own || sugg;
    if (!/\.[A-Za-z0-9]{2,5}$/.test(name)) name += ".webp";
    // Any media UUID in the download URL goes INTO the name (before the extension), so the tracker can match it to a scene.
    const ids = [...new Set((String(d.url()).match(UUID_RE) || []).map((x) => x.toLowerCase()))].slice(0, 3);
    if (ids.length) name = name.replace(/(\.[A-Za-z0-9]{2,5})$/, "__" + ids.join("_") + "$1");
    let target = path.join(DL_DIR, Date.now() + "-" + name);
    for (let n = 2; fs.existsSync(target); n++) target = path.join(DL_DIR, Date.now() + "-" + n + "-" + name);
    await d.saveAs(target).catch((e) => console.log("download save failed:", e.message));
    console.log("Saved", path.basename(target), own ? "(extension's own name)" : "(no extension name found)");
    DL_NAMES.push({ t: new Date().toISOString(), url: safeUrl(d.url()), url_uuids: ids.length, suggested: sugg, extension_name: own || null, extension_kind: rec ? rec.kind : null, saved_as: path.basename(target).replace(/^\d{10,}-/, "") });
  });
  context.pages().forEach(hook);
  context.on("page", hook);

  // The ZAPI Flow side panel looks for the active tab in the current window.
  // Opened as a normal tab it would find itself, so point it at the Flow tab instead.
  await context.addInitScript(() => {
    if (!(window.chrome && chrome.tabs && chrome.tabs.query)) return;
    const orig = chrome.tabs.query.bind(chrome.tabs);
    chrome.tabs.query = function (q, cb) {
      const wantActive = q && q.active && q.currentWindow;
      const query = wantActive ? { url: ["https://labs.google/fx/*", "https://flow.google.com/*"] } : q;
      const p = orig(query).then((r) => (wantActive ? r.slice(0, 1) : r));
      if (typeof cb === "function") { p.then(cb); return; }
      return p;
    };
  });

  let extId = process.env.EXT_ID || "";
  if (!extId) {
    try {
      let sw = context.serviceWorkers()[0];
      if (!sw) sw = await context.waitForEvent("serviceworker", { timeout: 20000 });
      extId = sw.url().split("/")[2];
    } catch (e) {
      console.log("Service worker not found");
    }
  }
  if (!extId) throw new Error("Could not detect the ZAPI Flow extension id - did the extension load?");
  console.log("Extension id:", extId);

  const flow = await context.newPage();
  attachDiagnostics(flow);
  mark('flow_page_opened');

  // Try a saved session first (if present), then the FLOW_COOKIES_B64 secret.
  // A logged-in project URL must stay on /project/...; Google sends signed-out visitors to the public /about page.
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
    mark('auth_attempt_' + label + '_' + (onProject ? 'ok' : 'rejected'));
    console.log("Auth attempt:", label, "->", safeUrl(flow.url()), "| on project:", onProject);
    if (onProject) { authed = true; break; }
  }
  EVID.flow.afterLoad = await snapshot(flow, '01b-flow-load');
  console.log('Flow after load:', JSON.stringify(EVID.flow.afterLoad));
  await flow.screenshot({ path: path.join(DEBUG_DIR, "01-flow.png") });
  if (!authed) throw new Error("AUTH FAILED: Flow did not open the project (ended on " + safeUrl(flow.url()) + "). Google rejected the cookies (expired, rotated or blocked for this server) - re-export fresh cookies from a private/incognito window and update FLOW_COOKIES_B64");
  try { saveCookies(await context.cookies()); } catch (e) {}

  // The ZAPI FLOW extension page (second tab). Opened on first use, so a run without a thumbnail behaves as before:
  // settings first, then this page.
  let ui = null;
  async function openUi() {
    if (ui) return ui;
    ui = await context.newPage();
    await ui.goto("chrome-extension://" + extId + "/" + cfg.extensionPage);
    await ui.waitForTimeout(4000);
    await ui.screenshot({ path: path.join(DEBUG_DIR, "02-ui.png") });
    await dumpUiText(ui, "02-ui-text.txt");

    // Serial number OFF + auto download ON, verified (result is written to debug/02d-serial-toggle.json).
    await turnOffSerial(ui, DEBUG_DIR);

    // Diagnostic only: record the extension's own generation-settings controls, then close the section again.
    try {
      const gs = ui.getByText(/generation settings/i).first();
      await gs.click({ timeout: 3000 });
      await ui.waitForTimeout(800);
      await dumpUiText(ui, "02c-ui-settings-text.txt");
      const ctrls = await ui.evaluate(() => [...document.querySelectorAll('input,select,textarea,button')].map((e) => ({ tag: e.tagName.toLowerCase(), id: e.id, type: e.type || '', label: String(e.getAttribute('aria-label') || e.innerText || e.value || '').slice(0, 40) })).slice(0, 80));
      fs.writeFileSync(path.join(DEBUG_DIR, "02c-ui-controls.json"), JSON.stringify(ctrls, null, 2));
      await ui.screenshot({ path: path.join(DEBUG_DIR, "02c-ui-settings.png") });
      await gs.click({ timeout: 3000 }).catch(() => {});
    } catch (e) { console.log("Extension settings section not inspected:", String(e.message).slice(0, 100)); }
    return ui;
  }

  // The wait for each pass has its own cap (thumbnail: THUMB_MINUTES, scenes: config timeoutMinutes, never more than 10).
  let deadline = Date.now();
  function setDeadline(minutes) { deadline = Date.now() + Math.min(minutes, 10) * 60000; }
  // Only account/quota problems end the wait early. A single failed prompt never does.
  const fatalRe = /sign in|not logged|quota|limit/i;
  let completionVia = "";
  // Flow's "We noticed some unusual activity" failure card: Google is blocking this session, retrying or waiting is pointless.
  let blocked = false;
  const BLOCK_RE = /noticed some unusual activity/i;

  // Wait until the WHOLE queue is accounted for (all `expected` prompts finished or failed), a fatal problem appears,
  // or the overall deadline passes. Signals: the extension's "Done — N succeeded, M failed" message when N+M equals the
  // number of prompts submitted, or every queue row being DONE/FAILED.
  async function waitForCompletion(ignoreText, expected, list) {
    const began = Date.now();
    let done = false;
    let last = "";
    let tick = 0;
    while (Date.now() < deadline) {
      const txt = await ui.locator(cfg.selectors.status).innerText().catch(() => "");
      if (txt !== last) { console.log("Status:", txt.slice(0, 300)); last = txt; }
      const stale = ignoreText && txt === ignoreText && Date.now() - began < 120000;
      if (!stale) {
        const aged = Date.now() - began > 20000;
        const body = aged ? await ui.locator("body").innerText().catch(() => "") : "";
        const rows = body ? parseQueue(body, list) : [];
        const doneMsg = parseDoneMessage(txt);
        const c = isQueueComplete({ doneMsg, rows, expected });
        if (c.complete) { done = true; completionVia = c.via; console.log("Queue complete via", c.via); break; }
        if (doneMsg) console.log("Done message counts " + doneMsg.total + " of " + expected + " prompts - still waiting");
        if (!doneMsg && fatalRe.test(txt)) { console.log("Extension reported an account/quota problem - stopping wait"); break; }
      }
      const ftxt = await flow.locator("body").innerText().catch(() => "");
      if (BLOCK_RE.test(ftxt)) {
        blocked = true;
        console.log("::warning::Flow shows 'We noticed some unusual activity' - stopping the Flow step now and moving on with the images made so far");
        await flow.screenshot({ path: path.join(DEBUG_DIR, "blocked.png") }).catch(() => {});
        break;
      }
      tick += 1;
      if (tick % 8 === 0) await flow.screenshot({ path: path.join(DEBUG_DIR, "progress-" + tick + ".png") });
      await flow.waitForTimeout(15000);
    }
    return done;
  }

  // ZAPI FLOW: one prompt per line
  async function submit(list) {
    await ui.locator(cfg.selectors.promptInput).fill(list.join(NL));
    const echoed = await ui.locator(cfg.selectors.promptInput).inputValue();
    console.log("Prompt box lines:", echoed.split(NL).filter((s) => s.trim()).length, "of", list.length);
    await ui.waitForFunction((sel) => { const b = document.querySelector(sel); return b && !b.disabled; }, cfg.selectors.startButton, { timeout: 90000 });
  }

  // Thumbnail pass: portrait settings -> one image -> copied out of the download folders. Never throws: a failed thumbnail
  // must not stop the scenes. Returns { ran, saved, blocked } for the summary.
  async function thumbnailPass() {
    const out = { ran: false, saved: null, blocked: false };
    try {
      const tPrompts = loadPrompts(THUMB_FILE);
      if (!tPrompts.length) { console.log("Thumbnail pass: no prompt in " + THUMB_FILE + " - skipped"); return out; }
      mark('thumb_pass_start');
      const tdir = path.join(DEBUG_DIR, "thumb");
      fs.mkdirSync(tdir, { recursive: true });
      await flow.bringToFront().catch(() => {});
      const tWant = Object.assign({}, WANT, { aspect: THUMB_ASPECT, count: 1 });
      const ts = await applyFlowSettings(flow, tWant, tdir);
      EVID.flow.thumbSettings = ts;
      console.log("Thumbnail settings verified:", ts.verified, "| verdict:", ts.verdict);
      if (ts.verdict === "wrong") {
        console.log("::warning::Flow shows other settings than the thumbnail wants (" + ts.chipAfter + ") - thumbnail skipped, nothing submitted");
        return out;
      }
      await openUi();
      const before = await ui.locator(cfg.selectors.status).innerText().catch(() => "");
      await submit(tPrompts);
      await ui.locator(cfg.selectors.startButton).click();
      out.ran = true;
      await flow.bringToFront().catch(() => {});
      mark('thumb_start_clicked');
      console.log("Thumbnail start clicked");
      await flow.waitForTimeout(8000);
      setDeadline(THUMB_MINUTES);
      const tDone = await waitForCompletion(before, tPrompts.length, tPrompts);
      console.log("Thumbnail queue finished:", tDone);
      await flow.screenshot({ path: path.join(tdir, "thumb-end.png") }).catch(() => {});
      await dumpUiText(ui, "thumb-ui-end-text.txt");
      // Let the download land BEFORE the folders are emptied.
      await flow.waitForTimeout(cfg.settleSeconds * 1000);
      const t = takeThumbnail();
      out.saved = t.saved;
      out.blocked = blocked;
      mark('thumb_pass_end_' + (t.saved ? 'saved' : 'none'));
      console.log(t.saved ? "Thumbnail saved: " + t.saved + " (" + THUMB_ASP.label + ")" : "::warning::No usable thumbnail image came back - no thumbnail this time");
      try { saveCookies(await context.cookies()); } catch (e) {}
    } catch (e) {
      console.log("::warning::Thumbnail pass failed, continuing with the scenes:", String(e && e.message).slice(0, 200));
      mark('thumb_pass_error');
    }
    // A block seen during the thumbnail is re-detected by the scenes wait if it is still on screen.
    blocked = false;
    return out;
  }

  // Scene settings (config + env), verified. After a thumbnail pass Flow is toggled back by the same clicks; if that does not
  // verify, the Flow project is reloaded once in the SAME session (no new login) and the settings are applied again.
  async function applySceneSettings(afterThumb) {
    await flow.bringToFront().catch(() => {});
    EVID.flow.settings = await applyFlowSettings(flow, WANT, DEBUG_DIR);
    if (afterThumb && EVID.flow.settings.verdict !== "ok") {
      console.log("::warning::Switching back to the scene settings did not verify (" + EVID.flow.settings.verdict + ") - reloading the Flow project once in this session and trying again");
      mark('scene_settings_reload');
      await flow.goto(FLOW_URL, { waitUntil: "domcontentloaded" });
      await flow.waitForTimeout(12000);
      EVID.flow.settings = await applyFlowSettings(flow, WANT, DEBUG_DIR);
    }
    console.log("Flow settings verified:", EVID.flow.settings.verified, "| verdict:", EVID.flow.settings.verdict);
    // Stop BEFORE submitting when Flow positively shows other settings.
    if (EVID.flow.settings.verdict === "wrong" && cfg.abortOnSettingsMismatch !== false) {
      throw new Error("SETTINGS MISMATCH: Flow shows '" + EVID.flow.settings.chipAfter + "' but wanted " + WANT.model + " / " + ASP.label + " / x" + WANT.count + ". Nothing was submitted. See debug/02b-settings-*.png and debug/02b-popover.html");
    }
    // An unreadable chip is NOT verified. By default the run continues (the per-image ratio check still protects the output)
    // but the batch can never end as SUCCESS. Set abortOnUnverifiedSettings to true in config.json to stop instead.
    if (!EVID.flow.settings.verified && EVID.flow.settings.verdict === "unknown") {
      console.log("::warning::Flow settings could NOT be verified (chip unreadable). Batch cannot be reported as SUCCESS.");
      if (cfg.abortOnUnverifiedSettings === true) {
        throw new Error("SETTINGS UNVERIFIED: could not read the Flow settings chip and abortOnUnverifiedSettings is true. Nothing was submitted. See debug/02b-settings-*.png");
      }
    }
  }

  // ---- Pass 1 (optional): thumbnail in its own settings, same session ----
  let thumb = { ran: false, saved: null, blocked: false };
  if (THUMB_FILE) thumb = await thumbnailPass();

  // ---- Pass 2: scenes in the normal settings ----
  await applySceneSettings(thumb.ran);
  await openUi();
  const staleStatus = thumb.ran ? await ui.locator(cfg.selectors.status).innerText().catch(() => "") : "";

  setDeadline(cfg.timeoutMinutes);

  try {
    await submit(prompts);
  } catch (e) {
    await ui.screenshot({ path: path.join(DEBUG_DIR, "03-disabled.png") });
    await dumpUiText(ui, "03-disabled-text.txt");
    await flow.screenshot({ path: path.join(DEBUG_DIR, "03-flow-state.png") });
    throw new Error("Start button stayed disabled - the Flow tab must be inside an open Flow project (check repo variable FLOW_PROJECT_URL) and Agent mode must be off");
  }
  await ui.screenshot({ path: path.join(DEBUG_DIR, "03-filled.png") });
  EVID.flow.beforeStart = await snapshot(flow, '03d-flow-before-start');
  fs.writeFileSync(path.join(DEBUG_DIR, 'downloads-before.json'), JSON.stringify(listFiles(DL_DIR, []).map((f) => path.basename(f))));
  mark('before_start_click');
  await ui.locator(cfg.selectors.startButton).click();
  await flow.bringToFront();
  mark('after_start_click');
  console.log("Start clicked");
  await flow.waitForTimeout(8000);
  const early = await ui.locator(cfg.selectors.status).innerText().catch(() => "");
  console.log("Status 8s after start:", early.slice(0, 200));
  await ui.screenshot({ path: path.join(DEBUG_DIR, "03b-started.png") });
  await dumpUiText(ui, "03b-started-text.txt");
  await flow.screenshot({ path: path.join(DEBUG_DIR, "03c-flow-started.png") });

  let done = await waitForCompletion(staleStatus, prompts.length, prompts);
  console.log("Extension finished:", done);
  await flow.screenshot({ path: path.join(DEBUG_DIR, "04-end.png") });
  await ui.screenshot({ path: path.join(DEBUG_DIR, "04-ui-end.png") });
  const endText = await dumpUiText(ui, "04-ui-end-text.txt");
  EVID.flow.end = await snapshot(flow, '04b-flow-end');
  mark('extension_finished_' + done);

  // Per-prompt state, tracked by original prompt number so retries do not shuffle anything.
  const state = prompts.map((p) => ({ state: "PROMPT_QUEUED", ui_status: "", attempts: 1 }));
  const first = parseQueue(endText, prompts);
  first.forEach((r, i) => { if (state[i]) { state[i].state = r.state; state[i].ui_status = r.ui_status; } });
  const parsedOk = first.length === prompts.length;

  // Let the last downloads land BEFORE deciding which scenes have no usable image.
  await flow.waitForTimeout(cfg.settleSeconds * 1000);
  let retried = false;

  // Flow showed "We noticed some unusual activity": back off, reload the project in the SAME session (no new login) and re-submit
  // only the scenes that still have no image. At most BLOCK_RETRIES (max 2) times; the second wait is longer than the first.
  // Stops early when the step time budget is nearly used. The images already made are kept either way.
  for (let n = 1; blocked && n <= BLOCK_RETRIES && parsedOk; n++) {
    const waitMs = Math.round((n === 1 ? 90 + Math.random() * 60 : 240 + Math.random() * 120) * 1000);
    if (Date.now() - SCRIPT_START + waitMs + 120000 > STEP_LIMIT_MS) {
      console.log("::warning::Block retry " + n + " skipped: not enough step time left");
      break;
    }
    console.log("::warning::Flow block retry " + n + " of " + BLOCK_RETRIES + ": waiting " + Math.round(waitMs / 1000) + "s, then reloading Flow");
    mark('block_retry_' + n + '_wait');
    await flow.waitForTimeout(waitMs);
    try {
      await flow.goto(FLOW_URL, { waitUntil: "domcontentloaded" });
      await flow.waitForTimeout(12000);
      const ftxt = await flow.locator("body").innerText().catch(() => "");
      if (BLOCK_RE.test(ftxt)) {
        console.log("Flow still shows the unusual-activity notice after the wait");
        await flow.screenshot({ path: path.join(DEBUG_DIR, "blocked-retry" + n + ".png") }).catch(() => {});
        continue;
      }
      blocked = false;
      await applySceneSettings(true);
      const cur = collectImages(orderScenesFromState(state, parsedOk));
      const miss = new Set(cur.missingScenes);
      const redo = state.map((s, i) => ({ s, i })).filter((x) => x.s.state !== "GENERATION_COMPLETED" || miss.has(x.i + 1));
      if (!redo.length) { console.log("Block retry " + n + ": every scene already has an image"); break; }
      console.log("Block retry " + n + " for prompts:", redo.map((x) => x.i + 1).join(","));
      const redoPrompts = redo.map((x) => prompts[x.i]);
      const before = await ui.locator(cfg.selectors.status).innerText().catch(() => "");
      await submit(redoPrompts);
      await ui.locator(cfg.selectors.startButton).click();
      await flow.bringToFront();
      await flow.waitForTimeout(8000);
      retried = true;
      mark('block_retry_' + n + '_started');
      const left = STEP_LIMIT_MS - (Date.now() - SCRIPT_START);
      setDeadline(Math.max(2, Math.min(6, Math.floor(left / 60000) - 1)));
      done = await waitForCompletion(before, redoPrompts.length, redoPrompts);
      const txt = await dumpUiText(ui, "05-blockretry" + n + "-ui-end-text.txt");
      await ui.screenshot({ path: path.join(DEBUG_DIR, "05-blockretry" + n + "-ui-end.png") });
      parseQueue(txt, redoPrompts).forEach((r, k) => {
        const o = redo[k]; if (!o) return;
        state[o.i].attempts += 1; state[o.i].state = r.state; state[o.i].ui_status = r.ui_status;
      });
      await flow.waitForTimeout(cfg.settleSeconds * 1000);
    } catch (e) {
      console.log("::warning::Block retry " + n + " failed to run:", String(e && e.message).slice(0, 160));
      blocked = true; // the original cause was the block: keep the images so far and let the normal providers fill the rest
      break;
    }
  }

  // Retry prompts the extension marked FAILED AND prompts marked done whose scene has no usable image
  // (only judged when the prompt->media mapping was captured), only when the queue was read cleanly.
  for (let pass = 1; pass <= RETRY_PASSES && parsedOk; pass++) {
    const cur = collectImages(orderScenesFromState(state, parsedOk));
    const miss = new Set(cur.missingScenes);
    const redo = state.map((s, i) => ({ s, i })).filter((x) => x.s.state === "GENERATION_FAILED" || miss.has(x.i + 1));
    if (!redo.length || Date.now() > deadline || blocked) break;
    console.log("Retry pass " + pass + " for prompts:", redo.map((x) => x.i + 1).join(","));
    try {
      const redoPrompts = redo.map((x) => prompts[x.i]);
      const before = await ui.locator(cfg.selectors.status).innerText().catch(() => "");
      await submit(redoPrompts);
      await ui.locator(cfg.selectors.startButton).click();
      await flow.bringToFront();
      await flow.waitForTimeout(8000);
      retried = true;
      done = (await waitForCompletion(before, redoPrompts.length, redoPrompts)) && done;
      const txt = await dumpUiText(ui, "05-retry" + pass + "-ui-end-text.txt");
      await ui.screenshot({ path: path.join(DEBUG_DIR, "05-retry" + pass + "-ui-end.png") });
      parseQueue(txt, redoPrompts).forEach((r, k) => {
        const o = redo[k]; if (!o) return;
        state[o.i].attempts += 1; state[o.i].state = r.state; state[o.i].ui_status = r.ui_status;
      });
    } catch (e) {
      console.log("Retry pass failed to run:", String(e && e.message).slice(0, 160));
      break;
    }
  }

  if (blocked) mark('blocked_unusual_activity');
  if (retried) await flow.waitForTimeout(cfg.settleSeconds * 1000);
  try { if (/\/project\//.test(flow.url())) saveCookies(await context.cookies()); } catch (e) {}
  await pullSwLog();
  await context.close();

  const img = collectImages(orderScenesFromState(state, parsedOk));
  TRACKER.writeDebug();
  writeDownloadNames();
  EVID.results = prompts.map((p, i) => ({ index: i + 1, prompt: p.slice(0, 80), state: state[i].state, ui_status: state[i].ui_status, attempts: state[i].attempts }));
  const count = (s) => EVID.results.filter((r) => r.state === s).length;
  const expectedImages = prompts.length * WANT.count;
  const completed = count('GENERATION_COMPLETED');
  const failedPrompts = count('GENERATION_FAILED');
  const unfinished = prompts.length - completed - failedPrompts;
  const over = detectOverGeneration({ found: img.accepted + img.rejected, expected: expectedImages });
  const verdict = computeBatchStatus({
    imagesAccepted: img.accepted, expectedImages, imagesWrongRatio: img.rejected,
    failedPrompts, unfinishedPrompts: unfinished, overGenerated: over.over, extraImages: over.extra,
    extensionDone: done, settingsVerified: !!EVID.flow.settings.verified
  });
  if (img.mapped && img.missingScenes.length) verdict.reasons.push("scenes without an image: " + img.missingScenes.join(","));
  EVID.summary = {
    batch_status: verdict.status, status_reasons: verdict.reasons,
    prompts: prompts.length, wanted: { model: WANT.model, ratio: ASP.label, per_prompt: WANT.count },
    settings_verified: EVID.flow.settings.verified, settings_verdict: EVID.flow.settings.verdict,
    extension_done: done, completion_via: completionVia || null,
    generation_completed: completed, generation_failed: failedPrompts, generation_unfinished: unfinished,
    expected_images: expectedImages, images_accepted: img.accepted, images_wrong_ratio: img.rejected,
    over_generated: over.over, extra_images: over.extra,
    mapping_available: img.mapped, missing_scenes: img.missingScenes,
    thumbnail: THUMB_FILE ? { requested: true, ratio: THUMB_ASP.label, ran: thumb.ran, saved: thumb.saved, settings_verdict: EVID.flow.thumbSettings ? EVID.flow.thumbSettings.verdict : null } : { requested: false }
  };
  writeEvidence();
  fs.writeFileSync(path.join(FINAL_DIR, "manifest.json"), JSON.stringify({ generated_at: new Date().toISOString(), batch_status: verdict.status, status_reasons: verdict.reasons, summary: EVID.summary, prompts: EVID.results, scenes: img.scenes, missing_scenes: img.missingScenes, images: img.items, note: "scene_NN.* = image for prompt NN, matched by Flow media UUID, by the [sNN] tag in the file name, or (last resort) by download order. unmatched_* = accepted images that map to no scene. Scenes listed in missing_scenes need the Cloudflare fallback. When mapping_available is false, images keep Flow's names and scene order is unknown." }, null, 2));
  console.log('PROMPT_RESULTS', JSON.stringify(EVID.results.map((r) => r.index + ':' + r.state)));
  console.log('EVIDENCE_SUMMARY', JSON.stringify(EVID.summary), 'http_errors=' + EVID.http.length, 'console_errors=' + EVID.console.length, 'failed_requests=' + EVID.requestFailed.length);
  console.log("RESULT prompts=" + prompts.length + " expected_images=" + expectedImages + " accepted=" + img.accepted + " wrong_ratio=" + img.rejected + " extra=" + over.extra + " extension_done=" + done);
  console.log("BATCH_STATUS " + verdict.status + (verdict.reasons.length ? " | " + verdict.reasons.join("; ") : ""));

  if (process.env.GITHUB_STEP_SUMMARY) {
    const s = EVID.summary;
    const md = ["### Flow batch result: " + s.batch_status, "", verdict.reasons.length ? "Reasons: " + verdict.reasons.join("; ") : "All prompts completed and verified.", "", "| Item | Value |", "|---|---|",
      "| Prompts | " + s.prompts + " |", "| Wanted | " + s.wanted.model + ", " + s.wanted.ratio + ", x" + s.wanted.per_prompt + " |",
      "| Settings verified | " + s.settings_verified + " (" + s.settings_verdict + ") |", "| Generation completed / failed / unfinished | " + s.generation_completed + " / " + s.generation_failed + " / " + s.generation_unfinished + " |",
      "| Images kept (right ratio) | " + s.images_accepted + " of " + s.expected_images + " expected |", "| Images rejected (wrong ratio) | " + s.images_wrong_ratio + " (in downloads/rejected) |",
      "| Extra images beyond request | " + s.extra_images + " |", "| Scene mapping | " + (s.mapping_available ? "UUID/tag/order match, missing scenes: " + (s.missing_scenes.join(",") || "none") : "NOT available") + " |",
      "| Thumbnail | " + (s.thumbnail.requested ? (s.thumbnail.saved ? "saved (" + s.thumbnail.ratio + ")" : "none (ran: " + s.thumbnail.ran + ")") : "not requested") + " |", ""].join(NL);
    try { fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, md + NL); } catch (e) {}
  }
  if (blocked) {
    console.log("::warning::Flow was blocked ('unusual activity'). Kept " + img.accepted + " image(s); the remaining scenes use the normal image providers.");
    process.exit(0);
  }
  if (verdict.status === "FAILED") { console.log("::error::Batch FAILED - " + verdict.reasons.join("; ")); process.exit(1); }
  if (verdict.status === "PARTIAL_SUCCESS") {
    console.log("::warning::PARTIAL_SUCCESS - kept " + img.accepted + " image(s); " + verdict.reasons.join("; ") + ". See manifest.json, debug/flow-network.json and downloads/rejected");
    if (cfg.failOnPartial === true) process.exit(1);
  }
})().catch((e) => { console.error(e.message || e); try { EVID.error = redact(e && e.message); writeEvidence(); writeDownloadNames(); } catch (_) {} process.exit(1); });
