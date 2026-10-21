import { test, expect } from "@playwright/test";
import {
  OTHER, providerSelect, modelSelect, providerExact, modelExact, setProvider, setModel, ensureCatalog,
} from "./route-helpers.mjs";

// Dropdown contract for the Model Switcher change editor: provider and model
// are native <select>s fed by the aggregate /catalog DTO (tests/mock-api.js),
// with an explicit "Other" entry for exact hand-typed ids.
const filterProvider = (page) => page.getByLabel("Filter by provider", { exact: true });
const filterModel = (page) => page.getByLabel("Filter by model", { exact: true });
const catalogRequests = (page) => page.evaluate(() =>
  window.__mockApi.requests.filter((r) => r.path.endsWith("/catalog")));
const modelRequests = (page) => page.evaluate(() =>
  window.__mockApi.requests.filter((r) => r.path.endsWith("/models")));
const optionTexts = (select) => select.locator("option").allTextContents();

async function open(page) {
  await page.goto("/fixture.html");
  await expect(providerSelect(page)).toBeVisible();
  await ensureCatalog(page);
  await expect(providerSelect(page).locator('option[value="nous"]')).toHaveCount(1);
}

test("provider and model are native selects scoped to the plugin root", async ({ page }) => {
  await open(page);
  for (const select of [providerSelect(page), modelSelect(page)]) {
    expect(await select.evaluate((el) => el.tagName)).toBe("SELECT");
    expect(await select.evaluate((el) => Boolean(el.closest(".hms-root")))).toBe(true);
  }
  // No custom popups or ARIA comboboxes remain anywhere in the editor.
  await expect(page.locator('[role="combobox"]:not(select)')).toHaveCount(0);
  await expect(page.locator('[role="listbox"]')).toHaveCount(0);

  // Model waits for a provider; both offer the explicit exact-id escape hatch.
  await expect(modelSelect(page)).toBeDisabled();
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a provider first");
  await expect(providerSelect(page).locator(`option[value="${OTHER}"]`)).toHaveText(/Other — type an exact provider slug/);
  await expect(modelSelect(page).locator(`option[value="${OTHER}"]`)).toHaveText(/Other — type an exact model id/);
});

test("catalog loads lazily on first dropdown interaction, once, and lists truthful availability", async ({ page }) => {
  await page.goto("/fixture.html");
  await expect(providerSelect(page)).toBeVisible();
  expect((await catalogRequests(page)).length).toBe(0); // nothing at mount
  await providerSelect(page).hover();
  await expect(providerSelect(page).locator('option[value="nous"]')).toHaveCount(1);
  const requests = await catalogRequests(page);
  expect(requests.length).toBe(1);
  expect(requests[0].method).toBe("POST");
  expect(requests[0].body.profile).toBe("default");
  expect(requests[0].body.refresh).toBeUndefined();
  await expect(page.getByTestId("live-status")).toContainText("15 models available, 12 priced");

  // Label (slug); authenticated providers first, alphabetical; sign-in last + disabled.
  const texts = await optionTexts(providerSelect(page));
  expect(texts).toEqual([
    "Choose a provider…",
    "Broken Provider (broken-provider)",
    "Nous (nous)",
    "Ollama (local) (ollama-local)",
    "OpenCode Go (opencode-go)",
    "Nous Portal (portal) — sign-in needed",
    "Other — type an exact provider slug…",
  ]);
  const portal = providerSelect(page).locator('option[value="portal"]');
  await expect(portal).toHaveAttribute("disabled", "");
  await expect(portal).toHaveAttribute("title", /Sign-in required/);

  // Focusing the dropdown again uses the cache: no refetch.
  await providerSelect(page).focus();
  await providerSelect(page).selectOption("nous");
  expect((await catalogRequests(page)).length).toBe(1);
});

test("catalog line is one compact summary; freshness detail stays collapsed; Refresh is the only refresh=1", async ({ page }) => {
  await open(page);
  const summary = page.getByTestId("catalog-summary");
  await expect(summary).toContainText("5 providers · 15 models");
  await expect(summary).toContainText("3 stale");
  await expect(summary).toContainText("scope default");

  const details = page.getByTestId("catalog-freshness");
  expect(await details.evaluate((el) => el.tagName)).toBe("DETAILS");
  expect(await details.evaluate((el) => el.open)).toBe(false);
  await expect(page.locator('[data-provider-freshness="nous"]')).toBeHidden();
  await details.locator("summary").click();
  await expect(page.locator('[data-provider-freshness="nous"]')).toContainText("2026-10-18T12:00:00Z");
  await expect(page.locator('[data-provider-freshness="opencode-go"]')).toContainText("stale");

  await page.getByTestId("refresh-catalog").click();
  await expect.poll(async () => (await catalogRequests(page)).length).toBe(2);
  const refreshed = await catalogRequests(page);
  expect(refreshed[1].body.refresh).toBe(true);
  await expect(page.locator('[data-provider-freshness="nous"]')).toContainText("2026-10-18T18:00:00Z");
});

