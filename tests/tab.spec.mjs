import { test, expect } from "@playwright/test";
import { setProvider, setModel } from "./route-helpers.mjs";

const URL = "/fixture.html";

async function open(page) {
  await page.goto(URL);
  await expect(page.getByTestId("live-status")).toBeAttached();
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();
}

// Computed colour of the visible status line — how the specs tell an explicit
// error kind apart from a neutral announcement.
const statusColor = (page) =>
  page.getByTestId("visible-status").evaluate((el) => getComputedStyle(el).color);

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
  // The summary counts come from the operation DTO's per-profile
  // ``results[].status`` — never from a per-step field the backend does not send.
  await expect(page.getByTestId("operation-summary")).toContainText("2 profiles applied · 0 need attention");

  // the preset apply pairs the reviewed preset with exactly this preview
  const opBodies = await page.evaluate(() =>
    window.__mockApi.requests
      .filter((r) => r.method === "POST" && r.path.endsWith("/operations"))
      .map((r) => r.body));
  expect(opBodies.length).toBe(1);
  expect(opBodies[0].presetId).toBe("p-fast");
  expect(typeof opBodies[0].previewId).toBe("string");

  // --- status comes straight from the operation DTO's results[].status ---
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
  await setProvider(page, "zhipu");
  await setModel(page, "glm-5.3-flash");
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
  await page.getByRole("button", { name: "Close operation drawer" }).click();
  await page.getByLabel("Select default main").check();
  await page.getByLabel("Select research main").check();
  await setProvider(page, "zhipu");
  await setModel(page, "glm-5.3-flash");
  await page.getByRole("checkbox", { name: /acknowledge unknown-cost/ }).check();
  await page.getByRole("button", { name: "Preview changes" }).click();
  await expect(page.getByTestId("preview-panel")).toContainText("2 changes");
  await expect(page.getByTestId("live-status")).toContainText("Preview ready");

  // The single custom Apply control must not carry the old presetId.
  await page.getByTestId("apply-custom").click();
  await expect(page.getByTestId("operation-drawer")).toContainText("Reference: op-2");
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
  await page.getByTestId("removal-advanced").locator('summary').click();
  await page.getByLabel("Exact provider/model to remove").fill("openai/gpt-5.1");
  await expect(page.getByRole("note")).toContainText("No selected configured slot matches exactly");
  await expect(page.getByRole("button", { name: "Preview changes" })).toBeDisabled();

  await page.getByLabel("Exact provider/model to remove").fill("anthropic/claude-sonnet-4-6");
  await expect(page.getByRole("note")).toContainText("1 selected configured slot");
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

  // (b) apply fails at write verification → the backend returns HTTP 200 with
  // ok:false AND the operation DTO (top-level ok is true only for
  // committed/undone). The UI must PRESERVE that DTO — render per-profile
  // FAILED, the safe failure category and the final state — instead of
  // discarding it for a generic error. No undo for a non-committed operation.
  await page.evaluate(() => { window.__mockApi.failReadback = true; });
  await page.getByRole("button", { name: /Fast lane/ }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await page.getByTestId("apply-preset").click();
  const drawer = page.getByTestId("operation-drawer");
  await expect(drawer).toBeVisible();
  await expect(page.getByTestId("result-default")).toContainText("FAILED — not verified");
  await expect(page.getByTestId("result-research")).toContainText("FAILED — not verified");
  // the safe failure category (rendered as descriptive copy, the enum kept on
  // data-failure-category) and final state come from the operation DTO
  await expect(page.getByTestId("operation-failure")).toContainText("Write verification failed");
  await expect(page.getByTestId("operation-failure")).toHaveAttribute("data-failure-category", "write_verification_failed");
  await expect(page.getByTestId("operation-failure")).toContainText("final state: recovery_required");
  await expect(page.getByTestId("operation-summary")).toContainText("0 profiles applied · 2 need attention");
  await expect(page.locator("[data-status='applied']")).toHaveCount(0);
  // Severity is explicit, not substring-matched: this summary legitimately
  // CONTAINS "failed" (per-profile statuses) but is not an app-level error, so
  // it must stay neutral instead of being painted red. It also names the
  // non-success outcome so "finished" alone never reads as success.
  await expect(page.getByTestId("live-status")).toContainText("finished");
  await expect(page.getByTestId("live-status")).toContainText("recovery_required");
  expect(await statusColor(page)).toBe("rgb(229, 83, 75)"); // explicit error kind; no regex severity
  // a failed (non-committed) operation is not undoable through the drawer, even
  // once the guard is acknowledged — the backend assembles this via ``undoable``.
  await expect(page.getByTestId("undo-button")).toBeDisabled();
  await page.getByTestId("undo-guard").check();
  await expect(page.getByTestId("undo-button")).toBeDisabled();
});

