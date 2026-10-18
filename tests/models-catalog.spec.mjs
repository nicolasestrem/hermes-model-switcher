import { test, expect } from "@playwright/test";

// Bird's-eye Model catalog section against tests/mock-api.js. The fixture is
// contract-exact: 5 providers / 15 models, with priced, free, included,
// unknown, an unauthenticated provider and a provider whose fetch failed.
const providerInput = (page) => page.getByLabel("New provider", { exact: true });
const modelInput = (page) => page.getByLabel("New model", { exact: true });
const toggle = (page) => page.getByTestId("catalog-toggle");
const section = (page) => page.getByTestId("catalog-section");
const view = (page) => page.getByTestId("catalog-view");
const rows = (page) => page.locator("[data-model-row]");
const groups = (page) => page.locator("[data-provider-group]");
const row = (page, provider, model) =>
  page.locator(`[data-model-row][data-provider-id="${provider}"][data-model-id="${model}"]`);
const catalogRequests = (page) => page.evaluate(() =>
  window.__mockApi.requests.filter((r) => r.path.endsWith("/catalog")));

async function openFixture(page) {
  await page.goto("/fixture.html");
  await expect(providerInput(page)).toBeVisible();
}

// Flags are read by the in-page mock on every request/load; /state is served
// once at mount, so flags that change slots need a reload.
async function openWithFlags(page, flags) {
  await page.goto("/fixture.html");
  await page.evaluate((value) => sessionStorage.setItem("hms-mock-flags", JSON.stringify(value)), flags);
  await page.reload();
  await expect(providerInput(page)).toBeVisible();
}

async function openCatalog(page) {
  await toggle(page).click();
  await expect(section(page)).toBeVisible();
  await expect(view(page)).toBeVisible();
}

const catalogProviderIds = (page) => groups(page).evaluateAll(
  (nodes) => nodes.map((node) => node.getAttribute("data-provider-group")));

test("collapsed by default, opens without resetting the workspace, and fetches once per profile", async ({ page }) => {
  await openFixture(page);
  expect(await catalogRequests(page)).toHaveLength(0);
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
  await expect(toggle(page)).toHaveAttribute("aria-controls", "hms-model-catalog");
  await expect(toggle(page)).toHaveText("Browse model catalog");
  await expect(section(page)).toBeHidden();

  // Build real workspace state: selection, editor values, acknowledgment and a
  // completed preview. Opening the catalog must preserve ALL of it.
  await page.getByLabel("Select default main", { exact: true }).check();
  // The editor dropdown loads the shared catalog lazily on focus; the browser
  // section below reuses that cache instead of fetching again.
  await providerInput(page).focus();
  await expect(providerInput(page).locator('option[value="nous"]')).toHaveCount(1);
  await providerInput(page).selectOption("nous");
  await modelInput(page).selectOption("hermes-4-405b");
  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  const editorBefore = await page.getByTestId("change-editor").innerText();

  await openCatalog(page);

  await expect(toggle(page)).toHaveAttribute("aria-expanded", "true");
  await expect(toggle(page)).toContainText("15 models");
  await expect(page.getByRole("heading", { name: "Model catalog — what this profile can route to" })).toBeVisible();
  await expect(section(page)).toHaveAttribute("aria-label", "Model catalog");
  await expect(page.getByLabel("Select default main", { exact: true })).toBeChecked();
  await expect(providerInput(page)).toHaveValue("nous");
  await expect(modelInput(page)).toHaveValue("hermes-4-405b");
  await expect(page.getByLabel("I acknowledge unknown-cost routes")).toBeChecked();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  expect(await page.getByTestId("change-editor").innerText()).toBe(editorBefore);

  if (process.env.HMS_TRACE === "1") console.log(editorBefore);
  const requests = await catalogRequests(page);
  expect(requests).toHaveLength(1);
  expect(requests[0].method).toBe("GET");
  expect(requests[0].search).toContain("profileId=default");
  expect(requests[0].search).not.toContain("refresh");

  await page.getByTestId("catalog-collapse").click();
  await expect(section(page)).toBeHidden();
  await expect(toggle(page)).toHaveAttribute("aria-expanded", "false");
  await openCatalog(page);
  expect(await catalogRequests(page)).toHaveLength(1); // reopen is cache-only
});