test("choosing a provider fills the model dropdown and clears a model absent from it", async ({ page }) => {
  await open(page);
  await providerSelect(page).selectOption("nous");
  await expect(page.getByTestId("live-status")).toContainText("Provider set to nous");
  await expect(modelSelect(page)).toBeEnabled();
  await modelSelect(page).selectOption("hermes-4-405b");
  await expect(modelSelect(page)).toHaveValue("hermes-4-405b");

  await providerSelect(page).selectOption("opencode-go");
  await expect(modelSelect(page)).toHaveValue("");

  await modelSelect(page).selectOption("deepseek-v4.1-flash");
  await providerSelect(page).selectOption("nous");
  await expect(modelSelect(page)).toHaveValue("");
});

test("model options state the catalog price and never render unknown as free", async ({ page }) => {
  await open(page);
  await providerSelect(page).selectOption("nous");
  const option = (id) => modelSelect(page).locator(`option[value="${id}"]`);
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (12)…");
  await expect(option("hermes-4-405b")).toHaveText("hermes-4-405b (Hermes 4 405B) — $50.00/1M out");
  await expect(option("hermes-4-70b")).toHaveText(/— free$/);
  await expect(option("hermes-4-70b")).not.toContainText("$0.00");
  await expect(option("hermes-4-mini")).toHaveText(/— included in subscription$/);
  await expect(option("hermes-unknown")).toHaveText(/— price not published$/);
  await expect(option("hermes-unknown")).not.toContainText("free");
  await expect(option("hermes-unknown")).not.toContainText("$0.00");

  // A catalogued provider needs no extra /models request.
  expect((await modelRequests(page)).length).toBe(0);
});

test("selecting a model announces and previews the exact route", async ({ page }) => {
  await open(page);
  await page.getByLabel("Select default main", { exact: true }).check();
  await providerSelect(page).selectOption("nous");
  await modelSelect(page).selectOption("hermes-filler-11");
  await expect(page.getByTestId("live-status")).toContainText("Route set to nous/hermes-filler-11");
  await expect(page.getByTestId("route-summary")).toHaveText("Route: nous/hermes-filler-11");

  await modelSelect(page).selectOption("hermes-4-405b");
  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  const previewBody = await page.evaluate(() =>
    window.__mockApi.requests.filter((r) => r.path.endsWith("/preview")).at(-1).body);
  expect(previewBody.changes[0].to).toEqual({ provider: "nous", model: "hermes-4-405b" });
  await expect(page.getByTestId("preview-panel")).toBeVisible();
});

test("Other keeps exact hand-typed provider slugs and model ids", async ({ page }) => {
  await open(page);
  await page.getByLabel("Select default main", { exact: true }).check();
  await expect(providerExact(page)).toHaveCount(0);
  await providerSelect(page).selectOption(OTHER);
  await expect(providerExact(page)).toBeVisible();
  await providerExact(page).fill("custom:my-endpoint");
  await expect(providerSelect(page)).toHaveValue(OTHER);

  await setModel(page, "provider-native/custom:id");
  await expect(modelExact(page)).toHaveValue("provider-native/custom:id");
  await expect(page.getByTestId("route-summary")).toHaveText("Route: custom:my-endpoint/provider-native/custom:id");

  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  const previewBody = await page.evaluate(() =>
    window.__mockApi.requests.filter((r) => r.path.endsWith("/preview")).at(-1).body);
  expect(previewBody.changes[0].to).toEqual({ provider: "custom:my-endpoint", model: "provider-native/custom:id" });

  // Going back to a listed provider hides the exact fields again.
  await providerSelect(page).selectOption("nous");
  await expect(providerExact(page)).toHaveCount(0);
  await expect(modelExact(page)).toHaveCount(0);
});

test("typing an exact provider slug fetches /models once, on leaving the field", async ({ page }) => {
  await open(page);
  await providerSelect(page).selectOption(OTHER);
  const exact = providerExact(page);
  await exact.focus();
  for (const ch of "zhipu") {
    await exact.press(ch);
    await page.waitForTimeout(300); // longer than the 250 ms debounce
  }
  expect((await modelRequests(page)).length).toBe(0); // no fetch per partial slug
  await expect(modelSelect(page).locator("option").first()).toHaveText("Finish the provider slug to list its models");
  await exact.blur();
  await expect.poll(async () => (await modelRequests(page)).length).toBe(1);
  expect((await modelRequests(page))[0].body.provider).toBe("zhipu");
});

