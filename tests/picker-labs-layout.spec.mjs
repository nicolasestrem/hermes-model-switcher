import { test, expect } from "@playwright/test";
import { modelSelect, providerSelect, ensureCatalog } from "./route-helpers.mjs";
import { installPickerMultilab } from "./fixtures/picker-multilab.js";

// Layout check for the Lab select + filter: the two controls sit on their own
// row above Model, controls stay at least 38px tall and focusable, and the page
// never scrolls horizontally at tablet/phone widths (DESIGN §4).
for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 900 }, { width: 375, height: 812 }]) {
  test(`lab and filter fit and stay focusable at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto("/fixture.html");
    await expect(providerSelect(page)).toBeVisible();
    await installPickerMultilab(page);
    await ensureCatalog(page);
    await providerSelect(page).selectOption("openrouter");

    const lab = page.getByTestId("lab-select");
    const filter = page.getByTestId("model-filter");
    await expect(lab).toBeVisible();
    await expect(filter).toBeVisible();
    for (const control of [lab, filter, modelSelect(page)]) {
      const box = await control.boundingBox();
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(viewport.width + 0.5);
      expect(box.height).toBeGreaterThanOrEqual(36);
      await control.focus();
      const ring = await control.evaluate((el) => {
        const css = getComputedStyle(el);
        return { style: css.outlineStyle, width: parseFloat(css.outlineWidth) };
      });
      expect(ring.style).not.toBe("none");
      expect(ring.width).toBeGreaterThanOrEqual(2);
    }
    // Reading order: provider → lab → filter → model.
    const order = await page.evaluate(() => {
      const ids = ["provider-select", "lab-select", "model-filter", "model-select"];
      const nodes = ids.map((id) => document.querySelector(`[data-testid="${id}"]`));
      return nodes.every((n, i) => n && (i === 0 || nodes[i - 1].compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING));
    });
    expect(order).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    // Keyboard selection still works: the select is the browser's own control.
    await lab.focus();
    await lab.selectOption("anthropic");
    await expect(modelSelect(page).locator("option").first()).toHaveText("Choose a model (9 of 30)…");
    await page.screenshot({ path: `test-results/picker-${viewport.width}.png`, fullPage: false });
  });
}

test("the editor rail has room for the filter placeholder and long ids at 1440px", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/fixture.html");
  await expect(providerSelect(page)).toBeVisible();
  await installPickerMultilab(page);
  await ensureCatalog(page);
  await providerSelect(page).selectOption("openrouter");

  const geometry = await page.evaluate(() => {
    const editor = document.querySelector('[data-testid="change-editor"]');
    const filter = document.querySelector('[data-testid="model-filter"]');
    return {
      editor: editor.getBoundingClientRect().width,
      filter: filter.getBoundingClientRect().width,
      wide: document.querySelector(".hms-workspace").classList.contains("hms-wide"),
      documentOverflow: document.documentElement.scrollWidth - innerWidth,
    };
  });
  expect(geometry.wide).toBe(true);
  // The rail keeps its DESIGN.md bound (~360px; usability-polish asserts
  // <= 380). Lab and Filter stack inside it instead of sharing a row, so the
  // filter gets the full rail width and "Match a model id or name" is not clipped.
  expect(geometry.editor).toBeLessThanOrEqual(380);
  expect(geometry.filter).toBeGreaterThanOrEqual(240);
  expect(geometry.documentOverflow).toBeLessThanOrEqual(0);

  // A namespaced id is never lost to the narrow control: the Route line shows
  // it whole and wraps instead of clipping.
  await modelSelect(page).selectOption("anthropic/claude-opus-5.5");
  const route = page.getByTestId("route-summary");
  await expect(route).toContainText("anthropic/claude-opus-5.5");
  const line = await route.evaluate((node) => ({
    scrollWidth: node.scrollWidth, clientWidth: node.clientWidth,
  }));
  expect(line.scrollWidth).toBeLessThanOrEqual(line.clientWidth + 1);
});
