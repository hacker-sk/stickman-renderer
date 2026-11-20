const fs = require("fs");
const path = require("path");

// Maps each submitted prompt (scene) to the Flow media UUIDs that came back for it.
// Flow names downloaded files by media UUID, so UUID-in-filename -> scene is deterministic.
// Every POST made by the browser is checked: if its payload contains one prompt's text, the UUIDs in its response belong to that prompt.
//
// Second way to match a file (used only when no media UUID in the file name matches): the pipeline starts every Flow prompt with a scene
// tag ([s01], [s02] ...). With "Include serial number in filename" off, the extension names a file after its prompt text, so the file name
// starts with that tag. The tag is turned into the position of the prompt in the batch (the position decides scene_NN), so it also works when
// some scenes had no prompt. A tag that belongs to no prompt is ignored.
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;
const PROMPT_TAG = /^\s*\[s(\d{1,3})\]/i;
// File name (timestamp prefix and extension already removed): tag at the very start (optionally after a serial number and
// brackets/other symbols), or a bracketed tag anywhere.
const NAME_TAG_START = /^(?:\d{1,4}[\s._-]+)?[^a-z0-9]*s(\d{1,3})(?![a-z0-9])/i;
const NAME_TAG_ANY = /(?:^|[^a-z0-9])\[s(\d{1,3})\]/i;

function strings(v, out) {
  if (typeof v === "string") out.push(v);
  else if (Array.isArray(v)) v.forEach((x) => strings(x, out));
  else if (v && typeof v === "object") Object.values(v).forEach((x) => strings(x, out));
  return out;
}
function short(u) { try { const x = new URL(u); return x.host + x.pathname; } catch (e) { return String(u).slice(0, 80); } }

function createMediaTracker(prompts, debugDir) {
  const sets = prompts.map(() => new Set());
  const calls = [];
  const samples = [];
  let map = null;

  // scene tag number -> 1-based position of the prompt in this batch (only prompts that start with [sNN]; a repeated tag is ambiguous)
  const tagMap = new Map();
  const dupTags = new Set();
  prompts.forEach((p, i) => {
    const m = PROMPT_TAG.exec(String(p || ""));
    if (!m) return;
    const n = parseInt(m[1], 10);
    if (tagMap.has(n)) dupTags.add(n); else tagMap.set(n, i + 1);
  });
  dupTags.forEach((n) => tagMap.delete(n));

  async function onResponse(r) {
    try {
      const req = r.request();
      if (req.method() !== "POST") return;
      const post = req.postData() || "";
      if (post.length < 20) return;
      let hay = [post];
      try { hay.push(decodeURIComponent(post.replace(/\+/g, " "))); } catch (e) {}
      try { hay = hay.concat(strings(JSON.parse(post), [])); } catch (e) {}
      let hit = [];
      prompts.forEach((p, i) => { if (p && hay.some((s) => s.indexOf(p) !== -1)) hit.push(i); });
      if (!hit.length) {
        // the extension may trim or wrap the text: fall back to the last 80 characters
        prompts.forEach((p, i) => { const t = p.slice(-80); if (t.length > 20 && hay.some((s) => s.indexOf(t) !== -1)) hit.push(i); });
      }
      if (!hit.length) return;
      const body = await r.text().catch(() => "");
      const ids = [...new Set((body.match(UUID) || []).map((x) => x.toLowerCase()))];
      if (hit.length === 1) ids.forEach((id) => sets[hit[0]].add(id));
      map = null;
      if (calls.length < 200) calls.push({ t: new Date().toISOString(), url: short(r.url()), status: r.status(), scenes: hit.map((i) => i + 1), uuids: ids.length, ambiguous: hit.length > 1 });
      if (samples.length < 4) samples.push({ url: short(r.url()), request: post.slice(0, 1500), response: body.slice(0, 2500) });
    } catch (e) {}
  }

  function build() {
    if (map) return map;
    const count = new Map();
    sets.forEach((s) => s.forEach((id) => count.set(id, (count.get(id) || 0) + 1)));
    map = new Map();
    sets.forEach((s, i) => s.forEach((id) => { if (count.get(id) === 1) map.set(id, i + 1); })); // a UUID seen under 2 prompts is ambiguous: ignore
    return map;
  }
  // A mapping exists when media UUIDs were captured OR the prompts carry scene tags the file names can be matched with.
  function hasMapping() { return build().size > 0 || tagMap.size > 0; }
  function tagOf(name) {
    let stem = String(name || "").replace(/^\d{10,}-/, "");
    stem = stem.replace(/\.(png|jpe?g|webp|gif|avif)$/i, "");
    const m = NAME_TAG_START.exec(stem) || NAME_TAG_ANY.exec(stem);
    return m ? parseInt(m[1], 10) : null;
  }
  function matchFile(name) {
    const m = build();
    const ids = (String(name).match(UUID) || []).map((x) => x.toLowerCase());
    for (const id of ids) if (m.has(id)) return { index: m.get(id), uuid: id };
    const t = tagOf(name);
    if (t !== null && tagMap.has(t)) return { index: tagMap.get(t), uuid: "tag:s" + String(t).padStart(2, "0") };
    return null;
  }
  function writeDebug() {
    try {
      fs.writeFileSync(path.join(debugDir, "media-map.json"), JSON.stringify({ calls, scenes: sets.map((s, i) => ({ scene: i + 1, uuids: [...s] })), tags: [...tagMap].map(([tag, pos]) => ({ tag: "s" + String(tag).padStart(2, "0"), scene: pos })) }, null, 2));
      fs.writeFileSync(path.join(debugDir, "media-samples.json"), JSON.stringify(samples, null, 2));
    } catch (e) {}
  }
  return { onResponse, hasMapping, matchFile, writeDebug, count: prompts.length };
}

module.exports = { createMediaTracker };

// Human-like clicks + a fresh Library every run (see human.js, fresh_library.js).
// Hooked here because run_flow.js requires this module at start-up, before the browser launches.
try { require("./human").install(); } catch (e) { console.log("Human layer not loaded:", String(e && e.message).slice(0, 120)); }
