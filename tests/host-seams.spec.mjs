import { test, expect } from "@playwright/test";
import { setRoute, modelExact } from "./route-helpers.mjs";
import { api, loadHostBundle, validateHostCall, validHostPart } from "../ui/src/api.js";

// Section E: the plugin's host seam. The tab reads Hermes' own model routes
// (/api/model/options|auxiliary|moa) through the dashboard SDK, sends that host
// bundle with /preview, /state and the mutation routes, and performs the
// operation's host steps itself — including the host's expensive-model
// confirmation. Everything here runs against tests/mock-api.js, which emulates
// the host routes in the host's own shapes.

const URL = "/fixture-sdk.html";

async function open(page, flags) {
  await page.goto(URL);
  if (flags) {
    await page.evaluate((value) => sessionStorage.setItem("hms-mock-flags", JSON.stringify(value)), flags);
    await page.reload();
  }
  await expect(page.getByTestId("live-status")).toBeAttached();
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();
}

const requests = (page) => page.evaluate(() => window.__mockApi.requests);
const hostRequests = async (page, part) => (await requests(page))
  .filter((r) => r.host && r.path === `/api/model/${part}`);

// Select a set of slots, then walk select → preview → apply with one route.
async function selectAndReview(page, labels, provider, model, opts) {
  for (const label of labels) await page.getByLabel(label, { exact: true }).check();
  await setRoute(page, provider, model, opts);
  await page.getByLabel("I acknowledge unknown-cost routes").check();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
}

test("host data loads through the SDK with an explicit profile per profile and rides on /preview", async ({ page }) => {
  await open(page);

  // Every host part for every profile /state listed — the root profile is
  // addressed by its reported name ("default", the host's alias for ~/.hermes).
  for (const profileId of ["default", "research", "archived"]) {
    for (const part of ["options", "auxiliary", "moa"]) {
      await expect.poll(async () => (await hostRequests(page, part))
        .filter((r) => r.search.includes(`profile=${profileId}`)).length).toBe(1);
    }
  }
  const options = await hostRequests(page, "options");
  expect(options[0].search).toContain("include_unconfigured=1");
  expect(options.map((r) => new URLSearchParams(r.search).get("profile")).sort())
    .toEqual(["archived", "default", "research"]);

  // The enriched state carries the bundle it was built from.
  const statePost = (await requests(page)).filter((r) => r.path.endsWith("/state") && r.method === "POST").pop();
  expect(statePost).toBeTruthy();
  expect(Object.keys(statePost.body.host.profiles).sort()).toEqual(["archived", "default", "research"]);
  expect(statePost.body.host.profiles.default.options.providers.length).toBeGreaterThan(0);

  // …and the same bundle is what /preview receives.
  await selectAndReview(page, ["Select default main"], "nous", "hermes-4-405b");
  const preview = (await requests(page)).filter((r) => r.path.endsWith("/preview")).pop();
  expect(preview.body.changes).toHaveLength(1);
  expect(preview.body.host.profiles.default.auxiliary.main).toEqual({ provider: "anthropic", model: "claude-sonnet-4-6" });
  expect(preview.body.host.profiles.default.moa.presets.default.aggregator.provider).toBe("anthropic");

  // No prettified host text is ever rendered as a catalog or state note.
  await expect(page.getByTestId("host-notice")).toHaveCount(0);
});

