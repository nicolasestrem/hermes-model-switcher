import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { resolvePython } from "./real-server.mjs";
import { setProvider, setModel } from "./route-helpers.mjs";

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

// The undo contract is a BYTE-EXACT restore of the pre-apply document. The
// baseline comes from the REAL seed source (tests/real_app.py PROFILES), never
// from hardcoded copies that drift from the fixture (e.g. omit delegation
// rows), and the pre-apply bytes are captured on disk before any apply.
const seedProfiles = JSON.parse(execFileSync(
  resolvePython(),
  [join(HERE, "real_app.py"), "--print-profiles"],
  { cwd: join(HERE, ".."), encoding: "utf8" }));

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

  // byte-exact undo baseline: the pre-apply bytes, cross-checked against the
  // seed source so a drifted fixture fails here, not at the undo compare
  const before = { default: cfg("default"), research: cfg("research") };
  expect(before.default).toBe(seedProfiles.default);
  expect(before.research).toBe(seedProfiles.research);

  // review the preset (auto-preview) then one-click apply — real engine
  await page.getByRole("button", { name: /Fast lane/ }).click();
  const panel = page.getByTestId("preview-panel");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("zhipu/glm-5.3-flash");
  await expect(page.getByTestId("apply-preset")).toBeEnabled();
  // Trace every real continuation. The initial operation is NOT committed:
  // the browser must execute hostCall, then report only seq/token/outcome/status.
  const hostWrites = [];
  const hostResults = [];
  const trace = [];
  page.on("response", (response) => {
    if (response.request().method() !== "POST") return;
    if (response.url().includes("/api/model/set?")) { hostWrites.push(response); trace.push("host"); }
    if (response.url().endsWith("/host-result")) { hostResults.push(response); trace.push("result"); }
  });
  const opsResponse = page.waitForResponse(
    (r) => r.url().endsWith("/api/plugins/hermes-model-switcher/operations")
      && r.request().method() === "POST");
  await page.getByTestId("apply-preset").click();
  const opEnvelope = await (await opsResponse).json();
  expect(opEnvelope.ok).toBe(false);
  expect(opEnvelope.operation.state).toBe("awaiting_host");
  expect(opEnvelope.operation.results[0].seq).toBe(0);
  expect(opEnvelope.hostCall).toMatchObject({
    opId: opEnvelope.operation.id, seq: 0, method: "POST", path: "/api/model/set",
    query: { profile: "default" },
    body: { scope: "main", provider: "zhipu", model: "glm-5.3-flash" },
  });

  const drawer = page.getByTestId("operation-drawer");
  await expect(drawer).toBeVisible();
  await expect(page.getByTestId("result-default")).toContainText("Applied + read back");
  await expect(page.getByTestId("result-research")).toContainText("Applied + read back");

  // The drawer's counts and per-profile labels come from ``results[].status`` and
  // the overall outcome from ``operation.state``. A real response carries NO
  // per-step ``readback`` (nor raw error text) — the UI must render without them.
  await expect(page.getByTestId("operation-summary")).toContainText("2 profiles applied · 0 need attention");
  expect(trace).toEqual(["host", "result", "host", "result"]);
  expect(hostWrites).toHaveLength(2);
  expect(hostResults).toHaveLength(2);
  for (const [seq, response] of hostWrites.entries()) {
    expect(response.status()).toBe(200);
    expect((await response.json()).ok).toBe(true);
    expect(new globalThis.URL(response.url()).searchParams.get("profile"))
      .toBe(seq === 0 ? "default" : "research");
    expect(response.request().postDataJSON()).toEqual(opEnvelope.hostCall.body);
    const report = hostResults[seq].request().postDataJSON();
    expect(report).toMatchObject({ seq, outcome: "ok" });
    expect(Object.keys(report).sort()).toEqual(["outcome", "seq", "token"]);
  }
  const firstResult = await hostResults[0].json();
  expect(firstResult.operation.state).toBe("awaiting_host");
  expect(firstResult.hostCall).toMatchObject({ seq: 1, query: { profile: "research" } });
  const committed = await hostResults[1].json();
  const opBody = committed.operation;
  expect(committed.ok).toBe(true);
  expect(opBody.results.map((r) => r.seq)).toEqual([0, 1]);
  expect(opBody.results.every((r) => r.store === "canonical_config")).toBe(true);
  expect(opBody.state).toBe("committed");
  expect(opBody.final_state).toBe("committed");
  expect(opBody.failure).toBeNull();
  expect(Array.isArray(opBody.results)).toBe(true);
  expect(opBody.results.length).toBeGreaterThan(0);
  expect(opBody.results.every((r) => r.status === "applied")).toBe(true);
  expect(opBody.results.every((r) => !("readback" in r))).toBe(true);
  expect(opBody.results.every((r) => !("error" in r))).toBe(true);

  // REAL file saves: the configs on disk changed
  expect(cfg("default")).toContain("provider: zhipu");
  expect(cfg("default")).toContain("default: glm-5.3-flash");
  expect(cfg("research")).toContain("provider: zhipu");
  expect(cfg("research")).toContain("default: glm-5.3-flash");
  // operation DTO is sanitized: no rev hashes, no snapshots, no paths
  const drawerText = await drawer.textContent();
  for (const m of LEAK_MARKERS) expect(drawerText, m).not.toContain(m);

  // guarded undo → real restore on disk. The drawer's "Restored (undo verified)"
  // label is driven by the undo DTO's per-step status ("compensated"), captured
  // from the real response.
  await page.getByTestId("undo-guard").check();
  const undoResponse = page.waitForResponse(
    (r) => r.url().endsWith("/operations/" + opBody.id + "/undo")
      && r.request().method() === "POST");
  await page.getByTestId("undo-button").click();
  const undoEnvelope = await (await undoResponse).json();
  const undoBody = undoEnvelope.operation;
  await expect(page.getByTestId("result-default")).toContainText("Restored (undo verified)");
  expect(undoEnvelope.ok).toBe(true); // a clean undo is a success
  expect(undoBody.state).toBe("undone");
  expect(undoBody.final_state).toBe("undone");
  expect(undoBody.results.every((r) => r.status === "compensated")).toBe(true);
  expect(cfg("default")).toBe(before.default);
  expect(cfg("research")).toBe(before.research);

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

