#!/usr/bin/env node
// Shorts Flow step with "fresh session" block retries (same idea as run_long.js and the "Flow images, pass 2" step of long_landscape.yml).
//
// Why: when Flow shows "We noticed some unusual activity", retrying inside the SAME browser (wait, reload, submit again) kept ending with
// the same block. A brand-new session (new browser process, deleted profile, cookies loaded again, fresh extension queue, fresh warm-up,
// only the scenes that still have no image) got past it and made images. So every block retry is now such a fresh session.
//
// How:
//  1. Session 1 = the normal `node run_flow.js` with the prompts file (PROMPTS_FILE), on the browser profile that is already on disk
//     (the restored history files). In-session block retries of run_flow.js are switched off (FLOW_BLOCK_RETRIES=0), so a blocked
//     session ends at once and keeps the images it made.
//  2. When the session ends blocked (debug/blocked-final.flag) and scenes are still missing: wait (LONG... see below), delete downloads,
//     debug and profile, and run run_flow.js again with ONLY the missing prompts. Up to SHORTS_BLOCK_SESSIONS fresh sessions (default 5).
//     An EARLY block counts too (same rule as run_long.js): the warm-up image hit the "unusual activity" notice (debug/cooldown/cooldown.json
//     says blocked) or the Start button never enabled ("Start button stayed disabled"), and the session made no image. run_flow.js writes NO
//     blocked-final.flag in that case, so it is recognised here and handled exactly like a block: wait, then a fresh session.
//  3. Every session's images are kept in shorts_images/scene_NN.<ext> (NN = position of the prompt in the ORIGINAL prompts file). At the
//     end downloads/final is rebuilt from that folder, so the "flow-images" artifact looks exactly like a single-session run
//     (scene_NN.<ext> + manifest.json) and the make job needs no change.
//  4. The cursor position is kept between the sessions (FLOW_CURSOR_FILE, see human.js).
// Stops when Flow is still blocked after all fresh sessions, on an auth/settings error, when the time budget is used up, or when every
// scene has an image.
//
// Variables: SHORTS_BLOCK_SESSIONS (default 5, 0 = old behaviour: in-session retries of run_flow.js, no fresh session),
//            SHORTS_BLOCK_WAITS_S (default "60,180,300,480,600" = seconds to wait before fresh session 1..5),
//            FLOW_STEP_LIMIT_MIN (total time budget of this script in minutes, set by the workflow, default 95).
const fs = require("fs");
const os = require("os");
const path = require("path");
const cp = require("child_process");

const ROOT = __dirname;
const PROMPTS_REL = process.env.PROMPTS_FILE || "prompts/batch.json";
const PROMPTS = path.resolve(ROOT, PROMPTS_REL);
const DL = path.join(ROOT, "downloads");
const FINAL = path.join(DL, "final");
const REJ = path.join(DL, "rejected");
const DEBUG = path.join(ROOT, "debug");
const SESS_DEBUG = path.join(ROOT, "debug_sessions");
const REJ_ALL = path.join(ROOT, "rejected_all");
const CACHE = path.join(ROOT, "shorts_images");
const CHUNK_FILE = "prompts/shorts_chunk.json";
const FATAL_RE = /AUTH FAILED|SETTINGS MISMATCH|SETTINGS UNVERIFIED|secret is missing|Invalid model|Unknown aspect/i;
const FRESH_MAX = (() => { const n = parseInt(process.env.SHORTS_BLOCK_SESSIONS || "5", 10); return Number.isNaN(n) ? 5 : Math.max(0, Math.min(n, 10)); })();
const FRESH_WAITS_S = (() => {
  const a = String(process.env.SHORTS_BLOCK_WAITS_S || "60,180,300,480,600").split(",").map((x) => parseFloat(x)).filter((x) => x >= 0);
  return a.length ? a : [60, 180, 300, 480, 600];
})();
const STEP_MS = (() => { const m = parseFloat(process.env.FLOW_STEP_LIMIT_MIN || ""); return (m > 0 ? m : 95) * 60000; })();
const CURSOR_FILE = path.join(os.tmpdir(), "flow_cursor_" + process.pid + ".json"); // one cursor memory for all sessions of this run
const started = Date.now();

function rm(p) { fs.rmSync(p, { recursive: true, force: true }); }
function sleepSec(s) { try { cp.spawnSync("sleep", [String(Math.max(0, Math.round(s)))]); } catch (e) {} }
function pad(n) { return String(n).padStart(2, "0"); }
try { rm(CURSOR_FILE); } catch (e) {}
process.on("exit", () => { try { rm(CURSOR_FILE); } catch (e) {} });

// Same reading rules as run_flow.js (so the positions are the same): .json = array of strings or {prompt}, .txt = blank-line separated.
function loadPrompts(file) {
  const text = fs.readFileSync(file, "utf8").replace(/\r/g, "");
  let list;
  if (file.endsWith(".txt")) list = text.split(/\n[ \t]*\n/);
  else list = JSON.parse(text).map((p) => (typeof p === "string" ? p : p.prompt));
  return list.filter(Boolean).map((p) => p.replace(/\s*\n\s*/g, " ").trim()).filter(Boolean);
}