test("profile scope change while open fetches exactly once for that profile", async ({ page }) => {
  await openFixture(page);
  await openCatalog(page);
  expect(await catalogRequests(page)).toHaveLength(1);

  await page.getByLabel("Profile Research", { exact: true }).check();
  await expect.poll(async () => (await catalogRequests(page)).length).toBe(2);
  let requests = await catalogRequests(page);
  expect(requests[1].search).toContain("profileId=research");
  expect(requests[1].search).not.toContain("refresh");

  // No N+1 on further renders/reopens for the already-loaded profile.
  await page.waitForTimeout(400);
  await page.getByTestId("catalog-collapse").click();
  await openCatalog(page);
  requests = await catalogRequests(page);
  expect(requests).toHaveLength(2);
  expect(requests.filter((r) => r.search.includes("profileId=research"))).toHaveLength(1);
});

test("provider groups and row counts mirror the /catalog payload exactly", async ({ page }) => {
  await openFixture(page);
  await openCatalog(page);
  await expect.poll(() => page.evaluate(() => Boolean(window.__mockApi.lastCatalog))).toBe(true);
  const payload = await page.evaluate(() => window.__mockApi.lastCatalog);

  expect(await catalogProviderIds(page)).toEqual(payload.providers.map((p) => p.id));
  expect(await groups(page).count()).toBe(payload.providers.length);
  expect(await rows(page).count()).toBe(15);
  for (const provider of payload.providers) {
    await expect(page.locator(`[data-provider-group="${provider.id}"] [data-model-row]`))
      .toHaveCount(provider.models.length);
  }
  // Providers with zero published models still get an honest group row.
  await expect(page.locator('[data-empty-provider="portal"]')).toBeVisible();
  await expect(page.locator('[data-empty-provider="broken-provider"]')).toBeVisible();
  // Group headers summarize the model count and the priced range they saw.
  await expect(page.locator('[data-provider-group="nous"] .hms-catalog-group-head'))
    .toContainText("12 models");
  await expect(page.locator('[data-provider-group="nous"] .hms-catalog-range'))
    .toHaveText("range $1.00–$10.00");
  await expect(page.locator('[data-provider-group="opencode-go"] .hms-catalog-range'))
    .toHaveText("range $0.10");
});

test("price matrix renders exact display strings, free badges only for free, and fabricates no $0.00", async ({ page }) => {
  await openFixture(page);
  await openCatalog(page);

  const hermes = row(page, "nous", "hermes-4-405b");
  await expect(hermes.locator('[data-column="input"]')).toHaveText("$10.00");
  await expect(hermes.locator('[data-column="output"]')).toHaveText("$50.00");
  await expect(hermes.locator('[data-column="status"]')).toHaveText("priced");
  await expect(row(page, "nous", "hermes-filler-12").locator('[data-column="output"]')).toHaveText("$12.00");

  const free = row(page, "nous", "hermes-4-70b");
  await expect(free).toHaveAttribute("data-pricing-status", "free");
  await expect(free.locator(".hms-badge-free")).toHaveCount(2);
  await expect(free.locator(".hms-badge-free").first()).toHaveText("free");
  await expect(free.locator('[data-column="input"]')).not.toContainText("$0.00");

  const included = row(page, "nous", "hermes-4-mini");
  await expect(included.locator('[data-column="input"]')).toHaveText("included in subscription");
  await expect(included.locator('[data-column="status"]')).toHaveText("included");
  await expect(included.locator(".hms-badge-free")).toHaveCount(0);

  const unknown = row(page, "nous", "hermes-unknown");
  await expect(unknown.locator('[data-column="input"]')).toHaveText("—");
  await expect(unknown.locator('[data-column="input"] .hms-catalog-unknown'))
    .toHaveAttribute("title", /no list price in the installed catalog; never treated as free/);
  await expect(unknown.locator('[data-column="output"]')).toHaveText("—");
  await expect(unknown.locator('[data-column="status"]')).toHaveText("unknown");

  // Exactly the one free fixture model wears the badge, and the default
  // fixture (no provider-stated zero) leaks no fabricated zero-dollar string.
  await expect(page.locator(".hms-badge-free")).toHaveCount(2);
  const text = await section(page).innerText();
  expect(text).not.toContain("$0.00");
  expect(text).toContain("unknown — never treated as free");
});