test("real router: backend delegation apply reads back lab config and undoes without a host write", async ({ page }) => {
  const requests = [];
  page.on("request", (request) => requests.push(request));
  await page.goto(URL);
  await expect(page.getByTestId("live-status")).toContainText("state loaded");
  const before = { default: cfg("default"), research: cfg("research") };
  await page.getByLabel("Profile research", { exact: true }).check();
  await page.getByLabel("Select research delegation.model", { exact: true }).check();
  await setProvider(page, "zhipu");
  await setModel(page, "glm-5.3-flash");
  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("apply-custom")).toBeEnabled();
  const responsePromise = page.waitForResponse((r) => r.url().endsWith("/operations") && r.request().method() === "POST");
  await page.getByTestId("apply-custom").click();
  const envelope = await (await responsePromise).json();
  expect(envelope.ok).toBe(true);
  expect(envelope.operation.state).toBe("committed");
  expect(envelope.hostCall).toBeUndefined();
  expect(envelope.operation.results).toMatchObject([{ seq: 0, profileId: "research", store: "canonical_config", status: "applied" }]);
  await expect(page.getByTestId("result-research")).toContainText("Applied + read back");
  expect(cfg("research")).toMatch(/delegation:\n\s+model: glm-5.3-flash\n\s+provider: zhipu/);
  expect(cfg("default")).toBe(before.default);
  await expect(page.locator('tr[data-row="research/delegation.model"] [data-column="configured"]')).toContainText("zhipu/glm-5.3-flash");
  expect(requests.filter((r) => r.url().includes("/api/model/set"))).toHaveLength(0);
  await page.getByTestId("undo-guard").check();
  await page.getByTestId("undo-button").click();
  await expect(page.getByTestId("result-research")).toContainText("Restored (undo verified)");
  expect(cfg("research")).toBe(before.research);
  expect(cfg("default")).toBe(before.default);
});

test("real router: the /probe route is GONE (no inference affordance survives)", async ({ page }) => {
  // Deletion check, not a weakening: the backend removed the /probe route
  // entirely, so an unauthenticated probe is a plain 404 — there is no route
  // left that could start a paid session, and no UI affordance for one.
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
  expect(res.status).toBe(404);
  expect(res.body.detail).toBe("Not Found");
});
