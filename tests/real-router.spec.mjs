import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
// Seeded by tests/real-server.mjs before the run; removed at teardown.
const labHome = JSON.parse(readFileSync(join(HERE, "real-home.json"), "utf8")).home;
const URL = "http://localhost:8932/fixture-real.html";

const profiles = {
  // Hermes semantics: the "default" profile IS the home-root config.yaml;
  // named profiles live under profiles/<name>/config.yaml.
  default: join(labHome, "config.yaml"),
  research: join(labHome, "profiles", "research", "config.yaml"),
};
const cfg = (p) => readFileSync(profiles[p], "utf8");

const ORIGINAL = {
  default: "model:\n  provider: anthropic\n  default: claude-sonnet-4-6\n",
  research: "model:\n  provider: openai\n  default: gpt-5.1\n",
};
const seedCfg = (p) =>
  p === "default" ? ORIGINAL.default
  : `model:\n  provider: anthropic\n  default: claude-sonnet-4-6\n`;

// Real-plugin-api responses never leak: no credentials, no host/lab paths,
// no journal snapshots, no raw change payloads.
const LEAK_MARKERS = ["sk-", "Bearer ", "LAB-LOCAL-FAKE", "OPENAI_API_KEY",
  "/home/", "/tmp/", "snapshot", "operations.db", ".env", "base_url"];

test("real router: preset select → preview → apply writes configs on disk → guarded undo restores them", async ({ page }) => {
  const apiResponses = [];
  page.on("response", async (res) => {
    if (res.url().includes("/api/plugins/")) apiResponses.push(res);
  });
  const requests = [];
  page.on("request", (req) => requests.push(req.url()));

  await page.goto(URL);
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();

  // review the preset (auto-preview) then one-click apply — real engine
  await page.getByRole("button", { name: /Fast lane/ }).click();
  const panel = page.getByTestId("preview-panel");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("zhipu/glm-5.3-flash");
  await expect(page.getByTestId("apply-preset")).toBeEnabled();
  await page.getByTestId("apply-preset").click();

  const drawer = page.getByTestId("operation-drawer");
  await expect(drawer).toBeVisible();
  await expect(page.getByTestId("result-default")).toContainText("Applied + read back");
  await expect(page.getByTestId("result-research")).toContainText("Applied + read back");

  // REAL file saves: the configs on disk changed
  expect(cfg("default")).toContain("provider: zhipu");
  expect(cfg("default")).toContain("default: glm-5.3-flash");
  expect(cfg("research")).toContain("provider: zhipu");
  expect(cfg("research")).toContain("default: glm-5.3-flash");
  // operation DTO is sanitized: no rev hashes, no snapshots, no paths
  const drawerText = await drawer.textContent();
  for (const m of LEAK_MARKERS) expect(drawerText, m).not.toContain(m);

  // guarded undo → real restore on disk
  await page.getByTestId("undo-guard").check();
  await page.getByTestId("undo-button").click();
  await expect(page.getByTestId("result-default")).toContainText("Restored (undo verified)");
  expect(cfg("default")).toBe(ORIGINAL.default);
  expect(cfg("research")).toBe(ORIGINAL.research);

  // every captured plugin-api response is leak-free
  for (const res of apiResponses) {
    const body = await res.text();
    for (const m of LEAK_MARKERS) expect(body, `${res.url()} → ${m}`).not.toContain(m);
  }
  // no inference and no off-host traffic: plugin-api requests stay local
  for (const u of requests) {
    if (u.includes("/api/plugins/")) {
      expect(u.startsWith("http://localhost:8932"), u).toBe(true);
    }
  }
});

test("real router: probe without explicit authorization is blocked (no inference)", async ({ page }) => {
  await page.goto(URL);
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();
  const res = await page.evaluate(async () => {
    const r = await fetch("/api/plugins/hermes-model-switcher/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: "zhipu", model: "glm-5.3-flash" }),
    });
    return { status: r.status, body: await r.json() };
  });
  expect(res.status).toBe(403);
  expect(res.body.detail.blocked).toBe(true);
});
