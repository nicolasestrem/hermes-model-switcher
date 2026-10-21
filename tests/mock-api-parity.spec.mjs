import { test, expect } from "@playwright/test";
import { fileURLToPath } from "node:url";

// Exercise the browser fixture itself without a live backend or application
// bundle. These assertions mirror plugin_api.py's mapper and engine.py's
// per-store journal: an accommodating mock must not make an invalid UI green.
const BASE = "/api/plugins/hermes-model-switcher";
const mockPath = fileURLToPath(new URL("./mock-api.js", import.meta.url));

async function openMock(page) {
  await page.route("https://mock.invalid/**", (route) => route.fulfill({ body: "<!doctype html><title>Mock parity</title>", contentType: "text/html" }));
  await page.goto("https://mock.invalid/");
  await page.evaluate(() => sessionStorage.setItem("hms-mock-flags", JSON.stringify({ catalogLinkedSlots: true })));
  await page.addScriptTag({ path: mockPath });
}

async function request(page, path, body) {
  return page.evaluate(async ({ path, body }) => {
    const response = await fetch(path, body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json", "X-Hermes-Session-Token": "fixture-session" }, body: JSON.stringify(body),
    });
    return { status: response.status, body: await response.json() };
  }, { path, body });
}

const change = (profileId, slotId, to, action = "set") => ({ profileId, slotId, action, to });
const route = { provider: "nous", model: "hermes-4-70b" };

async function previewAndStart(page, changes) {
  const preview = await request(page, BASE + "/preview", { changes });
  const started = await request(page, BASE + "/operations", { previewId: preview.body.preview.id });
  return { preview: preview.body.preview, ...started };
}

const readSlots = async (page) => (await request(page, BASE + "/state")).body.slots;
const slot = (rows, profileId, slotId) => rows.find((row) => row.profileId === profileId && row.slotId === slotId);

test("mock parity: mixed main batches are blocked at preview and both apply entry points", async ({ page }) => {
  await openMock(page);
  const before = await readSlots(page);
  const changes = [change("default", "main", route), change("default", "cron.model", route)];
  const { preview, status, body } = await previewAndStart(page, changes);
  expect(preview.changes).toEqual([]);
  expect(preview.blocked).toEqual([{
    profileId: "default", slotId: "main", kind: "host_scope",
    reason: "the main assignment must be applied on its own for this profile: a host assignment and another config change cannot share one atomic step — apply them separately",
  }]);
  expect(status).toBe(409);
  expect(body.detail).toMatchObject({ ok: false, blocked: true, error: "apply blocked: the preview contained blocked changes" });
  const direct = await request(page, BASE + "/operations", { changes });
  expect(direct.status).toBe(422);
  expect(direct.body.detail.blocked).toEqual(preview.blocked);
  expect(await readSlots(page)).toEqual(before);
});

test("mock parity: non-main targets sharing a config demote to one backend step without losing writes", async ({ page }) => {
  await openMock(page);
  const { preview, body } = await previewAndStart(page, [
    change("research", "auxiliary.vision", route), change("research", "delegation.fallback_providers", { chain: [route] }),
  ]);
  expect(preview.changes.map((row) => row.executor)).toEqual(["backend", "backend"]);
  expect(body.hostCall).toBeUndefined();
  expect(body.operation).toMatchObject({ state: "committed", undoable: true });
  expect(body.operation.results).toEqual([{
    profileId: "research", seq: 0, store: "canonical_config", status: "applied",
    revisionBefore: "revresearch-a", revisionAfter: "revresearch-b",
  }]);
  const rows = await readSlots(page);
  expect(slot(rows, "research", "auxiliary.vision")).toMatchObject(route);
  expect(slot(rows, "research", "delegation.fallback_providers")).toMatchObject(route);
});

for (const scenario of [
  { name: "same-provider auxiliary", slotId: "auxiliary.vision", to: { provider: "nous", model: "new-vision" }, host: true },
  { name: "auxiliary endpoint", slotId: "auxiliary.vision", to: { provider: "custom", model: "new-vision", base_url: "http://127.0.0.1:9000/v1" }, host: true },
  { name: "provider-change cleanup", slotId: "auxiliary.vision", to: { provider: "openai", model: "new-vision" }, host: false },
  { name: "extra protocol leaf", slotId: "auxiliary.vision", to: { provider: "nous", model: "new-vision", api_mode: "responses" }, host: false },
  { name: "endpoint clear", slotId: "auxiliary.vision", to: { provider: "nous", model: "new-vision", base_url: "" }, host: false },
  { name: "endpoint with userinfo", slotId: "auxiliary.vision", to: { provider: "custom", model: "new-vision", base_url: "https://fixture:fake@example.invalid/v1" }, host: false },
  { name: "endpoint with query", slotId: "auxiliary.vision", to: { provider: "custom", model: "new-vision", base_url: "https://example.invalid/v1?route=fixture" }, host: false },
  { name: "endpoint with fragment", slotId: "auxiliary.vision", to: { provider: "custom", model: "new-vision", base_url: "https://example.invalid/v1#fixture" }, host: false },
  { name: "judge-owned auxiliary", profileId: "default", slotId: "auxiliary.goal_judge", to: route, host: false },
  { name: "removal", slotId: "auxiliary.vision", to: null, action: "remove", host: false },
]) test(`mock parity: ${scenario.name} uses only its exact executor`, async ({ page }) => {
  await openMock(page);
  const profileId = scenario.profileId || "research";
  const { preview, body } = await previewAndStart(page, [change(profileId, scenario.slotId, scenario.to, scenario.action)]);
  expect(preview.changes[0].executor).toBe(scenario.host ? "host" : "backend");
  if (scenario.host) {
    const expected = { scope: "auxiliary", task: "vision", provider: scenario.to.provider, model: scenario.to.model };
    if (scenario.to.base_url) expected.base_url = scenario.to.base_url;
    expect(body.hostCall.body).toEqual(expected);
    expect(body.operation.undoable).toBe(false);
    await request(page, body.hostCall.path + `?profile=${profileId}`, body.hostCall.body);
    const completed = await request(page, `${BASE}/operations/${body.operation.id}/host-result`, {
      seq: body.hostCall.seq, token: body.hostCall.token, outcome: "ok",
    });
    expect(completed.body.operation.state).toBe("committed");
    expect(slot(await readSlots(page), profileId, scenario.slotId)).toMatchObject(scenario.to);
  } else {
    expect(body.hostCall).toBeUndefined();
    expect(body.operation.state).toBe("committed");
    expect(slot(await readSlots(page), profileId, scenario.slotId)).toMatchObject(
      scenario.action === "remove" ? { provider: "", model: "" } : scenario.to,
    );
  }
});

