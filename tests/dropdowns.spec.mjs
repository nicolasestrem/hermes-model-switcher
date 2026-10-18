import { test, expect } from "@playwright/test";

// Dropdown contract for the Model Switcher catalog comboboxes.
// Everything runs against tests/mock-api.js, whose /catalog fixture mirrors the
// frozen aggregate DTO and whose /models rows join back to those exact ids.
const providerInput = (page) => page.getByLabel("New provider", { exact: true });
const modelInput = (page) => page.getByLabel("New model", { exact: true });
const filterProvider = (page) => page.getByLabel("Filter by provider", { exact: true });
const filterModel = (page) => page.getByLabel("Filter by model", { exact: true });
const activeOption = (page) => page.locator('[role="option"][aria-selected="true"]');
const catalogRequests = (page) => page.evaluate(() =>
  window.__mockApi.requests.filter((r) => r.path.endsWith("/catalog")));

async function open(page) {
  await page.goto("/fixture.html");
  await expect(providerInput(page)).toBeVisible();
}

async function chooseProvider(page, id) {
  await providerInput(page).click();
  await page.getByTestId(`provider-option-${id}`).click();
  await expect(providerInput(page)).toHaveValue(id);
}

async function chooseModel(page, id) {
  await modelInput(page).click();
  await page.getByTestId(`model-option-${id}`).click();
  await expect(modelInput(page)).toHaveValue(id);
}

test("provider combobox ships the ARIA 1.2 contract and a non-modal listbox", async ({ page }) => {
  await open(page);
  const input = providerInput(page);
  await expect(page.getByRole("combobox", { name: "New provider", exact: true })).toBeVisible();
  await expect(input).toHaveAttribute("role", "combobox");
  await expect(input).toHaveAttribute("aria-haspopup", "listbox");
  await expect(input).toHaveAttribute("aria-autocomplete", "list");
  await expect(input).toHaveAttribute("aria-expanded", "false");
  await expect(input).toHaveAttribute("aria-controls", /.+/);
  await expect(page.getByTestId("provider-listbox")).toHaveCount(0);

  await input.click();
  const list = page.getByTestId("provider-listbox");
  await expect(list).toBeVisible();
  await expect(input).toHaveAttribute("aria-expanded", "true");
  expect(await input.getAttribute("aria-controls")).toBe(await list.getAttribute("id"));
  // Exactly one option is described as active/selected at any time.
  await expect(page.locator('[role="option"][aria-selected="true"]')).toHaveCount(1);
  const activeId = await input.getAttribute("aria-activedescendant");
  expect(activeId).toBeTruthy();
  await expect(page.locator(`#${activeId}`)).toHaveAttribute("role", "option");

  // The popup lives under the plugin root (never document.body) so token/scope
  // CSS applies, and options contain no nested interactive controls.
  expect(await list.evaluate((el) => Boolean(el.closest(".hms-root")))).toBe(true);
  expect(await list.locator("button, input, a, select, textarea").count()).toBe(0);
  await expect(list.locator('[role="option"]')).toHaveCount(5);
});

