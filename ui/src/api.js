// Plugin API client. Contract: /api/plugins/hermes-model-switcher/*
//
// HARD RULE (Review Focus #5): a response is a success only when the HTTP
// status is 2xx AND the parsed body has ok === true. A network failure, a
// non-JSON body, an HTTP error, or ok:false is a failure — never empty state,
// never a success badge.
export class ApiError extends Error {
  constructor(message, { status = 0, body = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

const BASE = "/api/plugins/hermes-model-switcher";

// Transport. The Dashboard SDK's ``authedFetch`` attaches the shell's auth
// (loopback session-token header, or the gated-mode session cookie) and the
// shell's base path / profile scope. A bare ``fetch`` carries none of that: in
// a session-token dashboard (loopback ``hermes dashboard``, the desktop app)
// every call 401s while the tab itself renders fine — the tab then looks
// inert. plugins/sdk.d.ts states plugins MUST route through the SDK; the bare
// fetch stays only as the fixture/standalone fallback.
function transport() {
  const sdk = globalThis.__HERMES_PLUGIN_SDK__;
  if (sdk && typeof sdk.authedFetch === "function") {
    return (url, init) => sdk.authedFetch(url, init);
  }
  return (url, init) => fetch(url, init);
}

async function request(path, opts = {}) {
  // Merge headers instead of letting a caller's `opts.headers` silently drop
  // Content-Type: `{headers: X, ...opts}` would replace the whole object, and
  // SDK transports add their own auth headers on top.
  const { headers, ...rest } = opts;
  let res;
  try {
    res = await transport()(BASE + path, {
      ...rest,
      // GETs must never be answered from the browser HTTP cache: a cached
      // /state or /models would show stale routes after an apply. Standard
      // RequestInit; the SDK transport passes it through.
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...(headers || {}) },
    });
  } catch (e) {
    throw new ApiError("Network failure: " + (e && e.message ? e.message : String(e)));
  }
  let body = null;
  try {
    body = await res.json();
  } catch (e) {
    if (res.ok) throw new ApiError("Malformed response body (not JSON)", { status: res.status });
    body = null;
  }
  if (!res.ok) {
    throw new ApiError(errorMessage(body, res.status), { status: res.status, body });
  }
  if (!body || body.ok !== true) {
    // ok:false (or missing ok) is a failure, not empty data.
    throw new ApiError(errorMessage(body, res.status), { status: res.status, body });
  }
  return body;
}

// The host mounts these routes on FastAPI, which wraps an HTTPException's payload
// under ``detail`` — and that payload is where the engine puts the ACTIONABLE
// sentence, e.g. "apply blocked: the canonical route was rejected" plus the reason
// ("Nous Portal is not connected: no API key or login was found…"). Reporting only
// the status leaves the user with "HTTP 409" and nothing to fix.
// Exported for unit tests: the unwrapping rules are the difference between an
// actionable sentence and a bare "HTTP 409".
export function errorMessage(body, status) {
  const detail = body && typeof body.detail === "object" && body.detail !== null ? body.detail : null;
  const message = asText(detail && detail.error) || asText(body && body.error) || null;
  // `reasons` may arrive as a slug list (array) or, unexpectedly, a plain
  // string — `.join` on a string would throw inside the error path itself.
  const raw = asPresent(detail && detail.reasons) || asPresent(body && body.reasons);
  const reasons = (Array.isArray(raw) ? raw : raw ? [raw] : []).map(asText).filter(Boolean);
  if (!message) return "HTTP " + status;
  return reasons.length ? `${message} — ${reasons.join("; ")}` : message;
}

// Only scalars are displayable. Ternaries alone are not enough: an object lands
// in the middle of a template string and the user reads "[object Object]".
function asText(v) {
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return null;
}

function asPresent(v) {
  return v === undefined || v === null || v === "" ? null : v;
}

// Provider-aware model catalog query. The endpoint is scoped to a provider (and
// optionally a profile) so the picker never offers models that cannot be routed
// there. `refresh` forces a live re-fetch instead of a cached catalog.
// Exported so the URL contract is unit-testable without a browser.
export function modelsUrl({ provider, profileId, refresh } = {}) {
  const qs = new URLSearchParams();
  if (provider) qs.set("provider", provider);
  if (profileId) qs.set("profileId", profileId);
  if (refresh) qs.set("refresh", "1");
  const s = qs.toString();
  return "/models" + (s ? "?" + s : "");
}

export const api = {
  loadState: () => request("/state"),
  preview: (changes) => request("/preview", { method: "POST", body: JSON.stringify({ changes }) }),
  savePreset: (preset) => request("/presets", { method: "POST", body: JSON.stringify(preset) }),
  // `allowUnknownCost` is the explicit, checked acknowledgement for the current
  // review; the backend enforces the cost-approval policy, so the UI never
  // implies an unknown-cost route is free by omitting the field.
  apply: (payload) => request("/operations", { method: "POST", body: JSON.stringify(payload) }),
  getOperation: (id) => request("/operations/" + encodeURIComponent(id)),
  undo: (id) => request("/operations/" + encodeURIComponent(id) + "/undo", { method: "POST" }),
  // GET /models?provider=&profileId=&refresh=1 →
  // { ok:true, provider, models:[{id,name}], fetchedAt, source, stale, error }
  getModels: (opts) => request(modelsUrl(opts)),
};
