// Outcome truthfulness: a failed apply that ROLLED BACK (per-step status
// ``compensated``) must never be dressed as a verified user undo, and the
// operation summary must count the affected profiles as needing attention.
//
// Round-1 F2: ``RESULT_LABEL.compensated`` was unconditionally the green
// "Restored (undo verified)" and ``attentionCount`` only counted failed/conflict,
// so a fully-compensated failed apply rendered "0 profiles applied · 0 need
// attention" — a clean success on screen for a write that did not stick.
import { test, expect } from "@playwright/test";

const URL = "/fixture.html";
// Theme fallbacks from ui/src/theme.js (the fixture defines no host tokens).
const OK = "rgb(63, 185, 80)"; // THEME.ok
const WARN = "rgb(210, 153, 34)"; // THEME.warn
const DANGER = "rgb(229, 83, 75)"; // THEME.danger
const FG = "rgb(230, 232, 238)"; // THEME.fg

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

const colorOf = (loc) => loc.evaluate((el) => getComputedStyle(el).color);

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

test("a fully compensated failed apply never reads as a verified undo", async ({ page }) => {
  await withFlags(page, { rollbackMode: "compensated" }, async () => {
    await open(page);
    await applyFastLane(page);
    const drawer = page.getByTestId("operation-drawer");

    // Two rolled-back steps, and NOT ONE may claim a verified restore.
    await expect(drawer.locator("[data-status='compensated']")).toHaveCount(2);
    await expect(drawer).not.toContainText("Restored (undo verified)");
    await expect(drawer).not.toContainText("Applied + read back");
    await expect(page.getByTestId("result-default")).toContainText("Rolled back after failed apply");
    await expect(page.getByTestId("result-research")).toContainText("Rolled back after failed apply");

    // The bug: this used to read "0 profiles applied · 0 need attention".
    await expect(page.getByTestId("operation-summary")).toContainText("0 profiles applied · 2 need attention");
    await expect(page.getByTestId("operation-summary")).toHaveAttribute("data-attention", "true");
    expect(await colorOf(page.getByTestId("operation-summary"))).toBe(WARN);

    // The rollback label is warning-coloured, never the success green.
    expect(await colorOf(drawer.locator("[data-status='compensated']").first())).toBe(WARN);

    // The REAL compensated DTO carries the safe category
    // ``write_verification_failed`` (the engine persists it BEFORE it
    // compensates), so the failure paragraph is on screen and the state line is
    // superseded — there is no ``operation-state`` line for this case.
    const failure = page.getByTestId("operation-failure");
    await expect(failure).toContainText("Write verification failed");
    await expect(failure).toHaveAttribute("data-failure-category", "write_verification_failed");
    await expect(failure).toContainText("final state: compensated");
    await expect(page.getByTestId("operation-state")).toHaveCount(0);

    // A non-committed operation is not undoable through the drawer.
    await expect(page.getByTestId("undo-button")).toBeDisabled();
  });
});

test("a partial rollback keeps the mixed per-step truth", async ({ page }) => {
  await withFlags(page, { rollbackMode: "partial" }, async () => {
    await open(page);
    await applyFastLane(page);
    const drawer = page.getByTestId("operation-drawer");

    await expect(drawer.locator("[data-status='compensated']")).toHaveCount(1);
    await expect(drawer.locator("[data-status='failed']")).toHaveCount(1);
    await expect(page.getByTestId("result-default")).toContainText("Rolled back after failed apply");
    await expect(page.getByTestId("result-research")).toContainText("FAILED — not verified");
    await expect(drawer).not.toContainText("Restored (undo verified)");
    await expect(page.getByTestId("operation-summary")).toContainText("0 profiles applied · 2 need attention");
    // The failing step's safe category rides along with state ``partial``; the
    // failure paragraph is on screen and there is no separate state line.
    const failure = page.getByTestId("operation-failure");
    await expect(failure).toContainText("Storage error while writing the change");
    await expect(failure).toHaveAttribute("data-failure-category", "io_error");
    await expect(failure).toContainText("final state: partial");
    await expect(page.getByTestId("operation-state")).toHaveCount(0);
  });
});

