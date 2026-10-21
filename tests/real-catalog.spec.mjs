import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// The REAL plugin backend over the isolated lab home (tests/real-server.mjs).
// No live prices are asserted: this lab frequently has no published pricing,
// and the point is honesty — unknown is never "$0.00" or "free". A provider-stated
// zero rate, however, is real data and renders verbatim.
const HERE = dirname(fileURLToPath(import.meta.url));
const labHome = JSON.parse(readFileSync(join(HERE, "real-home.json"), "utf8")).home;
const URL = "http://localhost:8932/fixture-real.html";
const configFiles = [
  join(labHome, "config.yaml"),
  join(labHome, "profiles", "research", "config.yaml"),
];
const configBytes = () => configFiles.map((file) => readFileSync(file, "utf8"));

// Cold uvicorn startup plus the real catalog probe can take longer than the
// 30s default on a loaded machine.
test.setTimeout(60000);


async function openReal(page) {
  await page.goto(URL);
  // First /state can be cold (uvicorn imports the plugin/adapters); wait for
  // the real app to finish mounting instead of assuming a warm backend.
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible({ timeout: 30000 });
  expect(await page.evaluate(() => Boolean(window.__mockApi))).toBe(false);
}

async function openCatalog(page) {
  const responsePromise = page.waitForResponse(
    (r) => r.url().endsWith("/catalog") && r.request().method() === "POST");
  await page.getByTestId("catalog-toggle").click();
  const payload = await (await responsePromise).json();
  await expect(page.getByTestId("catalog-view")).toBeVisible();
  return payload;
}

test("real catalog: one on-demand fetch, payload-shaped groups/rows, and honest unknown prices", async ({ page }) => {
  const requests = [];
  page.on("request", (request) => requests.push({ url: request.url(), method: request.method() }));
  await openReal(page);
  expect(requests.filter((r) => r.url.includes("/catalog"))).toHaveLength(0);

  const payload = await openCatalog(page);
  expect(payload.ok).toBe(true);
  const totalModels = payload.providers.reduce((n, p) => n + p.models.length, 0);
  expect(totalModels).toBeGreaterThan(0);

  await expect(page.getByTestId("catalog-toggle"))
    .toContainText(`${totalModels} model${totalModels === 1 ? "" : "s"}`);
  await expect(page.getByRole("heading", { name: "Model catalog — what this profile can route to" }))
    .toBeVisible();

  // Provider groups and per-group row counts mirror the real DTO exactly.
  const groupIds = await page.locator("[data-provider-group]").evaluateAll(
    (nodes) => nodes.map((node) => node.getAttribute("data-provider-group")));
  // Provider groups mirror the real DTO; set-up providers lead (current one
  // first), and every authenticated:false provider sits in the one collapsed
  // "Not set up" section rendered after the main table.
  const setUpIds = payload.providers.filter((p) => p.authenticated !== false)
    .sort((a, b) => Number(Boolean(b.isCurrent)) - Number(Boolean(a.isCurrent)))
    .map((p) => p.id);
  const notSetUpIds = payload.providers.filter((p) => p.authenticated === false).map((p) => p.id);
  expect(groupIds).toEqual([...setUpIds, ...notSetUpIds]);
  expect(await page.locator("[data-model-row]").count()).toBe(totalModels);
  for (const provider of payload.providers) {
    await expect(page.locator(`[data-provider-group="${provider.id}"] [data-model-row]`))
      .toHaveCount(provider.models.length);
  }

  // Both pure plugin transforms consume the SAME real host options payload.
  // No private catalog discovery, inference or query-only fallback survives.
  const withModels = payload.providers.find((p) => p.models.length > 0);
  expect(withModels).toBeTruthy();
  const models = await page.evaluate(async (providerId) => {
    const options = await window.__HERMES_PLUGIN_SDK__.fetchJSON(
      "/api/model/options?profile=default&include_unconfigured=1");
    const response = await window.__HERMES_PLUGIN_SDK__.authedFetch(
      "/api/plugins/hermes-model-switcher/models", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile: "default", provider: providerId, options }),
      });
    return response.json();
  }, withModels.id);
  expect(models.ok).toBe(true);
  const modelIds = new Set(models.models.map((m) => m.id));
  for (const catalogModel of withModels.models) {
    expect(modelIds.has(catalogModel.id), catalogModel.id).toBe(true);
  }
  const sdkCalls = await page.evaluate(() => window.__realSdkCalls);
  expect(sdkCalls.some((r) => r.url.startsWith("/api/model/options?profile=default&"))).toBe(true);
  expect(requests.some((r) => r.method === "GET" && r.url.includes("/catalog"))).toBe(false);

  // Priced rows echo the frozen display strings verbatim.
  if (!payload.cost.pending) {
    const priced = payload.providers.flatMap((p) => p.models
      .filter((m) => m.pricingStatus === "priced" && m.cost?.display?.input)
      .map((m) => ({ provider: p.id, model: m }))).slice(0, 8);
    for (const entry of priced) {
      const priceCell = page.locator(
        `[data-model-row][data-provider-id="${entry.provider}"][data-model-id="${entry.model.id}"] [data-column="input"]`);
      await expect(priceCell).toHaveText(entry.model.cost.display.input);
    }
  }

  // Unknown says not published with the never-free explanation — never a number.
  const unknownRows = payload.providers.flatMap((p) => p.models
    .filter((m) => m.pricingStatus === "unknown").map((m) => ({ provider: p.id, model: m.id })));
  await expect(page.locator('[data-model-row][data-pricing-status="unknown"]'))
    .toHaveCount(unknownRows.length);
  for (const entry of unknownRows.slice(0, 6)) {
    const priceCell = page.locator(
      `[data-model-row][data-provider-id="${entry.provider}"][data-model-id="${entry.model}"] [data-column="input"]`);
    await expect(priceCell).toHaveText("price not published");
    await expect(priceCell.locator(".hms-catalog-unknown"))
      .toHaveAttribute("title", /never treated as free/);
  }

  // Providers with no published models say so; silence is not free access.
  // An unauthenticated provider's empty row is inside the collapsed section.
  const emptyProvider = payload.providers.find((p) => p.models.length === 0);
  if (emptyProvider) {
    if (emptyProvider.authenticated === false) {
      await page.getByTestId("catalog-unsetup-summary").click();
    }
    const emptyRow = page.locator(`[data-empty-provider="${emptyProvider.id}"]`);
    await expect(emptyRow).toBeVisible();
    await expect(emptyRow).toContainText("not evidence of free access");
  }

  const sectionText = await page.getByTestId("catalog-section").innerText();
  // Honesty contract: "$0.00" is real data only when the payload itself states a
  // zero rate; with no provider-stated zero, any rendered $0.00 is fabricated.
  // Unknown rows must render "—", never a zero.
  const zeroStated = payload.providers
    .flatMap((p) => p.models)
    .some((m) => m.cost?.display && Object.values(m.cost.display).includes("$0.00"));
  if (zeroStated) {
    expect(sectionText).toContain("$0.00");
  } else {
    expect(sectionText).not.toContain("$0.00");
  }
  expect(sectionText).not.toMatch(/\b(just now|today|minutes? ago|hours? ago)\b/i);

  // Closing and reopening uses the shared per-profile cache: still ONE fetch.
  await page.getByTestId("catalog-collapse").click();
  await expect(page.getByTestId("catalog-section")).toBeHidden();
  await page.getByTestId("catalog-toggle").click();
  await expect(page.getByTestId("catalog-view")).toBeVisible();
  expect(requests.filter((r) => r.url.includes("/catalog"))).toHaveLength(1);
  const legacy = await page.request.get("http://localhost:8932/api/plugins/hermes-model-switcher/catalog");
  expect(legacy.status()).toBe(410);
  expect(await legacy.json()).toEqual({ ok: false, reason: "hostCatalogRequired" });
  expect(legacy.headers()["cache-control"]).toBe("no-store");
});

