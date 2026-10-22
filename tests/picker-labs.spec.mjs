import { test, expect } from "@playwright/test";
import { modelSelect, OTHER, providerSelect, ensureCatalog } from "./route-helpers.mjs";
import { installPickerMultilab, withdrawnZaiProviders } from "./fixtures/picker-multilab.js";

// Lab + filter contract for the routing panel's Model dropdown. A provider whose
// model ids span several vendor namespaces (openrouter, nous) gets a native Lab
// select and a model filter so its first screen is not just anthropic+openai;
// a single-lab or bare-id provider keeps today's flat full-id list. The write
// payload is unchanged: editor.provider + the full model id.
const labSelect = (page) => page.getByTestId("lab-select");
const filterInput = (page) => page.getByTestId("model-filter");
const priceNote = (page) => page.getByTestId("model-price-note");
const matchCount = (page) => page.getByTestId("picker-match-count");
const option = (page, id) => modelSelect(page).locator(`option[value="${id}"]`);
const optionValues = (select) => select.locator("option").evaluateAll(
  (nodes, other) => nodes.map((n) => n.value).filter((v) => v !== "" && v !== other),
  OTHER,
);
const optionTexts = (select) => select.locator("option").allTextContents();

async function open(page) {
  await page.goto("/fixture.html");
  await expect(providerSelect(page)).toBeVisible();
  // The mult-lab catalog is installed before the (lazy) first fetch.
  await installPickerMultilab(page);
  await ensureCatalog(page);
  await expect(providerSelect(page).locator('option[value="openrouter"]')).toHaveCount(1);
}

async function chooseProvider(page, id) {
  await providerSelect(page).selectOption(id);
  await expect(providerSelect(page)).toHaveValue(id);
}

test("a multi-lab provider gets a Lab select listing every lab with its count", async ({ page }) => {
  await open(page);
  await chooseProvider(page, "openrouter");

  await expect(labSelect(page)).toBeVisible();
  expect(await labSelect(page).evaluate((el) => el.tagName)).toBe("SELECT");
  expect(await optionTexts(labSelect(page))).toEqual([
    "All labs (30)",
    "Anthropic (9)",
    "DeepSeek (5)",
    "OpenAI (11)",
    "OpenRouter (1)",
    "Z.ai (4)",
  ]);
  // "All labs" is the opening state; the Model list is grouped per lab with the
  // same counts, alphabetically by label.
  await expect(labSelect(page)).toHaveValue("");
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (30)…");
  const groups = await modelSelect(page).locator("optgroup").evaluateAll(
    (nodes) => nodes.map((n) => n.getAttribute("label")),
  );
  expect(groups).toEqual(["Anthropic · 9", "DeepSeek · 5", "OpenAI · 11", "OpenRouter · 1", "Z.ai · 4"]);
  // anthropic and openai still open first inside their own group — the labs the
  // unfiltered list used to bury are now one click away.
  expect((await optionValues(modelSelect(page))).length).toBe(30);
});

test("choosing a lab narrows the Model select to leaf-named rows with full-id values", async ({ page }) => {
  await open(page);
  await chooseProvider(page, "openrouter");
  await labSelect(page).selectOption("deepseek");

  await expect(labSelect(page)).toHaveValue("deepseek");
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (5 of 30)…");
  await expect(matchCount(page)).toHaveText("5 of 30 models");
  // One lab: no optgroups, short leaf rows, the full id stays the option value
  // and the Route line keeps the exact identity.
  await expect(modelSelect(page).locator("optgroup")).toHaveCount(0);
  expect(await optionValues(modelSelect(page))).toEqual([
    "deepseek/deepseek-v4.1-flash",
    "deepseek/deepseek-v4.1",
    "deepseek/deepseek-v4.1-pro",
    "deepseek/deepseek-r2",
    "deepseek/deepseek-r1:free",
  ]);
  await expect(option(page, "deepseek/deepseek-v4.1-flash")).toHaveText("deepseek-v4.1-flash — $0.50/1M out");
  await expect(option(page, "deepseek/deepseek-v4.1")).toHaveText("deepseek-v4.1");
  await expect(option(page, "deepseek/deepseek-r1:free")).toHaveText("deepseek-r1:free — free");
  await expect(option(page, "deepseek/deepseek-v4.1-flash")).toHaveAttribute(
    "title", "deepseek/deepseek-v4.1-flash",
  );

  await modelSelect(page).selectOption("deepseek/deepseek-v4.1-flash");
  await expect(page.getByTestId("live-status")).toContainText("Route set to openrouter · deepseek/deepseek-v4.1-flash");
  await expect(page.getByTestId("route-summary")).toHaveText(
    "Route: openrouter · deepseek/deepseek-v4.1-flash",
  );
});