test("a provider-stated zero or 'free' rate for one direction is real data, never unknown", async ({ page }) => {
  await openWithFlags(page, { catalogZeroPriced: true });
  await openCatalog(page);

  // (a) priced with display input "$0.00" and a positive output: both strings
  // render verbatim, and the input cell is not the unknown treatment.
  const zero = row(page, "nous", "zero-input-paid-output");
  await expect(zero).toHaveAttribute("data-pricing-status", "priced");
  await expect(zero.locator('[data-column="input"]')).toHaveText("$0.00");
  await expect(zero.locator('[data-column="output"]')).toHaveText("$0.50");
  await expect(zero.locator('[data-column="input"] .hms-catalog-unknown')).toHaveCount(0);
  await expect(zero.locator('[data-column="input"] [title]')).toHaveCount(0);

  // (c) priced with "free" in ONE direction: literal text, but no free badge.
  const freeInput = row(page, "nous", "free-input-paid-output");
  await expect(freeInput).toHaveAttribute("data-pricing-status", "priced");
  await expect(freeInput.locator('[data-column="input"]')).toHaveText("free");
  await expect(freeInput.locator('[data-column="output"]')).toHaveText("$1.00");
  await expect(freeInput.locator(".hms-badge-free")).toHaveCount(0);

  // (b) absent cost stays unknown with the never-treated-as-free explanation.
  const unknown = row(page, "nous", "hermes-unknown");
  await expect(unknown.locator('[data-column="input"]')).toHaveText("—");
  await expect(unknown.locator('[data-column="input"] .hms-catalog-unknown'))
    .toHaveAttribute("title", "no list price in the installed catalog; never treated as free");

  // (d) the badge still appears only for pricingStatus === "free".
  await expect(page.locator('[data-pricing-status="priced"] .hms-badge-free')).toHaveCount(0);
  await expect(page.locator(".hms-badge-free")).toHaveCount(2);
  await expect(row(page, "nous", "hermes-4-70b").locator(".hms-badge-free")).toHaveCount(2);

  // A real zero is the cheapest known input, not an unknown sorted last.
  await expect(page.getByTestId("catalog-summary-strip").locator('[data-tile="cheapest"] dd'))
    .toContainText("$0.00");
  await expect(page.locator('[data-provider-group="nous"] .hms-catalog-range'))
    .toHaveText("range $0.00–$10.00");
});

test("summary strip reports provider/model counts, cheapest input and unknown honesty", async ({ page }) => {
  await openFixture(page);
  await openCatalog(page);
  const summary = page.getByTestId("catalog-summary-strip");
  await expect(summary).toBeVisible();
  await expect(summary.locator('[data-tile="providers"] dd')).toContainText("5");
  await expect(summary.locator('[data-tile="providers"] dd')).toContainText("(2 priced)");
  await expect(summary.locator('[data-tile="models"] dd')).toContainText("15");
  await expect(summary.locator('[data-tile="models"] dd')).toContainText("10 priced · 1 free");
  await expect(summary.locator('[data-tile="cheapest"] dd')).toContainText("$0.10");
  await expect(summary.locator('[data-tile="cheapest"] dd')).toContainText("per 1M tokens");
  await expect(summary.locator('[data-tile="unknown"] dd')).toContainText("3");
  await expect(summary.locator('[data-tile="unknown"] dd')).toContainText("unknown — never treated as free");
  await expect(page.getByTestId("catalog-provenance")).toContainText("10 of 15 models priced");
  await expect(page.getByTestId("catalog-provenance")).toContainText("3 with no list price");
});

test("Refresh catalog is the only refresh=1 trigger and updates freshness from the payload", async ({ page }) => {
  await openFixture(page);
  await openCatalog(page);
  expect(await catalogRequests(page)).toHaveLength(1);

  await page.getByTestId("catalog-view-refresh").click();
  await expect.poll(async () => (await catalogRequests(page)).length).toBe(2);
  const requests = await catalogRequests(page);
  expect(requests[0].search).not.toContain("refresh");
  expect(requests[1].search).toContain("refresh=1");
  expect(requests[1].search).toContain("profileId=default");

  const freshness = page.getByTestId("catalog-freshness-details");
  await freshness.locator("summary").click();
  await expect(freshness.locator('[data-provider-freshness="nous"]'))
    .toContainText("2026-10-18T18:00:00Z");
  await expect(freshness.locator('[data-provider-freshness="nous"]')).toContainText("fresh");
  await expect(toggle(page)).toContainText("15 models");
});

