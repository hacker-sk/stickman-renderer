// One-time step per Flow run: create a brand-new Library (the + next to "Library" in Flow's sidebar) right after the
// project loads, before anything else, and give it a unique name (run-<time>-<runid>). Then OPEN that collection so the
// prompt bar belongs to it and generations are saved inside it (not under "All media"). Returns false when the sidebar is
// not there yet (not logged in), so the caller can try again after the next page load; true once the step has been handled
// (even if it could not be confirmed).
// Before that, project_rotation.js runs from here: every 4-5 runs (all workflows together) a brand-new Flow project is created and used
// (FLOW_PROJECT_ROTATION=off switches that part off; FLOW_FRESH_LIBRARY=off in human.js skips this whole file, rotation included).
const fs = require("fs");
const path = require("path");

const DEBUG_DIR = path.join(__dirname, "debug");

async function ensureFreshLibrary(page, h) {
  fs.mkdirSync(DEBUG_DIR, { recursive: true });
  // false = the sidebar is not there yet (not logged in): same answer as the Library check below, the caller tries again later.
  let rotated = true;
  try { rotated = await require("./project_rotation").maybeRotate(page, h); }
  catch (e) { console.log("::warning::Project rotation step failed: " + String(e && e.message).slice(0, 160)); }
  if (rotated === false) return false;

  const label = page.getByText("Library", { exact: true }).locator("visible=true").first();
  try { await label.waitFor({ state: "visible", timeout: 25000 }); }
  catch (e) { console.log("Fresh library: sidebar not visible yet, skipped for now"); return false; }

  await h.pause(1500, 3500);
  await h.wander(page);

  // The sidebar row reads: [folder] Library [+] [^]. The + is the LEFT-most clickable element after the "Library" label
  // on the same row; the right-most one is the collapse arrow, which must never be clicked.
  const lb = await label.boundingBox();
  const pt = lb && await page.evaluate((r) => {
    let best = null;
    for (const el of document.querySelectorAll('button,[role="button"],a')) {
      const b = el.getBoundingClientRect();
      if (!b.width || !b.height) continue;
      if (Math.abs(b.y + b.height / 2 - (r.y + r.height / 2)) > r.height) continue;
      if (b.x < r.x + r.width - 2 || b.x > r.x + 300) continue;
      const hint = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).toLowerCase();
      if (/expand|collapse|arrow_up|arrow_down|chevron|keyboard_arrow/.test(hint)) continue;
      if (!best || b.x < best.x) best = { x: b.x + b.width / 2, y: b.y + b.height / 2 };
    }
    return best;
  }, lb);

  if (pt) {
    await h.moveTo(page, pt.x + h.rand(-3, 3), pt.y + h.rand(-3, 3));
    await h.pause(80, 250);
    await page.mouse.down();
    await h.pause(40, 110);
    await page.mouse.up();
  } else {
    const alt = page.locator('button[aria-label*="ibrary" i]').first();
    if (!(await alt.count())) { console.log("::warning::Fresh library: + button not found, using the existing library"); return true; }
    await alt.click();
  }
  await h.pause(900, 1800);

  const name = "run-" + new Date().toISOString().replace(/[-:T]/g, "").slice(0, 12) + "-" + String(process.env.GITHUB_RUN_ID || "local").slice(-5);
  let named = false;

  // Flow renames INLINE: the new "Untitled collection" row in the sidebar turns into an input with its text selected and a
  // check mark beside it. Find that editable element in the sidebar (left 320 px, below the Library row), not the search box.
  const findEditable = () => page.evaluate((r) => {
    const side = (el) => { const b = el.getBoundingClientRect(); return b.width > 0 && b.height > 0 && b.x < 320 && b.y > r.y; };
    const editable = (el) => el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
    const a = document.activeElement;
    if (editable(a) && side(a)) { const b = a.getBoundingClientRect(); return { x: b.x + b.width / 2, y: b.y + b.height / 2, focused: true }; }
    for (const el of document.querySelectorAll('input,textarea,[contenteditable="true"],[contenteditable=""]')) {
      if (side(el)) { const b = el.getBoundingClientRect(); return { x: b.x + b.width / 2, y: b.y + b.height / 2, focused: false }; }
    }
    return null;
  }, lb || { y: 0 });

  let field = null;
  for (let i = 0; i < 15 && !field; i++) { field = await findEditable().catch(() => null); if (!field) await h.pause(350, 500); }
  if (field) {
    try {
      if (!field.focused) {
        await h.moveTo(page, field.x + h.rand(-4, 4), field.y + h.rand(-2, 2));
        await h.pause(80, 200);
        await page.mouse.down();
        await h.pause(40, 100);
        await page.mouse.up();
        await h.pause(250, 600);
      }
      await page.keyboard.press("Control+A");
      await h.pause(120, 300);
      await h.type(page, name);
      await h.pause(400, 900);
      await page.keyboard.press("Enter");
      named = true;
    } catch (e) {
      console.log("Fresh library: typing the name failed: " + String(e && e.message).slice(0, 120));
    }
  } else {
    // Older Flow versions used a dialog instead.
    const dialog = page.locator('[role="dialog"]').locator("visible=true").first();
    const input = dialog.locator('input, textarea, [contenteditable="true"]').locator("visible=true").first();
    try {
      await input.waitFor({ state: "visible", timeout: 4000 });
      await input.click();
      await h.pause(250, 600);
      await page.keyboard.press("Control+A");
      await h.type(page, name);
      await h.pause(400, 900);
      await page.keyboard.press("Enter");
      named = true;
    } catch (e) {
      console.log("Fresh library: no name field appeared, the library keeps its default name");
    }
    await h.pause(1200, 2200);
    if (await dialog.count()) {
      const ok = dialog.getByRole("button", { name: /create|save|done|add|ok|confirm/i }).first();
      if (await ok.count()) await ok.click().catch(() => {});
      await h.pause(1000, 2000);
    }
  }
  await h.pause(1200, 2200);

  let confirmed = false;
  if (named) {
    const seen = () => page.getByText(name).first().waitFor({ state: "visible", timeout: 6000 }).then(() => true, () => false);
    confirmed = await seen();
    if (!confirmed) {
      // Enter did not save the name: press the check mark next to the field.
      const chk = await page.evaluate((r) => {
        for (const el of document.querySelectorAll('button,[role="button"]')) {
          const b = el.getBoundingClientRect();
          if (!b.width || !b.height || b.x > 320 || b.y <= r.y) continue;
          const hint = ((el.getAttribute("aria-label") || "") + " " + (el.textContent || "")).toLowerCase();
          if (/check|done|confirm|save/.test(hint)) return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
        }
        return null;
      }, lb || { y: 0 }).catch(() => null);
      if (chk) {
        await h.moveTo(page, chk.x + h.rand(-2, 2), chk.y + h.rand(-2, 2));
        await h.pause(80, 200);
        await page.mouse.down();
        await h.pause(40, 100);
        await page.mouse.up();
        await h.pause(900, 1600);
        confirmed = await seen();
      }
    }
  }

  // Open the new collection so the prompt bar (and therefore every generation) belongs to it instead of "All media".
  // Creating + naming it does NOT switch the view; without this click Flow saves images under All media.
  // Layout seen in debug/fresh-library.png: the new run-... row is the FIRST row under Library in the left sidebar, and the
  // same name also appears on a big tile in the All media grid (main area) - so only ever click inside the sidebar (x < 320).
  // The active view is the row with the highlighted pill: "All media" at load. When the collection is open that pill leaves it.
  let opened = false;
  if (named && confirmed) {
    const urlBefore = page.url();
    // Sidebar row for the new collection: leaf element holding the name, left 320 px, below the Library row.
    const findRow = () => page.evaluate((a) => {
      let best = null;
      for (const el of document.querySelectorAll("*")) {
        if (el.children.length || !(el.textContent || "").trim().startsWith(a.name)) continue;
        const b = el.getBoundingClientRect();
        if (!b.width || !b.height || b.x > 320 || b.y <= a.y) continue;
        if (!best || b.y < best.y) best = { x: b.x, y: b.y, w: b.width, h: b.height };
      }
      return best;
    }, { name, y: lb ? lb.y : 0 }).catch(() => null);
    // true while the "All media" sidebar row still has its highlight (a background the plain "Images" row does not have).
    const allMediaActive = () => page.evaluate(() => {
      const bgOf = (txt) => {
        for (const el of document.querySelectorAll("*")) {
          if (el.children.length || (el.textContent || "").trim() !== txt) continue;
          const r = el.getBoundingClientRect();
          if (!r.width || r.x > 320) continue;
          for (let n = el, i = 0; n && i < 6; n = n.parentElement, i++) {
            const c = getComputedStyle(n).backgroundColor;
            const nb = n.getBoundingClientRect();
            if (c && c !== "transparent" && !/rgba\(\s*0,\s*0,\s*0,\s*0\s*\)/.test(c) && nb.width > 120) return c;
          }
          return "none";
        }
        return null;
      };
      const all = bgOf("All media"), img = bgOf("Images");
      if (all === null) return null;           // sidebar not readable
      return all !== "none" && all !== img;     // highlighted and different from a normal row
    }).catch(() => null);

    const wasActive = await allMediaActive();
    for (let attempt = 1; attempt <= 3 && !opened; attempt++) {
      const row = await findRow();
      if (!row) { console.log("Fresh library: sidebar row for " + name + " not found (attempt " + attempt + ")"); await h.pause(600, 1200); continue; }
      await h.moveTo(page, row.x + Math.min(row.w / 2, 90) + h.rand(-3, 3), row.y + row.h / 2 + h.rand(-2, 2));
      await h.pause(80, 250);
      await page.mouse.down();
      await h.pause(40, 110);
      await page.mouse.up();
      for (let i = 0; i < 10 && !opened; i++) {
        await h.pause(400, 700);
        const urlChanged = page.url() !== urlBefore;
        const active = await allMediaActive();
        if (urlChanged || (wasActive && active === false)) {
          opened = true;
          console.log("Fresh library: collection opened (" + (urlChanged ? "url changed" : "All media no longer highlighted") + ")");
        }
      }
    }
    await h.pause(800, 1500);
  }

  await page.screenshot({ path: path.join(DEBUG_DIR, "fresh-library.png") }).catch(() => {});
  console.log("Fresh library step done | name: " + (named ? name : "default") + " | confirmed on page: " + confirmed + " | opened: " + opened);
  if (named && !confirmed) console.log("::warning::Fresh library name not seen on the page - check debug/fresh-library.png");
  if (named && confirmed && !opened) console.log("::warning::Fresh library: could not open the collection, images will land in All media - check debug/fresh-library.png");
  return true;
}

module.exports = { ensureFreshLibrary };
