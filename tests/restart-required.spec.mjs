import { test, expect } from "@playwright/test";
import {
  ApiError, catalogBody, catalogErrorText, hostFailureCategoryOf,
  hostFailureText, HOST_RESTART_REQUIRED, hostJSON, loadHostBundle,
} from "../ui/src/api.js";

// The host's 503 code-skew guard (upstream hermes_cli/web_routers/models.py
// `_dashboard_code_skew_guard`, commit f293e7206b / #86207): the picker routes
// refuse while the running dashboard process is older than the checkout on
// disk. The plugin classifies that case into ONE fixed category and shows ONE
// fixed actionable sentence — the host's detail text is matched against a
// prefix and NEVER displayed, stored, or forwarded.

// The real upstream detail (only ever used here as data the UI must NOT leak).
const HOST_DETAIL =
  "Restart required: This process is running code from git:main:aaaaaaaaaa " +
  "but the checkout on disk is now git:main:bbbbbbbbbb. The model picker " +
  "would risk a stale-module crash — restart this Hermes process to load " +
  "the new code (hermes dashboard --port <port>, or the equivalent service " +
  "restart for this install)";
const HOST_BODY = JSON.stringify({ detail: HOST_DETAIL });

// Error shaped like the dashboard SDK's fetchJSON ApiError: message is the
// sanitized detail, and the RAW response body rides along for diagnostics.
function sdkError(status, body, detail) {
  const err = new Error(detail || ("HTTP " + status));
  err.status = status;
  err.body = body;
  err.url = "/api/model/options";
  return err;
}

function withSdk(fetchJSON, fn) {
  const previous = globalThis.__HERMES_PLUGIN_SDK__;
  globalThis.__HERMES_PLUGIN_SDK__ = { fetchJSON };
  return Promise.resolve(fn()).finally(() => {
    globalThis.__HERMES_PLUGIN_SDK__ = previous;
  });
}

test("hostJSON classifies the host's 503 restart-required guard", async () => {
  await withSdk(async (path) => {
    throw sdkError(503, HOST_BODY, "Restart required: This process is running code…");
  }, async () => {
    const err = await hostJSON("/api/model/options?profile=default").then(
      () => { throw new Error("should have thrown"); },
      (e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(503);
    expect(hostFailureCategoryOf(err)).toBe(HOST_RESTART_REQUIRED);
    // The category rides on the error; the host's sentence does not.
    expect(err.message).not.toContain("checkout on disk");
    expect(JSON.stringify(err)).not.toContain("aaaaaaaaaa");
  });
});

test("other 503s stay unclassified", async () => {
  for (const body of [
    JSON.stringify({ detail: "Service unavailable: warming up" }),
    "upstream exploded",
    JSON.stringify({ detail: 42 }),
    "",
  ]) {
    await withSdk(async () => {
      throw sdkError(503, body, "The Hermes service is not ready yet. Try again in a moment.");
    }, async () => {
      const err = await hostJSON("/api/model/options?profile=default").then(
        () => { throw new Error("should have thrown"); }, (e) => e);
      expect(err.status).toBe(503);
      expect(hostFailureCategoryOf(err)).toBe(null);
    });
  }
});

test("non-503 restart-shaped detail is never classified", async () => {
  await withSdk(async () => {
    throw sdkError(500, HOST_BODY, "Restart required: This process is running code…");
  }, async () => {
    const err = await hostJSON("/api/model/options?profile=default").then(
      () => { throw new Error("should have thrown"); }, (e) => e);
    expect(err.status).toBe(500);
    expect(hostFailureCategoryOf(err)).toBe(null);
  });
});

test("loadHostBundle carries the category and hostFailureText shows the fixed restart sentence", async () => {
  await withSdk(async (path) => {
    if (path.includes("/options")) {
      throw sdkError(503, HOST_BODY, "Restart required: This process is running code…");
    }
    if (path.includes("/auxiliary")) {
      return { main: { provider: "nous", model: "m" }, tasks: [] };
    }
    return { default_preset: "default", active_preset: "", presets: {} };
  }, async () => {
    const { host, failures } = await loadHostBundle(["default"], { limit: 99 });
    // A failed part stays null ("no evidence"), never {} or a host payload.
    expect(host.profiles.default.options).toBeNull();
    const optionsFailure = failures.find((f) => f.part === "options");
    expect(optionsFailure.status).toBe(503);
    expect(optionsFailure.category).toBe(HOST_RESTART_REQUIRED);
    expect(hostFailureCategoryOf(optionsFailure)).toBe(HOST_RESTART_REQUIRED);
    // Fixed sentence, no host text, still names no host response body.
    const notice = hostFailureText(failures);
    expect(notice).toBe(catalogErrorText(HOST_RESTART_REQUIRED));
    expect(notice).toContain("Restart the dashboard");
    expect(notice).not.toContain("checkout on disk");
    expect(notice).not.toContain("aaaaaaaaaa");
    // A failure WITHOUT the category keeps the generic unavailable text.
    expect(hostFailureText([{ profileId: "default", part: "options", status: 503 }]))
      .not.toBe(notice);
    expect(hostFailureText([{ profileId: "default", part: "options", status: 503 }]))
      .toContain("Host data unavailable");
  });
});

test("catalogBody attaches optionsFailure only when no options exist", () => {
  expect(catalogBody({ profileId: "default", optionsFailure: HOST_RESTART_REQUIRED }).optionsFailure)
    .toBe(HOST_RESTART_REQUIRED);
  const withOptions = { providers: [] };
  const body = catalogBody({
    profileId: "default", host: { profiles: { default: { options: withOptions } } },
    optionsFailure: HOST_RESTART_REQUIRED,
  });
  expect(body.options).toBe(withOptions);
  expect(body.optionsFailure).toBeUndefined();
  // Any other value — including host-shaped text — is never attached.
  expect(catalogBody({ profileId: "p", optionsFailure: HOST_DETAIL }).optionsFailure).toBeUndefined();
});

test("catalogErrorText maps the fixed token to the fixed sentence, everything else passes through", () => {
  expect(catalogErrorText(HOST_RESTART_REQUIRED)).toContain("Restart the dashboard");
  expect(catalogErrorText("no providers in this profile's catalog")).toBe(
    "no providers in this profile's catalog");
  expect(catalogErrorText(null)).toBe(null);
});

test("the fixed sentences never contain the host's detail text", () => {
  for (const sentence of [hostFailureText([{ part: "options", status: 503, category: HOST_RESTART_REQUIRED }]),
    catalogErrorText(HOST_RESTART_REQUIRED)]) {
    expect(typeof sentence).toBe("string");
    expect(sentence.startsWith("Restart required:")).toBe(false);
    expect(sentence).not.toContain(HOST_DETAIL);
    expect(sentence).not.toContain("stale-module");
    expect(sentence).not.toContain("checkout");
  }
});
