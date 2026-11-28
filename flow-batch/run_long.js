#!/usr/bin/env node
// Long landscape video: generates 200+ images with the existing run_flow.js and keeps every image in a persistent folder
// (long_images/scene_NNN.<ext>, NNN = prompt number in long/long_flow.json). The workflow caches that folder, so:
//  - ALL missing prompts go into ONE Flow session (LONG_CHUNK=0, the default). Only an error ends the session early. Set LONG_CHUNK=N to
//    split into sessions of N prompts instead.
//  - whatever is still missing after the session is generated again in the next round (LONG_ROUNDS, default 3, again one session for all of it),
//  - a failed or cancelled run leaves everything it made in the cache and the next run continues with the missing ones only.
// run_flow.js caps its wait at 10 minutes, far too short for 200 images, so a patched COPY (run_flow_long.js) is used whose wait is
// FLOW_MAX_WAIT_MIN = the time left in LONG_BUDGET_MIN. run_flow.js itself is not touched.
// "Unusual activity": inside a session run_flow.js backs off and retries up to 5 times (growing waits, same browser session). Those retries
// need time too, so the session gets FLOW_STEP_LIMIT_MIN = wait + 12 minutes as its overall time budget (the process is killed 8 minutes later).
// Stops when Flow is STILL blocked after all retries (debug/blocked-final.flag), on an auth/settings error, when a round makes no progress,
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

// patched copy of run_flow.js with a longer wait (the original caps it at 10 minutes)
const FLOW_SCRIPT = "run_flow_long.js";
{
  let src = fs.readFileSync(path.join(ROOT, "run_flow.js"), "utf8");
  const a = src.replace("Math.min(minutes, 10)", "Math.min(minutes, +(process.env.FLOW_MAX_WAIT_MIN || 10))");
  const b = a.replace("setDeadline(cfg.timeoutMinutes)", "setDeadline(+(process.env.FLOW_MAX_WAIT_MIN || cfg.timeoutMinutes))");
  if (a === src || b === a) console.log("::warning::run_flow.js wait cap not patched (text changed?), the wait stays at 10 minutes");
  fs.writeFileSync(path.join(ROOT, FLOW_SCRIPT), b);
}

let blocked = false, fatal = false, outOfTime = false;

function runChunk(round, ci, list) {
  for (const d of ["downloads", "debug", "profile"]) rm(path.join(ROOT, d));
  rm(path.join(os.homedir(), "Downloads"));
  fs.writeFileSync(path.join(ROOT, CHUNK_FILE), JSON.stringify(list.map((p) => all[p - 1])));
  const waitMin = Math.max(10, Math.floor((BUDGET_MS - (Date.now() - started)) / 60000));
  console.log("== round " + round + " session " + ci + ": " + list.length + " prompt(s), wait up to " + waitMin + " min (block retries inside the session: up to 5)");
  const r = cp.spawnSync("node", [FLOW_SCRIPT], {
    cwd: ROOT, timeout: (waitMin + 20) * 60000, killSignal: "SIGKILL", encoding: "utf8", maxBuffer: 1 << 28,
    stdio: ["ignore", "inherit", "pipe"],
    env: Object.assign({}, process.env, { PROMPTS_FILE: CHUNK_FILE, FLOW_COUNT: "1", FLOW_MAX_WAIT_MIN: String(waitMin), FLOW_STEP_LIMIT_MIN: String(waitMin + 12) })
  });
  const err = String(r.stderr || "");
  if (err) process.stderr.write(err.slice(-4000));
  if (r.error) console.log("session run error:", String(r.error.message).slice(0, 160));
  try { cp.spawnSync("pkill", ["-f", "chrom"]); } catch (e) {}
  if (FATAL_RE.test(err)) { fatal = true; console.log("::error::Flow setup problem, no more sessions: " + err.trim().slice(0, 200)); }
  // Only a block that is still there after every retry stops the rounds (debug/blocked.png also exists when a retry got past the block).
  if (fs.existsSync(path.join(ROOT, "debug", "blocked-final.flag"))) blocked = true;

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
  console.log("session " + ci + " kept " + got + " of " + list.length + " new image(s)");
  try {
    fs.mkdirSync(DEBUG_ALL, { recursive: true });
    const dst = path.join(DEBUG_ALL, "r" + round + "s" + ci);
    if (fs.existsSync(path.join(ROOT, "debug"))) fs.renameSync(path.join(ROOT, "debug"), dst);
  } catch (e) {}
}

for (let round = 1; round <= ROUNDS && !blocked && !fatal && !outOfTime; round++) {
  const miss = missing();
  if (!miss.length) break;
  const before = N - miss.length;
  console.log("== round " + round + ": " + miss.length + " image(s) missing");
  let ci = 0;
  for (let i = 0; i < miss.length; i += CHUNK) {
    if (Date.now() - started > BUDGET_MS - 10 * 60000) { outOfTime = true; console.log("::warning::time budget used up, stopping (cache keeps everything)"); break; }
    ci += 1;
    runChunk(round, ci, miss.slice(i, i + CHUNK));
    if (blocked || fatal) break;
  }
  if (N - missing().length === before) { console.log("no progress in round " + round + ", stopping"); break; }
}
finish(blocked ? "Flow blocked (unusual activity), still blocked after the retries" : fatal ? "Flow setup problem" : outOfTime ? "time budget used up" : "");
