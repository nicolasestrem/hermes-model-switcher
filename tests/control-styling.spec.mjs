import { test, expect } from "@playwright/test";
import { setProvider, setModel } from "./route-helpers.mjs";
import fs from "node:fs";

const metrics = (el) => {
  const c = getComputedStyle(el);
  return { border: parseFloat(c.borderTopWidth), padding: parseFloat(c.paddingLeft),
    height: el.getBoundingClientRect().height, bg: c.backgroundColor, opacity: c.opacity,
    outline: c.outlineStyle, outlineWidth: parseFloat(c.outlineWidth) };
};

for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
  test(`controls ship their own chrome under host reset at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto("/fixture-sdk.html");
    await expect(page.getByLabel("New provider")).toBeVisible();
    for (const control of [page.getByLabel("New provider"), page.getByLabel("New model"),
      page.getByLabel("Filter by family"), page.getByRole("button", { name: "Preview changes" })]) {
      const c = await control.evaluate(metrics);
      expect(c.border).toBeGreaterThanOrEqual(1);
      expect(c.padding).toBeGreaterThanOrEqual(6);
      expect(c.height).toBeGreaterThanOrEqual(36);
      expect(c.bg).not.toBe("rgba(0, 0, 0, 0)");
    }
    const preview = page.getByRole("button", { name: "Preview changes" });
    const before = await preview.evaluate(metrics);
    await expect(preview).toBeDisabled();
    await page.getByLabel("Select default main").check();
    await setProvider(page, "zhipu");
    await setModel(page, "glm-5.3-flash");
    await expect(preview).toBeEnabled();
    const enabled = await preview.evaluate(metrics);
    expect(Number(before.opacity)).toBeLessThan(Number(enabled.opacity));
    await preview.focus();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Shift+Tab");
    const focus = await preview.evaluate(metrics);
    expect(focus.outline).not.toBe("none");
    expect(focus.outlineWidth).toBeGreaterThanOrEqual(2);
    await preview.click();
    await expect(page.getByTestId("preview-panel")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  });
}

test("manifest stylesheet is built and every rule is plugin-scoped", async ({ page }) => {
  const manifest = JSON.parse(fs.readFileSync("dashboard/manifest.json", "utf8"));
  expect(manifest.css).toBe("dist/style.css");
  const css = fs.readFileSync(`dashboard/${manifest.css}`, "utf8");
  expect(css).toBe(fs.readFileSync("ui/src/styles.css", "utf8"));
  // Parse grouping rules too: a media query is not itself a selector, but
  // every nested style rule must still be confined to the plugin root.
  const selectors = await page.evaluate(source => {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(source);
    const collect = rules => Array.from(rules).flatMap(rule =>
      rule.selectorText ? rule.selectorText.split(",").map(selector => selector.trim())
        : rule.cssRules ? collect(rule.cssRules) : []);
    return collect(sheet.cssRules);
  }, css);
  expect(selectors.length).toBeGreaterThan(0);
  for (const selector of selectors) expect(selector.startsWith(".hms-root")).toBe(true);
});

test("transparent host overlay never becomes invisible plugin text", async ({ page }) => {
  await page.route("**/fixture-sdk.html", async route => {
    const response = await route.fetch();
    const html = (await response.text()).replace("</head>", `<style>:root{--background:#041c1c;--midground:#ffe6cb;--foreground:transparent}</style></head>`);
    await route.fulfill({ response, body: html });
  });
  await page.goto("/fixture-sdk.html");
  await expect(page.getByLabel("New provider")).toHaveCSS("color", "rgb(255, 230, 203)");
  await expect(page.getByLabel("New provider")).toHaveCSS("background-color", "rgb(4, 28, 28)");
  await expect(page.locator("thead th").first()).toHaveCSS("background-color", "rgb(4, 28, 28)");
});

test("modern theme overrides a stale opaque legacy surface", async ({ page }) => {
  await page.route("**/fixture-sdk.html", async route => {
    const response = await route.fetch();
    const html = (await response.text()).replace("</head>", `<style>:root{--background:#fafafa;--midground:#151515;--surface-color:#16181d;--color-border:#777;--color-ring:#0055aa}</style></head>`);
    await route.fulfill({ response, body: html });
  });
  await page.goto("/fixture-sdk.html");
  await expect(page.locator("thead th").first()).toHaveCSS("background-color", "rgb(250, 250, 250)");
  await expect(page.getByLabel("New provider")).toHaveCSS("border-top-color", "rgb(119, 119, 119)");
});

for (const token of ["oklch(60% 0.1 200 / 0.5)", "rgba(240,240,240,0.5)"]) {
  test(`light host retains opaque readable header for ${token}`, async ({ page }) => {
    await page.route("**/fixture-sdk.html", async route => {
      const response = await route.fetch();
      const html = (await response.text()).replace("</head>", `<style>:root {--background:#fafafa;--midground:#151515;--text-color:#151515;--surface-color:${token}}body{background:#fafafa;color:#151515}</style></head>`);
      await route.fulfill({ response, body: html });
    });
    await page.goto("/fixture-sdk.html");
    const th = page.locator("thead th").first();
    await expect(th).toBeVisible();
    const c = await th.evaluate(el => ({ bg: getComputedStyle(el).backgroundColor, fg: getComputedStyle(el).color }));
    expect(c.bg).toBe("rgb(250, 250, 250)");
    expect(c.fg).toBe("rgb(21, 21, 21)");
  });
}

// The native route and filter dropdowns, and the exact-id field revealed by
// "Other", carry the same plugin-owned chrome as every other control: real
// borders/padding/38px targets and the global focus-visible ring.
test("route dropdowns and exact-id fields keep plugin-owned chrome", async ({ page }) => {
  await page.goto("/fixture-sdk.html");
  const provider = page.getByLabel("New provider", { exact: true });
  await expect(provider).toBeVisible();
  for (const control of [provider, page.getByLabel("New model", { exact: true }),
    page.getByLabel("Filter by provider", { exact: true }), page.getByLabel("Filter by model", { exact: true })]) {
    expect(await control.evaluate(el => el.tagName)).toBe("SELECT");
    const c = await control.evaluate(metrics);
    expect(c.border).toBeGreaterThanOrEqual(1);
    expect(c.padding).toBeGreaterThanOrEqual(6);
    expect(c.height).toBeGreaterThanOrEqual(36);
    expect(c.bg).not.toBe("rgba(0, 0, 0, 0)");
  }

  await provider.focus();
  const ring = await provider.evaluate(metrics);
  expect(ring.outline).not.toBe("none");
  expect(ring.outlineWidth).toBeGreaterThanOrEqual(2);

  await provider.selectOption("__other__");
  const exact = page.getByLabel("Exact provider slug", { exact: true });
  await expect(exact).toBeVisible();
  const e = await exact.evaluate(metrics);
  expect(e.border).toBeGreaterThanOrEqual(1);
  expect(e.padding).toBeGreaterThanOrEqual(6);
  expect(e.height).toBeGreaterThanOrEqual(36);
  expect(e.bg).not.toBe("rgba(0, 0, 0, 0)");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
});
