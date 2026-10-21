import { test, expect } from "@playwright/test";
import { realisticFixture, REALISTIC } from "./fixtures/catalog-realistic.js";
import { labCounts, labOf } from "../ui/src/labs.js";

// The Model Catalog against a fixture shaped like the audited live default
// profile: 54 providers (9 set up, 45 authenticated:false with zero models),
// 258 models, every pricingStatus "unknown", OpenRouter `:free` id suffixes,
// and a few `capabilities` objects. tests/mock-api.js serves it verbatim under
// the `catalogRealistic` flag (see tests/fixtures/catalog-realistic.js).

const FIXTURE = realisticFixture();
const SET_UP = FIXTURE.providers.default.filter((p) => p.authenticated !== false);
const NOT_SET_UP = FIXTURE.providers.default.filter((p) => p.authenticated === false);
const ENTRIES = SET_UP.flatMap((p) => p.models.map((m) => ({ providerId: p.id, modelId: m.id })));
const LABS = labCounts(ENTRIES, Object.fromEntries(SET_UP.map((p) => [p.id, p.label])));
const SLOT_PAIRS = new Set(FIXTURE.slots.map((s) => `${s.provider}|${s.model}`));
const IN_USE = ENTRIES.filter((e) => SLOT_PAIRS.has(`${e.providerId}|${e.modelId}`));
const FREE_IDS = ENTRIES.filter((e) => /:free$/.test(e.modelId));
const FAST_IDS = SET_UP.flatMap((p) => p.models
  .filter((m) => m.capabilities && m.capabilities.fast === true)
  .map((m) => ({ providerId: p.id, modelId: m.id })));
const CAN_DISABLE_IDS = SET_UP.flatMap((p) => p.models
  .filter((m) => m.capabilities && m.capabilities.canDisableReasoning === true)
  .map((m) => ({ providerId: p.id, modelId: m.id })));

const rows = (page) => page.locator("[data-model-row]");
const groups = (page) => page.locator("[data-provider-group]");

async function openRealistic(page, { prices = false, fixture = null } = {}) {
  const data = fixture || realisticFixture({ prices });
  await page.addInitScript((payload) => { window.__HMS_CATALOG_FIXTURE = payload; }, data);
  await page.goto("/fixture.html");
  await page.evaluate(() => sessionStorage.setItem(
    "hms-mock-flags", JSON.stringify({ catalogRealistic: true })));
  await page.reload();
  await expect(page.getByLabel("New provider", { exact: true })).toBeVisible();
  await page.getByTestId("catalog-toggle").click();
  await expect(page.getByTestId("catalog-view")).toBeVisible();
  await expect(page.getByTestId("catalog-table")).toBeVisible();
}

const catalogRequests = (page) => page.evaluate(() =>
  window.__mockApi.requests.filter((r) => r.path.endsWith("/catalog")));

test("set-up providers lead, opencode-go is visible, and unset-up providers collapse into one section", async ({ page }) => {
  await openRealistic(page);

  const groupIds = await groups(page).evaluateAll(
    (nodes) => nodes.map((node) => node.getAttribute("data-provider-group")));
  expect(groupIds.slice(0, REALISTIC.setUpProviderIds.length)).toEqual(REALISTIC.setUpProviderIds);
  expect(groupIds.slice(REALISTIC.setUpProviderIds.length)).toHaveLength(NOT_SET_UP.length);
  // The host returns opencode-go 34th of 54, behind 30+ empty headers; ordering
  // by setup state puts it in the set-up block (third group here).
  expect(REALISTIC.opencodeGoHostIndex).toBe(33);
  expect(groupIds.indexOf("opencode-go")).toBe(REALISTIC.setUpProviderIds.indexOf("opencode-go"));
  await expect(page.locator('[data-provider-group="opencode-go"] [data-model-row]').first()).toBeVisible();

  // One collapsed disclosure holds every authenticated:false provider.
  const unsetup = page.getByTestId("catalog-unsetup");
  await expect(unsetup).toBeVisible();
  await expect(page.getByTestId("catalog-unsetup-summary")).toContainText(`Not set up (${NOT_SET_UP.length})`);
  expect(await unsetup.evaluate((el) => el.open)).toBe(false);
  await expect(page.locator('[data-provider-group="azure-openai"]')).toBeHidden();
  await expect(page.getByTestId("catalog-scope-summary"))
    .toHaveText(`${SET_UP.length} set up · ${FIXTURE.providers.default.length} providers · ${REALISTIC.modelCount} models`);

  await page.getByTestId("catalog-unsetup-summary").click();
  await expect(page.locator('[data-provider-group="azure-openai"]')).toBeVisible();
  await expect(page.locator('[data-provider-group="azure-openai"] .hms-catalog-group-status'))
    .toContainText("not authenticated — sign-in needed");
  await expect(page.locator('[data-provider-group="azure-openai"] [data-empty-provider="azure-openai"]'))
    .toContainText("not evidence of free access");
});