test("no prices at all omits the cheapest tile and never implies free", async ({ page }) => {
  await openWithFlags(page, { catalogNoPrices: true });
  await openCatalog(page);
  const summary = page.getByTestId("catalog-summary-strip");
  await expect(summary.locator('[data-tile="cheapest"]')).toHaveCount(0);
  await expect(summary.locator('[data-tile="unknown"] dd')).toContainText("15");
  await expect(page.getByTestId("catalog-provenance")).toContainText("0 of 15 models priced");
  await expect(page.locator(".hms-badge-free")).toHaveCount(0);
  const text = await section(page).innerText();
  expect(text).not.toContain("$0.00");
});

test("search matches provider/model and keeps each row's provider+price association", async ({ page }) => {
  await openFixture(page);
  await openCatalog(page);
  const search = page.getByTestId("catalog-search");

  await search.fill("opencode");
  await expect(rows(page)).toHaveCount(2);
  for (const provider of await rows(page).evaluateAll((nodes) =>
    nodes.map((node) => node.getAttribute("data-provider-id")))) {
    expect(provider).toBe("opencode-go");
  }
  await expect(row(page, "opencode-go", "deepseek-v4.1-flash").locator('[data-column="input"]'))
    .toHaveText("$0.10");

  await search.fill("Hermes Filler 9");
  await expect(rows(page)).toHaveCount(1);
  await expect(rows(page).first()).toHaveAttribute("data-model-id", "hermes-filler-9");
  await expect(rows(page).first().locator('[data-column="output"]')).toHaveText("$9.00");

  await search.fill("nothing-matches-this");
  await expect(page.getByTestId("catalog-view-empty-matches")).toBeVisible();
  await expect(page.getByTestId("catalog-view-empty-matches")).toContainText("Clear the search");
  await expect(page.getByTestId("catalog-table")).toHaveCount(0);
  await page.getByTestId("catalog-clear-filters").click();
  await expect(rows(page)).toHaveCount(15);

  // '/' focuses the search unless the user is already typing in a field.
  await page.locator(".hms-page-header").click();
  await page.keyboard.press("/");
  await expect(search).toBeFocused();
});

test("free-only and priced-only toggles filter without inventing availability", async ({ page }) => {
  await openFixture(page);
  await openCatalog(page);
  const freeOnly = page.getByTestId("catalog-free-only");
  const pricedOnly = page.getByTestId("catalog-priced-only");

  await freeOnly.click();
  await expect(freeOnly).toHaveAttribute("aria-pressed", "true");
  await expect(rows(page)).toHaveCount(1);
  await expect(rows(page).first()).toHaveAttribute("data-pricing-status", "free");
  await expect(rows(page).first().locator(".hms-badge-free")).toHaveCount(2);

  await pricedOnly.click();
  await expect(pricedOnly).toHaveAttribute("aria-pressed", "true");
  await expect(freeOnly).toHaveAttribute("aria-pressed", "false");
  await expect(rows(page)).toHaveCount(10);
  const statuses = await rows(page).evaluateAll((nodes) =>
    nodes.map((node) => node.getAttribute("data-pricing-status")));
  expect(statuses.every((s) => s === "priced")).toBe(true);
  await expect(page.locator(".hms-badge-free")).toHaveCount(0);
});

test("sort select orders by price, name and provider with unknowns last and aria-sort updated", async ({ page }) => {
  await openFixture(page);
  await openCatalog(page);
  const sort = page.getByTestId("catalog-sort");
  const inputHeader = page.getByRole("columnheader", { name: /Input price, US dollars per million tokens/ });
  await expect(inputHeader).toHaveAttribute("aria-sort", "ascending");

  await page.getByTestId("catalog-group-toggle").click(); // flatten for global ordering
  await expect(rows(page).first()).toHaveAttribute("data-pricing-status", "free");

  await sort.selectOption("price-desc");
  await expect(inputHeader).toHaveAttribute("aria-sort", "descending");
  await expect(rows(page).first()).toHaveAttribute("data-model-id", "hermes-4-405b");
  await expect(rows(page).first().locator('[data-column="input"]')).toHaveText("$10.00");
  // Unknown/included stay last in BOTH directions: absence is not a bargain.
  await expect(rows(page).last()).toHaveAttribute("data-pricing-status", "unknown");

  await sort.selectOption("name");
  await expect(rows(page).first()).toHaveAttribute("data-model-id", "another-model");
  await expect(page.getByRole("columnheader", { name: "Model", exact: true }))
    .toHaveAttribute("aria-sort", "ascending");

  await sort.selectOption("provider");
  await expect(rows(page).first()).toHaveAttribute("data-provider-id", "nous");
});