test("an undo the backend refuses is announced and never claims a restore", async ({ page }) => {
  await open(page);
  await page.getByRole("button", { name: /Fast lane/ }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await page.getByTestId("apply-preset").click();
  await expect(page.getByTestId("result-default")).toContainText("Applied + read back");

  await page.getByTestId("undo-guard").check();
  await page.evaluate(() => { window.__mockApi.failUndo = true; });
  await page.getByTestId("undo-button").click();
  await expect(page.getByTestId("live-status")).toContainText("Undo failed");
  await expect(page.locator("[data-status='compensated']")).toHaveCount(0);
  await expect(page.getByTestId("result-default")).toContainText("Applied + read back");
});

test("a blocked apply reports the engine's reason, not a bare status code", async ({ page }) => {
  await page.goto(URL);
  await page.evaluate(() => {
    sessionStorage.setItem("hms-mock-flags", JSON.stringify({ blockedApply: true }));
  });
  await page.reload();
  try {
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
    // ...and it carries the explicit error kind (danger colour), not the muted
    // neutral one — a refusal must not read like a routine announcement.
    expect(await statusColor(page)).toBe("rgb(229, 83, 75)"); // THEME.danger fallback
    await expect(page.getByTestId("operation-drawer")).toHaveCount(0);
  } finally {
    // Clear in `finally`: a failed assertion above used to leave the flag set
    // and leak the blocked-apply behaviour into every later test.
    await page.evaluate(() => sessionStorage.removeItem("hms-mock-flags"));
  }
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

test("the table caption stays screen-reader-only without a host CSS class", async ({ page }) => {
  // The host Dashboard does not define `.visually-hidden` (unlike the earlier
  // fixtures), so a class-based hide rendered the caption as a stray visible
  // line above the matrix. The hidden styles are inline now.
  await open(page);
  const cap = page.locator('[data-testid="matrix-scroll"] caption');
  await expect(cap).toHaveCount(1);
  const r = await cap.evaluate((el) => {
    const b = el.getBoundingClientRect();
    return { left: b.left, width: b.width, text: el.textContent.trim() };
  });
  expect(r.text).toContain("Model slots per profile");
  expect(r.left).toBeLessThan(-1000); // off-screen, not a visible line
  expect(r.width).toBeLessThanOrEqual(1);
});

// The host token must be in the HTML, not injected by an init script: an init
// script can run before <html> exists (setProperty then silently does nothing)
// and the plugin resolves the colour while the entry module loads.
async function serveWithSurfaceToken(page, token) {
  await page.route("**/fixture.html", async (route) => {
    const res = await route.fetch();
    const html = (await res.text()).replace("</head>", `<style>:root{--surface-color:${token}}</style></head>`);
    await route.fulfill({ response: res, body: html });
  });
}

test("a PARTIALLY transparent surface token is rejected too, not just a fully transparent one", async ({ page }) => {
  // Codex review: only "transparent"/"rgba(0,0,0,0)" were rejected, so a 50%
  // token was accepted and rows stayed visible under the sticky header.
  await serveWithSurfaceToken(page, "rgba(255, 0, 0, 0.5)");
  await page.goto(URL);
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();

  // confirm the token really reached the page, so the assertion below is not vacuous
  const resolvedToken = await page.evaluate(() => {
    const probe = document.createElement("div");
    probe.style.cssText = "position:absolute;visibility:hidden;background:var(--surface-color, #16181d)";
    document.body.appendChild(probe);
    const v = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return v;
  });
  expect(resolvedToken).toBe("rgba(255, 0, 0, 0.5)");

  const bg = await page.locator('[data-testid="matrix-scroll"] thead th').first()
    .evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(bg).toBe("rgb(22, 24, 29)"); // solid fallback, not a 50%-alpha red
});

test("a fully opaque host surface token is still honoured", async ({ page }) => {
  await serveWithSurfaceToken(page, "#223040");
  await page.goto(URL);
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();
  const bg = await page.locator('[data-testid="matrix-scroll"] thead th').first()
    .evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(bg).toBe("rgb(34, 48, 64)"); // the host's colour, not our fallback
});

test("the sticky header follows a host theme change without a reload", async ({ page }) => {
  // Codex review: the surface colour was a module-scope snapshot, so a live
  // light/dark switch left the header on the old colour (mismatched contrast).
  await open(page);
  const th = page.locator('[data-testid="matrix-scroll"] thead th').first();
  expect(await th.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe("rgb(22, 24, 29)");

  await page.evaluate(() => {
    document.documentElement.style.setProperty("--surface-color", "#223040");
  });
  await expect.poll(() => th.evaluate((el) => getComputedStyle(el).backgroundColor))
    .toBe("rgb(34, 48, 64)"); // the new host colour, applied live

  // ...and back to the solid fallback when the host makes it translucent again
  await page.evaluate(() => {
    document.documentElement.style.setProperty("--surface-color", "rgba(34, 48, 64, 0.25)");
  });
  await expect.poll(() => th.evaluate((el) => getComputedStyle(el).backgroundColor))
    .toBe("rgb(22, 24, 29)");
});

test("the sticky matrix header stays opaque when the host surface token is transparent", async ({ page }) => {
  // `var(--surface-color, fallback)` only falls back when the variable is
  // UNDEFINED. A host theme defining it as a transparent value would leave the
  // sticky header see-through, with rows scrolling visibly underneath it.
  await page.addInitScript(() => {
    document.documentElement.style.setProperty("--surface-color", "transparent");
  });
  await page.goto(URL);
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();

  const th = page.locator('[data-testid="matrix-scroll"] thead th').first();
  await expect(th).toHaveCSS("position", "sticky");
  const bg = await th.evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(bg).toBe("rgb(22, 24, 29)"); // solid fallback, never transparent
  expect(bg).not.toBe("rgba(0, 0, 0, 0)");
});

test("detail and empty-state rows span exactly the rendered column count", async ({ page }) => {
  // Regression: both used colSpan={8} while the table actually renders 7 columns
  // (a leading checkbox column + the 6 headers), so each cell over-claimed a
  // column that does not exist.
  await open(page);
  const headerCount = await page.locator('[data-testid="matrix-scroll"] thead th').count();
  expect(headerCount).toBe(7);

  // expand a slot's routing detail: its cell spans every column
  await page.getByRole("button", { name: "auxiliary.goal_judge" }).click();
  const detail = page.locator('[data-testid="matrix-scroll"] tr[data-detail] td').first();
  expect(Number(await detail.getAttribute("colspan"))).toBe(headerCount);

  // filter to zero rows: the empty-state cell spans every column too
  // Dropdown filters only offer values that exist, so reach zero rows by
  // combining a provider with a state none of its slots are in.
  await page.getByLabel("Filter by provider").selectOption("anthropic");
  const states = await page.getByLabel("Filter by state").locator("option").evaluateAll(
    (opts) => opts.map((o) => o.value).filter((v) => v !== "all"));
  for (const state of states) {
    await page.getByLabel("Filter by state").selectOption(state);
    if (await page.locator('[data-testid="matrix-scroll"] tbody tr[data-row]').count() === 0) break;
  }
  const empty = page.locator('[data-testid="matrix-scroll"] tbody td').first();
  await expect(empty).toContainText("No slots match");
  expect(Number(await empty.getAttribute("colspan"))).toBe(headerCount);
});