test("apply performs a backend step then a host step on different profiles, shown per store", async ({ page }) => {
  await open(page);
  // Same-profile main + another config target is refused by the real API.
  // Cross-profile mixed execution is valid; backend seq 0 means host seq 1.
  await selectAndReview(page, [
    "Select default cron.model",
    "Select research main",
  ], "nous", "hermes-4-405b");

  const progress = page.getByTestId("apply-progress");
  await page.getByTestId("apply-custom").click();
  await expect(page.getByTestId("operation-drawer")).toBeVisible();

  // Each host step was performed with the host route, its explicit profile and
  // the exact assignment the engine asked for.
  const hostSets = (await requests(page)).filter((r) => r.host && r.path === "/api/model/set");
  expect(hostSets.length).toBe(1);
  expect(hostSets.map((r) => r.body.scope)).toEqual(["main"]);
  expect(hostSets.map((r) => new URLSearchParams(r.search).get("profile")))
    .toEqual(["research"]);
  expect(hostSets[0].body).toMatchObject({ provider: "nous", model: "hermes-4-405b" });

  // Each outcome was reported back with its sequence number and the token the
  // envelope handed out; the status is numeric only.
  const results = (await requests(page)).filter((r) => r.path.endsWith("/host-result"));
  expect(results.map((r) => r.body)).toEqual([
    { seq: 1, token: "tok-op-1-1", outcome: "ok" },
  ]);

  // One operation request only — the loop lives in the browser, not in a
  // second apply call.
  const operationRequests = (await requests(page)).filter((r) => r.path.endsWith("/operations"));
  expect(operationRequests).toHaveLength(1);
  const previewRequest = (await requests(page)).find((r) => r.path.endsWith("/preview"));
  expect(operationRequests[0].body.host).toEqual(previewRequest.body.host);
  expect(hostSets.every((r) => r.sessionHeader === "fixture-session")).toBe(true);

  // Per-store journal progress, with no invented extra result for a slot.
  await expect(progress).toContainText("Apply progress — finished");
  await expect(progress.locator("li")).toHaveCount(2);
  for (const seq of [0, 1]) await expect(page.getByTestId(`apply-step-step-${seq}`)).toHaveAttribute("data-step-status", "applied");
  await expect(page.getByTestId("result-default")).toContainText("Applied + read back");
  await expect(page.getByTestId("result-research")).toHaveCount(1);
  await expect(page.getByTestId("result-research")).toContainText("Applied + read back");
  await expect(page.getByTestId("live-status")).toContainText("finished");
});

test("the host's expensive-model guard: Confirm applies with confirm_expensive_model, Cancel declines", async ({ page }) => {
  await open(page);
  await selectAndReview(page, ["Select default main"], "nous", "hermes-4-405b-max", { exact: true });
  await expect(modelExact(page)).toHaveValue("hermes-4-405b-max");

  await page.getByTestId("apply-custom").click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute("role", "dialog");

  // Backend text is TEXT: the HTML in the host's message stays literal, and no
  // element from it is ever created.
  const message = page.getByTestId("confirm-message");
  await expect(message).toContainText("<b>Confirm</b>");
  await expect(message).toContainText("<img src=x onerror=alert(1)>");
  expect(await message.locator("b, img").count()).toBe(0);

  await page.getByTestId("confirm-accept").click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("result-default")).toContainText("Applied + read back");

  const hostSets = (await requests(page)).filter((r) => r.host && r.path === "/api/model/set");
  expect(hostSets).toHaveLength(2);
  expect(hostSets[0].body.confirm_expensive_model).toBeUndefined();
  expect(hostSets[1].body.confirm_expensive_model).toBe(true);
  expect(hostSets[1].body.model).toBe("hermes-4-405b-max");
  expect(await page.evaluate(() => window.__mockApi.hostAckCount)).toBe(1);

  // Second pass: Cancel is an answer, not an error. The browser reports
  // `declined` and the backend compensates with its own fixed category. An
  // apply consumes its review, so the change is reviewed again first.
  await expect(page.getByTestId("preview-panel")).toHaveCount(0);
  await page.getByRole("button", { name: "Close operation drawer", exact: true }).click();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await page.getByTestId("apply-custom").click();
  await expect(dialog).toBeVisible();
  await page.getByTestId("confirm-cancel").click();
  await expect(dialog).toHaveCount(0);
  const declined = (await requests(page)).filter((r) => r.path.endsWith("/host-result")).pop();
  expect(declined.body.outcome).toBe("declined");
  await expect(page.getByTestId("operation-failure")).toHaveAttribute("data-failure-category", "host_declined");
  await expect(page.getByTestId("operation-failure")).toContainText("declined at the model confirmation");
  // Cancel wrote nothing: engine.py leaves a failed step in a pending op,
  // requiring fresh review. No restore was performed or may be claimed.
  await expect(page.getByTestId("result-default")).toContainText("FAILED — not verified");
  await expect(page.getByTestId("operation-state")).toContainText("Not changed — review again");
});

