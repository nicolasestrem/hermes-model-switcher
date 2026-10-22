import { test, expect } from "@playwright/test";
import { realisticFixture, REALISTIC, SIGNED_OUT_PUBLISHER } from "./fixtures/catalog-realistic.js";
import { labOf } from "../ui/src/labs.js";

// The Model Catalog against a fixture shaped like the audited live default
// profile: 54 providers (9 set up, 45 authenticated:false with zero models),
// 258 models, every pricingStatus "unknown", OpenRouter `:free` id suffixes,
// and a few `capabilities` objects. tests/mock-api.js serves it verbatim under
// the `catalogRealistic` flag (see tests/fixtures/catalog-realistic.js).

const FIXTURE = realisticFixture();
const SET_UP = FIXTURE.providers.default.filter((p) => p.authenticated !== false);
const NOT_SET_UP = FIXTURE.providers.default.filter((p) => p.authenticated === false);
const ENTRIES = SET_UP.flatMap((p) => p.models.map((m) => ({ providerId: p.id, modelId: m.id })));
// Expected lab options, stated literally: deriving them with
// labs.js::labCounts (the function the view calls) would let a wrong label or
// count agree with itself. Each count is the fixture's published models in that
// namespace, and the select must equal this list exactly, in this order.
const LABS = [
  { lab: "anthropic", label: "Anthropic", count: 24 },
  { lab: "copilot-acp", label: "Copilot ACP", count: 17 },
  { lab: "deepseek", label: "DeepSeek", count: 8 },
  { lab: "copilot", label: "GitHub Copilot", count: 17 },
  { lab: "google", label: "Google", count: 7 },
  { lab: "meta", label: "Meta", count: 8 },
  { lab: "minimax", label: "MiniMax", count: 6 },
  { lab: "moonshotai", label: "Moonshot AI", count: 7 },
  { lab: "nvidia", label: "NVIDIA", count: 8 },
  { lab: "openai", label: "OpenAI", count: 23 },
  { lab: "opencode-go", label: "opencode go", count: 46 },
  { lab: "poolside", label: "Poolside", count: 6 },
  { lab: "primalabs-ai", label: "PrimaLabs", count: 13 },
  { lab: "qwen", label: "Qwen", count: 7 },
  { lab: "sakana", label: "Sakana AI", count: 6 },
  { lab: "stepfun", label: "StepFun", count: 6 },
  { lab: "tencent", label: "Tencent", count: 6 },
  { lab: "thinkingmachines", label: "Thinking Machines", count: 6 },
  { lab: "upstage", label: "Upstage", count: 6 },
  { lab: "x-ai", label: "xAI", count: 17 },
  { lab: "xiaomi", label: "Xiaomi", count: 6 },
  { lab: "z-ai", label: "Z.ai", count: 8 },
];
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
  // The host returns opencode-go 34th of 54, behind 30+ empty headers; the
  // fixture keeps that order, and the view must still put it in the set-up
  // block instead of inheriting host order.
  const hostIds = FIXTURE.providers.default.map((p) => p.id);
  expect(hostIds.indexOf("opencode-go")).toBeGreaterThan(REALISTIC.setUpProviderIds.length);
  expect(groupIds.indexOf("opencode-go")).toBeLessThan(REALISTIC.setUpProviderIds.length);
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
  // Exact list, exact order: a wrong label or a count that includes rows the
  // filter cannot reach would fail here.
  expect(labOptions).toEqual(["All labs", ...LABS.map((entry) => `${entry.label} (${entry.count})`)]);

  const anthropic = LABS.find((entry) => entry.lab === "anthropic");
  await labSelect.selectOption("anthropic");
  await expect(rows(page)).toHaveCount(anthropic.count);
  const labsOfRows = await rows(page).evaluateAll((nodes) => nodes.map((node) =>
    node.dataset.modelId.includes("/") ? node.dataset.modelId.split("/")[0] : node.dataset.providerId));
  expect(labsOfRows.every((lab) => lab === "anthropic")).toBe(true);
  // A lab spans providers; only providers that actually matched keep a group.
  // Every signed-out provider here publishes no models, so the not-set-up
  // section has nothing to show under this lab — the case where one DOES
  // publish anthropic ids is the next test's job.
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