test("provider keyboard: Enter/ArrowDown/ArrowUp/Home/End, Escape restores, Tab leaves", async ({ page }) => {
  await open(page);
  const input = providerInput(page);
  const list = page.getByTestId("provider-listbox");

  await input.focus();
  await page.keyboard.press("Enter");
  await expect(list).toBeVisible();
  await expect(activeOption(page)).toContainText("Nous");

  // ArrowDown skips the aria-disabled Portal row.
  await page.keyboard.press("ArrowDown");
  await expect(activeOption(page)).toContainText("OpenCode Go");
  await page.keyboard.press("ArrowUp");
  await expect(activeOption(page)).toContainText("Nous");
  // ArrowUp from the first row wraps to the last ENABLED row.
  await page.keyboard.press("ArrowUp");
  await expect(activeOption(page)).toContainText("Broken Provider");
  await page.keyboard.press("Home");
  await expect(activeOption(page)).toContainText("Nous");
  await page.keyboard.press("End");
  await expect(activeOption(page)).toContainText("Broken Provider");

  await page.keyboard.press("Escape");
  await expect(list).toHaveCount(0);
  await expect(input).toBeFocused();
  await expect(input).toHaveValue("");

  // Escape restores the value captured when the popup opened.
  await input.fill("opencode");
  await expect(list).toBeVisible();
  await expect(input).toHaveValue("opencode");
  await page.keyboard.press("Escape");
  await expect(list).toHaveCount(0);
  await expect(input).toHaveValue("");

  // Space opens an empty trigger, but never eats a space in a typed value.
  await input.focus();
  await page.keyboard.press(" ");
  await expect(list).toBeVisible();
  await page.keyboard.press("Escape");

  // ArrowUp on a closed combobox opens at the last enabled row; Alt+ArrowDown
  // opens at the first enabled row (ARIA APG optional keys).
  await input.focus();
  await page.keyboard.press("ArrowUp");
  await expect(list).toBeVisible();
  await expect(activeOption(page)).toContainText("Broken Provider");
  await page.keyboard.press("Escape");
  await input.focus();
  await page.keyboard.press("Alt+ArrowDown");
  await expect(list).toBeVisible();
  await expect(activeOption(page)).toContainText("Nous");
  await page.keyboard.press("Escape");

  // Tab commits + closes and moves on: no focus trap.
  await input.click();
  await expect(list).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(list).toHaveCount(0);
  await expect(modelInput(page)).toBeFocused();

  // Outside click closes without rewriting the route.
  await input.click();
  await expect(list).toBeVisible();
  await page.locator("h2").first().click();
  await expect(list).toHaveCount(0);
  await expect(input).toHaveValue("");

  // Disabled rows cannot be selected (dispatch bypasses Playwright's own
  // aria-disabled actionability guard; the component must ignore the click).
  await input.click();
  await page.getByTestId("provider-option-portal").dispatchEvent("click");
  await expect(list).toBeVisible();
  await expect(input).toHaveValue("");
});

test("provider rows state label, slug and truthful availability", async ({ page }) => {
  await open(page);
  expect((await catalogRequests(page)).length).toBe(0);

  const input = providerInput(page);
  await input.click();
  await expect(page.getByTestId("provider-listbox")).toBeVisible();

  const requests = await catalogRequests(page);
  expect(requests.length).toBe(1);
  expect(requests[0].search).toContain("profileId=default");
  expect(requests[0].search).not.toContain("refresh=");
  await expect(page.getByTestId("live-status")).toContainText("15 models available, 12 priced");

  const nous = page.getByTestId("provider-option-nous");
  await expect(nous).toContainText("Nous");
  await expect(nous).toContainText("nous");
  await expect(nous).toContainText("priced");
  const name = nous.locator(".hms-opt-name");
  const slug = nous.locator(".hms-opt-slug");
  const status = nous.locator(".hms-opt-status");
  expect(await name.evaluate((el) => Number(getComputedStyle(el).fontWeight))).toBeGreaterThanOrEqual(600);
  expect(await slug.evaluate((el) => getComputedStyle(el).fontFamily)).toMatch(/mono/i);
  const nb = await name.boundingBox();
  const sb = await status.boundingBox();
  expect(sb.x).toBeGreaterThan(nb.x + nb.width);

  const portal = page.getByTestId("provider-option-portal");
  await expect(portal).toHaveAttribute("aria-disabled", "true");
  await expect(portal).toContainText("sign-in needed");
  await expect(portal).toContainText("Sign-in required");

  await expect(page.getByTestId("provider-option-ollama-local")).toContainText("no list price");
  await expect(page.getByTestId("provider-option-broken-provider"))
    .toContainText("catalog fetch failed: upstream timeout");

  // Freshness is per provider: timestamps + stale flags from the catalog DTO.
  const freshness = page.getByTestId("catalog-freshness");
  await expect(freshness).toContainText("5 providers · 15 models");
  await expect(freshness).toContainText("3 stale");
  await expect(page.locator('[data-provider-freshness="nous"]')).toContainText("2026-10-18T12:00:00Z");
  await expect(page.locator('[data-provider-freshness="opencode-go"]')).toContainText("stale");

  // Cached: closing and reopening the same profile does not refetch.
  await page.keyboard.press("Escape");
  await input.click();
  await expect(page.getByTestId("provider-listbox")).toBeVisible();
  expect((await catalogRequests(page)).length).toBe(1);

  // Empty filter copy keeps exact-slug entry available.
  await input.fill("zzz");
  await expect(page.getByTestId("provider-listbox"))
    .toContainText("No provider matches “zzz” — you can still type an exact provider slug.");
  await input.press("Enter");
  await expect(page.getByTestId("provider-listbox")).toHaveCount(0);
  await expect(input).toHaveValue("zzz");

  // Refresh is the ONLY action that asks for refresh=1.
  await page.getByTestId("refresh-catalog").click();
  const refreshed = await catalogRequests(page);
  expect(refreshed.length).toBe(2);
  expect(refreshed[1].search).toContain("refresh=1");
  await expect(page.locator('[data-provider-freshness="nous"]')).toContainText("2026-10-18T18:00:00Z");
});

