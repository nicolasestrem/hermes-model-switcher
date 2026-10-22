import { test, expect } from "@playwright/test";
import { realisticFixture } from "./fixtures/catalog-realistic.js";

// Layout contracts for the catalog, measured rather than eyeballed. The visual
// QA pass found the Action column pushed outside a 708px scroll region at
// 768px (the table's 780px min-width), "unknown" wrapping mid-word, the Sort
// select flush to the panel edge, group headers disappearing under the sticky
// column header, and a closed disclosure keeping a laid-out box. Each check
// below fails if any of those come back.

async function openCatalogAt(page, width, height = 900) {
  await page.setViewportSize({ width, height });
  await page.addInitScript((payload) => { window.__HMS_CATALOG_FIXTURE = payload; }, realisticFixture());
  await page.goto("/fixture.html");
  await page.evaluate(() => sessionStorage.setItem(
    "hms-mock-flags", JSON.stringify({ catalogRealistic: true })));
  await page.reload();
  await expect(page.getByLabel("New provider", { exact: true })).toBeVisible();
  await page.getByTestId("catalog-toggle").click();
  await expect(page.getByTestId("catalog-table")).toBeVisible();
  await page.locator('[data-testid="catalog-scroll"]').scrollIntoViewIfNeeded();
}

// Everything the table must satisfy to be usable: the Action column inside the
// visible scroll box, no mid-word status wrap, no sideways page overflow.
async function tableFaults(page) {
  return page.evaluate(() => {
    const box = document.querySelector('[data-testid="catalog-scroll"]');
    const region = box.getBoundingClientRect();
    const uses = [...document.querySelectorAll('[data-column="use"] button')];
    const escaped = uses.filter((button) => {
      const r = button.getBoundingClientRect();
      return r.right > region.right + 0.5 || r.left < region.left - 0.5;
    });
    const wrappedStatus = [...document.querySelectorAll('[data-column="status"]')]
      .filter((cell) => {
        const range = document.createRange();
        range.selectNodeContents(cell);
        return range.getClientRects().length > 1 || cell.scrollWidth > cell.clientWidth + 1;
      })
      .map((cell) => cell.textContent);
    return {
      escaped: escaped.length,
      useCount: uses.length,
      wrappedStatus,
      horizontalOverflow: box.scrollWidth - box.clientWidth,
      documentOverflow: document.documentElement.scrollWidth - window.innerWidth,
    };
  });
}

for (const width of [768, 1440]) {
  test(`catalog table is fully usable at ${width}px`, async ({ page }) => {
    await openCatalogAt(page, width);
    const faults = await tableFaults(page);
    expect(faults.useCount).toBeGreaterThan(0);
    // The Action column is inside the scroll box at rest: no hidden controls.
    expect(faults.escaped, "Use buttons outside the scroll region").toBe(0);
    // No sideways scroll is needed at these widths at all.
    expect(faults.horizontalOverflow, "catalog table overflows its scroll box").toBeLessThanOrEqual(1);
    // Short status tokens never break mid-word.
    expect(faults.wrappedStatus).toEqual([]);
    expect(faults.documentOverflow).toBeLessThanOrEqual(0);
  });
}

test("a pane too narrow for the columns keeps the Action column pinned", async ({ page }) => {
  // The table fits its pane whenever the columns allow; below that the region
  // scrolls sideways, and the pinned Use column keeps the row's action inside
  // the visible box (no scrollbar hunt).
  await openCatalogAt(page, 620);
  const faults = await tableFaults(page);
  expect(faults.useCount).toBeGreaterThan(0);
  expect(faults.escaped, "Use buttons outside the scroll region").toBe(0);
  expect(faults.wrappedStatus).toEqual([]);
  expect(faults.documentOverflow).toBeLessThanOrEqual(0);
});

