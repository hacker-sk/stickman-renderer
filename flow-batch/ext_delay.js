const fs = require("fs");
const path = require("path");

// Sets the ZAPI FLOW "Random delay from X to Y sec" fields (default 10 to 25, override with FLOW_DELAY_MIN / FLOW_DELAY_MAX).
// Called from human.js right after the extension page is opened, so it runs before the queue is started.
// Clicks each field with the human mouse path, selects the old value, types the new one slowly, then re-reads it.
// Best effort: never throws. Writes debug/02e-random-delay.json and a screenshot.
const DEBUG_DIR = path.resolve(__dirname, "debug");

async function findFields(ui) {
  return ui.evaluate(() => {
    document.querySelectorAll("[data-delay-field]").forEach((e) => e.removeAttribute("data-delay-field"));
    const cands = [...document.querySelectorAll("body *")].filter((e) => /random delay/i.test(e.textContent || "") && (e.textContent || "").length < 60);
    if (!cands.length) return { found: false };
    const label = cands.sort((a, b) => a.textContent.length - b.textContent.length)[0];
    let box = label;
    for (let k = 0; k < 6 && box; k++, box = box.parentElement) {
      const inputs = [...box.querySelectorAll("input")].filter((i) => !["checkbox", "radio", "hidden", "file", "button", "submit"].includes(i.type));
      if (inputs.length >= 2) {
        inputs[0].setAttribute("data-delay-field", "from");
        inputs[1].setAttribute("data-delay-field", "to");
        return { found: true, before: [inputs[0].value, inputs[1].value] };
      }
    }
    return { found: false, text: label.textContent.trim() };
  });
}

async function setField(ui, which, value, H) {
  const loc = ui.locator('[data-delay-field="' + which + '"]');
  await loc.scrollIntoViewIfNeeded({ timeout: 5000 }).catch(() => {});
  try {
    const b = await loc.boundingBox();
    if (b && H) {
      await H.pause(150, 450);
      await H.moveTo(ui, b.x + b.width * H.rand(0.3, 0.7), b.y + b.height * H.rand(0.3, 0.7));
      await H.pause(60, 220);
    }
  } catch (e) { /* plain click below */ }
  await loc.click({ timeout: 5000, delay: 70 });
  await ui.keyboard.press("Control+A");
  await ui.keyboard.type(String(value), { delay: 90 });
  await ui.keyboard.press("Tab");
  let now = await loc.inputValue().catch(() => "");
  if (String(now).trim() !== String(value)) {
    // Fallback: set the value inside the page the way a framework expects (native setter + input/change events).
    await loc.evaluate((el, v) => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      set.call(el, String(v));
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }, value);
    now = await loc.inputValue().catch(() => "");
  }
  return String(now).trim();
}

async function applyRandomDelay(ui, H) {
  const min = parseInt(process.env.FLOW_DELAY_MIN || "10", 10);
  const max = parseInt(process.env.FLOW_DELAY_MAX || "25", 10);
  const report = { wanted: [min, max], before: null, after: null, ok: false, error: null };
  try {
    if (!(min >= 1 && max > min)) throw new Error("bad FLOW_DELAY_MIN/MAX: " + min + "/" + max);
    await ui.waitForFunction(() => /random delay/i.test(document.body ? document.body.innerText : ""), null, { timeout: 10000 });
    const f = await findFields(ui);
    if (!f.found) throw new Error("Random delay fields not found in the extension panel");
    report.before = f.before;
    const a = await setField(ui, "from", min, H);
    const b = await setField(ui, "to", max, H);
    report.after = [a, b];
    report.ok = a === String(min) && b === String(max);
    console.log("Random delay:", report.ok ? "set to " + min + "-" + max + " sec (verified)" : "could not be verified, shows " + a + "-" + b);
  } catch (e) {
    report.error = String(e && e.message).slice(0, 200);
    console.log("::warning::Random delay not set:", report.error);
  }
  try { fs.mkdirSync(DEBUG_DIR, { recursive: true }); fs.writeFileSync(path.join(DEBUG_DIR, "02e-random-delay.json"), JSON.stringify(report, null, 2)); } catch (e) {}
  try { await ui.screenshot({ path: path.join(DEBUG_DIR, "02e-random-delay.png") }); } catch (e) {}
  return report;
}

module.exports = { applyRandomDelay };
