// Regression guard: render the plugin under the REAL host dashboard stylesheet
// (served from the pinned Hermes checkout by tests/server.mjs) with catalog
// data shaped like real providers (label === id).
//
// Why this exists: 0.3.x shipped a catalog where every row showed the model id
// twice inside a solid bar. The fixtures hand-copied a subset of the host
// preflight and the mock always sent distinct labels, so neither the host's
// bare `code` chip rule nor the label === id case was ever rendered in a test.
import { test, expect } from "@playwright/test";

async function openCatalogUnderHostCss(page, { idLabels = true } = {}) {
  await page.route("**/fixture.html", async (route) => {
    const response = await route.fetch();
    const html = (await response.text()).replace(
      '<link rel="stylesheet" href="dashboard/dist/style.css">',
      '<link rel="stylesheet" href="/host/index.css">\n<link rel="stylesheet" href="dashboard/dist/style.css">',
    );
    await route.fulfill({ response, body: html });
  });
  const hostCss = await page.request.get("/host/index.css");
  expect(hostCss.status(), await hostCss.text().catch(() => "")).toBe(200);
  await page.goto("/fixture.html");
  await page.evaluate((flag) => {
    window.__mockApi.catalogPortalModels = true;
    window.__mockApi.catalogIdLabels = flag;
  }, idLabels);
  await page.getByTestId("catalog-toggle").click();
  await expect(page.getByTestId("catalog-table")).toBeVisible();
  await expect(page.locator("[data-model-row]").first()).toBeVisible();
}

for (const idLabels of [true, false]) {
  test(`catalog rows stay single-line and legible under host CSS (label ${idLabels ? "=" : "≠"} id)`, async ({ page }) => {
    await openCatalogUnderHostCss(page, { idLabels });

    const rows = await page.locator("[data-model-row]").evaluateAll((trs) => trs.map((tr) => {
      const cell = tr.querySelector('td[data-column="model"]');
      const id = tr.dataset.modelId;
      const text = cell.innerText;
      return {
        id,
        idCount: text.split(id).length - 1,
        // Model-cell CONTENT height: the Use cell may legitimately add a
        // "sign-in needed" reason line, so the row height is not the signal.
        height: (() => { const r = document.createRange(); r.selectNodeContents(cell); return r.getBoundingClientRect().height; })(),
        hasIdLine: !!cell.querySelector(".hms-catalog-model-id"),
        // Rendered text lines in the model cell (block children stack).
        modelLines: [...cell.children].filter((el) => el.getClientRects().length
          && getComputedStyle(el).display === "block").length,
      };
    }));
    expect(rows.length).toBeGreaterThan(3);
    for (const row of rows) {
      if (idLabels) {
        expect(row.idCount, `${row.id} rendered more than once`).toBe(1);
        expect(row.hasIdLine, `${row.id} kept a redundant id line`).toBe(false);
        expect(row.modelLines, `${row.id} model cell stacks ${row.modelLines} lines`).toBe(1);
        expect(row.height, `${row.id} model cell too tall`).toBeLessThanOrEqual(30);
      } else {
        expect(row.hasIdLine, `${row.id} lost its id line`).toBe(true);
        expect(row.modelLines, `${row.id} model cell`).toBe(2);
        expect(row.height, `${row.id} model cell too tall`).toBeLessThanOrEqual(56);
      }
    }

    // No host chip styling may leak into plugin <code>, and no plugin text may
    // paint the same colour as its own background (the "solid bar" failure).
    const leaks = await page.locator(".hms-root").evaluate((root) => {
      const bad = [];
      for (const el of root.querySelectorAll("code")) {
        const bg = getComputedStyle(el).backgroundColor;
        if (bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") bad.push(`code bg ${bg}: ${el.textContent}`);
      }
      for (const el of root.querySelectorAll("*")) {
        const own = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
        if (!own) continue;
        const cs = getComputedStyle(el);
        if (cs.backgroundColor !== "rgba(0, 0, 0, 0)" && cs.backgroundColor === cs.color) {
          bad.push(`invisible text (${cs.color} on ${cs.backgroundColor}): ${el.textContent.trim().slice(0, 40)}`);
        }
      }
      return bad;
    });
    expect(leaks).toEqual([]);
  });
}

test("catalog under host CSS matches the approved screenshot", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await openCatalogUnderHostCss(page);
  await expect(page.getByTestId("catalog-table")).toHaveScreenshot("catalog-host-css.png", {
    maxDiffPixelRatio: 0.01,
    animations: "disabled",
  });
});