test("Configured in cross-references loaded /state slots by exact provider+model", async ({ page }) => {
  await openWithFlags(page, { catalogLinkedSlots: true });
  await openCatalog(page);
  const joined = row(page, "nous", "hermes-4-405b").locator('[data-column="configured"]');
  await expect(joined).toHaveText("3 slots · default, research");
  await expect(joined).toHaveAttribute("title", /default\/auxiliary.vision/);
  await expect(row(page, "nous", "hermes-4-mini").locator('[data-column="configured"]')).toHaveText("—");
});

test("Use pre-fills the editor, scrolls it into view, and announces the route", async ({ page }) => {
  await openFixture(page);
  await openCatalog(page);
  await page.getByTestId("catalog-use-nous-hermes-4-405b").click();

  await expect(providerInput(page)).toHaveValue("nous");
  await expect(modelInput(page)).toHaveValue("hermes-4-405b");
  await expect(page.getByTestId("change-type")).toHaveValue("set");
  await expect(page.getByTestId("live-status")).toContainText("Route set to nous/hermes-4-405b");
  await expect(page.getByLabel("I acknowledge unknown-cost routes")).not.toBeChecked();
  await expect.poll(async () => {
    const box = await page.getByTestId("change-editor").boundingBox();
    return Boolean(box && box.y >= 0 && box.y < 800);
  }).toBe(true);
});

test("Use is disabled with a visible reason when the provider is not authenticated", async ({ page }) => {
  await openWithFlags(page, { catalogPortalModels: true });
  await openCatalog(page);
  const use = page.getByTestId("catalog-use-portal-portal-chat");
  await expect(use).toBeDisabled();
  await expect(use).toHaveAttribute("title", /Sign-in needed/);
  const portalRow = row(page, "portal", "portal-chat");
  await expect(portalRow.locator(".hms-catalog-use-reason"))
    .toContainText("Sign-in needed — not authenticated in profile");
  await expect(page.locator('[data-provider-group="portal"] .hms-catalog-group-status'))
    .toContainText("not authenticated — sign-in needed");
});

test("provider warnings and unavailable-price notes stay visible beside healthy rows", async ({ page }) => {
  await openFixture(page);
  await openCatalog(page);
  await expect(page.locator('[data-provider-group="broken-provider"] [data-provider-warning]'))
    .toContainText("catalog fetch failed: upstream timeout");
  await expect(page.locator('[data-provider-group="portal"] [data-provider-warning]'))
    .toContainText("Sign-in required");
  await expect(page.locator('[data-provider-group="portal"] [data-unavailable-provider]'))
    .toContainText("nothing is treated as free");
  await expect(page.locator('[data-provider-group="broken-provider"] [data-unavailable-provider]'))
    .toBeVisible();
  await expect(row(page, "nous", "hermes-4-405b")).toBeVisible();

  // Flattening removes group headers; the same notices must survive above the table.
  await page.getByTestId("catalog-group-toggle").click();
  await expect(page.getByTestId("catalog-provider-notices")).toBeVisible();
  await expect(page.getByTestId("catalog-provider-notices")).toContainText("upstream timeout");
  await expect(page.getByTestId("catalog-provider-notices")).toContainText("prices unavailable");
});

test("stale and fetchedAt labels come only from payload timestamps", async ({ page }) => {
  await openFixture(page);
  await openCatalog(page);
  const freshness = page.getByTestId("catalog-freshness-details");
  await expect(freshness.locator("summary")).toContainText("4 with a fetch time");
  await expect(freshness.locator("summary")).toContainText("3 of 5 marked stale");
  await freshness.locator("summary").click();
  await expect(freshness.locator('[data-provider-freshness="nous"]'))
    .toContainText("2026-10-18T12:00:00Z");
  await expect(freshness.locator('[data-provider-freshness="nous"]')).toContainText("fresh");
  await expect(freshness.locator('[data-provider-freshness="opencode-go"]'))
    .toContainText("2026-10-17T09:30:00Z");
  await expect(freshness.locator('[data-provider-freshness="opencode-go"]')).toContainText("stale");
  // portal has fetchedAt:null — omitted, never invented.
  await expect(freshness.locator('[data-provider-freshness="portal"]')).toHaveCount(0);
  const text = await section(page).innerText();
  expect(text).not.toMatch(/\b(just now|today|minutes? ago|hours? ago)\b/i);
});