test("real catalog: browsing and Use prefill never change config bytes or send a write", async ({ page }) => {
  const requests = [];
  page.on("request", (request) => requests.push({ url: request.url(), method: request.method() }));
  await openReal(page);
  const before = configBytes();

  await openCatalog(page);
  await page.getByTestId("catalog-search").fill("gpt");
  // Price filters exist only when the catalog carries a priced/free status; the
  // lab home commonly has none, and the UI says so instead of offering a dead
  // filter. Exercise them when present, never assume them.
  if (await page.getByTestId("catalog-free-only").count()) {
    await page.getByTestId("catalog-free-only").click();
    await page.getByTestId("catalog-priced-only").click();
  }
  await page.getByTestId("catalog-group-select").selectOption("none");
  await page.getByTestId("catalog-sort").selectOption("name");
  await page.getByTestId("catalog-search").fill("");
  if (await page.getByTestId("catalog-priced-only").count()) {
    await page.getByTestId("catalog-priced-only").click();
  }
  await page.getByTestId("catalog-group-select").selectOption("provider");

  // Use pre-fills the editor only; nothing is previewed or applied.
  const enabledUse = page.locator("[data-model-row] [data-column=\"use\"] button:not([disabled])").first();
  if (await enabledUse.count()) {
    await enabledUse.click();
    await expect(page.getByTestId("live-status")).toContainText("Route set to");
  }
  await expect(page.getByLabel("I acknowledge unknown-cost routes")).not.toBeChecked();

  await page.waitForTimeout(300);
  expect(configBytes()).toEqual(before);
  // POST /state and /catalog are read-only host-evidence transforms, not writes.
  expect(requests.filter((r) => r.method !== "GET" &&
    !/\/(state|catalog|models)$/.test(r.url))).toHaveLength(0);
  expect(requests.filter((r) => /\/(preview|operations|presets)(\?|$)/.test(r.url))).toHaveLength(0);
});