test("the Sort control keeps room and a gutter from the panel edge", async ({ page }) => {
  await openCatalogAt(page, 1440);
  const box = await page.evaluate(() => {
    const sort = document.querySelector('[data-testid="catalog-sort"]').getBoundingClientRect();
    const controls = document.querySelector(".hms-catalog-controls").getBoundingClientRect();
    const label = document.querySelector('[data-testid="catalog-sort"]').closest("label");
    return { width: sort.width, right: sort.right, controlsRight: controls.right,
      clipped: label.scrollWidth - label.clientWidth };
  });
  // Wide enough for "Price ↑" / "Provider" without truncation…
  expect(box.width).toBeGreaterThanOrEqual(120);
  expect(box.clipped).toBeLessThanOrEqual(1);
  // …and not flush against the panel edge.
  expect(box.controlsRight - box.right).toBeGreaterThanOrEqual(4);
});

test("group headers stay visible under the sticky column header", async ({ page }) => {
  await openCatalogAt(page, 1440);
  await page.evaluate(() => { document.querySelector('[data-testid="catalog-scroll"]').scrollTop = 400; });
  await expect.poll(() => page.evaluate(
    () => document.querySelector('[data-testid="catalog-scroll"]').scrollTop)).toBe(400);

  const geo = await page.evaluate(() => {
    const box = document.querySelector('[data-testid="catalog-scroll"]');
    const region = box.getBoundingClientRect();
    // The column header is the sticky CELL (the <thead> box itself scrolls).
    const thead = box.querySelector("thead th").getBoundingClientRect();
    const heads = [...box.querySelectorAll(".hms-catalog-group th")].map((th) => ({
      group: th.closest("tbody").dataset.providerGroup,
      top: th.getBoundingClientRect().top,
      bottom: th.getBoundingClientRect().bottom,
    }));
    // The header pinned directly under the column header (not under it, not
    // still at its static position further down).
    const pinned = heads.find((head) => head.top >= thead.bottom - 1 && head.top <= thead.bottom + 2);
    if (!pinned) return { pinned: null };
    const rows = [...box.querySelectorAll("[data-model-row]")].map((row) => ({
      group: row.closest("tbody").dataset.providerGroup,
      top: row.getBoundingClientRect().top,
    }));
    const firstBelow = rows.find((row) => row.top >= pinned.bottom - 1);
    return {
      pinned: pinned.group,
      headTop: pinned.top,
      theadTop: thead.top,
      regionTop: region.top,
      theadBottom: thead.bottom,
      regionBottom: region.bottom,
      firstBelow: firstBelow ? firstBelow.group : null,
    };
  });
  // The column header is pinned at the top of the scroll region…
  expect(geo.theadTop).toBeGreaterThanOrEqual(geo.regionTop - 1);
  expect(geo.pinned, "no group header is pinned under the column header").not.toBeNull();
  // The header sits below the column header (never underneath it)…
  expect(geo.headTop).toBeGreaterThanOrEqual(geo.theadBottom - 1);
  expect(geo.headTop).toBeLessThan(geo.regionBottom);
  // …and every row beneath it belongs to that group, so no row is orphaned.
  expect(geo.firstBelow).toBe(geo.pinned);
});

test("the closed Advanced disclosure takes no layout space", async ({ page }) => {
  await openCatalogAt(page, 1440);
  const box = await page.evaluate(() => {
    const details = document.querySelector('[data-testid="removal-advanced"]');
    const summary = details.querySelector("summary").getBoundingClientRect();
    const hidden = [...details.querySelectorAll("label, input, select")];
    const tools = document.querySelector(".hms-catalog-tools").getBoundingClientRect();
    return {
      open: details.open,
      height: details.getBoundingClientRect().height,
      summaryHeight: summary.height,
      bottom: details.getBoundingClientRect().bottom,
      toolsTop: tools.top,
      laidOutHiddenChildren: hidden.filter((el) => el.getBoundingClientRect().height > 0).length,
    };
  });
  expect(box.open).toBe(false);
  // Closed: only the summary line is laid out.
  expect(box.height - box.summaryHeight).toBeLessThanOrEqual(1);
  expect(box.laidOutHiddenChildren).toBe(0);
  // Nothing hangs over the tools row underneath.
  expect(box.bottom).toBeLessThanOrEqual(box.toolsTop + 1);
});