test("choosing a provider clears a model absent from the new provider", async ({ page }) => {
  await open(page);
  await chooseProvider(page, "nous");
  await modelInput(page).fill("hermes-4-405b");

  await chooseProvider(page, "opencode-go");
  await expect(modelInput(page)).toHaveValue("");

  await modelInput(page).fill("deepseek-v4.1-flash");
  await chooseProvider(page, "nous");
  await expect(modelInput(page)).toHaveValue("");

  await chooseProvider(page, "opencode-go");
  await modelInput(page).fill("deepseek-v4.1-flash");
  await chooseProvider(page, "opencode-go");
  await expect(modelInput(page)).toHaveValue("deepseek-v4.1-flash");
});

test("model rows join prices from /catalog and never render unknown as free", async ({ page }) => {
  await open(page);
  await chooseProvider(page, "nous");
  await page.evaluate(() => { window.__mockApi.modelsDelayMs = 300; });
  await modelInput(page).click();

  // Skeleton rows while the debounced request is in flight.
  await expect(page.getByTestId("model-skeleton")).toBeVisible();
  await expect(page.getByTestId("model-option-hermes-4-405b")).toBeVisible();
  await expect(page.getByTestId("model-listbox")).not.toHaveAttribute("aria-busy", "true");

  const priced = page.getByTestId("model-option-hermes-4-405b");
  await expect(priced).toContainText("→ $50.00 out");
  const free = page.getByTestId("model-option-hermes-4-70b");
  await expect(free.locator(".hms-badge-free")).toHaveText("free");
  await expect(free).not.toContainText("$0.00");
  await expect(page.getByTestId("model-option-hermes-4-mini"))
    .toContainText("included in subscription");
  const unknown = page.getByTestId("model-option-hermes-unknown");
  await expect(unknown.locator(".hms-price")).toHaveAttribute("data-price-kind", "unknown");
  await expect(unknown.locator(".hms-price")).toHaveText("—");
  await expect(unknown.locator(".hms-price")).toHaveAttribute(
    "title", "no list price in the installed catalog; never treated as free");
  await expect(unknown).not.toContainText("$0.00");
  await expect(unknown).not.toContainText("free");

  const modelReqs = await page.evaluate(() =>
    window.__mockApi.requests.filter((r) => r.path.endsWith("/models")));
  expect(modelReqs.at(-1).search).toContain("provider=nous");
  expect(modelReqs.at(-1).search).toContain("profileId=default");
  expect(modelReqs.at(-1).search).not.toContain("refresh=");

  // Client-side filter matches id AND human name.
  await modelInput(page).fill("Filler 9");
  await expect(page.getByTestId("model-option-hermes-filler-9")).toBeVisible();
  await expect(page.getByTestId("model-option-hermes-4-405b")).toHaveCount(0);

  // No match: Enter commits the exact typed id.
  await modelInput(page).fill("nope-model");
  await expect(page.getByTestId("model-listbox"))
    .toContainText("No catalog match — press Enter to use “nope-model” as an exact model id.");
  await modelInput(page).press("Enter");
  await expect(page.getByTestId("model-listbox")).toHaveCount(0);
  await expect(modelInput(page)).toHaveValue("nope-model");
});

