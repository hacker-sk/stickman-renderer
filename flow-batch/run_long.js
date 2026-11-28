#!/usr/bin/env node
// Long landscape video: generates 200+ images with the existing run_flow.js and keeps every image in a persistent folder
// (long_images/scene_NNN.<ext>, NNN = prompt number in long/long_flow.json). The workflow caches that folder, so:
//  - ALL missing prompts go into ONE Flow session (LONG_CHUNK=0, the default). Only an error ends the session early. Set LONG_CHUNK=N to
//    split into sessions of N prompts instead.
//  - whatever is still missing after the session is generated again in the next round (LONG_ROUNDS, default 3, again one session for all of it),
//  - a failed or cancelled run leaves everything it made in the cache and the next run continues with the missing ones only.
// run_flow.js caps its wait at 10 minutes, far too short for 200 images, so a patched COPY (run_flow_long.js) is used whose wait is
// FLOW_MAX_WAIT_MIN = the time left in LONG_BUDGET_MIN. run_flow.js itself is not touched.
// The same copy also gets a second patch: if the extension's prompt box / Start button is not usable for the real batch (this happened
// after a blocked warm-up, the warm-up queue never finished and the extension page stayed busy), the extension page is closed and opened
// fresh ONCE and the batch is submitted again, the same thing the block retries of run_flow.js already do.
//
// "Unusual activity" = Flow blocks the session. Retrying inside the SAME browser (wait, reload, submit again) kept ending with the same
// block. What worked was the "Flow images, pass 2" workflow step: a brand-new `node run_long.js`, i.e. a new browser with a deleted
// profile, fresh cookies load, fresh extension queue, fresh warm-up, and only the still-missing scenes. So EVERY block retry now does
// exactly that: when a session ends blocked (debug/blocked-final.flag), this script waits (LONG_BLOCK_WAITS_S), starts a FRESH session
// (new browser process, `profile` folder deleted, same as runChunk always does) and generates only what is still missing. Up to
// LONG_BLOCK_SESSIONS fresh sessions (default 5). The in-session retries of run_flow.js are switched off for this (FLOW_BLOCK_RETRIES=0)
// so a blocked session ends at once and keeps the images it made. LONG_BLOCK_SESSIONS=0 = the old behaviour (in-session retries, stop).
// The cursor position is kept between these sessions (FLOW_CURSOR_FILE, see human.js), so each new session starts the mouse where the
// last one left it.
// A block can also show up EARLY: the very first (warm-up) image hits the notice, run_flow.js then never gets its Start button enabled and
// ends with "Start button stayed disabled" without writing blocked-final.flag. That case is now recognised too (debug/cooldown/cooldown.json
// says blocked, or the Start-button error appeared, and the session made no image) and handled exactly like a block: wait, fresh session.
// Stops when Flow is STILL blocked after all fresh sessions, on an auth/settings error, when a normal round makes no progress,
// or when the time budget is used up.
// `--check` only lists what is missing. Exit code 0 = every image exists, 1 = some are missing.
const fs = require("fs");
const os = require("os");
const path = require("path");
const cp = require("child_process");

const ROOT = __dirname;
const PROMPTS = path.resolve(ROOT, process.env.LONG_PROMPTS || "long/long_flow.json");
const CACHE = path.resolve(ROOT, process.env.LONG_CACHE || "long_images");
const DEBUG_ALL = path.resolve(ROOT, "long_debug");
const CHUNK_FILE = "prompts/long_chunk.json";
const ROUNDS = Math.max(1, parseInt(process.env.LONG_ROUNDS || "3", 10) || 3);
const BUDGET_MS = (parseFloat(process.env.LONG_BUDGET_MIN || "140") || 140) * 60000;
const FATAL_RE = /AUTH FAILED|SETTINGS MISMATCH|SETTINGS UNVERIFIED|secret is missing|Invalid model|Unknown aspect/i;
// Fresh-session block retries (see the header). Waits (seconds) before fresh session 1..5; the last value repeats if more are asked for.
// Default waits are longer than before (2, 5, 10, 15, 20 min): two fresh sessions started only 3 minutes apart were both blocked again at
// their first image. Override with LONG_BLOCK_WAITS_S (comma separated seconds).
const FRESH_MAX = (() => { const n = parseInt(process.env.LONG_BLOCK_SESSIONS || "5", 10); return Number.isNaN(n) ? 5 : Math.max(0, Math.min(n, 10)); })();
const FRESH_WAITS_S = (() => {
  const a = String(process.env.LONG_BLOCK_WAITS_S || "120,300,600,900,1200").split(",").map((x) => parseFloat(x)).filter((x) => x >= 0);
  return a.length ? a : [120, 300, 600, 900, 1200];
})();
const CURSOR_FILE = path.join(os.tmpdir(), "flow_cursor_" + process.pid + ".json"); // one cursor memory for all sessions of this run
const started = Date.now();

