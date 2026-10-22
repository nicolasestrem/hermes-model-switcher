import { test, expect } from "@playwright/test";
import { REALISTIC } from "./fixtures/catalog-realistic.js";
import { chainUsageScenario } from "./fixtures/catalog-chain-usage.js";

// The Model Catalog's "Configured in" column and its In-use filter are joins
// between the loaded catalog and /state. An ordered-chain slot reports a BLANK
// top-level provider/model and keeps its own routes under `chain` (see
// tests/fixtures/catalog-chain-usage.js and dashboard/plugin_api.py's chain
// branch), so indexing the top-level scalars alone left every configured
// fallback route invisible: the route looked unused, the "Configured in" cell
// read "—" and no count was shown.
//
// This spec drives the fixture-rendered view (tests/mock-api.js) with the chain
// states the backend can emit, and asserts the cell text/title per row plus the
// In-use row set — the two places a route is called "used".

const SCENARIO = chainUsageScenario();
const FIXTURE = SCENARIO.fixture;
const SET_UP = FIXTURE.providers.default.filter((p) => p.authenticated !== false);
const ENTRIES = SET_UP.flatMap((p) => p.models.map((m) => ({ providerId: p.id, modelId: m.id })));
const key = (providerId, modelId) => `${providerId}\u0000${modelId}`;
// Slots tests/mock-api.js already serves for the realistic fixture (its own
// set-up rows join the same catalog), plus the routes this scenario declares.
const BASE_SLOT_KEYS = new Set(FIXTURE.slots.map((s) => key(s.provider, s.model)));
const CHAIN_KEYS = new Set(SCENARIO.usedRoutes.map((r) => key(r.providerId, r.modelId)));
const IN_USE = ENTRIES.filter((entry) =>
  BASE_SLOT_KEYS.has(key(entry.providerId, entry.modelId)) ||
  CHAIN_KEYS.has(key(entry.providerId, entry.modelId)));

const rows = (page) => page.locator("[data-model-row]");
const rowFor = (page, providerId, modelId) =>
  page.locator(`[data-model-row][data-provider-id="${providerId}"][data-model-id="${modelId}"]`);
const configuredCell = (page, providerId, modelId) =>
  rowFor(page, providerId, modelId).locator('[data-column="configured"]');

// The mocked /state cannot be extended through window.__mockApi (its slot list
// is closure-local), and tests/mock-api.js is shared. Intercept the DTO instead:
// wrap whatever fetch the page installs and merge the scenario's slots into
// both GET /state and POST /state, keyed by profile+slot like the real backend.
async function mergeChainSlotsIntoState(page, chainSlots) {
  await page.addInitScript((extraSlots) => {
    const STATE_RE = /\/api\/plugins\/hermes-model-switcher\/state$/;
    const wrap = (inner) => async function (url, opts) {
      const res = await inner.call(this, url, opts);
      let pathname = "";
      try {
        pathname = new URL(typeof url === "string" ? url : url?.url, location.origin).pathname;
      } catch (e) {
        return res;
      }
      if (!STATE_RE.test(pathname)) return res;
      const body = await res.json();
      const merged = Array.isArray(body.slots) ? body.slots.slice() : [];
      for (const slot of extraSlots) {
        const at = merged.findIndex((s) => s.profileId === slot.profileId && s.slotId === slot.slotId);
        if (at >= 0) merged[at] = slot; else merged.push(slot);
      }
      return new window.Response(JSON.stringify({ ...body, slots: merged }), {
        status: res.status, headers: { "Content-Type": "application/json" },
      });
    };
    // The page's mock assigns window.fetch once, before the bundle loads; the
    // setter wraps whatever it installs and stays out of the way afterwards.
    let current = window.fetch;
    Object.defineProperty(window, "fetch", {
      configurable: true,
      enumerable: true,
      get() { return current; },
      set(fn) { current = wrap(fn); },
    });
  }, chainSlots);
}

