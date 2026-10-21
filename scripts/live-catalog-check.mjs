// Post-install check against a RUNNING Hermes dashboard: open the Model
// Switcher catalog with the host's real CSS, real theme and real provider data
// and fail on the regressions host-css-guard.spec.mjs pins in the fixture.
//
// Usage: HMS_LIVE_URL=http://host:9119 [HMS_LIVE_COOKIE="a=1; b=2"] \
//        [HMS_LIVE_PROFILE=default] node scripts/live-catalog-check.mjs
// Read-only: it opens the catalog and reads the DOM; it never clicks "Use".
import { chromium, firefox } from "@playwright/test";

const base = process.env.HMS_LIVE_URL;
if (!base) { console.error("HMS_LIVE_URL is required"); process.exit(2); }
const profile = process.env.HMS_LIVE_PROFILE || "default";
// The plugin scopes its catalog from its own profile filter, not the URL, so
// the check selects the profile in the UI and then asserts the shown scope.
const url = `${base.replace(/\/$/, "")}/model-switcher`;

async function check(engine) {
  const browser = await engine.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  if (process.env.HMS_LIVE_COOKIE) {
    // "name=value; name2=value2" — the dashboard session is several cookies.
    await context.addCookies(process.env.HMS_LIVE_COOKIE.split(";").map((pair) => pair.trim()).filter(Boolean)
      .map((pair) => { const i = pair.indexOf("="); return { name: pair.slice(0, i), value: pair.slice(i + 1), url: base }; }));
  }
  const page = await context.newPage();
  const failedAssets = [];
  page.on("response", (r) => {
    if (r.status() >= 400 && /hermes-model-switcher/.test(r.url())) failedAssets.push(`${r.status()} ${r.url()}`);
  });
  await page.goto(url, { waitUntil: "networkidle" });
  const toggle = page.getByTestId("catalog-toggle");
  if (!(await toggle.count())) throw new Error(`plugin did not mount at ${page.url()} (login required? set HMS_LIVE_COOKIE)`);
  // Exactly one profile ticked = the catalog's scope (main.jsx catalogProfileId).
  const option = page.locator(`[data-profile-option="${profile}"]`);
  if (!(await option.count())) throw new Error(`profile "${profile}" is not offered by the plugin's profile filter`);
  for (const other of await page.locator("[data-profile-option]:checked").all()) {
    if ((await other.getAttribute("data-profile-option")) !== profile) await other.uncheck();
  }
  await option.check();
  if (!(await page.getByTestId("catalog-table").isVisible())) await toggle.click();
  const scope = page.locator(".hms-catalog-readonly code").first();
  await scope.waitFor({ timeout: 20000 });
  const shown = (await scope.textContent())?.trim();
  if (shown !== profile) throw new Error(`catalog scope is "${shown}", expected "${profile}"`);
  await page.locator("[data-model-row]").first().waitFor({ timeout: 20000 });
  const problems = await page.evaluate(() => {
    const out = [];
    for (const tr of document.querySelectorAll("[data-model-row]")) {
      const cell = tr.querySelector('td[data-column="model"]');
      const id = tr.dataset.modelId;
      const label = cell.querySelector(".hms-catalog-model-label")?.textContent;
      if (label === id && cell.innerText.split(id).length - 1 > 1) out.push(`duplicated id: ${id}`);
      // Count stacked block lines, not pixels: a long id may legitimately wrap.
      const blocks = [...cell.children].filter((el) => el.getClientRects().length
        && getComputedStyle(el).display === "block").length;
      if (blocks > (label === id ? 1 : 2)) out.push(`model cell stacks ${blocks} lines: ${id}`);
    }
    for (const el of document.querySelectorAll(".hms-root code")) {
      const bg = getComputedStyle(el).backgroundColor;
      if (bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") out.push(`host chip leaked onto code (${bg}): ${el.textContent}`);
    }
    for (const el of document.querySelectorAll(".hms-root *")) {
      if (![...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
      const cs = getComputedStyle(el);
      if (cs.backgroundColor !== "rgba(0, 0, 0, 0)" && cs.backgroundColor === cs.color) out.push(`invisible text: ${el.textContent.trim().slice(0, 40)}`);
    }
    return out;
  });
  const rows = await page.locator("[data-model-row]").count();
  await browser.close();
  return { rows, profile, problems: [...failedAssets.map((a) => `asset failed: ${a}`), ...new Set(problems)] };
}

let failed = false;
for (const [name, engine] of [["chromium", chromium], ["firefox", firefox]]) {
  try {
    const { rows, problems } = await check(engine);
    console.log(`${name}: profile "${profile}", ${rows} catalog rows, ${problems.length} problem(s)`);
    for (const p of problems.slice(0, 20)) console.log(`  - ${p}`);
    if (problems.length) failed = true;
  } catch (err) {
    console.log(`${name}: could not check: ${err.message}`);
    failed = true;
  }
}
process.exit(failed ? 1 : 0);