const all = loadPrompts(PROMPTS);
const N = all.length;
if (!N) { console.error("No prompts found in " + PROMPTS); process.exit(1); }
rm(CACHE);
fs.mkdirSync(CACHE, { recursive: true });
rm(SESS_DEBUG);
rm(REJ_ALL);

function have(n) {
  const pre = "scene_" + pad(n) + ".";
  return fs.readdirSync(CACHE).some((f) => f.indexOf(pre) === 0 && fs.statSync(path.join(CACHE, f)).size > 10000);
}
function missing() { const m = []; for (let n = 1; n <= N; n++) if (!have(n)) m.push(n); return m; }

let blocked = false, fatal = false, outOfTime = false, lastStatus = 0;
const sessions = [];

// list = the ORIGINAL prompt numbers (1-based) this session generates, in order; first = session 1 (original prompts file, profile kept).
function runSession(sno, list, first) {
  const miss0 = missing().length;
  if (!first) {
    for (const d of ["downloads", "debug", "profile"]) rm(path.join(ROOT, d));
    rm(path.join(os.homedir(), "Downloads"));
    fs.writeFileSync(path.join(ROOT, CHUNK_FILE), JSON.stringify(list.map((n) => all[n - 1])));
  }
  const leftMin = Math.max(12, Math.floor((STEP_MS - (Date.now() - started)) / 60000));
  console.log("== Flow session " + sno + (first ? " (first)" : " (fresh: new browser, deleted profile)") + ": " + list.length + " prompt(s), " + leftMin + " min of step time left" + (FRESH_MAX > 0 ? ", a block ends the session at once" : ", in-session block retries on"));
  const env = Object.assign({}, process.env, { FLOW_STEP_LIMIT_MIN: String(leftMin), FLOW_CURSOR_FILE: CURSOR_FILE });
  if (!first) env.PROMPTS_FILE = CHUNK_FILE;
  if (FRESH_MAX > 0) env.FLOW_BLOCK_RETRIES = "0"; // no in-session retries: the next retry is a fresh session
  const r = cp.spawnSync("node", ["run_flow.js"], {
    cwd: ROOT, timeout: (leftMin + 5) * 60000, killSignal: "SIGKILL", encoding: "utf8", maxBuffer: 1 << 28,
    stdio: ["ignore", "inherit", "pipe"], env
  });
  const err = String(r.stderr || "");
  if (err) process.stderr.write(err.slice(-4000));
  if (r.error) console.log("session run error:", String(r.error.message).slice(0, 160));
  lastStatus = typeof r.status === "number" ? r.status : 1;
  try { cp.spawnSync("pkill", ["-f", "chrom"]); } catch (e) {}
  if (FATAL_RE.test(err)) { fatal = true; console.log("::error::Flow setup problem, no more sessions: " + err.trim().slice(0, 200)); }
  blocked = fs.existsSync(path.join(DEBUG, "blocked-final.flag"));
  // The early block (same rule as run_long.js): the warm-up image already hit the "unusual activity" notice (debug/cooldown/cooldown.json
  // -> blocked) and/or the extension's Start button never enabled. run_flow.js then ends with an error and writes NO blocked-final.flag.
  // Judged after the images are collected (a session that made images is not blocked).
  let warmBlocked = false;
  try { warmBlocked = JSON.parse(fs.readFileSync(path.join(DEBUG, "cooldown", "cooldown.json"), "utf8")).blocked === true; } catch (e) {}
  const startDisabled = /Start button stayed disabled/i.test(err);

  // collect: scene_NN in downloads/final = NN-th prompt of THIS session -> original number list[NN-1] (session 1: list is 1..N)
  let got = 0;
  if (fs.existsSync(FINAL)) {
    for (const f of fs.readdirSync(FINAL)) {
      const m = f.match(/^scene_(\d+)\.(png|jpe?g|webp)$/i);
      if (!m) continue;
      const orig = list[parseInt(m[1], 10) - 1];
      if (!orig || have(orig)) continue;
      fs.copyFileSync(path.join(FINAL, f), path.join(CACHE, "scene_" + pad(orig) + "." + m[2].toLowerCase()));
      got += 1;
    }
  }
  if (!blocked && !fatal && got === 0 && (warmBlocked || startDisabled)) {
    blocked = true;
    console.log("::warning::Flow is blocking this session (" + (warmBlocked ? "the warm-up image hit the 'unusual activity' notice" : "the Start button never enabled") + ") and no image was made - handled as a block: wait, then a fresh session");
  }
  console.log("session " + sno + " kept " + got + " of " + list.length + " new image(s)" + (blocked ? " | Flow blocked this session" : ""));
  sessions.push({ session: sno, fresh: !first, prompts: list.length, new_images: got, blocked, exit_status: lastStatus, missing_before: miss0, missing_after: missing().length });

  // keep this session's debug files, manifest and wrong-ratio images for the artifact
  try {
    const dst = path.join(SESS_DEBUG, "s" + sno);
    fs.mkdirSync(dst, { recursive: true });
    if (fs.existsSync(DEBUG)) fs.cpSync(DEBUG, dst, { recursive: true });
    if (fs.existsSync(path.join(FINAL, "manifest.json"))) fs.copyFileSync(path.join(FINAL, "manifest.json"), path.join(dst, "session-manifest.json"));
    if (fs.existsSync(REJ)) fs.cpSync(REJ, path.join(REJ_ALL, "s" + sno), { recursive: true });
  } catch (e) {}
}

