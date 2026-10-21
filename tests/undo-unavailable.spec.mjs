// Claude review #4: when Undo cannot restore, the drawer must say WHY in plain
// language beside the control and what to do next — a bare disabled button is a
// dead end. Two shapes are covered:
//   * an APPLY that ends ``recovery_required`` (undoable:false, fixed failure
//     category on the DTO) — Undo was never available;
//   * an UNDO that runs and ends ``recovery_required`` (a profile moved since
//     the apply) — Undo did not restore everything. The real DTO carries NO
//     op-level ``failure`` category for this shape (engine.undo_operation
//     finishes recovery_required without set_failure_category), so the reason
//     is mapped from the terminal state.
// Both must stay non-focusable: no new control ahead of the pinned first-Tab
// target, and nothing added to the drawer's tab order.
import { test, expect } from "@playwright/test";

const URL = "/fixture.html";

async function open(page) {
  await page.goto(URL);
  await expect(page.getByTestId("live-status")).toBeAttached();
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();
}

async function applyFastLane(page) {
  await page.getByRole("button", { name: /Fast lane/ }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await page.getByTestId("apply-preset").click();
  await expect(page.getByTestId("operation-drawer")).toBeVisible();
}

// Flags are read by mock-api.js from sessionStorage at load; set + reload so the
// NEXT operation uses them, and always clear so a failed assertion cannot leak
// the behaviour into later specs.
async function withFlags(page, flags, body) {
  await page.goto(URL);
  await page.evaluate((f) => sessionStorage.setItem("hms-mock-flags", JSON.stringify(f)), flags);
  await page.reload();
  try {
    await body();
  } finally {
    await page.evaluate(() => sessionStorage.removeItem("hms-mock-flags"));
  }
}

test("the undo explanation adds no control ahead of the pinned first Tab target", async ({ page }) => {
  await open(page);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeFocused();
});

test("an undo that ends recovery_required explains why and keeps Undo disabled", async ({ page }) => {
  await withFlags(page, { undoConflict: true }, async () => {
    await open(page);
    await applyFastLane(page);
    await page.getByTestId("undo-guard").check();
    await expect(page.getByTestId("undo-button")).toBeEnabled();
    await page.getByTestId("undo-button").click();

    // The announcement is a sentence, not raw state tokens.
    const live = page.getByTestId("live-status");
    await expect(live).toContainText("Undo finished");
    await expect(live).toContainText("the config changed outside this operation");
    await expect(live).toContainText("Review the profile config, then refresh.");
    await expect(live).not.toContainText("recovery_required");
    await expect(live).not.toContainText("conflict");

    // The drawer names the reason beside the control, and the control is off.
    const note = page.getByTestId("undo-unavailable");
    await expect(note).toBeVisible();
    await expect(note).toContainText("Undo did not restore every profile");
    await expect(note).toContainText("the config changed outside this operation");
    await expect(note).toContainText("Review the profile config, then refresh.");
    await expect(note).not.toContainText("recovery_required");
    await expect(note).toHaveAttribute("data-tone", "danger");
    expect(await note.evaluate((el) => getComputedStyle(el).color)).toBe("rgb(229, 83, 75)");
    await expect(page.getByTestId("undo-button")).toBeDisabled();

    // Not a new focusable control: a plain paragraph outside the tab order.
    expect(await note.evaluate((el) => el.tagName)).toBe("P");
    expect(await note.evaluate((el) => el.tabIndex)).toBe(-1);
    const focusable = await page.getByTestId("operation-drawer").evaluate((el) =>
      Array.from(el.querySelectorAll('button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])'))
        .map((n) => n.getAttribute("data-testid") || n.tagName));
    expect(focusable).not.toContain("undo-unavailable");
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press("Tab");
      expect(await note.evaluate((el) => el === document.activeElement)).toBe(false);
    }
  });
});

test("an apply that ends recovery_required says why Undo is unavailable", async ({ page }) => {
  await withFlags(page, { rollbackMode: "recovery_required" }, async () => {
    await open(page);
    await applyFastLane(page);

    // Mapped from the DTO's fixed category, never the enum itself.
    const note = page.getByTestId("undo-unavailable");
    await expect(note).toBeVisible();
    await expect(note).toContainText("Undo is unavailable");
    await expect(note).toContainText("the last write could not be verified");
    await expect(note).toContainText("Check the profile config before trying again.");
    await expect(note).not.toContainText("write_verification_failed");
    await expect(page.getByTestId("undo-button")).toBeDisabled();

    // Acknowledging the guard cannot revive a non-undoable operation.
    await page.getByTestId("undo-guard").check();
    await expect(page.getByTestId("undo-button")).toBeDisabled();
    await expect(note).toBeVisible();
  });
});

test("a clean undo announces the restore in plain words and explains the disabled control", async ({ page }) => {
  await open(page);
  await applyFastLane(page);
  await page.getByTestId("undo-guard").check();
  await page.getByTestId("undo-button").click();

  // The old announcement was "Undo finished: default compensated, research
  // compensated" — profile ids and journal statuses, not a sentence.
  await expect(page.getByTestId("live-status")).toContainText(
    "Undo finished: previous revisions restored for Default and Research.");
  const note = page.getByTestId("undo-unavailable");
  await expect(note).toContainText("Undo is unavailable: this operation is already undone.");
  await expect(note).toContainText("Refresh to confirm the restored revisions.");
  await expect(note).toHaveAttribute("data-reason", "undone");
  await expect(note).toHaveAttribute("data-tone", "warning");
  expect(await note.evaluate((el) => getComputedStyle(el).color)).toBe("rgb(210, 153, 34)");
  await expect(page.getByTestId("undo-button")).toBeDisabled();
});
