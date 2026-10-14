// Plugin API client. Contract: /api/plugins/hermes-model-switcher/*
//
// HARD RULE (Review Focus #5): a response is a success only when the HTTP
// status is 2xx AND the parsed body has ok === true. A network failure, a
// non-JSON body, an HTTP error, or ok:false is a failure — never empty state,
// never a success badge.
//
// The ONE exception is the operation envelope (apply/undo/get): a non-success
// outcome (partial, compensated, recovery_required, pending) is HTTP 200 with
// ok:false but a real ``operation`` DTO attached. There ok:false means "did not
// fully succeed", not "discard the data" — see `preserveOperationFailure` in
// `request` and the `api` operation methods below.
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
  // `preserveOperationFailure` opts an /operations call into receiving an
  // HTTP-200 body whose top-level ``ok`` is false but which still carries a
  // structured ``operation`` DTO (a partial / compensated / recovery_required
  // apply). The operation endpoints are the ONLY ones where ok:false is a
  // meaningful outcome rather than an error.
  // `headers` is pulled out (not spread) so a caller's `opts.headers` cannot
  // drop Content-Type: `{headers: X, ...opts}` would replace the whole object,
  // and SDK transports add their own auth headers on top.
  const { headers, preserveOperationFailure = false, ...rest } = opts;
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
    // ok:false (or missing ok) is a failure, not empty data — EXCEPT when the
    // operation contract says otherwise: the backend reports a partial /
    // compensated / recovery_required apply as HTTP 200 with ok:false AND the
    // operation DTO (its top-level ok is true only for committed/undone).
    // Throwing would discard the per-profile statuses + safe failure category
    // and show a generic error instead of the real outcome. When the caller
    // opted in and a DTO is present, hand the body back for the drawer.
    if (preserveOperationFailure && body && typeof body.operation === "object" && body.operation !== null) {
      return body;
    }
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
  // FastAPI's block contract wraps a per-target list under ``detail.blocked``
  // ([{profileId, slotId?, reason}]). Rendering only the generic summary hides
  // WHICH slot was refused and why, so the user cannot fix anything: unwrap it
  // here, alongside ``reasons``. Malformed payloads are skipped, never printed
  // as "[object Object]", and never allowed to throw in this error path.
  const blocked = blockedReasons(
    asPresent(detail && detail.blocked) || asPresent(body && body.blocked));
  const parts = reasons.concat(blocked);
  if (!message) return "HTTP " + status;
  return parts.length ? `${message} — ${parts.join("; ")}` : message;
}

// Per-target block reasons (`{profileId, slotId?, reason}`) flattened into
// displayable sentences. A bare STRING entry is a single reason; anything
// without a scalar reason (or a non-string, non-object entry) is dropped rather
// than stringified.
function blockedReasons(raw) {
  const items = Array.isArray(raw) ? raw : raw ? [raw] : [];
  const out = [];
  for (const item of items) {
    if (item && typeof item === "object") {
      const reason = asText(item.reason);
      if (!reason) continue;
      const profileId = asText(item.profileId);
      const slotId = asText(item.slotId);
      // Match PreviewPanel's own blocked formatting: "profile/slot: reason".
      out.push(profileId ? `${profileId}${slotId ? "/" + slotId : ""}: ${reason}` : reason);
    } else if (typeof item === "string" && item.trim()) {
      out.push(item.trim());
    }
  }
  return out;
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
  // The three operation endpoints use `preserveOperationFailure`: a non-success
  // apply/undo (partial, compensated, recovery_required) arrives as HTTP 200
  // with ok:false AND the operation DTO, which the drawer must render.
  apply: (payload) => request("/operations", {
    method: "POST", body: JSON.stringify(payload), preserveOperationFailure: true }),
  getOperation: (id) => request("/operations/" + encodeURIComponent(id),
    { preserveOperationFailure: true }),
  undo: (id) => request("/operations/" + encodeURIComponent(id) + "/undo",
    { method: "POST", preserveOperationFailure: true }),
  // GET /models?provider=&profileId=&refresh=1 →
  // { ok:true, provider, models:[{id,name}], fetchedAt, source, stale, error }
  getModels: (opts) => request(modelsUrl(opts)),
};
