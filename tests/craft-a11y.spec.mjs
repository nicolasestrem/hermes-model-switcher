// Craft-floor + a11y seam coverage for the fix pass (FIX-LEDGER P3/P4/P5 and
// defects U1-U12). RED first: every assertion here encodes a decided fix, so
// the spec is run before the implementation lands and must fail, then green
// after. Pinned behaviours it must never break: native selects, the pinned
// first Tab target (the preset strip), editor-first DOM, seven matrix
// columns, repeated-profile identity.
import { test, expect } from "@playwright/test";
import { setProvider, setModel } from "./route-helpers.mjs";

const URL = "/fixture-sdk.html";
const BASE = "/api/plugins/hermes-model-switcher";

async function open(page, { width = 1280, height = 900 } = {}) {
  await page.setViewportSize({ width, height });
  await page.goto(URL);
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();
}

// The pinned first Tab target: the first preset button. No fix may add a
// focusable control ahead of it.
async function expectPinnedFirstTab(page) {
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
}

async function selectAndReview(page, labels, provider, model, opts) {
  for (const label of labels) await page.getByLabel(label, { exact: true }).check();
  await setProvider(page, provider);
  await setModel(page, model, opts);
  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
}

const rgb = (el, prop) => el.evaluate((node, p) => getComputedStyle(node)[p], prop);

// ---------- U1: token-based checkbox chrome, focus-visible, disabled, AA ------

for (const theme of ["dark", "light"]) {
  test(`checkboxes ship token chrome instead of UA squares (${theme})`, async ({ page }) => {
    if (theme === "light") {
      await page.route("**/fixture-sdk.html", async route => {
        const response = await route.fetch();
        const tokens = "--background:#fafafa;--midground:#151515;--text-muted-color:#595959;--color-border:#777;--color-ring:#0055aa";
        await route.fulfill({ response, body: (await response.text()).replace("</head>", `<style>:root{${tokens}}</style></head>`) });
      });
    }
    await open(page);
    await expectPinnedFirstTab(page);

    const row = page.getByLabel("Select default main", { exact: true });
    const unchecked = await row.evaluate(el => {
      const c = getComputedStyle(el);
      return { appearance: c.appearance, bg: c.backgroundColor, borderWidth: parseFloat(c.borderTopWidth), cursor: c.cursor };
    });
    // appearance:auto paints the UA's white square on dark surfaces.
    expect(unchecked.appearance).toBe("none");
    expect(unchecked.bg).toBe(theme === "light" ? "rgb(250, 250, 250)" : "rgb(27, 30, 36)");
    expect(unchecked.borderWidth).toBeGreaterThanOrEqual(1);
    expect(unchecked.cursor).toBe("pointer");

    // The disabled read-only checkbox stays visibly distinct, not just inert.
    const readonly = page.getByLabel("Select archived main", { exact: true });
    await expect(readonly).toBeDisabled();
    const disabledOpacity = Number(await readonly.evaluate(el => getComputedStyle(el).opacity));
    expect(disabledOpacity).toBeLessThan(1);

    // Checked: the box itself changes to the accent so the state is never
    // colour-alone, and the focus ring survives the custom chrome.
    await row.check();
    const checked = await row.evaluate(el => {
      const c = getComputedStyle(el);
      return { bg: c.backgroundColor, borderColor: c.borderTopColor };
    });
    const accent = theme === "light" ? "rgb(0, 85, 170)" : "rgb(111, 155, 255)";
    expect(checked.bg).toBe(accent);
    expect(checked.borderColor).toBe(accent);
    const focus = await row.evaluate(el => {
      el.blur(); // the check() click left it focused; focus() is a no-op then
      el.focus({ focusVisible: true });
      const c = getComputedStyle(el);
      return { style: c.outlineStyle, width: parseFloat(c.outlineWidth) };
    });
    expect(focus.style).not.toBe("none");
    expect(focus.width).toBeGreaterThanOrEqual(2);

    // Indeterminate bulk control paints its own dash, not a UA tristate.
    await page.getByLabel("Select research main", { exact: true }).check();
    const master = page.getByTestId("select-all");
    await expect(master).toHaveJSProperty("indeterminate", true);
    const ind = await master.evaluate(el => ({ bg: getComputedStyle(el).backgroundColor, appearance: getComputedStyle(el).appearance }));
    expect(ind.appearance).toBe("none");
    expect(ind.bg).toBe(accent);
  });
}