test("the filter matches ids and names case-insensitively across labs and combines with a lab", async ({ page }) => {
  await open(page);
  await chooseProvider(page, "openrouter");

  await filterInput(page).fill("GLM");
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (4 of 30)…");
  expect(await optionValues(modelSelect(page))).toEqual([
    "z-ai/glm-5.3",
    "z-ai/glm-5.3-flash",
    "z-ai/glm-5-air",
    "z-ai/glm-4.7",
  ]);
  await expect(modelSelect(page).locator("optgroup")).toHaveCount(1);
  await expect(modelSelect(page).locator("optgroup")).toHaveAttribute("label", "Z.ai · 4");

  // Lab + filter combine: a lab with no match states it once, in words.
  await labSelect(page).selectOption("anthropic");
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (0 of 30)…");
  await expect(matchCount(page)).toHaveText("0 of 30 models");
  const none = modelSelect(page).locator("option", { hasText: "No models match" });
  await expect(none).toHaveCount(1);
  await expect(none).toHaveAttribute("disabled", "");
  await expect(modelSelect(page).locator("optgroup")).toHaveCount(0);

  // "Other" survives every filter state: exact ids stay reachable.
  await expect(modelSelect(page).locator(`option[value="${OTHER}"]`)).toHaveText(/Other — type an exact model id/);
  await filterInput(page).fill("");
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (9 of 30)…");
  await expect(modelSelect(page).locator(`option[value="${OTHER}"]`)).toHaveCount(1);
});

test("a filtered-out selection stays in the select and in the write payload", async ({ page }) => {
  await open(page);
  await page.getByLabel("Select default main", { exact: true }).check();
  await chooseProvider(page, "openrouter");
  await modelSelect(page).selectOption("z-ai/glm-5.3");
  await expect(page.getByTestId("route-summary")).toHaveText("Route: openrouter · z-ai/glm-5.3");

  await filterInput(page).fill("claude");
  // The chosen model never disappears from its own select: it is pinned first
  // with its lab named, and the editor keeps the value.
  await expect(modelSelect(page)).toHaveValue("z-ai/glm-5.3");
  const pinned = option(page, "z-ai/glm-5.3");
  await expect(pinned).toHaveCount(1);
  await expect(pinned).toHaveAttribute("data-pinned", "true");
  await expect(pinned).toHaveText("Z.ai · glm-5.3");
  await expect(page.getByTestId("route-summary")).toHaveText("Route: openrouter · z-ai/glm-5.3");

  // A lab that excludes it pins it the same way, and the payload uses the full id.
  await labSelect(page).selectOption("deepseek");
  await expect(modelSelect(page)).toHaveValue("z-ai/glm-5.3");
  await expect(option(page, "z-ai/glm-5.3")).toHaveAttribute("data-pinned", "true");

  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  const previewBody = await page.evaluate(() =>
    window.__mockApi.requests.filter((r) => r.path.endsWith("/preview")).at(-1).body);
  expect(previewBody.changes[0].to).toEqual({ provider: "openrouter", model: "z-ai/glm-5.3" });
});

test("preview and apply still send the full namespaced model id", async ({ page }) => {
  await open(page);
  await page.getByLabel("Select default main", { exact: true }).check();
  await chooseProvider(page, "openrouter");
  await labSelect(page).selectOption("deepseek");
  await modelSelect(page).selectOption("deepseek/deepseek-v4.1-flash");
  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();

  const previewBody = await page.evaluate(() =>
    window.__mockApi.requests.filter((r) => r.path.endsWith("/preview")).at(-1).body);
  expect(previewBody.changes[0].to).toEqual({
    provider: "openrouter",
    model: "deepseek/deepseek-v4.1-flash",
  });

  await page.getByTestId("apply-custom").click();
  await expect.poll(async () => page.evaluate(() =>
    window.__mockApi.requests.filter((r) => r.host && r.path === "/api/model/set").length,
  )).toBe(1);
  const hostBody = await page.evaluate(() =>
    window.__mockApi.requests.filter((r) => r.host && r.path === "/api/model/set").at(-1).body);
  expect(hostBody).toEqual({ scope: "main", provider: "openrouter", model: "deepseek/deepseek-v4.1-flash" });
});

test("changing the provider resets the lab and the filter", async ({ page }) => {
  await open(page);
  await chooseProvider(page, "openrouter");
  await labSelect(page).selectOption("deepseek");
  await filterInput(page).fill("deepseek");

  await chooseProvider(page, "primalabs");
  await expect(labSelect(page)).toHaveCount(0); // single lab: no Lab select
  await expect(filterInput(page)).toHaveCount(0); // seven rows: no filter needed
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (7)…");

  await chooseProvider(page, "openrouter");
  await expect(labSelect(page)).toHaveValue("");
  await expect(filterInput(page)).toHaveValue("");
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (30)…");
  expect((await optionValues(modelSelect(page))).length).toBe(30);
});