test("cost.pending shows 'prices still loading' and never a free badge or $0.00", async ({ page }) => {
  await openWithFlags(page, { catalogPending: true });
  await openCatalog(page);
  await expect(page.getByTestId("catalog-pending")).toBeVisible();
  await expect(page.getByTestId("catalog-pending")).toContainText("Prices are still loading");
  await expect(page.getByTestId("catalog-provenance")).toContainText("prices still loading");
  await expect(page.locator(".hms-catalog-pending").first()).toHaveText("prices still loading");
  await expect(page.locator(".hms-badge-free")).toHaveCount(0);
  const text = await section(page).innerText();
  expect(text).not.toContain("$0.00");
  await expect(row(page, "nous", "hermes-4-70b").locator('[data-column="input"]'))
    .toHaveText("prices still loading");
});

test("catalog failure renders an error panel with Retry, never an empty table", async ({ page }) => {
  await openFixture(page);
  await page.evaluate(() => { window.__mockApi.catalogFail = true; });
  await toggle(page).click();
  const error = page.getByTestId("catalog-view-error");
  await expect(error).toBeVisible();
  await expect(error).toContainText("Catalog failed to load");
  await expect(error).toContainText("upstream timeout");
  await expect(page.getByTestId("catalog-table")).toHaveCount(0);
  await expect(page.getByTestId("catalog-view-retry")).toBeVisible();

  await page.evaluate(() => { window.__mockApi.catalogFail = false; });
  await page.getByTestId("catalog-view-retry").click();
  await expect(page.getByTestId("catalog-table")).toBeVisible();
  await expect(rows(page)).toHaveCount(15);
  expect(await catalogRequests(page)).toHaveLength(2);
});

test("empty catalog explains itself without implying free access", async ({ page }) => {
  await openWithFlags(page, { catalogEmpty: true });
  await openCatalog(page);
  const empty = page.getByTestId("catalog-view-empty");
  await expect(empty).toBeVisible();
  await expect(empty).toContainText("reports no providers");
  await expect(empty).toContainText("treated as free");
  await expect(page.getByTestId("catalog-table")).toHaveCount(0);
  await expect(toggle(page)).toContainText("0 models");
});

test("loading state is explicit before the catalog resolves", async ({ page }) => {
  await openFixture(page);
  await page.evaluate(() => { window.__mockApi.catalogDelayMs = 600; });
  await toggle(page).click();
  await expect(page.getByTestId("catalog-view-loading")).toBeVisible();
  await expect(page.getByTestId("catalog-view-loading")).toContainText("Loading model catalog for profile");
  await expect(page.getByTestId("catalog-table")).toBeVisible({ timeout: 5000 });
  await expect(rows(page)).toHaveCount(15);
});

test("50-row cap per provider with a Show all N expander", async ({ page }) => {
  await openWithFlags(page, { catalogManyModels: true });
  await openCatalog(page);
  await expect(toggle(page)).toContainText("75 models");
  await expect(page.locator('[data-provider-group="nous"] [data-model-row]')).toHaveCount(50);
  const showAll = page.getByTestId("catalog-show-all-nous");
  await expect(showAll).toHaveText("Show all 72");
  await expect(showAll).toHaveAttribute("aria-expanded", "false");
  await showAll.click();
  await expect(page.locator('[data-provider-group="nous"] [data-model-row]')).toHaveCount(72);
  await expect(showAll).toHaveText("Show fewer");
  await expect(showAll).toHaveAttribute("aria-expanded", "true");
});

test("the same model id on two providers renders as two rows with their own prices", async ({ page }) => {
  await openWithFlags(page, { catalogDuplicateModel: true });
  await openCatalog(page);
  await page.getByTestId("catalog-search").fill("hermes-4-405b");
  await expect(rows(page)).toHaveCount(2);
  const providers = await rows(page).evaluateAll((nodes) =>
    nodes.map((node) => node.getAttribute("data-provider-id")).sort());
  expect(providers).toEqual(["gateway", "nous"]);
  await expect(row(page, "nous", "hermes-4-405b").locator('[data-column="input"]')).toHaveText("$10.00");
  await expect(row(page, "gateway", "hermes-4-405b").locator('[data-column="input"]')).toHaveText("$99.00");
  await expect(row(page, "gateway", "hermes-4-405b").locator('[data-column="output"]')).toHaveText("$199.00");
});