// ---------- U2: route ids never split mid-token in the preview --------------

test("preview route ids stay whole at 390px", async ({ page }) => {
  await open(page, { width: 390, height: 844 });
  await selectAndReview(page, ["Select default main"], "zhipu", "glm-5.3-flash-preview-token");
  const panel = page.getByTestId("preview-panel");
  const ids = panel.locator(".hms-route-id");
  await expect(ids.first()).toBeVisible();
  const count = await ids.count();
  expect(count).toBeGreaterThan(0);
  for (const [text, wrap, rects] of await ids.evaluateAll(nodes => nodes.map(node => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return [node.textContent, getComputedStyle(node).whiteSpace, range.getClientRects().length];
  }))) {
    expect(wrap).toBe("nowrap");
    expect(rects, text).toBe(1);
  }
});

// ---------- U3: informational side-effect prose is muted --------------------

test("side-effect notes read as information, not warnings", async ({ page }) => {
  await open(page);
  await selectAndReview(page, ["Select default main"], "zhipu", "glm-5.3-flash");
  const note = page.getByTestId("preview-panel").locator("li").first()
    .locator("span").filter({ hasText: "canonical tool defaults seeded" });
  await expect(note).toBeVisible();
  expect(await rgb(note, "color")).toBe("rgb(154, 161, 173)"); // muted token
});

// ---------- U4: narrow viewports unstick the select column ------------------

test("at 390px the select column unsticks while header and profile stay pinned", async ({ page }) => {
  await open(page, { width: 390, height: 844 });
  const matrix = page.getByTestId("matrix-scroll");
  const row = matrix.locator('[data-row="default/main"]');
  const selectTd = row.locator('[data-column="select"]');
  const profileTd = row.locator('[data-column="profile"]');
  expect(await selectTd.evaluate(el => getComputedStyle(el).position)).toBe("static");
  expect(await profileTd.evaluate(el => getComputedStyle(el).position)).toBe("sticky");
  expect(await profileTd.evaluate(el => getComputedStyle(el).left)).toBe("0px");
  const headerTh = matrix.locator("thead th").first();
  const profileTh = matrix.locator("thead th").nth(1);
  expect(await headerTh.evaluate(el => getComputedStyle(el).position)).toBe("sticky"); // vertical pin kept
  expect(await profileTh.evaluate(el => getComputedStyle(el).position)).toBe("sticky");
  // Route content keeps its guaranteed floor even while the profile pins.
  const route = row.locator('[data-column="configured"]');
  expect(await route.evaluate(el => parseFloat(getComputedStyle(el).minWidth))).toBeGreaterThanOrEqual(240);
});

// ---------- U5: pluralized summary ------------------------------------------

test("a single-profile apply reads '1 profile applied'", async ({ page }) => {
  await open(page);
  await selectAndReview(page, ["Select default main"], "zhipu", "glm-5.3-flash");
  await page.getByTestId("apply-custom").click();
  await expect(page.getByTestId("operation-drawer")).toBeVisible();
  await expect(page.getByTestId("operation-summary")).toContainText("1 profile applied · 0 need attention");
});

// ---------- U7: presets strip is a real list --------------------------------