test("a recovery_required apply shows descriptive failure copy, not a raw category", async ({ page }) => {
  await withFlags(page, { rollbackMode: "recovery_required" }, async () => {
    await open(page);
    await applyFastLane(page);
    const drawer = page.getByTestId("operation-drawer");

    await expect(page.getByTestId("result-default")).toContainText("FAILED — not verified");
    await expect(drawer).not.toContainText("Restored (undo verified)");
    const failure = page.getByTestId("operation-failure");
    await expect(failure).toContainText("Write verification failed");
    await expect(failure).toHaveAttribute("data-failure-category", "write_verification_failed");
    await expect(failure).toContainText("final state: recovery_required");
    await expect(page.getByTestId("operation-summary")).toContainText("0 profiles applied · 2 need attention");
  });
});

test("a verified undo still reads as a green restore with zero attention", async ({ page }) => {
  await open(page);
  await applyFastLane(page);
  await expect(page.getByTestId("result-default")).toContainText("Applied + read back");

  await page.getByTestId("undo-guard").check();
  await page.getByTestId("undo-button").click();
  await expect(page.getByTestId("live-status")).toContainText("Undo finished");

  const drawer = page.getByTestId("operation-drawer");
  await expect(page.getByTestId("result-default")).toContainText("Restored (undo verified)");
  await expect(page.getByTestId("result-research")).toContainText("Restored (undo verified)");
  expect(await colorOf(drawer.locator("[data-status='compensated']").first())).toBe(OK);
  // A clean undo restored everything: nothing needs attention.
  await expect(page.getByTestId("operation-summary")).toContainText("0 profiles applied · 0 need attention");
  await expect(page.getByTestId("operation-summary")).toHaveAttribute("data-attention", "false");
  await expect(page.getByTestId("operation-state")).toContainText("Undo verified");
  // The undo path is not a failure.
  await expect(page.getByTestId("operation-failure")).toHaveCount(0);
});

test("a known no-write conflict shows a red failed step and a reachable no-change state", async ({ page }) => {
  await withFlags(page, { noWrite: true }, async () => {
    await open(page);
    await applyFastLane(page);
    const drawer = page.getByTestId("operation-drawer");

    // F1 is the first step failing its pre-write recheck (status ``failed``,
    // red) with the steps behind it never attempted (status ``pending``,
    // neutral). ``not_written`` is NOT a journal vocabulary word and must never
    // appear anywhere in the drawer.
    const failed = drawer.locator("[data-status='failed']");
    await expect(failed).toHaveCount(1);
    await expect(failed.first()).toContainText("FAILED — not verified");
    expect(await colorOf(failed.first())).toBe(DANGER);
    const pending = drawer.locator("[data-status='pending']");
    await expect(pending).toHaveCount(1);
    expect(await colorOf(pending.first())).toBe(FG);
    await expect(drawer).not.toContainText("not_written");
    await expect(drawer).not.toContainText("Applied + read back");
    await expect(drawer).not.toContainText("Restored (undo verified)");

    // The operation never wrote, so it stays retryable: the reachable no-change
    // headline rides on the op-level state line, beside the safe category.
    await expect(page.getByTestId("operation-state")).toContainText("Not changed — review again");
    await expect(page.getByTestId("operation-state")).toHaveAttribute("data-state", "pending");
    await expect(page.getByTestId("operation-summary")).toContainText("0 profiles applied · 2 need attention");
    await expect(page.getByTestId("operation-summary")).toHaveAttribute("data-attention", "true");
    const failure = page.getByTestId("operation-failure");
    await expect(failure).toContainText("Revision conflict");
    await expect(failure).toHaveAttribute("data-failure-category", "revision_conflict");
    await expect(failure).toContainText("final state: pending");
    await expect(page.getByTestId("undo-button")).toBeDisabled();
  });
});

test("an unknown per-step status renders neutral, never green", async ({ page }) => {
  await withFlags(page, { statusOverride: "quantum_flux", overrideState: "recovery_required" }, async () => {
    await open(page);
    await applyFastLane(page);
    const drawer = page.getByTestId("operation-drawer");

    const span = drawer.locator("[data-status='quantum_flux']");
    await expect(span).toHaveCount(2);
    await expect(span.first()).toContainText("quantum_flux");
    expect(await colorOf(span.first())).toBe(FG);
    await expect(drawer).not.toContainText("Applied + read back");
    await expect(drawer).not.toContainText("Restored (undo verified)");
    // An unrecognised status is still counted as needing attention.
    await expect(page.getByTestId("operation-summary")).toContainText("0 profiles applied · 2 need attention");
  });
});