test("browsing is read-only: no POST/PUT, no preview/apply, acknowledgment untouched", async ({ page }) => {
  await openFixture(page);
  const ack = page.getByLabel("I acknowledge unknown-cost routes");
  await ack.check();
  await openCatalog(page);

  await page.getByTestId("catalog-search").fill("hermes");
  await page.getByTestId("catalog-free-only").click();
  await page.getByTestId("catalog-priced-only").click();
  await page.getByTestId("catalog-sort").selectOption("name");
  await page.getByTestId("catalog-use-nous-hermes-4-405b").click();
  await expect(ack).toBeChecked();

  const requests = await page.evaluate(() => window.__mockApi.requests);
  expect(requests.filter((r) => r.method === "POST" || r.method === "PUT")).toHaveLength(0);
  expect(requests.filter((r) => /\/(preview|operations|presets)$/.test(r.path))).toHaveLength(0);
  expect(requests.filter((r) => r.path.endsWith("/catalog"))).toHaveLength(1);
});

test("table accessibility: caption, scoped headers, sort state, keyboard scroll, 38px targets", async ({ page }) => {
  await openFixture(page);
  await openCatalog(page);
  const table = page.getByTestId("catalog-table");
  const caption = table.locator("caption");
  await expect(caption).toContainText("per-million-token prices");
  const captionBox = await caption.boundingBox();
  expect(captionBox === null || captionBox.width <= 1).toBe(true);

  await expect(table.locator('thead th[scope="col"]')).toHaveCount(6);
  await expect(page.locator('[data-provider-group="nous"] th[scope="colgroup"]')).toHaveCount(1);
  await expect(page.getByRole("columnheader", { name: "Input price, US dollars per million tokens" }))
    .toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Output price, US dollars per million tokens" }))
    .toBeVisible();

  const region = page.getByTestId("catalog-scroll");
  await expect(region).toHaveAttribute("role", "region");
  await expect(region).toHaveAttribute("tabindex", "0");
  await expect(region).toHaveAttribute("aria-label", "Scrollable model catalog");
  await region.focus();
  await expect(region).toBeFocused();

  expect(await table.locator("button button").count()).toBe(0);
  const useBox = await page.getByTestId("catalog-use-nous-hermes-4-405b").boundingBox();
  expect(useBox.height).toBeGreaterThanOrEqual(38);
});

for (const width of [375, 768]) {
  test(`no document overflow at ${width}px; the table keeps its own scroll region`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await openFixture(page);
    await openCatalog(page);
    const bounds = await page.evaluate(() => ({
      doc: document.documentElement.scrollWidth, viewport: innerWidth,
    }));
    expect(bounds.doc).toBeLessThanOrEqual(bounds.viewport);

    const sectionBox = await section(page).boundingBox();
    expect(sectionBox.x).toBeGreaterThanOrEqual(0);
    expect(sectionBox.x + sectionBox.width).toBeLessThanOrEqual(width + 0.5);

    const region = page.getByTestId("catalog-scroll");
    await region.focus();
    await page.keyboard.press("ArrowRight");
    await expect.poll(() => region.evaluate((node) => node.scrollLeft)).toBeGreaterThan(0);
    await region.evaluate((node) => { node.scrollLeft = 0; });

    if (width === 375) {
      const toggleCols = await page.getByTestId("catalog-free-only").evaluate((node) => {
        const style = getComputedStyle(node.parentElement);
        return { display: style.display, columns: style.gridTemplateColumns };
      });
      expect(toggleCols.display).toBe("grid");
      expect(toggleCols.columns.split(" ").length).toBe(2);
    }
    const summaryCols = await page.getByTestId("catalog-summary-strip").evaluate(
      (node) => getComputedStyle(node).gridTemplateColumns.split(" ").length);
    expect(summaryCols).toBe(2);
  });
}

test("the section is full width and sits above the workspace grid", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await openFixture(page);
  await openCatalog(page);
  const sectionBox = await section(page).boundingBox();
  const workspaceBox = await page.locator(".hms-workspace").boundingBox();
  expect(sectionBox.y + sectionBox.height).toBeLessThanOrEqual(workspaceBox.y + 1);
  expect(Math.abs(sectionBox.width - workspaceBox.width)).toBeLessThanOrEqual(2);
});
