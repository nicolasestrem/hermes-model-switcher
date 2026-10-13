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

  // the preset apply pairs the reviewed preset with exactly this preview
  const opBodies = await page.evaluate(() =>
    window.__mockApi.requests
      .filter((r) => r.method === "POST" && r.path.endsWith("/operations"))
      .map((r) => r.body));
  expect(opBodies.length).toBe(1);
  expect(opBodies[0].presetId).toBe("p-fast");
  expect(typeof opBodies[0].previewId).toBe("string");

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

test("select → preview → editor Apply → guarded undo; changing selection invalidates preview", async ({ page }) => {
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

  // a preview alone is not enough: apply stays disabled until the
  // unknown-cost acknowledgment is checked
  await expect(apply).toBeDisabled();
  await page.getByRole("checkbox", { name: /acknowledge unknown-cost/ }).check();
  await expect(apply).toBeEnabled();

  // the editor's primary Apply button runs the same apply action and shows
  // operation feedback (regression: data-testid=apply-custom must not be inert)
  await page.evaluate(() => { window.__mockApi.requests.length = 0; });
  await apply.click();
  await expect(page.getByTestId("operation-drawer")).toBeVisible();
  await expect(page.getByTestId("result-default")).toContainText("Applied + read back");
  await expect(page.getByTestId("result-research")).toContainText("Applied + read back");
  await expect(page.getByTestId("live-status")).toContainText("finished");
  const bodies = await page.evaluate(() =>
    window.__mockApi.requests
      .filter((r) => r.method === "POST" && r.path.endsWith("/operations"))
      .map((r) => r.body));
  expect(bodies.length).toBe(1);
  expect(bodies[0].presetId).toBeUndefined(); // no preset was reviewed
  expect(typeof bodies[0].previewId).toBe("string");

  // --- guarded undo ---
  const undo = page.getByTestId("undo-button");
  await expect(undo).toBeDisabled();
  await page.getByTestId("undo-guard").check();
  await undo.click();
  await expect(page.getByTestId("result-default")).toContainText("Restored (undo verified)");
  await expect(page.getByTestId("result-research")).toContainText("Restored (undo verified)");
  await expect(page.getByTestId("live-status")).toContainText("Undo finished");

  // changing selection invalidates the preview
  await page.getByLabel("Select default main").uncheck();
  await expect(page.getByTestId("preview-panel")).toHaveCount(0);
  await expect(apply).toBeDisabled();
});

test("applying a custom preview after reviewing a preset sends no stale presetId", async ({ page }) => {
  await open(page);

  // review the preset first; its apply legitimately carries presetId
  await page.getByRole("button", { name: /Fast lane/ }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await page.getByTestId("apply-preset").click();
  await expect(page.getByTestId("operation-drawer")).toBeVisible();
  await expect(page.getByTestId("live-status")).toContainText("finished");

  // now build a custom preview in the editor on top of that context
  await page.getByLabel("Select default main").check();
  await page.getByLabel("Select research main").check();
  await page.getByLabel("New provider").fill("zhipu");
  await page.getByLabel("New model").fill("glm-5.3-flash");
  await page.getByRole("checkbox", { name: /acknowledge unknown-cost/ }).check();
  await page.getByRole("button", { name: "Preview changes" }).click();
  await expect(page.getByTestId("preview-panel")).toContainText("2 changes");
  await expect(page.getByTestId("live-status")).toContainText("Preview ready");

  // the working apply path for custom previews: "Apply reviewed changes"
  await page.getByTestId("apply-from-drawer").click(); // must not carry the old presetId
  await expect(page.locator('[data-testid="operation-drawer"] h3')).toContainText("op-2");
  await expect(page.getByTestId("result-default")).toContainText("Applied + read back");

  const bodies = await page.evaluate(() =>
    window.__mockApi.requests
      .filter((r) => r.method === "POST" && r.path.endsWith("/operations"))
      .map((r) => r.body));
  expect(bodies.length).toBe(2);
  expect(bodies[0].presetId).toBe("p-fast"); // preset apply keeps its pairing
  expect(bodies[1].previewId).not.toBe(bodies[0].previewId); // the custom preview, not the preset's
  expect(bodies[1].presetId).toBeUndefined(); // custom apply: no stale presetId
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

test("a blocked apply reports the engine's reason, not a bare status code", async ({ page }) => {
  await page.goto(URL);
  await page.evaluate(() => {
    sessionStorage.setItem("hms-mock-flags", JSON.stringify({ blockedApply: true }));
  });
  await page.reload();
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();

  await page.getByRole("button", { name: /Fast lane/ }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await page.getByTestId("apply-preset").click();

  // FastAPI wraps the engine's refusal under `detail`; that sentence is the
  // whole point — "HTTP 409" alone tells the user nothing to fix.
  await expect(page.getByTestId("live-status")).toContainText("the canonical route was rejected");
  await expect(page.getByTestId("live-status")).toContainText("Nous Portal is not connected");
  // ...and it is on screen, not only in the screen-reader region: a refused
  // apply that shows nothing is exactly how "the tab does nothing" happens.
  await expect(page.getByTestId("visible-status")).toBeVisible();
  await expect(page.getByTestId("visible-status")).toContainText("the canonical route was rejected");
  await expect(page.getByTestId("operation-drawer")).toHaveCount(0);
  await page.evaluate(() => sessionStorage.removeItem("hms-mock-flags"));
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