test("Lab, Provider, Status, In-use and capability filters narrow with honest counts", async ({ page }) => {
  await openRealistic(page);
  const labSelect = page.getByTestId("catalog-lab");
  const labOptions = await labSelect.locator("option").allTextContents();
  expect(labOptions[0]).toBe("All labs");
  expect(labOptions).toHaveLength(LABS.length + 1);
  for (const { label, count } of LABS) expect(labOptions).toContain(`${label} (${count})`);

  const anthropic = LABS.find((entry) => entry.lab === "anthropic");
  await labSelect.selectOption("anthropic");
  await expect(rows(page)).toHaveCount(anthropic.count);
  const labsOfRows = await rows(page).evaluateAll((nodes) => nodes.map((node) =>
    node.dataset.modelId.includes("/") ? node.dataset.modelId.split("/")[0] : node.dataset.providerId));
  expect(labsOfRows.every((lab) => lab === "anthropic")).toBe(true);
  // A lab spans providers; only providers that actually matched keep a group,
  // and the whole not-set-up section is filtered out.
  const labProviders = new Set(ENTRIES
    .filter((entry) => labOf(entry.providerId, entry.modelId) === "anthropic")
    .map((entry) => entry.providerId));
  expect(labProviders.size).toBeGreaterThan(1);
  await expect(groups(page)).toHaveCount(labProviders.size);
  await expect(page.locator("[data-empty-provider]")).toHaveCount(0);
  await expect(page.getByTestId("catalog-unsetup")).toHaveCount(0);
  await page.getByTestId("catalog-clear-filters-control").click();
  await expect(page.getByTestId("catalog-match-count"))
    .toContainText(`Showing ${REALISTIC.modelCount} of ${REALISTIC.modelCount} models`);

  // Provider select offers set-up providers only.
  const providerOptions = await page.getByTestId("catalog-provider").locator("option").allTextContents();
  expect(providerOptions).toHaveLength(SET_UP.length + 1);
  expect(providerOptions[0]).toBe("All providers");
  await page.getByTestId("catalog-provider").selectOption("opencode-go");
  await expect(rows(page)).toHaveCount(REALISTIC.opencodeGoModels);
  await page.getByTestId("catalog-clear-filters-control").click();

  // Status: Needs sign-in switches the page to the collapsed section (open).
  await page.getByTestId("catalog-status").selectOption("signin");
  await expect(page.getByTestId("catalog-table")).toHaveCount(0);
  await expect(page.getByTestId("catalog-unsetup")).toBeVisible();
  expect(await page.getByTestId("catalog-unsetup").evaluate((el) => el.open)).toBe(true);
  await expect(groups(page)).toHaveCount(NOT_SET_UP.length);
  await page.getByTestId("catalog-status").selectOption("setup");
  await expect(groups(page)).toHaveCount(SET_UP.length);
  await expect(page.getByTestId("catalog-match-count"))
    .toContainText(`Showing ${REALISTIC.modelCount} of ${REALISTIC.modelCount} models`);
  await expect(page.getByTestId("catalog-unsetup")).toHaveCount(0);
  await page.getByTestId("catalog-status").selectOption("all");

  // In use = exact provider+model identity joined against loaded /state slots.
  await page.getByTestId("catalog-in-use").click();
  await expect(page.getByTestId("catalog-in-use")).toHaveAttribute("aria-pressed", "true");
  await expect(rows(page)).toHaveCount(IN_USE.length);
  const inUseKeys = await rows(page).evaluateAll((nodes) =>
    nodes.map((node) => `${node.dataset.providerId}|${node.dataset.modelId}`));
  expect(inUseKeys.sort()).toEqual(IN_USE.map((e) => `${e.providerId}|${e.modelId}`).sort());
  await page.getByTestId("catalog-clear-filters-control").click();

  // Capability toggles exist only because the fixture carries the field.
  await expect(page.getByTestId("catalog-fast-only")).toBeVisible();
  await expect(page.getByTestId("catalog-can-disable-only")).toBeVisible();
  await page.getByTestId("catalog-fast-only").click();
  await expect(rows(page)).toHaveCount(FAST_IDS.length);
  await page.getByTestId("catalog-fast-only").click();
  await page.getByTestId("catalog-can-disable-only").click();
  await expect(rows(page)).toHaveCount(CAN_DISABLE_IDS.length);
  await page.getByTestId("catalog-clear-filters-control").click();

  // :free is an id suffix, never a price claim, and is labelled as such.
  await expect(page.getByTestId("catalog-free-ids"))
    .toHaveAttribute("title", /not a price claim/);
  await page.getByTestId("catalog-free-ids").click();
  await expect(rows(page)).toHaveCount(FREE_IDS.length);
  const freeIds = await rows(page).evaluateAll((nodes) => nodes.map((node) => node.dataset.modelId));
  expect(freeIds.every((id) => id.endsWith(":free"))).toBe(true);
});