test("model keyboard navigation pages, selects, announces and previews the exact route", async ({ page }) => {
  await open(page);
  await chooseProvider(page, "nous");
  await page.getByLabel("Select default main", { exact: true }).check();
  const input = modelInput(page);
  await input.click();
  await expect(page.getByTestId("model-option-hermes-4-405b")).toBeVisible();
  await expect(activeOption(page)).toContainText("hermes-4-405b");

  await page.keyboard.press("End");
  await expect(activeOption(page)).toContainText("hermes-filler-12");
  // The active option is scrolled into view inside the popup, not the page.
  expect(await page.getByTestId("model-listbox").evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
  await page.keyboard.press("Home");
  await expect(activeOption(page)).toContainText("hermes-4-405b");
  await page.keyboard.press("PageDown");
  await expect(activeOption(page)).toContainText("hermes-filler-11");
  await page.keyboard.press("ArrowDown");
  await expect(activeOption(page)).toContainText("hermes-filler-12");
  await page.keyboard.press("ArrowUp");
  await expect(activeOption(page)).toContainText("hermes-filler-11");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("model-listbox")).toHaveCount(0);
  await expect(input).toHaveValue("hermes-filler-11");
  await expect(page.getByTestId("live-status")).toContainText("Route set to nous/hermes-filler-11");

  // Selecting the reviewed row drives the same preview payload as the old
  // option buttons: provider + exact native id, changeType "set".
  await chooseModel(page, "hermes-4-405b");
  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  const previewBody = await page.evaluate(() =>
    window.__mockApi.requests.filter((r) => r.path.endsWith("/preview")).at(-1).body);
  expect(previewBody.changes[0].to).toEqual({ provider: "nous", model: "hermes-4-405b" });
  await expect(page.getByTestId("preview-panel")).toBeVisible();

  // Free-text exact-id entry survives alongside the listbox.
  await input.fill("provider-native/custom:id");
  await expect(input).toHaveValue("provider-native/custom:id");
});

test("model fetch surfaces inline errors and Retry, with a 1.2s hard timeout", async ({ page }) => {
  await open(page);
  await chooseProvider(page, "nous");
  await page.evaluate(() => { window.__mockApi.modelsFail = true; });
  await modelInput(page).click();
  const error = page.getByTestId("model-combo-error");
  await expect(error).toContainText("model catalog unavailable: upstream timeout");
  const retry = page.getByTestId("model-combo-retry");
  await expect(retry).toBeVisible();

  await page.evaluate(() => { window.__mockApi.modelsFail = false; });
  await retry.click();
  await expect(page.getByTestId("model-option-hermes-4-405b")).toBeVisible();
  await expect(error).toHaveCount(0);

  // A provider slower than the hard timeout fails visibly instead of hanging.
  await page.keyboard.press("Escape");
  await page.evaluate(() => { window.__mockApi.modelsDelayMs = 1500; });
  await chooseProvider(page, "opencode-go");
  await modelInput(page).click();
  await expect(error).toContainText("timed out after 1.2s", { timeout: 4000 });

  await page.evaluate(() => { window.__mockApi.modelsDelayMs = 0; });
  await retry.click();
  await expect(page.getByTestId("model-option-another-model")).toBeVisible();
});

test("catalog failure is visible with a Retry that recovers", async ({ page }) => {
  await open(page);
  await page.evaluate(() => { window.__mockApi.catalogFail = true; });
  await providerInput(page).click();
  await expect(page.getByTestId("catalog-error")).toContainText("catalog unavailable: upstream timeout");
  await expect(page.getByTestId("catalog-inline-error")).toContainText("catalog unavailable: upstream timeout");

  await page.evaluate(() => { window.__mockApi.catalogFail = false; });
  await page.getByTestId("catalog-retry").click();
  await expect(page.getByTestId("provider-option-nous")).toBeVisible();
  await expect(page.getByTestId("catalog-inline-error")).toHaveCount(0);
});