test("the presets strip owns listitem children, not bare buttons", async ({ page }) => {
  await open(page);
  const list = page.locator('[aria-label="Favorite presets"] ul');
  await expect(list).toBeVisible();
  const buttons = list.locator("li > button");
  await expect(buttons).toHaveCount(1);
  const structure = await list.evaluate(el => ({
    children: Array.from(el.children).map(child => child.tagName + ":" + (child.getAttribute("role") || "")),
    bareButtons: Array.from(el.children).filter(child => child.tagName === "BUTTON").length,
  }));
  expect(structure.bareButtons).toBe(0);
  expect(structure.children[0]).toBe("LI:");
  // The strip keeps the pinned first Tab stop.
  await expectPinnedFirstTab(page);
});

// ---------- U8: inline border fallback matches the stylesheet ----------------

test("the matrix border fallback resolves to the stylesheet fallback", async ({ page }) => {
  await open(page); // fixture defines no border tokens at all
  const row = page.locator('[data-row="default/main"]');
  expect(await rgb(row, "borderBottomColor")).toBe("rgb(58, 63, 72)"); // #3a3f48
});

// ---------- U9 + A2: gate status is live and programmatically described ------

test("the apply gate announces politely and associates its reasons", async ({ page }) => {
  await open(page);
  await page.getByLabel("Select default main", { exact: true }).check();
  const gate = page.locator("#hms-apply-gate");
  await expect(gate).toBeVisible();
  await expect(gate).toHaveAttribute("role", "status");
  await expect(gate).toHaveAttribute("aria-live", "polite");
  const apply = page.getByTestId("apply-custom");
  // Apply stays focusable while aria-disabled (DESIGN §5) — never a dead button.
  await expect(apply).toHaveAttribute("aria-disabled", "true");
  await expect(apply).not.toHaveAttribute("disabled");
  await apply.focus();
  await expect(apply).toBeFocused();
  await expect(apply).toHaveAttribute("aria-describedby", "hms-apply-gate");
  // A reviewed-but-unacknowledged cost note is associated too.
  await setProvider(page, "zhipu");
  await setModel(page, "glm-5.3-flash");
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await expect(page.locator("#hms-apply-cost-note")).toBeVisible();
  await expect(apply).toHaveAttribute("aria-describedby", /hms-apply-gate\s+hms-apply-cost-note/);
  // The disabled select-all names the count line that explains why.
  const selectAll = page.getByTestId("select-all");
  await expect(selectAll).toHaveAttribute("aria-describedby", "hms-matrix-count");
  // A read-only row checkbox is described by its visible reason.
  const archivedRow = page.locator('tr[data-row="archived/main"]');
  const reason = archivedRow.getByTestId("readonly-reason");
  await expect(reason).toBeVisible();
  const reasonId = await reason.evaluate(el => el.id);
  expect(reasonId).toBeTruthy();
  const readonlyBox = page.getByLabel("Select archived main", { exact: true });
  await expect(readonlyBox).toBeDisabled();
  await expect(readonlyBox).toHaveAttribute("aria-describedby", reasonId);
});

// ---------- U10: inventory warnings are polite -------------------------------

// The in-page mock must be neutralized so the network-level /state route
// below is the one source (same pattern as polish-contract's scene()).
test("inventory warnings are polite status, not interrupts", async ({ page }) => {
  await page.route("**/tests/mock-api.js", route => route.fulfill({ body: "", contentType: "application/javascript" }));
  await page.route("**" + BASE + "/state", route => route.fulfill({ json: {
    ok: true,
    profiles: [
      { id: "default", name: "Default", writable: true },
      { id: "broken", name: "Broken", writable: false, inventoryWarnings: ["Auxiliary task discovery unavailable."] },
    ],
    slots: [{ profileId: "default", slotId: "main", writable: true, state: "configured", provider: "openai", model: "m" }],
    presets: [],
  } }));
  await page.goto(URL);
  const warnings = page.getByTestId("inventory-warnings");
  await expect(warnings).toBeVisible();
  await expect(warnings).toHaveAttribute("role", "status");
  await expect(warnings).toHaveAttribute("aria-live", "polite");
});

// ---------- U11 + U12: type scale and muted fallback ------------------------

