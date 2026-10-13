// Dashboard-contract regression test.
//
// The real Hermes Dashboard shell exposes window.__HERMES_PLUGIN_SDK__ and
// window.__HERMES_PLUGINS__ and NEVER sets globalThis.React. A plugin bundle that
// destructures globalThis.React therefore dies the instant the host appends it:
//   Uncaught TypeError: Cannot destructure property 'useState' of 'q' as it is undefined.
//
// tests/fixture-sdk.html reproduces that shell exactly (see tests/build-vendor.mjs
// for the SDK shape, mirrored from hermes-agent web/src/plugins/registry.ts) and
// loads the shipped artifact — dashboard/dist/index.js — not the source.
import { test, expect } from "@playwright/test";

const URL = "/fixture-sdk.html";

async function open(page) {
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  await page.goto(URL);
  await expect(page.getByTestId("live-status")).toBeAttached();
  return errors;
}

test("registers with the host SDK and renders with no globalThis.React present", async ({ page }) => {
  const errors = await open(page);

  // The host provides only the SDK globals — this is the condition that broke us.
  expect(await page.evaluate(() => typeof window.React)).toBe("undefined");
  expect(await page.evaluate(() => typeof window.ReactDOM)).toBe("undefined");
  expect(await page.evaluate(() => window.__HERMES_PLUGIN_SDK__.sdkVersion)).toBe("1.1.0");

  // Registered synchronously under the manifest name, before the host mounts,
  // and mounted by the HOST — we never create our own root.
  expect(await page.evaluate(() => Object.keys(window.__HERMES_PLUGINS__.registered)))
    .toEqual(["hermes-model-switcher"]);
  expect(await page.evaluate(() => window.__hostMountError)).toBeNull();
  expect(await page.evaluate(() => window.__hostMounted)).toBe(true);
  expect(await page.evaluate(() => Boolean(document.querySelector('[data-plugin-root]')))).toBe(false);

  // The host-mounted component is our real UI, fed by the plugin backend.
  await expect(page.getByRole("heading", { name: "Model Switcher" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Fast lane/ })).toBeVisible();

  // And it selected the SDK path, not a legacy global.
  expect(await page.evaluate(() => window.__mockApi.requests.some((r) => r.path.endsWith("/state"))))
    .toBe(true);

  expect(errors).toEqual([]);
});

test("negative control: the pre-fix globalThis.React destructure still throws here", async ({ page }) => {
  await page.goto(URL);

  // The exact head of the bundle that shipped before this fix. If the fixture
  // ever starts providing globalThis.React, this test fails and the suite would
  // stop guarding the real failure mode.
  const message = await page.evaluate(() => {
    try {
      // eslint-disable-next-line no-eval
      eval("var q = globalThis.React; const { useState } = q;");
      return null;
    } catch (e) {
      return String((e && e.message) || e);
    }
  });

  expect(message).toMatch(/Cannot destructure property 'useState'/);
});