test("uncatalogued model lists fall back to /models with inline error and Retry", async ({ page }) => {
  await open(page);
  // broken-provider has no catalog models, so /models is the source.
  await page.evaluate(() => { window.__mockApi.modelsFail = true; });
  await providerSelect(page).selectOption("broken-provider");
  const error = page.getByTestId("model-error");
  await expect(error).toContainText("model catalog unavailable: upstream timeout");
  const req = (await modelRequests(page)).at(-1);
  expect(req.method).toBe("POST");
  expect(req.body.provider).toBe("broken-provider");
  expect(req.body.profile).toBe("default");
  expect(req.body.refresh).toBeUndefined();

  await page.evaluate(() => { window.__mockApi.modelsFail = false; });
  await page.getByTestId("model-retry").click();
  await expect(error).toHaveCount(0);
  await expect(page.getByTestId("model-note")).toContainText("catalog fetch failed: upstream timeout");
  await expect(modelSelect(page).locator("option").first()).toHaveText("No listed models — choose Other");
});

test("catalog failure is visible with a Retry that recovers", async ({ page }) => {
  await page.goto("/fixture.html");
  await page.evaluate(() => { window.__mockApi.catalogFail = true; });
  await providerSelect(page).focus();
  await expect(page.getByTestId("catalog-error")).toContainText("catalog unavailable: upstream timeout");
  await expect(providerSelect(page).locator('option[value="nous"]')).toHaveCount(0);
  // Exact entry still works while the list is unavailable.
  await setProvider(page, "nous", { exact: true });
  await expect(providerExact(page)).toHaveValue("nous");

  await page.evaluate(() => { window.__mockApi.catalogFail = false; });
  await page.getByTestId("catalog-retry").click();
  await expect(providerSelect(page).locator('option[value="nous"]')).toHaveCount(1);
  await expect(page.getByTestId("catalog-error")).toHaveCount(0);
});

test("matrix filter dropdowns are local, exact and invalidate reviews", async ({ page }) => {
  await open(page);
  await expect(page.locator("tr[data-row]")).toHaveCount(6);
  for (const select of [filterProvider(page), filterModel(page)]) {
    expect(await select.evaluate((el) => el.tagName)).toBe("SELECT");
  }

  const before = await page.evaluate(() =>
    window.__mockApi.requests.filter((r) => /\/catalog|\/models/.test(r.path)).length);
  expect(await optionTexts(filterProvider(page))).toEqual(["All providers", "anthropic", "openai"]);
  await filterProvider(page).selectOption("openai");
  await expect(page.locator("tr[data-row]")).toHaveCount(3);
  // The model filter narrows to models seen under the chosen provider.
  const models = await optionTexts(filterModel(page));
  expect(models[0]).toBe("All models");
  expect(models).toContain("gpt-5.1");
  const after = await page.evaluate(() =>
    window.__mockApi.requests.filter((r) => /\/catalog|\/models/.test(r.path)).length);
  expect(after).toBe(before); // filters never fetch

  await filterModel(page).selectOption("gpt-5.1");
  await expect(page.locator("tr[data-row]")).toHaveCount(2);
  // Changing provider resets the dependent model filter.
  await filterProvider(page).selectOption("");
  await expect(filterModel(page)).toHaveValue("");
  await expect(page.locator("tr[data-row]")).toHaveCount(6);

  // Selection survives filter changes.
  await page.getByLabel("Select default main", { exact: true }).check();
  await expect(page.getByTestId("selection-summary")).toContainText("1");
  await filterModel(page).selectOption("gpt-5.1");
  await expect(page.getByLabel("Select default main", { exact: true })).toHaveCount(0);
  await filterModel(page).selectOption("");
  await expect(page.getByLabel("Select default main", { exact: true })).toBeChecked();

  // A filter change invalidates the reviewed custom route.
  await setProvider(page, "zhipu");
  await setModel(page, "glm-5.3-flash");
  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await filterProvider(page).selectOption("openai");
  await expect(page.getByTestId("preview-panel")).toHaveCount(0);
  await expect(page.getByTestId("apply-custom")).toBeDisabled();
});

for (const viewport of [{ width: 375, height: 812 }, { width: 768, height: 900 }]) {
  test(`route dropdowns fit and stay focusable at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await open(page);
    await providerSelect(page).selectOption("nous");
    await modelSelect(page).selectOption("hermes-4-405b");

    for (const select of [providerSelect(page), modelSelect(page)]) {
      const box = await select.boundingBox();
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(viewport.width + 0.5);
      expect(box.height).toBeGreaterThanOrEqual(36);
      await select.focus();
      const ring = await select.evaluate((el) => {
        const css = getComputedStyle(el);
        return { style: css.outlineStyle, width: parseFloat(css.outlineWidth) };
      });
      expect(ring.style).not.toBe("none");
      expect(ring.width).toBeGreaterThanOrEqual(2);
    }
    // Long model labels are cut by the select, never by page overflow; the
    // full route stays readable in the summary line.
    await expect(page.getByTestId("route-summary")).toHaveText("Route: nous/hermes-4-405b");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}
