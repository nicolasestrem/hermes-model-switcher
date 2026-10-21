import { test, expect } from "@playwright/test";
import { providerSelect, modelSelect, setProvider, setModel, ensureCatalog } from "./route-helpers.mjs";

// Displayed routes are `provider · model`, matching Hermes' own Models page
// (`${eff.provider} · ${eff.model}`). Hermes model ids usually carry their own
// vendor namespace (`primalabs-ai/DeepSeek-V4.1-Flash-1017`,
// `openrouter/pareto-code`), so composing `provider/model` for display doubled
// the prefix. These fixtures pin BOTH halves of the contract: what is rendered
// and announced, and that the request body still sends provider and model as
// separate, unprefixed fields.
const NAMESPACED = {
  primalabs: "primalabs-ai/DeepSeek-V4.1-Flash-1017",
  openrouter: "openrouter/pareto-code",
};
const PRIMALABS_ROUTE = `primalabs · ${NAMESPACED.primalabs}`;
const OPENROUTER_ROUTE = `openrouter · ${NAMESPACED.openrouter}`;

// Flags are read by the in-page mock on every request; /state is served once at
// mount, so a flag that adds slots needs a reload.
async function openWithFlags(page, flags) {
  await page.goto("/fixture.html");
  await page.evaluate((value) => sessionStorage.setItem("hms-mock-flags", JSON.stringify(value)), flags);
  await page.reload();
  await expect(providerSelect(page)).toBeVisible();
}

const previewBodies = (page) => page.evaluate(() =>
  window.__mockApi.requests.filter((r) => r.path.endsWith("/preview")).map((r) => r.body));

async function expectNoDoubledPrefix(page) {
  const text = await page.evaluate(() => document.body.innerText);
  expect(text).not.toContain("primalabs/primalabs-ai");
  expect(text).not.toContain("openrouter/openrouter/");
}

test("matrix renders configured and observed namespaced routes as provider · model", async ({ page }) => {
  await openWithFlags(page, { namespacedRoutes: true });

  const primalabsRow = page.locator('tr[data-row="default/auxiliary.compiler"]');
  await expect(primalabsRow.locator('[data-column="configured"]')).toHaveText(PRIMALABS_ROUTE);
  await expect(primalabsRow.locator('[data-column="observed"]')).toHaveText(PRIMALABS_ROUTE);

  const openrouterRow = page.locator('tr[data-row="research/auxiliary.scout"]');
  await expect(openrouterRow.locator('[data-column="configured"]')).toHaveText(OPENROUTER_ROUTE);
  await expect(openrouterRow.locator('[data-column="observed"]')).toHaveText(OPENROUTER_ROUTE);

  await expectNoDoubledPrefix(page);
});

test("editor summary and announcement use the model id verbatim and keep the write payload unprefixed", async ({ page }) => {
  await openWithFlags(page, { namespacedRoutes: true });
  await page.getByLabel("Select default main", { exact: true }).check();
  await ensureCatalog(page);

  const provider = providerSelect(page);
  await setProvider(page, "primalabs");
  await expect(provider).toHaveValue("primalabs");
  await modelSelect(page).selectOption(NAMESPACED.primalabs);

  await expect(page.getByTestId("live-status")).toContainText(`Route set to ${PRIMALABS_ROUTE}`);
  await expect(page.getByTestId("route-summary")).toHaveText(`Route: ${PRIMALABS_ROUTE}`);
  await expectNoDoubledPrefix(page);

  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  const panel = page.getByTestId("preview-panel");
  await expect(panel).toContainText(`→ ${PRIMALABS_ROUTE}`);

  // The write path is untouched: separate provider/model fields, no prefix.
  const bodies = await previewBodies(page);
  expect(bodies.at(-1).changes[0].to).toEqual({
    provider: "primalabs", model: NAMESPACED.primalabs,
  });
});

test("catalog Use announces the namespaced route and previews it unprefixed", async ({ page }) => {
  await openWithFlags(page, { namespacedRoutes: true });
  await page.getByLabel("Select default main", { exact: true }).check();
  await page.getByTestId("catalog-toggle").click();
  await expect(page.getByTestId("catalog-view")).toBeVisible();

  const use = page.getByTestId(`catalog-use-openrouter-${NAMESPACED.openrouter}`);
  await expect(use).toHaveAttribute("title", `Use ${OPENROUTER_ROUTE}`);
  await use.click();

  await expect(page.getByTestId("live-status")).toContainText(`Route set to ${OPENROUTER_ROUTE}`);
  await expect(page.getByTestId("route-summary")).toHaveText(`Route: ${OPENROUTER_ROUTE}`);
  await expect(providerSelect(page)).toHaveValue("openrouter");
  await expect(modelSelect(page)).toHaveValue(NAMESPACED.openrouter);
  await expectNoDoubledPrefix(page);

  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toContainText(`→ ${OPENROUTER_ROUTE}`);
  const bodies = await previewBodies(page);
  expect(bodies.at(-1).changes[0].to).toEqual({
    provider: "openrouter", model: NAMESPACED.openrouter,
  });
});