const all = JSON.parse(fs.readFileSync(PROMPTS, "utf8")).map((p) => (typeof p === "string" ? p : p.prompt));
const N = all.length;
const CHUNK = (parseInt(process.env.LONG_CHUNK || "0", 10) || 0) > 0 ? parseInt(process.env.LONG_CHUNK, 10) : N;
fs.mkdirSync(CACHE, { recursive: true });

function pad(n) { return String(n).padStart(3, "0"); }
function have(p) {
  const pre = "scene_" + pad(p) + ".";
  return fs.readdirSync(CACHE).some((f) => f.indexOf(pre) === 0 && fs.statSync(path.join(CACHE, f)).size > 10000);
}
function missing() { const m = []; for (let p = 1; p <= N; p++) if (!have(p)) m.push(p); return m; }
function rm(p) { fs.rmSync(p, { recursive: true, force: true }); }
function sleepSec(s) { try { cp.spawnSync("sleep", [String(Math.max(0, Math.round(s)))]); } catch (e) {} }

try { rm(CURSOR_FILE); } catch (e) {}
process.on("exit", () => { try { rm(CURSOR_FILE); } catch (e) {} });

function finish(msg) {
  const m = missing();
  const line = "LONG_IMAGES " + (N - m.length) + " of " + N + (m.length ? " | missing: " + m.join(",") : " | complete");
  console.log(line + (msg ? " | " + msg : ""));
  if (process.env.GITHUB_STEP_SUMMARY) {
    try { fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, "### Long video images: " + (N - m.length) + " of " + N + (m.length ? " (missing " + m.length + ")" : " complete") + "\n"); } catch (e) {}
  }
  process.exit(m.length ? 1 : 0);
}

if (process.argv.includes("--check")) finish("check only");

// patched copy of run_flow.js: (a) longer wait (the original caps it at 10 minutes), (b) one reopen of the extension page when the
// real batch cannot be submitted (prompt box or Start button not usable)
const FLOW_SCRIPT = "run_flow_long.js";
{
  let src = fs.readFileSync(path.join(ROOT, "run_flow.js"), "utf8");
  const a = src.replace("Math.min(minutes, 10)", "Math.min(minutes, +(process.env.FLOW_MAX_WAIT_MIN || 10))");
  const b = a.replace("setDeadline(cfg.timeoutMinutes)", "setDeadline(+(process.env.FLOW_MAX_WAIT_MIN || cfg.timeoutMinutes))");
  if (a === src || b === a) console.log("::warning::run_flow.js wait cap not patched (text changed?), the wait stays at 10 minutes");
  const oldSubmit = "    await submit(prompts);\n  } catch (e) {\n    await ui.screenshot({ path: path.join(DEBUG_DIR, \"03-disabled.png\") });";
  const newSubmit = "    try { await submit(prompts); } catch (e0) { console.log(\"Real batch: the extension prompt box / Start button was not usable (\" + String(e0 && e0.message).slice(0, 100) + \") - reopening the extension page once\"); await resetUi(); await submit(prompts); }\n  } catch (e) {\n    await ui.screenshot({ path: path.join(DEBUG_DIR, \"03-disabled.png\") });";
  const c = b.replace(oldSubmit, newSubmit);
  if (c === b) console.log("::warning::run_flow.js submit retry not patched (text changed?), a blocked extension page is not reopened");
  fs.writeFileSync(path.join(ROOT, FLOW_SCRIPT), c);
}

let blocked = false, fatal = false, outOfTime = false;