test("a host error during apply is reported by fixed category, never the host's text", async ({ page }) => {
  await open(page, { hostSetHttp: 500 });
  await selectAndReview(page, ["Select default main"], "nous", "hermes-4-405b");
  await page.getByTestId("apply-custom").click();

  await expect(page.getByTestId("operation-drawer")).toBeVisible();
  // The host route answered 500; only the NUMBER crosses into the backend.
  const reported = (await requests(page)).filter((r) => r.path.endsWith("/host-result")).pop();
  expect(reported.body).toEqual({ seq: 0, token: "tok-op-1-0", outcome: "error", status: 500 });

  await expect(page.getByTestId("operation-failure")).toHaveAttribute("data-failure-category", "host_error");
  await expect(page.getByTestId("operation-failure")).toContainText("The host refused the write");
  await expect(page.getByTestId("visible-status")).toHaveAttribute("data-kind", "error");
  await expect(page.getByTestId("visible-status")).toHaveCSS("color", "rgb(229, 83, 75)");
  // Nothing was written for this profile: its step is a failed write, never a
  // verified restore.
  await expect(page.getByTestId("result-default")).toContainText("FAILED — not verified");
  // The host's own body ("host assignment failed") never reaches the page.
  await expect(page.getByTestId("operation-drawer")).not.toContainText("host assignment failed");
  await expect(page.getByTestId("visible-status")).not.toContainText("500:");
});

test("a missing host route degrades with a notice instead of blocking the tab", async ({ page }) => {
  // One host route absent (an older Hermes); the config half still works.
  await open(page, { hostMissingParts: ["options"] });

  const notice = page.getByTestId("host-notice");
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("Host data unavailable (options — HTTP 404)");

  // The tab is fully usable: rows, selection and the editor still render.
  await expect(page.locator('tr[data-row^="default/"]')).not.toHaveCount(0);
  await page.getByLabel("Select default main", { exact: true }).check();
  await expect(page.getByTestId("apply-custom")).toBeDisabled();

  // The catalog says the fixed reason instead of inventing an empty provider
  // list or a free price.
  await page.getByTestId("catalog-toggle").click();
  await expect(page.getByTestId("catalog-error")).toContainText("hostCatalogUnavailable");

  // The other host parts still arrived.
  expect((await hostRequests(page, "auxiliary")).length).toBeGreaterThan(0);
});