test("capability toggles stay absent when the catalog has no capabilities field", async ({ page }) => {
  const fixture = realisticFixture();
  for (const provider of fixture.providers.default) {
    for (const model of provider.models) delete model.capabilities;
  }
  await openRealistic(page, { fixture });
  await expect(page.getByTestId("catalog-fast-only")).toHaveCount(0);
  await expect(page.getByTestId("catalog-can-disable-only")).toHaveCount(0);
  await expect(page.getByTestId("catalog-match-count"))
    .toContainText(`Showing ${REALISTIC.modelCount} of ${REALISTIC.modelCount} models`);
});

test("price filters and price sorts hide with a note when nothing is priced or free", async ({ page }) => {
  await openRealistic(page);
  await expect(page.getByTestId("catalog-free-only")).toHaveCount(0);
  await expect(page.getByTestId("catalog-priced-only")).toHaveCount(0);
  await expect(page.getByTestId("catalog-price-note"))
    .toHaveText("No list prices in this catalog — price filters hidden.");
  await expect(page.getByTestId("catalog-sort")).toHaveValue("name");
  const sortValues = await page.getByTestId("catalog-sort").locator("option")
    .evaluateAll((options) => options.map((o) => o.value));
  expect(sortValues).toEqual(["name", "provider"]);
});

test("price filters and sorts work as before when the catalog carries prices", async ({ page }) => {
  await openRealistic(page, { prices: true });
  await expect(page.getByTestId("catalog-free-only")).toBeVisible();
  await expect(page.getByTestId("catalog-priced-only")).toBeVisible();
  await expect(page.getByTestId("catalog-price-note")).toHaveCount(0);
  const sortValues = await page.getByTestId("catalog-sort").locator("option")
    .evaluateAll((options) => options.map((o) => o.value));
  expect(sortValues).toEqual(["price-asc", "price-desc", "name", "provider"]);
  await expect(page.getByTestId("catalog-sort")).toHaveValue("price-asc");
  await page.getByTestId("catalog-priced-only").click();
  await expect(rows(page)).toHaveCount(3);
  await page.getByTestId("catalog-free-only").click();
  await expect(rows(page)).toHaveCount(1);
  await expect(rows(page).first()).toHaveAttribute("data-pricing-status", "free");
});

