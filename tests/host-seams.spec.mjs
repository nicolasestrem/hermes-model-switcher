import { test, expect } from "@playwright/test";
import { setRoute, modelExact } from "./route-helpers.mjs";

// Section E: the plugin's host seam. The tab reads Hermes' own model routes
// (/api/model/options|auxiliary|moa) through the dashboard SDK, sends that host
// bundle with /preview, /state and the mutation routes, and performs the
// operation's host steps itself — including the host's expensive-model
// confirmation. Everything here runs against tests/mock-api.js, which emulates
// the host routes in the host's own shapes.

const URL = "/fixture.html";

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

test("apply performs two host steps plus a mixed backend step, shown per step", async ({ page }) => {
  await open(page);
  // Slot order decides step order: default/main (host), research/main (host),
  // research/delegation.fallback_providers (backend — a chain the engine owns).
  await selectAndReview(page, [
    "Select default main",
    "Select research main",
    "Select research delegation.fallback_providers",
  ], "nous", "hermes-4-405b");

  const progress = page.getByTestId("apply-progress");
  await page.getByTestId("apply-custom").click();
  await expect(page.getByTestId("operation-drawer")).toBeVisible();

  // Each host step was performed with the host route, its explicit profile and
  // the exact assignment the engine asked for.
  const hostSets = (await requests(page)).filter((r) => r.host && r.path === "/api/model/set");
  expect(hostSets.length).toBe(2);
  expect(hostSets.map((r) => r.body.scope)).toEqual(["main", "main"]);
  expect(hostSets.map((r) => new URLSearchParams(r.search).get("profile")))
    .toEqual(["default", "research"]);
  expect(hostSets[0].body).toMatchObject({ provider: "nous", model: "hermes-4-405b" });

  // Each outcome was reported back with its sequence number and the token the
  // envelope handed out; the status is numeric only.
  const results = (await requests(page)).filter((r) => r.path.endsWith("/host-result"));
  expect(results.map((r) => r.body)).toEqual([
    { seq: 1, token: "tok-op-1-1", outcome: "ok" },
    { seq: 2, token: "tok-op-1-2", outcome: "ok" },
  ]);

  // One operation request only — the loop lives in the browser, not in a
  // second apply call.
  expect((await requests(page)).filter((r) => r.path.endsWith("/operations"))).toHaveLength(1);

  // Per-step progress, and the final per-profile truth from the DTO.
  await expect(progress).toContainText("Apply progress — finished");
  await expect(progress.locator("li")).toHaveCount(2);
  await expect(page.getByTestId("apply-step-step-default")).toHaveAttribute("data-step-status", "applied");
  await expect(page.getByTestId("apply-step-step-research")).toHaveAttribute("data-step-status", "applied");
  await expect(page.getByTestId("result-default")).toContainText("Applied + read back");
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
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  await expect(page.getByTestId("preview-panel")).toBeVisible();
  await page.getByTestId("apply-custom").click();
  await expect(dialog).toBeVisible();
  await page.getByTestId("confirm-cancel").click();
  await expect(dialog).toHaveCount(0);
  const declined = (await requests(page)).filter((r) => r.path.endsWith("/host-result")).pop();
  expect(declined.body.outcome).toBe("declined");
  await expect(page.getByTestId("operation-failure")).toHaveAttribute("data-failure-category", "host_declined");
  await expect(page.getByTestId("operation-failure")).toContainText("declined at the cost confirmation");
  await expect(page.getByTestId("result-default")).toContainText("Rolled back after failed apply");
});

test("a host error during apply is reported by fixed category, never the host's text", async ({ page }) => {
  await open(page, { hostSetHttp: 500 });
  await selectAndReview(page, ["Select default main"], "nous", "hermes-4-405b");
  await page.getByTestId("apply-custom").click();

  await expect(page.getByTestId("operation-drawer")).toBeVisible();
  // The host route answered 500; only the NUMBER crosses into the backend.
  const reported = (await requests(page)).filter((r) => r.path.endsWith("/host-result")).pop();
  expect(reported.body).toEqual({ seq: 1, token: "tok-op-1-1", outcome: "error", status: 500 });

  await expect(page.getByTestId("operation-failure")).toHaveAttribute("data-failure-category", "host_error");
  await expect(page.getByTestId("operation-failure")).toContainText("The host refused the write");
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