function runChunk(round, ci, list) {
  // A fresh browser every time: downloads, debug and the browser profile are deleted first (this is what makes a "pass 2" different
  // from a retry inside the same browser).
  for (const d of ["downloads", "debug", "profile"]) rm(path.join(ROOT, d));
  rm(path.join(os.homedir(), "Downloads"));
  fs.writeFileSync(path.join(ROOT, CHUNK_FILE), JSON.stringify(list.map((p) => all[p - 1])));
  const waitMin = Math.max(10, Math.floor((BUDGET_MS - (Date.now() - started)) / 60000));
  console.log("== round " + round + " session " + ci + ": " + list.length + " prompt(s), wait up to " + waitMin + " min (" + (FRESH_MAX > 0 ? "a block ends the session at once, a fresh session follows" : "block retries inside the session: up to 5") + ")");
  const childEnv = { PROMPTS_FILE: CHUNK_FILE, FLOW_COUNT: "1", FLOW_MAX_WAIT_MIN: String(waitMin), FLOW_STEP_LIMIT_MIN: String(waitMin + 12), FLOW_CURSOR_FILE: CURSOR_FILE };
  if (FRESH_MAX > 0) childEnv.FLOW_BLOCK_RETRIES = "0"; // no in-session retries: the next retry is a fresh session
  const r = cp.spawnSync("node", [FLOW_SCRIPT], {
    cwd: ROOT, timeout: (waitMin + 20) * 60000, killSignal: "SIGKILL", encoding: "utf8", maxBuffer: 1 << 28,
    stdio: ["ignore", "inherit", "pipe"],
    env: Object.assign({}, process.env, childEnv)
  });
  const err = String(r.stderr || "");
  if (err) process.stderr.write(err.slice(-4000));
  if (r.error) console.log("session run error:", String(r.error.message).slice(0, 160));
  try { cp.spawnSync("pkill", ["-f", "chrom"]); } catch (e) {}
  if (FATAL_RE.test(err)) { fatal = true; console.log("::error::Flow setup problem, no more sessions: " + err.trim().slice(0, 200)); }
  // Only a block that is still there when the session ends counts (debug/blocked.png also exists when a retry got past the block).
  blocked = fs.existsSync(path.join(ROOT, "debug", "blocked-final.flag"));
  // The early block: the warm-up image already hit the "unusual activity" notice (debug/cooldown/cooldown.json -> blocked) and/or the
  // extension's Start button never enabled. run_flow.js then ends with an error and writes NO blocked-final.flag, so it used to look like
  // "no progress" and the run stopped after a few minutes. Judged after the images are collected (a session that made images is not blocked).
  let warmBlocked = false;
  try { warmBlocked = JSON.parse(fs.readFileSync(path.join(ROOT, "debug", "cooldown", "cooldown.json"), "utf8")).blocked === true; } catch (e) {}
  const startDisabled = /Start button stayed disabled/i.test(err);

  // collect: scene_NN in downloads/final = NN-th prompt of THIS session
  const fin = path.join(ROOT, "downloads", "final");
  let got = 0;
  if (fs.existsSync(fin)) {
    for (const f of fs.readdirSync(fin)) {
      const m = f.match(/^scene_(\d+)\.(png|jpe?g|webp)$/i);
      if (!m) continue;
      const p = list[parseInt(m[1], 10) - 1];
      if (!p || have(p)) continue;
      fs.copyFileSync(path.join(fin, f), path.join(CACHE, "scene_" + pad(p) + "." + m[2].toLowerCase()));
      got += 1;
    }
  }
  if (!blocked && !fatal && got === 0 && (warmBlocked || startDisabled)) {
    blocked = true;
    console.log("::warning::Flow is blocking this session (" + (warmBlocked ? "the warm-up image hit the 'unusual activity' notice" : "the Start button never enabled") + ") and no image was made - handled as a block: wait, then a fresh session");
  }
  console.log("session " + ci + " kept " + got + " of " + list.length + " new image(s)" + (blocked ? " | Flow blocked this session" : ""));
  try {
    fs.mkdirSync(DEBUG_ALL, { recursive: true });
    const dst = path.join(DEBUG_ALL, "r" + round + "s" + ci);
    if (fs.existsSync(path.join(ROOT, "debug"))) fs.renameSync(path.join(ROOT, "debug"), dst);
  } catch (e) {}
}

let rounds = 0;    // normal rounds (limit LONG_ROUNDS)
let freshRuns = 0; // fresh sessions started because the last session was blocked (limit LONG_BLOCK_SESSIONS)
let passNo = 0;    // numbering for logs and debug folders
while (!fatal && !outOfTime) {
  const miss = missing();
  if (!miss.length) break;
  if (blocked) {
    if (freshRuns >= FRESH_MAX) break; // still blocked after every fresh session
    const w = FRESH_WAITS_S[Math.min(freshRuns, FRESH_WAITS_S.length - 1)];
    const left = BUDGET_MS - (Date.now() - started);
    if (left < (w + 15 * 60) * 1000) { outOfTime = true; console.log("::warning::not enough time budget left for another fresh session (" + Math.max(0, Math.round(left / 60000)) + " min left)"); break; }
    freshRuns += 1;
    console.log("::warning::Flow blocked ('unusual activity'): fresh session " + freshRuns + " of " + FRESH_MAX + " (new browser, new profile, only the " + miss.length + " missing image(s)) after a " + w + " s wait");
    sleepSec(w);
    blocked = false;
  } else {
    if (rounds >= ROUNDS) break;
    rounds += 1;
  }
  passNo += 1;
  const before = N - miss.length;
  console.log("== round " + passNo + ": " + miss.length + " image(s) missing");
  let ci = 0;
  for (let i = 0; i < miss.length; i += CHUNK) {
    if (Date.now() - started > BUDGET_MS - 10 * 60000) { outOfTime = true; console.log("::warning::time budget used up, stopping (cache keeps everything)"); break; }
    ci += 1;
    runChunk(passNo, ci, miss.slice(i, i + CHUNK));
    if (blocked || fatal) break;
  }
  // A blocked session that made no image is handled by the fresh-session retries above; only a normal round without progress stops here.
  if (!blocked && !fatal && !outOfTime && N - missing().length === before) { console.log("no progress in round " + passNo + ", stopping"); break; }
}
finish(blocked ? "Flow blocked (unusual activity), still blocked after " + freshRuns + " fresh session(s)" : fatal ? "Flow setup problem" : outOfTime ? "time budget used up" : "");