// A lab pick belongs to the options it was chosen from. A catalog scope switch
// or a refresh can withdraw that lab while the provider stays multi-lab: the
// pick must not outlive its option, or the select reads "All labs" over a list
// narrowed to a lab that no longer exists.
async function pickZai(page) {
  await chooseProvider(page, "openrouter");
  await labSelect(page).selectOption("z-ai");
  await expect(labSelect(page)).toHaveValue("z-ai");
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (4 of 30)…");
  await expect(matchCount(page)).toHaveText("4 of 30 models");
}

test("a lab the new catalog scope no longer offers stops narrowing the model list", async ({ page }) => {
  await page.goto("/fixture.html");
  await expect(providerSelect(page)).toBeVisible();
  // `research` serves the same openrouter with its Z.ai rows withdrawn: still
  // anthropic, deepseek, openai and its own passthrough id, no `z-ai`.
  await installPickerMultilab(page, { profiles: { research: withdrawnZaiProviders() } });
  await ensureCatalog(page);
  await pickZai(page);

  await page.getByLabel("Profile Research", { exact: true }).check();
  // The picker loads the new scope lazily, exactly as a user's next focus does.
  await providerSelect(page).focus();
  await expect(labSelect(page)).toHaveValue("");
  await expect(labSelect(page).locator("option:checked")).toHaveText("All labs (26)");
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (26)…");
  expect((await optionValues(modelSelect(page))).length).toBe(26);
  await expect(matchCount(page)).toHaveCount(0);
});

test("a refresh that withdraws the chosen lab clears the pick, not just the rows", async ({ page }) => {
  await page.goto("/fixture.html");
  await expect(providerSelect(page)).toBeVisible();
  await installPickerMultilab(page, { refreshed: { default: withdrawnZaiProviders() } });
  await ensureCatalog(page);
  await pickZai(page);

  await page.getByTestId("refresh-catalog").click();
  await expect(labSelect(page)).toHaveValue("");
  await expect(labSelect(page).locator("option:checked")).toHaveText("All labs (26)");
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (26)…");
  await expect(matchCount(page)).toHaveCount(0);
  // The cleared pick leaves a working control: narrowing again counts honestly.
  await labSelect(page).selectOption("deepseek");
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (5 of 26)…");
});

test("a single-lab provider keeps today's flat full-id list and one price note", async ({ page }) => {
  await open(page);
  await chooseProvider(page, "primalabs");

  await expect(labSelect(page)).toHaveCount(0);
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (7)…");
  // No per-row "price not published": every row of this provider is unknown, so
  // the fact is stated once under the select instead of eleven times.
  await expect(priceNote(page)).toHaveText("No list prices published for this provider.");
  await expect(modelSelect(page).locator("option").first()).not.toContainText("price not published");
  await expect(modelSelect(page).locator("optgroup")).toHaveCount(0);
  await expect(option(page, "primalabs-ai/Primalabs-Model-1-1017")).toHaveText(
    "primalabs-ai/Primalabs-Model-1-1017 (Primalabs Model 1)",
  );
  const texts = await optionTexts(modelSelect(page));
  expect(texts.join(" ")).not.toContain("price not published");
});

test("a long bare-id provider gets the filter but no Lab select and no price note", async ({ page }) => {
  await open(page);
  await chooseProvider(page, "opencode-go");

  await expect(labSelect(page)).toHaveCount(0);
  await expect(filterInput(page)).toBeVisible();
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (46)…");
  await expect(priceNote(page)).toHaveCount(0); // one row carries a real price
  await expect(option(page, "opencode-model-01")).toHaveText(
    "opencode-model-01 (OpenCode Model 1) — $0.20/1M out",
  );

  await filterInput(page).fill("model-46");
  await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (1 of 46)…");
  expect(await optionValues(modelSelect(page))).toEqual(["opencode-model-46"]);
  await expect(option(page, "opencode-model-46")).toHaveText("opencode-model-46 (OpenCode Model 46)");
});

test("no option text anywhere repeats the removed price-not-published suffix", async ({ page }) => {
  await open(page);
  for (const provider of ["openrouter", "primalabs", "opencode-go"]) {
    await chooseProvider(page, provider);
    const rows = (await optionTexts(modelSelect(page))).slice(1)
      .filter((text) => !text.startsWith("Other — type an exact model id"));
    expect(rows.join(" ")).not.toContain("price not published");
    // A price says what it is: a rate, "free" or "included in subscription".
    for (const text of rows) {
      if (text.includes(" — ")) expect(text).toMatch(/ — (\$[\d.]+\/1M (in|out)|free|included in subscription)$/);
    }
  }
});
