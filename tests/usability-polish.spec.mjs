// Usability-polish spec (fixture-based, mock API — no real backend needed).
//
// Approved scope: DESIGN.md sections 4/5/6.
//  - 'All profiles' view filtering distinct from bulk selection; selection is
//    preserved when filters narrow but the review is invalidated.
//  - Indeterminate select-all over matching writable rows + Clear selection;
//    selection counts include targets outside the current filters.
//  - Human-readable family/state labels that retain the backend ids visible.
//  - Read-only rows show the reason; slots without observations read
//    "No recorded usage".
//  - Clearer change-type labels, applicability summary (no silent target
//    drops), exact-model removal as a separate advanced action.
//  - Wide view: matrix + bounded editor rail; narrow: stacked, no overflow;
//    the editor stays before the matrix in the DOM.
//  - Operation drawer: focus entry, Escape close, focus return.
import { test, expect } from "@playwright/test";
import { setProvider, setModel } from "./route-helpers.mjs";

const URL = "/fixture.html";
const STATE = "/api/plugins/hermes-model-switcher/state";
const selectAll = (page) => page.getByTestId("select-all");
const summary = (page) => page.getByTestId("selection-summary");

async function open(page) {
  await page.goto(URL);
  await expect(page.getByTestId("live-status")).toBeAttached();
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();
}

