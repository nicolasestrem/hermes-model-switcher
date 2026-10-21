// Publication captures for the 0.3.0 Hermes plugin-catalog listing.
//
// OPT-IN ONLY. The normal suite must never rewrite tracked images, so every
// test here skips unless HMS_WRITE_CATALOG_CAPTURES=1. When it does run it
// still asserts the things the evidence claims: no console errors, no failed
// requests, no document horizontal overflow at the captured viewport.
//
// Boundary (same as visual-evidence.spec.mjs): the compiled plugin is served
// inside the Dashboard SDK fixture, but every request goes to the plugin's real
// backend and to Hermes' own /api/model/* routers over a throwaway lab home.
// Nothing here is a live Dashboard deployment or provider inference.
import { test, expect } from "@playwright/test";
import { setProvider, setModel } from "./route-helpers.mjs";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const ENABLED = process.env.HMS_WRITE_CATALOG_CAPTURES === "1";
const OUT = path.join(process.cwd(), "docs", "images");
const PREFIX = "model-switcher-0.3.0";
const MANIFEST = path.join(OUT, "catalog-captures.json");
const FIXTURE = "http://localhost:8932/fixture-real.html";

const BOUNDARY =
  "Dashboard SDK fixture with the plugin's real backend and Hermes' own " +
  "/api/model/* routers over an isolated lab home";

// Host theme tokens. The fixture ships the dark surface but no tokens, so both
// themes are injected into the SERVED HTML — the same mechanism as the light
// theme case in craft-a11y.spec.mjs. (Never addInitScript: the plugin's
// stylesheet would simply out-cascade an element-level variable.)
const DARK =
  "--background:#16181d;--midground:#e6e8ee;--text-muted-color:#9aa1ad;" +
  "--color-border:#3a3f48;--color-ring:#6f9bff";
const LIGHT =
  "--background:#fafafa;--midground:#151515;--text-muted-color:#595959;" +
  "--color-border:#777;--color-ring:#0055aa";

// Surface digest: identical file list and sha256 method to
// visual-evidence.spec.mjs so both manifests describe the same surface.
const SOURCE_FILES = ["ui/src/main.jsx", "ui/src/api.js", "ui/src/styles.css", "dashboard/dist/index.js"];
const digest = crypto.createHash("sha256");
for (const file of SOURCE_FILES) digest.update(fs.readFileSync(file));
const surfaceDigest = digest.digest("hex");

const captures = [];

test.use({ deviceScaleFactor: 1 });

test.beforeEach(() => {
  test.skip(!ENABLED, "set HMS_WRITE_CATALOG_CAPTURES=1 to (re)write docs/images catalog captures");
});

