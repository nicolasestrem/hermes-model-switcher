import { test, expect } from "@playwright/test";

const URL = "/fixture.html";

async function open(page) {
  await page.goto(URL);
  await expect(page.getByTestId("live-status")).toBeAttached();
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();
}

test("select → preview → save → apply → readback → undo", async ({ page }) => {
  await open(page);

  // --- select group (favorite preset) and review it ---
  await page.getByRole("button", { name: /Fast lane/ }).click();
  await expect(page.getByTestId("preset-review")).toBeVisible();
  await expect(page.getByTestId("preset-review")).toContainText("billing policy");

  // preview runs automatically on review; one-click apply becomes available
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await expect(page.getByTestId("preview-panel")).toContainText("default");
  await expect(page.getByTestId("preview-panel")).toContainText("→ zhipu/glm-5.3-flash");
  await expect(page.getByTestId("live-status")).toContainText("Preview ready");
  await expect(page.getByTestId("apply-preset")).toBeEnabled();

  // --- one-click apply (saved preset: no repeated confirmation modal) ---
  await page.getByTestId("apply-preset").click();
  const drawer = page.getByTestId("operation-drawer");
  await expect(drawer).toBeVisible();
  await expect(page.getByTestId("result-default")).toContainText("Applied + read back");
  await expect(page.getByTestId("result-research")).toContainText("Applied + read back");
  await expect(page.getByTestId("live-status")).toContainText("finished");

  // --- readback: applied statuses verified via readback.ok === true ---
  await expect(page.getByTestId("result-default").locator("[data-status='applied']")).toBeVisible();

  // --- guarded undo ---
  const undo = page.getByTestId("undo-button");
  await expect(undo).toBeDisabled();
  await page.getByTestId("undo-guard").check();
  await undo.click();
  await expect(page.getByTestId("result-default")).toContainText("Restored (undo verified)");
  await expect(page.getByTestId("result-research")).toContainText("Restored (undo verified)");
  await expect(page.getByTestId("live-status")).toContainText("Undo finished");
});

test("custom edit requires preview before apply; changing selection invalidates preview", async ({ page }) => {
  await open(page);
  const apply = page.getByTestId("apply-custom");
  await expect(apply).toBeDisabled();

  // select two main slots
  await page.getByLabel("Select default main").check();
  await page.getByLabel("Select research main").check();
  await page.getByLabel("New provider").fill("zhipu");
  await page.getByLabel("New model").fill("glm-5.3-flash");
  await expect(page.getByTestId("preview-panel")).toHaveCount(0);
  await expect(apply).toBeDisabled(); // still no preview
  await expect(page.getByText("Preview required")).toBeVisible();

  await page.getByRole("button", { name: "Preview changes" }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await expect(page.getByTestId("preview-panel")).toContainText("2 changes");
  await expect(page.getByTestId("live-status")).toContainText("Preview ready");

  // changing selection invalidates the preview
  await page.getByLabel("Select default main").uncheck();
  await expect(page.getByTestId("preview-panel")).toHaveCount(0);
  await expect(apply).toBeDisabled();
});

test("exact-model removal only matches exact identity", async ({ page }) => {
  await open(page);
  await page.getByLabel("Select default main").check();
  await page.getByLabel("Exact provider/model to remove").fill("openai/gpt-5.1");
  await expect(page.getByRole("note")).toContainText("No selected configured slot matches exactly");
  await expect(page.getByRole("button", { name: "Preview changes" })).toBeDisabled();

  await page.getByLabel("Exact provider/model to remove").fill("anthropic/claude-sonnet-4-6");
  await expect(page.getByRole("note")).toHaveCount(0);
  await page.getByRole("button", { name: "Preview changes" }).click();
  await expect(page.getByTestId("preview-panel")).toContainText("remove anthropic/claude-sonnet-4-6");
});

test("failed read and ok:false are not success", async ({ page }) => {
  await open(page);

  // (a) GET /state ok:false → visible failure, no data, no success badge, retry offered
  await page.evaluate(() => {
    sessionStorage.setItem("hms-mock-flags", JSON.stringify({ failState: true }));
  });
  await page.reload();
  await expect(page.getByTestId("load-error")).toContainText("raw config unreadable");
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(page.getByTestId("live-status")).toContainText("Load failed");
  await expect(page.locator("[data-status='applied']")).toHaveCount(0);
  await page.evaluate(() => sessionStorage.removeItem("hms-mock-flags"));
  await page.reload();
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();

  // (b) apply succeeds but readback fails → per-profile FAILED, no success badge, undo still offered but result not claimed as success
  await page.evaluate(() => { window.__mockApi.failReadback = true; });
  await page.getByRole("button", { name: /Fast lane/ }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await page.getByTestId("apply-preset").click();
  const drawer = page.getByTestId("operation-drawer");
  await expect(drawer).toBeVisible();
  await expect(page.getByTestId("result-default")).toContainText("FAILED — readback not verified");
  await expect(page.getByTestId("result-research")).toContainText("FAILED — readback not verified");
  await expect(page.locator("[data-status='applied']")).toHaveCount(0);
  // undo remains available for recovery, but guarded
  await expect(page.getByTestId("undo-button")).toBeDisabled();
  await page.getByTestId("undo-guard").check();
  await page.evaluate(() => { window.__mockApi.failUndo = true; });
  await page.getByTestId("undo-button").click();
  await expect(page.getByTestId("live-status")).toContainText("Undo failed");
  await expect(page.locator("[data-status='restored']")).toHaveCount(0);
});

test("keyboard navigation and aria-live announcements", async ({ page }) => {
  await open(page);

  // aria-live region exists and is a polite status region
  const live = page.getByTestId("live-status");
  await expect(live).toHaveAttribute("aria-live", "polite");

  // keyboard: reach preset buttons and matrix expand buttons via Tab/Enter
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeFocused();
  await page.keyboard.press("Enter"); // review preset via keyboard
  await expect(page.getByTestId("preset-review")).toBeVisible();
  await expect(live).toContainText("Preview ready");

  // expand a routing detail row via keyboard
  await page.getByRole("button", { name: "auxiliary.goal_judge" }).focus();
  await expect(page.getByRole("button", { name: "auxiliary.goal_judge" })).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Enter");
  await expect(page.getByText(/Protocol:.*typed-decision/)).toBeVisible();

  // matrix filters are keyboard-reachable labelled controls
  await page.getByLabel("Filter by family").selectOption("main");
  await expect(page.getByRole("row", { name: /Default/ })).toBeVisible();
});