// --- M1: a lab filter must reach signed-out providers that publish rows ------

test("a not-set-up provider that publishes a lab's ids is counted and rendered under that lab", async ({ page }) => {
  const fixture = realisticFixture({ signedOutModels: true });
  const total = REALISTIC.modelCount + SIGNED_OUT_PUBLISHER.modelCount;
  await openRealistic(page, { fixture });

  const labSelect = page.getByTestId("catalog-lab");
  // The option counts every published row, including the two that sit in the
  // not-set-up section: the count and the rows it promises must be the same set.
  await expect(labSelect.locator(`option[value="${SIGNED_OUT_PUBLISHER.lab}"]`))
    .toHaveText(`Anthropic (${LABS.find((l) => l.lab === "anthropic").count + SIGNED_OUT_PUBLISHER.modelCount})`);

  await labSelect.selectOption(SIGNED_OUT_PUBLISHER.lab);
  await expect(page.getByTestId("catalog-match-count"))
    .toHaveText(`Showing ${LABS.find((l) => l.lab === "anthropic").count + SIGNED_OUT_PUBLISHER.modelCount} of ${total} models (filtered).`);
  // Set-up rows render in the table; the publisher's rows render in the section.
  await expect(page.locator("[data-model-row]:visible")).toHaveCount(LABS.find((l) => l.lab === "anthropic").count);
  const unsetup = page.getByTestId("catalog-unsetup");
  await expect(unsetup).toBeVisible();
  await expect(page.getByTestId("catalog-unsetup-summary"))
    .toContainText(`Not set up (1 of ${NOT_SET_UP.length})`);
  await page.getByTestId("catalog-unsetup-summary").click();
  const published = page.locator(`[data-provider-group="${SIGNED_OUT_PUBLISHER.providerId}"] [data-model-row]`);
  await expect(published).toHaveCount(SIGNED_OUT_PUBLISHER.modelCount);
  // The rows are visible but unroutable: sign-in is still required.
  for (const button of await page.locator(`[data-testid^="catalog-use-${SIGNED_OUT_PUBLISHER.providerId}-"]`).all()) {
    await expect(button).toBeDisabled();
  }

  // V1: the set-up group header states rows-of-published, not a catalog-wide
  // total, while the filter is active.
  const nous = SET_UP.find((p) => p.id === "nous");
  const nousShown = nous.models.filter((m) => labOf(nous.id, m.id) === "anthropic").length;
  await expect(page.locator('[data-provider-group="nous"] .hms-catalog-group-head span').first())
    .toHaveText(`${nousShown} of ${nous.models.length} models`);
});

test("group headers count what is rendered, not the whole catalog", async ({ page }) => {
  await openRealistic(page);
  const needle = "model-1";
  const nous = SET_UP.find((p) => p.id === "nous");
  const nousShown = nous.models.filter((m) => m.id.toLowerCase().includes(needle) ||
    String(m.label).toLowerCase().includes(needle)).length;
  expect(nousShown).toBeLessThan(nous.models.length);
  await page.getByTestId("catalog-search").fill(needle);
  await expect(page.locator('[data-provider-group="nous"] .hms-catalog-group-head span').first())
    .toHaveText(`${nousShown} of ${nous.models.length} models`);

  await page.getByTestId("catalog-group-select").selectOption("lab");
  const anthropicTotal = LABS.find((l) => l.lab === "anthropic").count;
  const anthropicShown = SET_UP.flatMap((p) => p.models.filter((m) =>
    labOf(p.id, m.id) === "anthropic" &&
    (m.id.toLowerCase().includes(needle) || String(m.label).toLowerCase().includes(needle)))).length;
  expect(anthropicShown).toBeLessThan(anthropicTotal);
  await expect(page.locator('[data-lab-group="anthropic"] .hms-catalog-group-head span').first())
    .toHaveText(`${anthropicShown} of ${anthropicTotal} models`);

  // Unfiltered again: the header is the plain total, never "24 of 24".
  await page.getByTestId("catalog-search").fill("");
  await expect(page.locator('[data-lab-group="anthropic"] .hms-catalog-group-head span').first())
    .toHaveText(`${anthropicTotal} models`);
  await page.getByTestId("catalog-group-select").selectOption("provider");
  await expect(page.locator('[data-provider-group="nous"] .hms-catalog-group-head span').first())
    .toHaveText(`${nous.models.length} models`);
});