function watch(page) {
  const consoleErrors = [];
  const failedRequests = [];
  page.on("pageerror", (error) => consoleErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("requestfailed", (request) =>
    failedRequests.push(`${request.url()} ${request.failure()?.errorText || ""}`.trim()));
  return { consoleErrors, failedRequests };
}

async function themeHost(page, tokens) {
  await page.route("**/fixture-real.html", async (route) => {
    const response = await route.fetch();
    const body = (await response.text()).replace(
      "</head>",
      `<style>:root{${tokens}}body{background:var(--background);color:var(--midground);}</style></head>`,
    );
    await route.fulfill({ response, body });
  });
}

async function open(page, tokens) {
  await themeHost(page, tokens);
  await page.goto(FIXTURE);
  await expect(page.getByLabel("Select default main", { exact: true })).toBeVisible();
  // Capture the SETTLED state, not the first paint: host evidence loads after
  // the initial state, and the pre-host build legitimately warns that runtime
  // selectors were not discovered yet. Once the host answers, the polite
  // notice clears and the fail-closed inventory warning is gone.
  await expect(page.getByTestId("host-notice")).toHaveText("");
  await expect(page.getByTestId("inventory-warnings")).toHaveCount(0);
  await expect(page.locator('tr[data-row="default/main"]')).toBeVisible();
  // Warm the lazily-loaded host catalog (read-only) so the idle frame shows the
  // real provider/model counts instead of a "not loaded yet" placeholder. This
  // selects nothing and previews nothing.
  const provider = page.getByLabel("New provider", { exact: true });
  if (!(await provider.isDisabled())) {
    await provider.focus();
    await expect(page.getByTestId("catalog-summary")).toContainText("providers ·");
    await provider.blur();
  }
}

async function capture(page, name, state, watcher) {
  fs.mkdirSync(OUT, { recursive: true });
  const file = path.join(OUT, `${PREFIX}-${name}.png`);
  await page.screenshot({ path: file, fullPage: false, animations: "disabled", caret: "hide" });
  const bounds = await page.evaluate(() => ({
    width: innerWidth,
    height: innerHeight,
    documentWidth: document.documentElement.scrollWidth,
    documentHeight: document.documentElement.scrollHeight,
  }));
  expect(bounds.documentWidth, `${name}: document horizontal overflow`).toBeLessThanOrEqual(bounds.width);
  const bytes = fs.statSync(file).size;
  captures.push({
    name,
    state,
    file: path.relative(process.cwd(), file),
    viewport: { width: bounds.width, height: bounds.height },
    bytes,
    sha256: crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex"),
  });
  expect(watcher.consoleErrors, `${name}: console errors`).toEqual([]);
  expect(watcher.failedRequests, `${name}: failed requests`).toEqual([]);
  return bytes;
}

test("catalog capture: dashboard idle, dark theme", async ({ page }) => {
  const watcher = watch(page);
  await page.setViewportSize({ width: 1280, height: 800 });
  await open(page, DARK);
  await page.evaluate(() => scrollTo(0, 0));
  await capture(page, "dashboard", "idle routing matrix and change editor, dark host theme", watcher);
});

test("catalog capture: dashboard idle, light theme", async ({ page }) => {
  const watcher = watch(page);
  await page.setViewportSize({ width: 1280, height: 800 });
  await open(page, LIGHT);
  await page.evaluate(() => scrollTo(0, 0));
  await capture(page, "light", "idle routing matrix and change editor, light host theme", watcher);
});

test("catalog capture: narrow viewport", async ({ page }) => {
  const watcher = watch(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page, DARK);
  // The narrow frame is about the matrix, not the page chrome: land the match
  // count line at the top edge so no filter control is cut in half above it.
  await page.getByTestId("matrix-scroll").evaluate((node) => node.scrollIntoView({ block: "start" }));
  await page.evaluate(() => scrollBy(0, -34));
  await capture(page, "narrow", "narrow 390x844 layout with the routing matrix scrolling inside its own region", watcher);
});

test("catalog capture: model catalog open", async ({ page }) => {
  const watcher = watch(page);
  await page.setViewportSize({ width: 1280, height: 800 });
  await open(page, DARK);
  await page.getByTestId("catalog-toggle").click();
  await expect(page.getByTestId("catalog-table")).toBeVisible();
  await expect(page.getByTestId("catalog-summary-strip")).toBeVisible();
  // Frame the catalog section from its own header, then bring the first
  // POPULATED provider group (Anthropic, 14 models) to the top of the bounded
  // catalog scroll region: the unfiltered list starts with unauthenticated
  // provider skeletons, which say nothing about the catalog's price columns.
  await page.getByTestId("catalog-section").evaluate((node) => node.scrollIntoView({ block: "start" }));
  await page.evaluate(() => scrollBy(0, -8));
  await page.getByTestId("catalog-scroll").evaluate((box) => {
    const group = box.querySelector('tbody[data-provider-group="anthropic"]');
    if (!group) return;
    // Land the group header just BELOW the sticky column header, never under it.
    const head = box.querySelector("thead");
    const sticky = head ? head.getBoundingClientRect().height : 0;
    const boxRect = box.getBoundingClientRect();
    box.scrollTop += group.getBoundingClientRect().top - boxRect.top - sticky;
  });
  await capture(page, "catalog", "provider-grouped model catalog with the host's per-1M-token price columns", watcher);
});

test("catalog capture: reviewed preview", async ({ page }) => {
  const watcher = watch(page);
  await page.setViewportSize({ width: 1280, height: 800 });
  await open(page, DARK);
  await page.getByLabel("Select default main", { exact: true }).check();
  await setProvider(page, "zhipu");
  await setModel(page, "glm-5.3-flash");
  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  const panel = page.getByTestId("preview-panel");
  await expect(panel).toBeVisible();
  await panel.evaluate((node) => node.scrollIntoView({ block: "center" }));
  await capture(page, "preview", "reviewed default/main change to zhipu/glm-5.3-flash, before apply", watcher);
});

test("catalog capture: applied operation drawer", async ({ page }) => {
  const watcher = watch(page);
  await page.setViewportSize({ width: 1280, height: 800 });
  await open(page, DARK);
  await page.getByLabel("Select default main", { exact: true }).check();
  await setProvider(page, "zhipu");
  await setModel(page, "glm-5.3-flash");
  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await page.getByTestId("apply-custom").click();
  const drawer = page.getByTestId("operation-drawer");
  await expect(drawer).toBeVisible();
  await expect(page.getByTestId("result-default")).toContainText("Applied + read back");
  const box = await drawer.boundingBox();
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(1280);
  await capture(page, "operation", "operation drawer after Apply with per-profile readback", watcher);
});

test.afterAll(() => {
  if (!ENABLED || captures.length === 0) return;
  const manifest = {
    prefix: PREFIX,
    captureBoundary: BOUNDARY,
    surfaceDigest,
    sourceFiles: SOURCE_FILES,
    captureViewport: "1280x800 (narrow capture 390x844), deviceScaleFactor 1, viewport screenshot (not fullPage)",
    fixtures: "tests/fixture-real.html served by tests/real_app.py (real plugin backend + Hermes /api/model/* routers, isolated lab home)",
    optIn: "HMS_WRITE_CATALOG_CAPTURES=1",
    observedAt: new Date().toISOString(),
    captures,
  };
  fs.writeFileSync(MANIFEST, JSON.stringify(manifest, null, 2) + "\n");
});