test("catalog summary uses the 14/13 scale", async ({ page }) => {
  await open(page);
  await page.getByTestId("catalog-toggle").click();
  await expect(page.getByTestId("catalog-table")).toBeVisible();
  const dd = page.locator(".hms-catalog-summary dd").first();
  expect(await dd.evaluate(el => parseFloat(getComputedStyle(el).fontSize))).toBe(14);
  expect(await dd.locator("span").evaluate(el => parseFloat(getComputedStyle(el).fontSize))).toBe(13);
});

test("a legacy --text-color host keeps notes muted", async ({ page }) => {
  await page.route("**/fixture-sdk.html", async route => {
    const response = await route.fetch();
    const html = (await response.text()).replace("</head>",
      `<style>:root{--text-color:#ff0000;--surface-color:#16181d}</style></head>`);
    await route.fulfill({ response, body: html });
  });
  await page.goto(URL);
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();
  const note = page.locator(".hms-workspace-tools > .hms-note");
  await expect(note).toBeVisible();
  expect(await rgb(note, "color")).toBe("rgb(154, 161, 173)"); // muted fallback, never full-strength text
});

// ---------- U6 + drawer coverage: entry focus, wrap, opener return ----------

test("drawer focus enters a visible control, wraps, and returns to the opener", async ({ page }) => {
  await open(page);
  await page.getByRole("button", { name: /Fast lane/ }).click();
  await page.getByTestId("apply-preset").click();
  const drawer = page.getByTestId("operation-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer).toHaveAttribute("role", "dialog");
  await expect(drawer).toHaveAttribute("aria-modal", "true");

  // Focus entry lands on the Close control — never an outline:none container.
  const close = page.getByRole("button", { name: "Close operation drawer" });
  await expect(close).toBeFocused();

  // Shift+Tab from the first control wraps to the last focusable inside.
  await page.keyboard.press("Shift+Tab");
  const lastFocused = await drawer.evaluate(el => {
    const focusables = Array.from(el.querySelectorAll('button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])'))
      .filter(node => !node.disabled && node.offsetParent !== null);
    return focusables[focusables.length - 1] === document.activeElement;
  });
  expect(lastFocused).toBe(true);
  await page.keyboard.press("Tab");
  await expect(close).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);

  // Reopen from the toolbar: opener return must land back on that button.
  const showLast = page.getByRole("button", { name: "Show last operation" });
  await showLast.click();
  await expect(drawer).toBeVisible();
  await expect(close).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(showLast).toBeFocused();
});

// ---------- A1: live regions exist before their first message ---------------

test("live regions mount empty before their first message", async ({ page }) => {
  await open(page);
  // Apply progress region is present and idle-quiet before any apply.
  const progress = page.getByTestId("apply-progress");
  await expect(progress).toHaveCount(1);
  await expect(progress).toHaveAttribute("aria-live", "polite");
  await expect(progress).toHaveAttribute("role", "status");
  await expect(progress.locator("li")).toHaveCount(0);

  // Host notice region exists from the first paint (empty until set).
  const notice = page.getByTestId("host-notice");
  await expect(notice).toHaveCount(1);
  await expect(notice).toHaveAttribute("aria-live", "polite");

  // Undo reason region exists inside an open drawer before a reason appears.
  await page.getByRole("button", { name: /Fast lane/ }).click();
  await page.getByTestId("apply-preset").click();
  await expect(page.getByTestId("operation-drawer")).toBeVisible();
  const undoReason = page.getByTestId("undo-unavailable");
  await expect(undoReason).toHaveCount(1);
  await expect(undoReason).toHaveAttribute("aria-live", "polite");
  await expect(undoReason).toHaveText("");
});

// ---------- coverage: both dialogs expose dialog semantics -------------------

test("the host confirmation dialog is a modal dialog", async ({ page }) => {
  await open(page);
  await selectAndReview(page, ["Select default main"], "nous", "hermes-4-405b-max", { exact: true });
  await page.getByTestId("apply-custom").click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute("role", "dialog");
  await expect(dialog).toHaveAttribute("aria-modal", "true");
});