// --- M2: controls that disappear must not keep filtering ----------------------

// `default` publishes prices, `moa` is the same catalog plus one unpriced
// provider and no priced/free row at all — the smallest shape where switching
// the catalog profile takes the price controls away.
function mixedPriceFixture() {
  const priced = realisticFixture({ prices: true });
  const unpriced = priced.providers.default.map((provider) => ({
    ...provider,
    models: provider.models.map((model) => ({ ...model, pricingStatus: "unknown", cost: null })),
  }));
  const gateway = priced.providers.moa[priced.providers.moa.length - 1];
  return {
    profiles: priced.profiles,
    slots: priced.slots,
    providers: { default: priced.providers.default, research: priced.providers.research, moa: [...unpriced, gateway] },
  };
}

test("a profile switch clears a price filter and a price sort whose controls disappear", async ({ page }) => {
  await openRealistic(page, { fixture: mixedPriceFixture() });
  const total = REALISTIC.modelCount;
  await expect(page.getByTestId("catalog-sort")).toHaveValue("price-asc");
  await page.getByTestId("catalog-priced-only").click();
  await expect(page.getByTestId("catalog-match-count")).toHaveText("Showing 3 of 258 models (filtered).");

  await page.getByTestId("catalog-profile-select").selectOption("moa");
  // Controls gone, and the state went with them: the table is not narrowed and
  // the sort select is genuinely on Name.
  await expect(page.getByTestId("catalog-priced-only")).toHaveCount(0);
  await expect(page.getByTestId("catalog-free-only")).toHaveCount(0);
  await expect(page.getByTestId("catalog-sort")).toHaveValue("name");
  await expect(page.getByTestId("catalog-match-count")).toHaveText(`Showing ${total + 2} of ${total + 2} models.`);
  await expect(page.getByTestId("catalog-price-note")).toBeVisible();

  // Coming back to the priced profile must not resurrect what it dropped.
  await page.getByTestId("catalog-profile-select").selectOption("default");
  await expect(page.getByTestId("catalog-priced-only")).toHaveAttribute("aria-pressed", "false");
  await expect(page.getByTestId("catalog-sort")).toHaveValue("name");
  await expect(page.getByTestId("catalog-match-count")).toHaveText(`Showing ${total} of ${total} models.`);
});

test("a profile switch clears a lab and a provider filter whose options vanish", async ({ page }) => {
  await openRealistic(page);
  await page.getByTestId("catalog-profile-select").selectOption("moa");
  // `moa` is the only profile that publishes these two options.
  await page.getByTestId("catalog-lab").selectOption("moa");
  await page.getByTestId("catalog-provider").selectOption("moa-gateway");
  await expect(page.locator("[data-model-row]")).toHaveCount(2);

  await page.getByTestId("catalog-profile-select").selectOption("default");
  // Neither option exists here; the selects must not read "All labs"/"All
  // providers" while a stale filter empties the table.
  await expect(page.getByTestId("catalog-lab")).toHaveValue("all");
  await expect(page.getByTestId("catalog-lab").locator("option:checked")).toHaveText("All labs");
  await expect(page.getByTestId("catalog-provider")).toHaveValue("all");
  await expect(page.getByTestId("catalog-match-count"))
    .toHaveText(`Showing ${REALISTIC.modelCount} of ${REALISTIC.modelCount} models.`);
  await expect(page.getByTestId("catalog-view-empty-matches")).toHaveCount(0);
});