test("Group by Lab groups across providers; None shows Provider and Lab columns with the 100-row page", async ({ page }) => {
  await openRealistic(page);
  await page.getByTestId("catalog-group-select").selectOption("lab");
  const labGroups = page.locator("[data-lab-group]");
  await expect(labGroups).toHaveCount(LABS.length);
  await expect(page.locator('[data-lab-group="anthropic"] .hms-catalog-group-head'))
    .toContainText("Anthropic");
  const anthropicLab = LABS.find((entry) => entry.lab === "anthropic");
  await expect(page.locator('[data-lab-group="anthropic"] .hms-catalog-group-head'))
    .toContainText(`${anthropicLab.count} models`);
  const anthropicRows = page.locator('[data-lab-group="anthropic"] [data-model-row]');
  await expect(anthropicRows).toHaveCount(anthropicLab.count);
  const first = anthropicRows.first();
  await expect(first).toHaveAttribute("data-provider-id", "anthropic");
  await expect(first.locator(".hms-catalog-provider-tag")).toHaveText("Anthropic");
  // The leaf is shown; the full id stays available as the title.
  const leaf = first.locator("code.hms-catalog-model-id");
  await expect(leaf).toHaveAttribute("title", /^anthropic\//);
  await expect(leaf).not.toContainText("/");
  // The not-set-up section still sits at the bottom, collapsed.
  await expect(page.getByTestId("catalog-unsetup-summary")).toContainText(`Not set up (${NOT_SET_UP.length})`);
  expect(await page.getByTestId("catalog-unsetup").evaluate((el) => el.open)).toBe(false);

  await page.getByTestId("catalog-group-select").selectOption("none");
  const headers = await page.getByTestId("catalog-table").locator('thead th[scope="col"]')
    .allTextContents();
  expect(headers).toContain("Provider");
  expect(headers).toContain("Lab");
  expect(headers).toHaveLength(8);
  await expect(rows(page)).toHaveCount(100);
  await expect(rows(page).first().locator('[data-column="provider"]')).not.toBeEmpty();
  await expect(rows(page).first().locator('[data-column="lab"]')).not.toBeEmpty();
  await expect(page.getByTestId("catalog-show-all-flat")).toContainText(`Show all ${REALISTIC.modelCount}`);
  await page.getByTestId("catalog-show-all-flat").click();
  await expect(rows(page)).toHaveCount(REALISTIC.modelCount);
});

test("the catalog profile is explicit and switching it loads that profile's catalog", async ({ page }) => {
  await openRealistic(page);
  const select = page.getByTestId("catalog-profile-select");
  await expect(page.getByLabel("Catalog for profile")).toBeVisible();
  await expect(select).toHaveValue("default");
  const values = await select.locator("option").evaluateAll((options) => options.map((o) => o.value));
  expect(values).toEqual(expect.arrayContaining(["default", "research", "moa"]));
  await expect(page.getByTestId("catalog-provenance")).toContainText("profile default");
  await expect(page.getByTestId("catalog-scope-summary")).toHaveText("9 set up · 54 providers · 258 models");
  await expect(page.locator('[data-provider-group="moa-gateway"]')).toHaveCount(0);

  const before = (await catalogRequests(page)).length;
  await select.selectOption("moa");
  await expect.poll(async () => (await catalogRequests(page)).length).toBe(before + 1);
  const requests = await catalogRequests(page);
  expect(requests[requests.length - 1].body.profile).toBe("moa");
  await expect(select).toHaveValue("moa");
  await expect(page.getByTestId("catalog-provenance")).toContainText("profile moa");
  await expect(page.getByTestId("catalog-scope-summary")).toHaveText("10 set up · 55 providers · 260 models");
  await expect(page.locator('[data-provider-group="moa-gateway"]')).toBeVisible();
  await expect(page.locator('[data-provider-group="moa-gateway"] [data-model-row]')).toHaveCount(2);
  // Use still pre-fills the editor from the profile the catalog is showing.
  await page.getByTestId("catalog-use-moa-gateway-moa/ensemble-deep").click();
  await expect(page.getByLabel("New provider", { exact: true })).toHaveValue("moa-gateway");
  await expect(page.getByLabel("New model", { exact: true })).toHaveValue("moa/ensemble-deep");
  await expect(page.getByTestId("live-status")).toContainText("Route set to moa-gateway · moa/ensemble-deep");

  // Switching back is a cache hit on the existing loading path: no second POST,
  // and the provenance line names the profile being shown.
  const afterMoa = (await catalogRequests(page)).length;
  await select.selectOption("default");
  await expect(page.getByTestId("catalog-provenance")).toContainText("profile default");
  expect((await catalogRequests(page)).length).toBe(afterMoa);
  await expect(page.getByTestId("catalog-scope-summary")).toHaveText("9 set up · 54 providers · 258 models");
  await expect(page.locator('[data-provider-group="moa-gateway"]')).toHaveCount(0);
});

test("a single checked matrix profile seeds the catalog select, never a silent fallback", async ({ page }) => {
  await openRealistic(page);
  await page.getByLabel("Profile Research", { exact: true }).check();
  await expect(page.getByTestId("catalog-profile-select")).toHaveValue("research");
  await expect(page.getByTestId("catalog-provenance")).toContainText("profile research");
  // Choosing a profile explicitly overrides the matrix filter from then on.
  await page.getByTestId("catalog-profile-select").selectOption("default");
  await page.getByLabel("Profile Research", { exact: true }).uncheck();
  await expect(page.getByTestId("catalog-profile-select")).toHaveValue("default");
});
