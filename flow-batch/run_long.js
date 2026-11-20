#!/usr/bin/env node
// Long landscape video: generates 200+ images with the existing run_flow.js in small CHUNKS and keeps every image in a persistent
// folder (long_images/scene_NNN.<ext>, NNN = prompt number in long/long_flow.json). The workflow caches that folder, so:
//  - a failed prompt/chunk is simply still "missing" and is generated again in the next round (LONG_ROUNDS, default 3),
//  - a failed or cancelled run leaves everything it made in the cache and the next run continues with the missing ones only.
// Stops early when Flow shows "unusual activity" (blocked), on an auth/settings error, when a round makes no progress, or when the
// time budget (LONG_BUDGET_MIN) is used up (no new chunk is started after that). `--check` only lists what is missing.
// Exit code 0 = every image exists, 1 = some are missing.
const fs = require("fs");
const os = require("os");
const path = require("path");
const cp = require("child_process");

const ROOT = __dirname;
const PROMPTS = path.resolve(ROOT, process.env.LONG_PROMPTS || "long/long_flow.json");
const CACHE = path.resolve(ROOT, process.env.LONG_CACHE || "long_images");
const DEBUG_ALL = path.resolve(ROOT, "long_debug");
const CHUNK_FILE = "prompts/long_chunk.json";
const CHUNK = Math.max(1, parseInt(process.env.LONG_CHUNK || "20", 10) || 20);
const ROUNDS = Math.max(1, parseInt(process.env.LONG_ROUNDS || "3", 10) || 3);
const BUDGET_MS = (parseFloat(process.env.LONG_BUDGET_MIN || "140") || 140) * 60000;
const CHUNK_TIMEOUT_MS = 25 * 60000;
const FATAL_RE = /AUTH FAILED|SETTINGS MISMATCH|SETTINGS UNVERIFIED|secret is missing|Invalid model|Unknown aspect/i;
const started = Date.now();

const all = JSON.parse(fs.readFileSync(PROMPTS, "utf8")).map((p) => (typeof p === "string" ? p : p.prompt));
const N = all.length;
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

let blocked = false, fatal = false, outOfTime = false;

function runChunk(round, ci, list) {
  for (const d of ["downloads", "debug", "profile"]) rm(path.join(ROOT, d));
  rm(path.join(os.homedir(), "Downloads"));
  fs.writeFileSync(path.join(ROOT, CHUNK_FILE), JSON.stringify(list.map((p) => all[p - 1])));
  console.log("== round " + round + " chunk " + ci + ": prompts " + list.join(","));
  const r = cp.spawnSync("node", ["run_flow.js"], {
    cwd: ROOT, timeout: CHUNK_TIMEOUT_MS, killSignal: "SIGKILL", encoding: "utf8", maxBuffer: 1 << 28,
    stdio: ["ignore", "inherit", "pipe"],
    env: Object.assign({}, process.env, { PROMPTS_FILE: CHUNK_FILE, FLOW_COUNT: "1" })
  });
  const err = String(r.stderr || "");
  if (err) process.stderr.write(err.slice(-4000));
  if (r.error) console.log("chunk run error:", String(r.error.message).slice(0, 160));
  try { cp.spawnSync("pkill", ["-f", "chrom"]); } catch (e) {}
  if (FATAL_RE.test(err)) { fatal = true; console.log("::error::Flow setup problem, no more chunks: " + err.trim().slice(0, 200)); }
  if (fs.existsSync(path.join(ROOT, "debug", "blocked.png"))) blocked = true;

  // collect: scene_NN in downloads/final = NN-th prompt of THIS chunk
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
  console.log("chunk " + ci + " kept " + got + " of " + list.length + " new image(s)");
  try {
    fs.mkdirSync(DEBUG_ALL, { recursive: true });
    const dst = path.join(DEBUG_ALL, "r" + round + "c" + ci);
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
    if (Date.now() - started > BUDGET_MS) { outOfTime = true; console.log("::warning::time budget used up, stopping (cache keeps everything)"); break; }
    ci += 1;
    runChunk(round, ci, miss.slice(i, i + CHUNK));
    if (blocked || fatal) break;
  }
  if (N - missing().length === before) { console.log("no progress in round " + round + ", stopping"); break; }
}
finish(blocked ? "Flow blocked (unusual activity)" : fatal ? "Flow setup problem" : outOfTime ? "time budget used up" : "");