for (const width of [1440, 768, 375]) test(`confirmation owns keyboard focus at ${width}px and Escape cancels`, async ({ page }) => {
  await page.setViewportSize({ width, height: 812 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await open(page);
  await selectAndReview(page, ["Select default main"], "nous", "hermes-4-405b-max", { exact: true });
  await page.getByTestId("apply-custom").click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();
  const confirm = page.getByTestId("confirm-accept");
  const cancel = page.getByTestId("confirm-cancel");
  await expect(confirm).toBeFocused();
  await expect(dialog).toHaveCSS("animation-name", "none");
  await expect(dialog).toHaveCSS("box-shadow", "none");
  for (const button of [confirm, cancel]) expect((await button.boundingBox()).height).toBeGreaterThanOrEqual(38);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  await page.keyboard.press("Tab");
  await expect(cancel).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(confirm).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("operation-failure")).toHaveAttribute("data-failure-category", "host_declined");
  await page.getByRole("button", { name: "Close operation drawer", exact: true }).click();
  await expect(page.getByTestId("apply-custom")).toBeFocused();
});

test("Refresh catalog refetches the host options with refresh=1 before the transform", async ({ page }) => {
  await open(page);
  await page.getByTestId("catalog-toggle").click();
  await expect(page.getByTestId("catalog-view")).toBeVisible();
  expect((await hostRequests(page, "options")).filter((r) => r.search.includes("refresh=1"))).toHaveLength(0);

  await page.getByTestId("catalog-view-refresh").click();
  await expect.poll(async () => (await hostRequests(page, "options"))
    .filter((r) => r.search.includes("refresh=1")).length).toBe(1);
  const refreshed = (await requests(page)).filter((r) => r.path.endsWith("/catalog") && r.body.refresh === true);
  expect(refreshed).toHaveLength(1);
  expect(refreshed[0].body.options.providers.length).toBeGreaterThan(0);
});

for (const part of ["options", "auxiliary", "moa"]) {
  test(`HTTP-200 malformed ${part} is null evidence, not an empty success`, async ({ page }) => {
    await open(page, { hostWrongShapeParts: [part] });
    const notice = page.getByTestId("host-notice");
    await expect(notice).toHaveAttribute("data-state", "partial");
    await expect(notice).toContainText(part);
    await expect.poll(async () => (await requests(page)).filter((r) => r.path.endsWith("/state") && r.method === "POST").length).toBeGreaterThan(0);
    const sent = (await requests(page)).filter((r) => r.path.endsWith("/state") && r.method === "POST").pop();
    expect(sent.body.host.profiles.default[part]).toBeNull();
    await expect(page.getByLabel("Select default main", { exact: true })).toBeEnabled();
  });
}

test("host reads are non-blocking, missing routes become unavailable, and notices are live", async ({ page }) => {
  await open(page, { hostMissing: true, hostDelayMs: 250 });
  await expect(page.getByRole("heading", { name: "Model Switcher", exact: true })).toBeVisible();
  await expect(page.getByLabel("Select default main", { exact: true })).toBeEnabled();
  const notice = page.getByTestId("host-notice");
  await expect(notice).toHaveAttribute("aria-live", "polite");
  await expect(notice).toHaveAttribute("data-state", "loading");
  await expect(notice).toHaveAttribute("data-state", "unavailable");
});

test("a second host step can fail after the first wrote; only a numeric status leaves the UI", async ({ page }) => {
  await open(page, { hostFailAfter: 1 });
  await selectAndReview(page, ["Select default main", "Select research main"], "nous", "hermes-4-405b");
  await page.getByTestId("apply-custom").click();
  await expect(page.getByTestId("operation-failure")).toHaveAttribute("data-failure-category", "host_error");
  const reports = (await requests(page)).filter((r) => r.path.endsWith("/host-result"));
  expect(reports.map((r) => r.body)).toEqual([
    { seq: 0, token: "tok-op-1-0", outcome: "ok" },
    { seq: 1, token: "tok-op-1-1", outcome: "error", status: 503 },
  ]);
  expect(JSON.stringify(reports)).not.toContain("private host failure");
  await expect(page.getByTestId("apply-progress")).toHaveAttribute("aria-live", "polite");
  await expect(page.getByTestId("apply-progress")).toContainText("rolled back");
});

test("HTTP-200 ok:false without confirmation is an error, not success or an invented dialog", async ({ page }) => {
  await open(page, { hostSetBody: { ok: false, message: "private refusal" } });
  await selectAndReview(page, ["Select default main"], "nous", "hermes-4-405b");
  await page.getByTestId("apply-custom").click();
  await expect(page.getByTestId("operation-failure")).toHaveAttribute("data-failure-category", "host_error");
  await expect(page.getByTestId("confirm-dialog")).toHaveCount(0);
  const report = (await requests(page)).find((r) => r.path.endsWith("/host-result"));
  expect(report.body).toEqual({ seq: 0, token: "tok-op-1-0", outcome: "error", status: 200 });
});

test("retired catalog/model GETs are never used; POSTs carry the exact host bundle", async ({ page }) => {
  await open(page);
  await page.getByTestId("catalog-toggle").click();
  await expect(page.getByTestId("catalog-summary-strip")).toBeVisible();
  await setRoute(page, "unknown-provider", "unknown-model", { exact: true });
  await expect.poll(async () => (await requests(page)).filter((r) => r.path.endsWith("/models")).length).toBeGreaterThan(0);
  const transforms = (await requests(page)).filter((r) => /\/(catalog|models)$/.test(r.path));
  expect(transforms.every((r) => r.method === "POST")).toBe(true);
  for (const r of transforms) {
    expect(r.body.options).toEqual(r.body.host.profiles[r.body.profile].options);
  }
});

test("SDK is resolved at request time, injects auth, and is never bypassed by bare host fetch", async () => {
  const previous = globalThis.__HERMES_PLUGIN_SDK__;
  const raw = globalThis.fetch;
  const calls = [];
  globalThis.fetch = () => { throw new Error("bare fetch forbidden"); };
  try {
    for (const id of ["first", "replacement"]) {
      globalThis.__HERMES_PLUGIN_SDK__ = { fetchJSON: async (url, init) => {
        calls.push({ id, url, init });
        return { ok: true };
      }};
      await api.hostCall({ opId: "op", seq: 0, token: "token", method: "POST", path: "/api/model/set",
        query: { profile: "default" }, body: { scope: "main", provider: "nous", model: "model" } });
    }
    expect(calls.map((c) => c.id)).toEqual(["first", "replacement"]);
    expect(calls.every((c) => c.url === "/api/model/set?profile=default")).toBe(true);
    expect(calls[0].init.headers["Content-Type"]).toBe("application/json");
    globalThis.__HERMES_PLUGIN_SDK__ = undefined;
    await expect(api.hostCall({ opId: "op", seq: 0, token: "token", method: "POST", path: "/api/model/set",
      query: { profile: "default" }, body: { scope: "main", provider: "nous", model: "model" } })).rejects.toThrow("SDK unavailable");
  } finally { globalThis.__HERMES_PLUGIN_SDK__ = previous; globalThis.fetch = raw; }
});

test("overlapping host bundles cap concurrency at three and validate real response shapes", async () => {
  const previous = globalThis.__HERMES_PLUGIN_SDK__;
  let active = 0, peak = 0;
  const calls = [];
  globalThis.__HERMES_PLUGIN_SDK__ = { fetchJSON: async (path) => {
    active += 1; peak = Math.max(peak, active); calls.push(path);
    await new Promise((resolve) => setTimeout(resolve, 5));
    active -= 1;
    if (path.includes("/options")) return { provider: "nous", model: "model", providers: [] };
    if (path.includes("/auxiliary")) return { main: { provider: "nous", model: "model" }, tasks: [] };
    return { default_preset: "default", active_preset: "", presets: {} };
  }};
  try {
    const results = await Promise.all([loadHostBundle(["default", "research"], { limit: 99 }), loadHostBundle(["archived"])]);
    expect(peak).toBe(3);
    expect(calls).toHaveLength(9);
    expect(results.every((r) => r.failures.length === 0)).toBe(true);
    for (const part of ["options", "auxiliary", "moa"]) expect(validHostPart(part, {})).toBe(false);
  } finally { globalThis.__HERMES_PLUGIN_SDK__ = previous; }
});

test("host instructions fail closed before credentials, profile overrides, or preconfirmation can escape", async () => {
  const base = { opId: "op", seq: 0, token: "token", method: "POST", path: "/api/model/set",
    query: { profile: "default" }, body: { scope: "main", provider: "nous", model: "model" } };
  expect(validateHostCall(base, "op")).toBe(base);
  for (const override of [
    { opId: "other" }, { seq: -1 }, { path: "https://foreign.invalid/api/model/set" }, { method: "GET" },
    { body: { ...base.body, profile: "other" } }, { body: { ...base.body, api_key: "not-allowed" } },
    { body: { ...base.body, confirm_expensive_model: true } }, { body: { ...base.body, base_url: "https://example.invalid" } },
    { body: { scope: "auxiliary", provider: "p", model: "m", task: "vision", base_url: "https://u:p@example.invalid" } },
  ]) expect(() => validateHostCall({ ...base, ...override }, "op")).toThrow("Invalid host step");
});


test("lost host-result response is reconciled, not replayed; earlier host writes roll back", async ({ page }) => {
  await open(page, { hostResultLoseOnce: true });
  await selectAndReview(page, ["Select default main", "Select research main"], "nous", "hermes-4-405b");
  await page.getByTestId("apply-custom").click();
  await expect(page.getByTestId("operation-drawer")).toBeVisible();
  await expect(page.getByTestId("live-status")).toContainText("Operation readback: compensated");
  const recorded = await requests(page);
  expect(recorded.filter((r) => r.path.endsWith("/set"))).toHaveLength(1);
  expect(recorded.filter((r) => r.path.endsWith("/host-result"))).toHaveLength(1);
  expect(recorded.filter((r) => r.path.endsWith("/recover"))).toHaveLength(1);
  await expect(page.getByTestId("preview-panel")).toHaveCount(0);
  const configured = await page.evaluate(async () => (await (await fetch("/api/plugins/hermes-model-switcher/state")).json()).slots);
  const original = configured.find((s) => s.profileId === "default" && s.slotId === "main");
  expect(original.provider).toBe("anthropic");
  expect(original.model).toBe("claude-sonnet-4-6");
});

test("410 catalog/model refusals surface without any fallback GET", async () => {
  const previous = globalThis.__HERMES_PLUGIN_SDK__;
  const calls = [];
  globalThis.__HERMES_PLUGIN_SDK__ = { authedFetch: async (path, init) => {
    calls.push({ path, method: init.method });
    return new Response(JSON.stringify({ ok: false, reason: "hostCatalogRequired" }), { status: 410 });
  }};
  try {
    for (const transform of [api.getCatalog, api.getModels]) {
      await expect(transform({ profileId: "default", host: { profiles: {} } })).rejects.toMatchObject({ status: 410 });
    }
    expect(calls).toEqual([
      { path: "/api/plugins/hermes-model-switcher/catalog", method: "POST" },
      { path: "/api/plugins/hermes-model-switcher/models", method: "POST" },
    ]);
  } finally { globalThis.__HERMES_PLUGIN_SDK__ = previous; }
});

test("null numeric prices keep host display text and sort after known numeric prices in both directions", async ({ page }) => {
  await open(page, { catalogNullNumericModel: "hermes-4-405b" });
  await page.getByTestId("catalog-toggle").click();
  const row = page.locator('tr[data-provider-id="nous"][data-model-id="hermes-4-405b"]');
  await expect(row.locator('[data-column="input"]')).toHaveText("$10.00");
  const payload = await page.evaluate(() => window.__mockApi.lastCatalog);
  expect(payload.providers.find((p) => p.id === "nous").models.find((m) => m.id === "hermes-4-405b").cost.input).toBeNull();
  await page.getByTestId("catalog-group-toggle").click();
  for (const sort of ["price-asc", "price-desc"]) {
    await page.getByTestId("catalog-sort").selectOption(sort);
    const models = await page.locator('tr[data-model-id]').evaluateAll((rows) => rows.map((r) => r.dataset.modelId));
    expect(models.indexOf("deepseek-v4.1-flash")).toBeLessThan(models.indexOf("hermes-4-405b"));
    expect(models.indexOf("hermes-filler-5")).toBeLessThan(models.indexOf("hermes-4-405b"));
  }
  await expect(page.locator('tr[data-model-id="hermes-unknown"] [data-column="input"]')).toHaveText("price not published");
});

test("cache-only host provenance does not label every provider stale or invent fetch times", async ({ page }) => {
  await open(page, { catalogPickerCache: true });
  await page.getByTestId("catalog-toggle").click();
  const freshness = page.getByTestId("catalog-freshness-details");
  await expect(freshness.locator("summary")).toContainText("from Hermes picker cache");
  await expect(freshness.locator("summary")).not.toContainText("marked stale");
  await expect(page.locator(".hms-catalog-stale")).toHaveCount(0);
  await expect(page.getByTestId("catalog-summary")).toContainText("from Hermes picker cache");
  await page.getByTestId("catalog-view-refresh").click();
  await expect(freshness.locator("summary")).toContainText("refresh requested");
  await freshness.locator("summary").click();
  await expect(freshness).toContainText("No provider reported a fetch timestamp");
});
