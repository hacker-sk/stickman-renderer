// Cooldown helpers for run_flow.js.
// After the real batch is finished and safe, Flow is switched to RANDOM settings (a different ratio and model than the wanted ones)
// and 1-2 throwaway images are made. They are never kept. Flow is left on the random setting, so the NEXT run has to switch back to
// the wanted settings, like a person would.
const { resolveAspect } = require("./flow_helpers");

function esc(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

// Same loose model matching as flow_helpers.js: "Nano Banana Lite" also matches "Nano Banana 2 Lite".
function looseModelRe(model) {
  return new RegExp(String(model).trim().split(/\s+/).map(esc).join(".{0,12}?"), "i");
}

const PROMPTS = [
  "a red apple on a wooden table, soft daylight",
  "a small paper boat floating on calm water",
  "a cozy reading chair next to a window, watercolor style",
  "a lone lighthouse on a rocky coast at sunset",
  "a bowl of oranges on a kitchen counter",
  "a bicycle leaning against a brick wall",
  "a cup of tea with steam rising, close up",
  "a green hill with a single tree under a blue sky",
  "a sleeping cat on a sunny windowsill",
  "a stack of old books on a desk with a lamp",
  "a wooden bridge over a quiet stream in autumn",
  "a hot air balloon above a field of flowers"
];

// A random { aspect, model } that differs from the wanted ratio (always) and the wanted model (when the list allows it).
// `tried` = picks already used, never repeated. Returns null when nothing is left.
function chooseCooldown(opts) {
  const wantRe = looseModelRe(opts.wantModel);
  const aspects = (opts.aspects || []).map((a) => resolveAspect(a)).filter(Boolean).map((a) => a.label).filter((l, i, arr) => arr.indexOf(l) === i && l !== opts.wantAspect);
  const models = opts.models || [];
  const other = models.filter((m) => !wantRe.test(m));
  const pool = other.length ? other : models;
  const used = new Set((opts.tried || []).map((t) => t.aspect + "|" + t.model));
  const pairs = [];
  for (const a of aspects) for (const m of pool) if (!used.has(a + "|" + m)) pairs.push({ aspect: a, model: m });
  if (!pairs.length) return null;
  return pairs[Math.floor(Math.random() * pairs.length)];
}

// n different random one-line prompts.
function pickCooldownPrompts(n) {
  const a = PROMPTS.slice();
  for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); const t = a[i]; a[i] = a[j]; a[j] = t; }
  return a.slice(0, Math.max(1, n));
}

module.exports = { chooseCooldown, pickCooldownPrompts, looseModelRe };
