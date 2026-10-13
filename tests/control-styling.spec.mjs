import { test, expect } from "@playwright/test";
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
    await page.getByLabel("New provider").fill("zhipu");
    await page.getByLabel("New model").fill("glm-5.3-flash");
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

test("manifest stylesheet is built and every rule is plugin-scoped", () => {
  const manifest = JSON.parse(fs.readFileSync("dashboard/manifest.json", "utf8"));
  expect(manifest.css).toBe("dist/style.css");
  const css = fs.readFileSync(`dashboard/${manifest.css}`, "utf8");
  expect(css).toBe(fs.readFileSync("ui/src/styles.css", "utf8"));
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const selectors = [...withoutComments.matchAll(/([^{}]+)\{/g)]
    .flatMap(match => match[1].trim().split(",").map(selector => selector.trim()));
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