test("mock parity: backend steps count in host seq and token refusals preserve exact pending state", async ({ page }) => {
  await openMock(page);
  const { body } = await previewAndStart(page, [change("default", "cron.model", route), change("research", "main", route)]);
  const { hostCall, operation } = body;
  expect(hostCall.seq).toBe(1);
  expect(operation.undoable).toBe(false);
  expect(operation.results.map(({ seq, store, status }) => ({ seq, store, status }))).toEqual([
    { seq: 0, store: "canonical_config", status: "applied" }, { seq: 1, store: "canonical_config", status: "awaiting_host" },
  ]);
  const reportPath = `${BASE}/operations/${operation.id}/host-result`;
  const refused = await request(page, reportPath, { seq: hostCall.seq, token: "foreign-token", outcome: "ok" });
  expect(refused).toEqual({ status: 409, body: { detail: { ok: false, error: "host_token_mismatch", failure: "host_token_mismatch" } } });
  expect((await request(page, `${BASE}/operations/${operation.id}`)).body.operation).toEqual(operation);
  await request(page, hostCall.path + "?profile=research", hostCall.body);
  const report = { seq: hostCall.seq, token: hostCall.token, outcome: "ok" };
  const committed = await request(page, reportPath, report);
  expect(committed.body.operation.state).toBe("committed");
  expect((await request(page, reportPath, report))).toEqual(refused);
  expect((await request(page, `${BASE}/operations/${operation.id}`)).body.operation).toEqual(committed.body.operation);
});

for (const writeLanded of [false, true]) test(`mock parity: abandoned awaiting-host recovery restores ${writeLanded ? "a landed write and prior backend step" : "only prior backend writes"}`, async ({ page }) => {
  await openMock(page);
  const before = await readSlots(page);
  const { body } = await previewAndStart(page, [change("default", "cron.model", route), change("research", "main", route)]);
  if (writeLanded) await request(page, body.hostCall.path + "?profile=research", body.hostCall.body);
  const recovered = await request(page, `${BASE}/operations/${body.operation.id}/recover`, {});
  expect(recovered.body).toMatchObject({ ok: false, operation: { state: "compensated", final_state: "compensated", failure: "host_error", undoable: false } });
  expect(recovered.body.operation.results.map((row) => row.status)).toEqual(["compensated", writeLanded ? "compensated" : "failed"]);
  expect(await readSlots(page)).toEqual(before);
  expect((await request(page, `${BASE}/operations/${body.operation.id}/recover`, {})).body).toEqual(recovered.body);
});

test("mock parity: decline without a write needs fresh review, never a verified restore", async ({ page }) => {
  await openMock(page);
  const before = await readSlots(page);
  const { body } = await previewAndStart(page, [change("default", "main", route)]);
  const declined = await request(page, `${BASE}/operations/${body.operation.id}/host-result`, {
    seq: body.hostCall.seq, token: body.hostCall.token, outcome: "declined",
  });
  expect(declined.body).toMatchObject({ ok: false, operation: { state: "pending", failure: "host_declined", undoable: false } });
  expect(declined.body.operation.results[0].status).toBe("failed");
  expect(await readSlots(page)).toEqual(before);
});

test("mock parity: an identical host assignment is a known no-write, not a verified apply", async ({ page }) => {
  await openMock(page);
  const before = await readSlots(page);
  const { provider, model } = slot(before, "default", "main");
  const { body } = await previewAndStart(page, [change("default", "main", { provider, model })]);
  await request(page, body.hostCall.path + "?profile=default", body.hostCall.body);
  const reported = await request(page, `${BASE}/operations/${body.operation.id}/host-result`, {
    seq: body.hostCall.seq, token: body.hostCall.token, outcome: "ok",
  });
  expect(reported.body).toMatchObject({ ok: false, operation: { state: "pending", failure: "host_no_write", undoable: false } });
  expect(reported.body.operation.results[0]).toMatchObject({ status: "failed", revisionAfter: null });
  expect(await readSlots(page)).toEqual(before);
});