test("a profile switch clears capability and :free-id filters the new profile cannot offer", async ({ page }) => {
  const priced = realisticFixture({ prices: true });
  // `research` keeps the same providers with no capabilities and no `:free`
  // ids, so the Fast and :free-id toggles have no reason to exist there.
  const research = priced.providers.default.map((provider) => ({
    ...provider,
    models: provider.models
      .filter((model) => !model.id.endsWith(":free"))
      .map((model) => {
        const copy = { ...model };
        delete copy.capabilities;
        return copy;
      }),
  }));
  const researchModels = research.reduce((n, provider) => n + provider.models.length, 0);
  await openRealistic(page, {
    fixture: { profiles: priced.profiles, slots: priced.slots,
      providers: { default: priced.providers.default, research, moa: priced.providers.moa } },
  });

  await page.getByTestId("catalog-fast-only").click();
  await page.getByTestId("catalog-free-ids").click();
  await expect(page.locator("[data-model-row]:visible")).toHaveCount(0);

  await page.getByTestId("catalog-profile-select").selectOption("research");
  await expect(page.getByTestId("catalog-fast-only")).toHaveCount(0);
  await expect(page.getByTestId("catalog-free-ids")).toHaveCount(0);
  await expect(page.getByTestId("catalog-match-count"))
    .toHaveText(`Showing ${researchModels} of ${researchModels} models.`);

  // Back on the priced profile both toggles exist again, unpressed.
  await page.getByTestId("catalog-profile-select").selectOption("default");
  await expect(page.getByTestId("catalog-fast-only")).toHaveAttribute("aria-pressed", "false");
  await expect(page.getByTestId("catalog-free-ids")).toHaveAttribute("aria-pressed", "false");
});

// --- m3: one announcement per settled query --------------------------------

test("the match count is announced once it settles, not on every keystroke", async ({ page }) => {
  await openRealistic(page);
  const count = page.getByTestId("catalog-match-count");
  // The visible count is not itself the live region…
  await expect(count).not.toHaveAttribute("aria-live", /.+/);
  const announce = page.getByTestId("catalog-match-announce");
  await expect(announce).toHaveAttribute("aria-live", "polite");

  await page.getByTestId("catalog-search").fill("primalabs");
  await expect(count).toHaveText(/^Showing \d+ of 258 models \(filtered\)\.$/);
  const visible = (await count.innerText()).replace("Showing ", "").split(" of ")[0];
  // …the hidden region catches up with the settled text.
  await expect.poll(async () => (await announce.innerText()).trim()).toBe(`Showing ${visible} of 258 models (filtered).`);
  await page.getByTestId("catalog-search").fill("");
  await expect.poll(async () => (await announce.innerText()).trim())
    .toBe(`Showing ${REALISTIC.modelCount} of ${REALISTIC.modelCount} models.`);
});

// --- m5: the catalog profile vs the write targets ---------------------------

test("the editor names a catalog profile that differs from the write targets", async ({ page }) => {
  await openRealistic(page);
  await page.getByLabel("Select default main", { exact: true }).check();
  await expect(page.getByTestId("picker-scope-note")).toHaveCount(0);

  await page.getByTestId("catalog-profile-select").selectOption("moa");
  await page.getByTestId("catalog-use-moa-gateway-moa/ensemble-deep").click();
  const note = page.getByTestId("picker-scope-note");
  await expect(note).toBeVisible();
  await expect(note).toContainText("moa");
  await expect(note).toContainText("default");

  await page.getByTestId("catalog-profile-select").selectOption("default");
  await expect(page.getByTestId("picker-scope-note")).toHaveCount(0);
});
