// Logs in to Google with credentials from repo secrets and saves fresh Flow session cookies
// to session/cookies.json (same format run_flow.js reads). Never logs or uploads cookie values.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const EMAIL = (process.env.FLOW_GOOGLE_EMAIL || "").trim();
const PASSWORD = process.env.FLOW_GOOGLE_PASSWORD || "";
const TOTP_SECRET = (process.env.FLOW_GOOGLE_TOTP_SECRET || "").replace(/\s+/g, "");
const FLOW_URL = (process.env.FLOW_URL || "").trim() || "https://labs.google/fx/tools/flow";
const SESS_DIR = path.resolve(__dirname, "session");
const SESS_FILE = path.join(SESS_DIR, "cookies.json");
const DEBUG_DIR = path.resolve(__dirname, "debug-login");
fs.mkdirSync(SESS_DIR, { recursive: true });
fs.mkdirSync(DEBUG_DIR, { recursive: true });

function base32Decode(s) {
  const alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of s.toUpperCase().replace(/=+$/, "")) {
    const v = alpha.indexOf(ch);
    if (v >= 0) bits += v.toString(2).padStart(5, "0");
  }
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}

function totp(secret) {
  const counter = Math.floor(Date.now() / 30000);
  const buf = Buffer.alloc(8);
  buf.writeUInt32BE(Math.floor(counter / 2 ** 32), 0);
  buf.writeUInt32BE(counter >>> 0, 4);
  const h = crypto.createHmac("sha1", base32Decode(secret)).update(buf).digest();
  const o = h[h.length - 1] & 0xf;
  const code = (((h[o] & 0x7f) << 24) | (h[o + 1] << 16) | (h[o + 2] << 8) | h[o + 3]) % 1000000;
  return String(code).padStart(6, "0");
}

const log = (m) => console.log(new Date().toISOString().slice(11, 19) + " " + m);

(async () => {
  if (!EMAIL || !PASSWORD) {
    console.error("FLOW_GOOGLE_EMAIL / FLOW_GOOGLE_PASSWORD secrets are missing");
    process.exit(1);
  }
  const args = ["--disable-blink-features=AutomationControlled"];
  const opts = { headless: false, ignoreDefaultArgs: ["--enable-automation"], args };
  let browser;
  try {
    browser = await chromium.launch({ ...opts, channel: "chrome" });
  } catch (e) {
    log("Chrome channel unavailable, using bundled Chromium");
    browser = await chromium.launch(opts);
  }
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: "en-US" });
  const page = await context.newPage();
  const shot = (name) => page.screenshot({ path: path.join(DEBUG_DIR, name + ".png") }).catch(() => {});
  const clickText = async (re) => {
    const el = page.getByText(re).first();
    if (await el.isVisible().catch(() => false)) {
      await el.click({ timeout: 5000 }).catch(() => {});
      return true;
    }
    return false;
  };

  let exitCode = 0;
  try {
    log("Opening Google sign-in");
    await page.goto("https://accounts.google.com/ServiceLogin?hl=en&continue=" + encodeURIComponent(FLOW_URL), { waitUntil: "domcontentloaded" });
    const emailBox = page.locator('input[type="email"]').first();
    await emailBox.waitFor({ timeout: 30000 });
    await emailBox.fill(EMAIL);
    await page.keyboard.press("Enter");
    log("Email submitted");

    let pwDone = false, totpTries = 0, lastTotpAt = 0, altTried = 0;
    const deadline = Date.now() + 150000;
    while (Date.now() < deadline) {
      await page.waitForTimeout(1500);
      const url = page.url();
      if (!/accounts\.google\.com/.test(url)) break;
      const body = (await page.locator("body").innerText().catch(() => "")).toLowerCase();

      if (/wrong password|couldn.t find your google account|couldn.t sign you in|this browser or app may not be secure/.test(body)) {
        throw new Error("Google refused the sign-in (wrong credentials, or the automated browser was blocked)");
      }
      if (/captcha|type the text you hear or see/.test(body) || (await page.locator('img[src*="captcha"]').count().catch(() => 0))) {
        throw new Error("Google showed a captcha - cannot continue automatically");
      }

      const totpBox = page.locator('input[name="totpPin"], input#totpPin').first();
      if (await totpBox.isVisible().catch(() => false)) {
        if (!TOTP_SECRET) throw new Error("Google asks for a 2-step code but FLOW_GOOGLE_TOTP_SECRET is not set");
        if (totpTries === 0 || (Date.now() - lastTotpAt > 35000 && totpTries < 2)) {
          await totpBox.fill(totp(TOTP_SECRET));
          await page.keyboard.press("Enter");
          totpTries++;
          lastTotpAt = Date.now();
          log("2-step code submitted (try " + totpTries + ")");
        } else if (totpTries >= 2 && Date.now() - lastTotpAt > 35000) {
          throw new Error("Google rejected the 2-step code");
        }
        continue;
      }

      const pw = page.locator('input[type="password"]:visible, input[name="Passwd"]:visible').first();
      if (!pwDone && (await pw.isVisible().catch(() => false))) {
        await pw.fill(PASSWORD);
        await page.keyboard.press("Enter");
        pwDone = true;
        log("Password submitted");
        continue;
      }

      if (pwDone && TOTP_SECRET && altTried < 3 && /try another way/.test(body)) {
        altTried++;
        await clickText(/try another way/i);
        await page.waitForTimeout(1500);
        await clickText(/authenticator|verification code/i);
        log("Switched to authenticator-code challenge");
        continue;
      }

      if (/passkeyenrollment|speedbump|signin\/v2\/(challenge\/)?(recovery|selection)?/.test(url) || /not now|skip|no thanks|i understand/.test(body)) {
        const btn = page.getByRole("button", { name: /not now|skip|no thanks|i understand|confirm|continue/i }).first();
        if (await btn.isVisible().catch(() => false)) {
          await btn.click({ timeout: 5000 }).catch(() => {});
          log("Dismissed an extra Google prompt");
        }
      }

      if (pwDone && /verify it.s you|confirm your recovery|check your phone|get a verification code/.test(body) && !TOTP_SECRET) {
        throw new Error("Google wants extra verification (phone/recovery) that this job cannot complete");
      }
    }
    if (/accounts\.google\.com/.test(page.url())) {
      throw new Error("Still on Google sign-in after timeout (path: " + new URL(page.url()).pathname + ")");
    }

    log("Signed in, opening Flow");
    await page.goto(FLOW_URL, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(8000);
    if (/accounts\.google\.com/.test(page.url())) throw new Error("Flow redirected back to sign-in");

    const all = await context.cookies();
    const google = all.filter((c) => /google\./.test(c.domain || ""));
    const hasSession = google.some((c) => c.name === "SID" || c.name === "__Secure-1PSID");
    if (!hasSession) throw new Error("Login finished but no Google session cookie was set");
    fs.writeFileSync(SESS_FILE, JSON.stringify(google));
    await shot("flow-after-login");
    log("Saved " + google.length + " Google cookies to session/cookies.json");
  } catch (e) {
    console.error("LOGIN FAILED: " + e.message);
    await shot("failure");
    exitCode = 1;
  }
  await browser.close();
  process.exit(exitCode);
})();