async function openCatalogWithChains(page) {
  await page.addInitScript((payload) => { window.__HMS_CATALOG_FIXTURE = payload; }, FIXTURE);
  await mergeChainSlotsIntoState(page, SCENARIO.slots);
  await page.goto("/fixture.html");
  await page.evaluate(() => sessionStorage.setItem(
    "hms-mock-flags", JSON.stringify({ catalogRealistic: true })));
  await page.reload();
  await expect(page.getByLabel("New provider", { exact: true })).toBeVisible();
  // The slots really reached the page: the matrix (mounted below the catalog in
  // the same workspace) lists every chain row, so a silently unmerged /state
  // would fail here instead of passing on "—" cells.
  for (const slot of SCENARIO.slots) {
    await expect(page.locator(`tr[data-row="${slot.profileId}/${slot.slotId}"]`)).toHaveCount(1);
  }
  await page.getByTestId("catalog-toggle").click();
  await expect(page.getByTestId("catalog-view")).toBeVisible();
  await expect(page.getByTestId("catalog-table")).toBeVisible();
}

test("Configured in and In use index a configured fallback chain, and only a configured one", async ({ page }) => {
  await openCatalogWithChains(page);

  // Per-row "Configured in": exact cell text and title. A chain route has to be
  // found, and its slot counted ONCE however many times the chain repeats it.
  for (const expectation of SCENARIO.expectations) {
    const cell = configuredCell(page, expectation.providerId, expectation.modelId);
    await expect(rowFor(page, expectation.providerId, expectation.modelId),
      `${expectation.label}: row must render`).toHaveCount(1);
    await expect(cell, `${expectation.label}: ${expectation.providerId}/${expectation.modelId}`)
      .toHaveText(expectation.text);
    await expect(cell, `${expectation.label}: ${expectation.providerId}/${expectation.modelId}`)
      .toHaveAttribute("title", expectation.title);
  }

  // In use = the same join, narrowing the catalog to rows some loaded slot
  // really routes to. Chain-only routes must be there: this row set is the
  // whole point of the fix, and the count line must agree with it.
  await page.getByTestId("catalog-in-use").click();
  await expect(page.getByTestId("catalog-in-use")).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("catalog-match-count"))
    .toHaveText(`Showing ${IN_USE.length} of ${REALISTIC.modelCount} models (filtered).`);
  await expect(rows(page)).toHaveCount(IN_USE.length);
  const inUseKeys = await rows(page).evaluateAll((nodes) =>
    nodes.map((node) => `${node.dataset.providerId}\u0000${node.dataset.modelId}`));
  expect(inUseKeys.sort()).toEqual(IN_USE.map((e) => key(e.providerId, e.modelId)).sort());

  // A chain route is "in use" but is not a price or capability claim: the row
  // keeps its own unknown status, so this narrowing can never look like one.
  for (const route of SCENARIO.usedRoutes) {
    await expect(rowFor(page, route.providerId, route.modelId)).toHaveAttribute(
      "data-pricing-status", "unknown");
  }
});

test("a chain route the catalog serves under another provider stays unmatched", async ({ page }) => {
  await openCatalogWithChains(page);

  // `openrouter` is a set-up provider in this catalog, but it publishes its own
  // `openrouter/*` ids; the chain entry spells this model id under `openrouter`
  // only, so the row that really exists (anthropic's) must stay unmatched and
  // no phantom row may appear. Exactly one row in the whole table serves it.
  const otherProvider = SCENARIO.expectations.find(
    (e) => e.label === "the same model under a different provider");
  const sameIdRows = page.locator(`[data-model-row][data-model-id="${otherProvider.modelId}"]`);
  await expect(sameIdRows).toHaveCount(1);
  await expect(sameIdRows).toHaveAttribute("data-provider-id", "anthropic");
  await expect(configuredCell(page, otherProvider.providerId, otherProvider.modelId)).toHaveText("—");
  await expect(configuredCell(page, otherProvider.providerId, otherProvider.modelId))
    .toHaveAttribute("title", "No loaded profile slot routes to this provider/model.");

  // And it is not "in use": the narrowing drops it.
  await page.getByTestId("catalog-in-use").click();
  await expect(sameIdRows).toHaveCount(0);
});