runSession(1, Array.from({ length: N }, (_, i) => i + 1), true);
let freshRuns = 0;
while (blocked && !fatal) {
  const miss = missing();
  if (!miss.length) break;
  if (freshRuns >= FRESH_MAX) break; // still blocked after every fresh session
  const w = FRESH_WAITS_S[Math.min(freshRuns, FRESH_WAITS_S.length - 1)];
  const left = STEP_MS - (Date.now() - started);
  if (left < (w + 12 * 60) * 1000) { outOfTime = true; console.log("::warning::not enough step time left for another fresh session (" + Math.max(0, Math.round(left / 60000)) + " min left)"); break; }
  freshRuns += 1;
  console.log("::warning::Flow blocked ('unusual activity'): fresh session " + freshRuns + " of " + FRESH_MAX + " (new browser, new profile, only the " + miss.length + " missing scene(s)) after a " + w + " s wait");
  sleepSec(w);
  blocked = false;
  runSession(1 + freshRuns, miss, false);
}

// Rebuild the folders the workflow uploads, exactly like a single-session run: downloads/final = scene_NN.<ext> + manifest.json.
rm(FINAL);
fs.mkdirSync(FINAL, { recursive: true });
const scenes = [];
for (let n = 1; n <= N; n++) {
  const f = fs.readdirSync(CACHE).find((x) => x.indexOf("scene_" + pad(n) + ".") === 0 && fs.statSync(path.join(CACHE, x)).size > 10000);
  if (f) fs.copyFileSync(path.join(CACHE, f), path.join(FINAL, f));
  scenes.push({ scene: n, file: f ? "final/" + f : null });
}
const miss = missing();
fs.writeFileSync(path.join(FINAL, "manifest.json"), JSON.stringify({
  generated_at: new Date().toISOString(),
  batch_status: miss.length === 0 ? "SUCCESS" : (miss.length < N ? "PARTIAL_SUCCESS" : "FAILED"),
  status_reasons: miss.length ? ["scenes without an image: " + miss.join(",")] : [],
  merged_from_sessions: sessions,
  scenes, missing_scenes: miss,
  note: "Merged from " + sessions.length + " Flow session(s) by run_shorts_flow.js. scene_NN.* = image for prompt NN of the original prompts file. Scenes in missing_scenes are made by the normal image providers in the make job."
}, null, 2));
try {
  rm(REJ);
  if (fs.existsSync(REJ_ALL)) {
    for (const d of fs.readdirSync(REJ_ALL)) {
      for (const f of fs.readdirSync(path.join(REJ_ALL, d))) {
        fs.mkdirSync(REJ, { recursive: true });
        fs.copyFileSync(path.join(REJ_ALL, d, f), path.join(REJ, d + "_" + f));
      }
    }
  }
  // debug/ = every session's debug files in a subfolder (s1, s2 ...) + a summary
  rm(DEBUG);
  fs.mkdirSync(DEBUG, { recursive: true });
  if (fs.existsSync(SESS_DEBUG)) fs.cpSync(SESS_DEBUG, DEBUG, { recursive: true });
  fs.writeFileSync(path.join(DEBUG, "shorts-sessions.json"), JSON.stringify({ sessions, fresh_sessions_used: freshRuns, fresh_sessions_max: FRESH_MAX, still_blocked: blocked, out_of_time: outOfTime }, null, 2));
} catch (e) {}

const line = "SHORTS_FLOW_IMAGES " + (N - miss.length) + " of " + N + (miss.length ? " | missing: " + miss.join(",") : " | complete") + " | sessions " + sessions.length;
console.log(line + (blocked ? " | Flow blocked (unusual activity), still blocked after " + freshRuns + " fresh session(s)" : fatal ? " | Flow setup problem" : outOfTime ? " | step time budget used up" : ""));
if (process.env.GITHUB_STEP_SUMMARY) {
  try { fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, "### Shorts Flow images: " + (N - miss.length) + " of " + N + " (" + sessions.length + " session(s), " + freshRuns + " fresh)\n"); } catch (e) {}
}
// Exit codes like run_flow.js: a block is not an error (the normal providers fill the gaps), a setup problem or a failed batch is.
process.exit(miss.length === 0 || blocked ? 0 : (fatal ? 1 : lastStatus || 0));