test("matrix filter comboboxes are local, substring-preserving and invalidate reviews", async ({ page }) => {
  await open(page);
  await expect(page.locator("tr[data-row]")).toHaveCount(6);

  const before = await page.evaluate(() =>
    window.__mockApi.requests.filter((r) => /\/catalog|\/models/.test(r.path)).length);
  await filterProvider(page).click();
  await expect(page.getByTestId("filter-provider-listbox")).toBeVisible();
  await expect(page.getByTestId("filter-provider-listbox").locator('[role="option"]')).toHaveCount(2);
  const after = await page.evaluate(() =>
    window.__mockApi.requests.filter((r) => /\/catalog|\/models/.test(r.path)).length);
  expect(after).toBe(before); // filters never fetch

  await page.getByTestId("filter-provider-option-openai").click();
  await expect(filterProvider(page)).toHaveValue("openai");
  await expect(page.locator("tr[data-row]")).toHaveCount(3);

  // Typed text still filters as a substring, not just as an exact option.
  await filterProvider(page).fill("anth");
  await expect(page.locator("tr[data-row]")).toHaveCount(3);
  await filterProvider(page).fill("");
  await expect(page.locator("tr[data-row]")).toHaveCount(6);

  await filterModel(page).click();
  await page.getByTestId("filter-model-option-gpt-5.1").click();
  await expect(page.locator("tr[data-row]")).toHaveCount(2);

  // Escape restores the value that was present when the popup opened.
  await filterModel(page).fill("gpt-5.1");
  await filterModel(page).focus();
  await filterModel(page).fill("zzz");
  await expect(page.locator("tr[data-row]")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(filterModel(page)).toHaveValue("gpt-5.1");
  await expect(page.locator("tr[data-row]")).toHaveCount(2);

  // Profile checkbox cloud is unchanged; selection survives filter changes.
  await page.getByLabel("Profile Research", { exact: true }).check();
  await expect(page.locator('[data-testid="profile-options"] input[type="checkbox"]')).toHaveCount(3);
  await page.getByLabel("Profile Research", { exact: true }).uncheck();
  await filterModel(page).fill("");
  await page.getByLabel("Select default main", { exact: true }).check();
  await expect(page.getByTestId("selection-summary")).toContainText("1");
  await filterModel(page).fill("gpt-5.1");
  await expect(page.getByLabel("Select default main", { exact: true })).toHaveCount(0);
  await filterModel(page).fill("");
  await expect(page.getByLabel("Select default main", { exact: true })).toBeChecked();

  // A filter change invalidates the reviewed custom route.
  await page.getByLabel("New provider", { exact: true }).fill("zhipu");
  await page.getByLabel("New model", { exact: true }).fill("glm-5.3-flash");
  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await filterProvider(page).fill("openai");
  await expect(page.getByTestId("preview-panel")).toHaveCount(0);
  await expect(page.getByTestId("apply-custom")).toBeDisabled();
});

for (const viewport of [{ width: 375, height: 812 }, { width: 768, height: 900 }]) {
  test(`catalog popups stay viewport-bounded and unclipped at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await open(page);
    await chooseProvider(page, "nous");

    // Provider option targets are touch-reachable and the input shows a focus
    // ring (global focus-visible contract).
    await providerInput(page).click();
    const nousBox = await page.getByTestId("provider-option-nous").boundingBox();
    expect(nousBox.height).toBeGreaterThanOrEqual(36);
    await page.keyboard.press("Escape");
    await providerInput(page).focus();
    const ring = await providerInput(page).evaluate((el) => {
      const css = getComputedStyle(el);
      return { style: css.outlineStyle, width: parseFloat(css.outlineWidth) };
    });
    expect(ring.style).not.toBe("none");
    expect(ring.width).toBeGreaterThanOrEqual(2);

    await modelInput(page).click();
    await expect(page.getByTestId("model-option-hermes-4-405b")).toBeVisible();
    const popup = page.getByTestId("model-combobox-popup");
    const box = await popup.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(viewport.width + 0.5);
    expect(box.y + box.height).toBeLessThanOrEqual(viewport.height + 0.5);
    expect(box.width).toBeGreaterThanOrEqual(359);
    expect(await popup.evaluate((el) => Boolean(el.closest(".hms-root")))).toBe(true);
    const list = page.getByTestId("model-listbox");
    expect(await list.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
    expect(await list.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return el.contains(hit);
    })).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

    await page.getByTestId("model-option-hermes-4-405b").click();
    await expect(page.getByTestId("model-listbox")).toHaveCount(0);
    await expect(modelInput(page)).toHaveValue("hermes-4-405b");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

    // A filter popup opened above the matrix scroll region is not clipped by
    // it: the anchored fixed popup paints over the scroll box.
    await filterProvider(page).click();
    const fPopup = page.getByTestId("filter-provider-listbox").locator("xpath=..");
    await expect(fPopup).toBeVisible();
    expect(await fPopup.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, Math.min(r.top + 20, r.bottom - 2));
      return el.contains(hit);
    })).toBe(true);
    await filterProvider(page).press("Escape");
  });
}