test("Profiles shown filters the view only: selection survives, review is invalidated, hidden targets are counted", async ({ page }) => {
  await open(page);

  // bulk-select two targets and build a reviewed preview
  await page.getByLabel("Select default main").check();
  await page.getByLabel("Select research main").check();
  await setProvider(page, "zhipu");
  await setModel(page, "glm-5.3-flash");
  expect(await summary(page).textContent()).toContain("2 slots on 2 profiles selected");
  await page.getByRole("button", { name: "Preview changes" }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();

  // narrow the VIEW to research: default/main leaves the table but must stay
  // selected, and the reviewed preview must die with the view change.
  await page.getByLabel("Profile Research").check();
  await expect(page.locator('tr[data-row^="default/"]')).toHaveCount(0);
  await expect(page.getByLabel("Select research main")).toBeChecked(); // selection preserved
  const t = await summary(page).textContent();
  expect(t).toContain("2 slots on 2 profiles selected");
  expect(t).toContain("1 hidden by current filters");
  await expect(page.getByTestId("preview-panel")).toHaveCount(0); // review invalidated
  await expect(page.getByTestId("apply-custom")).toBeDisabled();

  // the explicit 'All profiles' reset returns the hidden row still selected
  await page.getByRole("button", { name: "All profiles" }).click();
  await expect(page.locator('tr[data-row="default/main"]')).toBeVisible();
  await expect(page.getByLabel("Select default main")).toBeChecked();
  await expect(page.getByTestId("preview-panel")).toHaveCount(0);
});

test("header select-all covers matching writable rows, goes indeterminate, and Clear selection empties everything", async ({ page }) => {
  await open(page);

  const master = selectAll(page);
  await expect(master).not.toBeChecked();
  expect(await master.evaluate((el) => el.indeterminate)).toBe(false);
  await expect(page.getByTestId("clear-selection")).toBeDisabled();

  // partial completion → indeterminate, not checked
  await page.getByLabel("Select default main").check();
  expect(await master.evaluate((el) => el.indeterminate)).toBe(true);
  await expect(master).not.toBeChecked();

  // one click completes every matching writable row (all five) and never the
  // read-only archived row
  await master.click();
  await expect(master).toBeChecked();
  expect(await master.evaluate((el) => el.indeterminate)).toBe(false);
  for (const label of ["Select default main", "Select default auxiliary.goal_judge",
    "Select default cron.model", "Select research main",
    "Select research delegation.fallback_providers"]) {
    await expect(page.getByLabel(label)).toBeChecked();
  }
  await expect(page.getByLabel("Select archived main")).toBeDisabled();
  await expect(page.getByLabel("Select archived main")).not.toBeChecked();
  expect(await summary(page).textContent()).toContain("5 slots on 2 profiles selected");

  // clearing removes every target, including any outside the current filters
  await page.getByTestId("clear-selection").click();
  for (const label of ["Select default main", "Select research main"]) {
    await expect(page.getByLabel(label)).not.toBeChecked();
  }
  await expect(page.getByTestId("clear-selection")).toBeDisabled();
  await expect(master).not.toBeChecked();
  await expect(summary(page)).toContainText("No slots selected");
});

test("select-all respects the current filters: only matching writable rows are added", async ({ page }) => {
  await open(page);

  await page.getByLabel("Filter by family").selectOption("main");
  await expect(page.locator("tr[data-row]")).toHaveCount(3); // 3 main rows, 1 read-only
  await page.getByTestId("select-all").click();

  // only the two writable rows in view become selected — nothing outside
  await expect(page.getByLabel("Select default main")).toBeChecked();
  await expect(page.getByLabel("Select research main")).toBeChecked();
  await expect(page.locator('tr[data-row="default/cron.model"]')).toHaveCount(0);
  await expect(page.locator('tr[data-row="default/auxiliary.goal_judge"]')).toHaveCount(0);
  await expect(page.getByLabel("Select archived main")).toBeDisabled();
  await expect(page.getByTestId("select-all")).toBeChecked(); // all writable in view
});

test("family/name labels are human-readable while backend ids stay attached", async ({ page }) => {
  await open(page);

  const row = page.locator('tr[data-row="default/auxiliary.goal_judge"]');
  const family = row.locator("td[data-family-id='judges']");
  await expect(family).toContainText("Judges / Reviewers"); // readable label leads
  expect(await family.getAttribute("data-family-id")).toBe("judges"); // backend id retained

  const stateCell = row.locator("[data-column='state']");
  await expect(stateCell).toContainText("Configured");
  expect(await stateCell.locator(".hms-badge").getAttribute("data-state")).toBe("configured");
});

test("read-only rows show their reason; slots without observations read 'No recorded usage'", async ({ page }) => {
  // mock-api.js overrides window.fetch in-page, which bypasses Playwright
  // route interception — fulfill it empty so native transport is used and the
  // /state interception below actually reaches the app
  await page.route("**/tests/mock-api.js", (route) =>
    route.fulfill({ contentType: "application/javascript", body: "" }));
  await page.route("**" + STATE, (route) => route.fulfill({
    json: {
      ok: true,
      profiles: [
        { id: "default", name: "Default", writable: true },
        { id: "archived", name: "Archived", writable: false },
      ],
      slots: [
        { profileId: "default", slotId: "main", family: "main", provider: "anthropic",
          model: "claude-sonnet-4-6", state: "configured", writable: true },
        { profileId: "default", slotId: "main_chain", family: "fallback", provider: "openai",
          model: "gpt-5-mini", state: "configured", writable: true }, // no lastObserved
        { profileId: "archived", slotId: "main", family: "main", provider: "anthropic",
          model: "claude-sonnet-4-6", state: "configured", writable: false }, // no note: profile-level reason
        { profileId: "archived", slotId: "specialist.stt", family: "specialist",
          provider: "nous", model: "hermes-4", state: "configured", writable: false,
          note: "Unsupported specialist slot; voice and language settings are preserved" },
      ],
      presets: [],
    },
  }));
  await page.goto(URL);
  await expect(page.locator('tr[data-row="archived/main"]')).toBeVisible();

  // profile-level read-only carries an explicit reason, not just "(read-only)"
  const archived = page.locator('tr[data-row="archived/main"]');
  await expect(archived.getByTestId("readonly-reason")).toContainText("Read-only");
  await expect(archived.getByTestId("readonly-reason")).toContainText("Routing cannot be changed on this profile.");
  // slot-level reason comes from the slot's own note
  const specialist = page.locator('tr[data-row="archived/specialist.stt"]');
  await expect(specialist.getByTestId("readonly-reason")).toContainText("voice and language");

  // absence of lastObserved is stated in words, with the value still exact
  const observedCell = page.locator('tr[data-row="default/main_chain"] [data-column="observed"]');
  await expect(observedCell).toContainText("No recorded usage");
  const observed = await page.getByLabel("Select default main", { exact: true }).count();
  expect(observed).toBe(1); // matrix rendered normally otherwise
});

test("editor: clearer action labels, applicability summary instead of silent drops, removal as a separate advanced action", async ({ page }) => {
  await open(page);

  const type = page.getByTestId("change-type");
  const options = await type.locator("option").allTextContents();
  expect(options.join("\n")).toContain("Choose a model");
  expect(options.join("\n")).toContain("Set fallback sequence");
  expect(options.join("\n")).toContain("Reset to default or inherited routing");
  expect(options.join("\n")).toContain("Turn off fallback");

  // applicability: empty set-type intent is stated, not just a dead button
  await page.getByLabel("Select research main").check();
  await expect(page.getByTestId("editor-applicability")).toContainText("provider and model");

  // 'Turn off fallback' does not silently drop the non-chain target
  await page.getByLabel("Select research delegation.fallback_providers").check();
  await type.selectOption("disable");
  const note = await page.getByTestId("editor-applicability").textContent();
  expect(note).toContain("1 of 2");
  expect(note).toContain("fallback chain slots");

  // exact-model removal lives in its own, clearly advanced block — collapsed
  // until requested
  const removal = page.locator('[data-testid="removal-advanced"]');
  await expect(removal).toBeVisible();
  await expect(removal.locator("summary")).toContainText("Advanced");
  expect(await removal.evaluate((el) => el.open)).toBe(false);
  await removal.locator("summary").click();
  await expect(removal.getByLabel("Exact provider/model to remove")).toBeVisible();
});

test("exact removal stays functional behind the advanced block", async ({ page }) => {
  await open(page);
  await page.getByLabel("Select default main").check();
  // the advanced block is collapsed by default — open it before use
  await page.getByTestId("removal-advanced").locator("summary").click();
  const removal = page.getByLabel("Exact provider/model to remove");
  await removal.fill("anthropic/claude-sonnet-4-6");
  await page.getByRole("button", { name: "Preview changes" }).click();
  await expect(page.getByTestId("preview-panel")).toContainText("remove anthropic · claude-sonnet-4-6");
});

test("layout: editor before matrix in DOM; wide view is matrix + rail; narrow stacks without overflow", async ({ page }) => {
  await open(page);

  // editor/action surface must still precede the matrix in the DOM
  const domOrder = await page.evaluate(() => {
    const rail = document.querySelector('[data-testid="action-surface"]');
    const matrix = document.querySelector('section[aria-label="Profile model matrix"]');
    return !!(rail.compareDocumentPosition(matrix) & Node.DOCUMENT_POSITION_FOLLOWING);
  });
  expect(domOrder).toBe(true);

  // wide: the editor becomes a bounded right-hand rail next to the matrix
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect.poll(() => page.evaluate(() =>
    getComputedStyle(document.querySelector(".hms-workspace")).display)).toBe("grid");
  const rail = await page.locator('[data-testid="action-surface"]').boundingBox();
  const matrixBox = await page.locator('section[aria-label="Profile model matrix"]').boundingBox();
  expect(rail.x).toBeGreaterThan(matrixBox.x);
  expect(rail.width).toBeLessThanOrEqual(380);

  // narrow: stacked, rail above matrix, no document horizontal overflow
  for (const size of [{ width: 900, height: 900 }, { width: 768, height: 900 }, { width: 375, height: 844 }]) {
    await page.setViewportSize(size);
    await expect.poll(() => page.evaluate(() =>
      getComputedStyle(document.querySelector(".hms-workspace")).display)).toBe("block");
    const stack = await page.evaluate(() => {
      const rail = document.querySelector('[data-testid="action-surface"]').getBoundingClientRect();
      const matrix = document.querySelector('section[aria-label="Profile model matrix"]').getBoundingClientRect();
      return rail.top < matrix.top;
    });
    expect(stack).toBe(true);
    expect(await page.evaluate(() =>
      document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  }
});

test("operation drawer: focus enters the drawer, Escape closes it, focus returns to the opener or Refresh state", async ({ page }) => {
  await open(page);

  await page.getByRole("button", { name: /Fast lane/ }).click();
  await page.getByTestId("apply-preset").click();
  await expect(page.getByTestId("operation-drawer")).toBeVisible();
  const inside = await page.evaluate(() =>
    !!document.activeElement.closest('[data-testid="operation-drawer"]'));
  expect(inside).toBe(true);

  // focus is trapped while the drawer is open
  for (let i = 0; i < 5; i++) await page.keyboard.press("Tab");
  expect(await page.evaluate(() =>
    !!document.activeElement.closest('[data-testid="operation-drawer"]'))).toBe(true);

  await page.keyboard.press("Escape");
  await expect(page.getByTestId("operation-drawer")).toHaveCount(0);
  // apply cleared the preview so the opener (apply-preset) is disabled —
  // focus falls back to Refresh state
  await expect(page.getByTestId("refresh-state")).toBeFocused();
});
