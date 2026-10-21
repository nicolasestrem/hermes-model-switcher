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

// ---------------------------------------------------------------------------
// Host data: Hermes' own public model routes
// ---------------------------------------------------------------------------
// These are NOT plugin routes. They live on the host (`/api/model/...`), so
// they are called through the dashboard SDK's `fetchJSON` (session token, base
// path and auth all handled by the shell) with an EXPLICIT `?profile=`, never
// through the plugin prefix and never through a helper that defaults to the
// "management profile" — the switcher must read the profile it is showing.
export const HOST_ROUTES = {
  options: "/api/model/options",
  auxiliary: "/api/model/auxiliary",
  moa: "/api/model/moa",
  set: "/api/model/set",
};

// The profile name `/state` reports ("default" for the root home — verified
// against the host's `_config_profile_scope`/`_resolve_profile_dir`, where
// "default" is the alias for ~/.hermes).
export function hostOptionsPath(profileId, { refresh = false } = {}) {
  const qs = new URLSearchParams();
  if (profileId) qs.set("profile", profileId);
  // Dashboard surfaces keep the full provider universe (setup affordances).
  qs.set("include_unconfigured", "1");
  if (refresh) qs.set("refresh", "1");
  return HOST_ROUTES.options + "?" + qs.toString();
}

export function hostAuxiliaryPath(profileId) {
  const qs = new URLSearchParams();
  if (profileId) qs.set("profile", profileId);
  const s = qs.toString();
  return HOST_ROUTES.auxiliary + (s ? "?" + s : "");
}

export function hostMoaPath(profileId) {
  const qs = new URLSearchParams();
  if (profileId) qs.set("profile", profileId);
  const s = qs.toString();
  return HOST_ROUTES.moa + (s ? "?" + s : "");
}

// Numeric HTTP status of anything a host call threw. The SDK throws an
// `ApiError` carrying `status`; the fixture SDK throws `Error("<status>: ...")`.
// Anything else (network failure) is 0. Only the NUMBER ever leaves this
// module — host response text is never forwarded anywhere.
export function hostStatusOf(err) {
  if (!err) return 0;
  const direct = Number(err.status);
  if (Number.isFinite(direct) && direct > 0) return Math.trunc(direct);
  const m = /^\s*(\d{3})\b/.exec(String(err.message || ""));
  return m ? Number(m[1]) : 0;
}

// A failed host part, as a fixed category the UI can show: the route name and
// the numeric status. Never the host's message.
export function hostFailureText(failures) {
  const list = Array.isArray(failures) ? failures : [];
  if (!list.length) return "";
  const routes = [...new Set(list.map((f) => f.part))];
  const statuses = [...new Set(list.map((f) => f.status))];
  const routeText = routes.join(", ");
  const statusText = statuses.filter((s) => s > 0).map((s) => "HTTP " + s).join(", ") || "unreachable";
  return "Host data unavailable (" + routeText + " — " + statusText + "). " +
    "Catalog and assignment details may be incomplete.";
}

// SDK.fetchJSON injects the session token. Resolve the SDK on every request;
// missing SDK is unavailable evidence, never permission for a bare host fetch.
function hostTransport() {
  const sdk = globalThis.__HERMES_PLUGIN_SDK__;
  if (sdk && typeof sdk.fetchJSON === "function") {
    return (url, init) => sdk.fetchJSON(url, init);
  }
  throw new ApiError("Dashboard SDK unavailable", { status: 0 });
}

// Validate real host read shapes, not the plugin's {ok:true} envelope.
const record = (v) => Boolean(v && typeof v === "object" && !Array.isArray(v));
const routePair = (v) => record(v) && typeof v.provider === "string" && typeof v.model === "string";
export function validHostPart(part, value) {
  if (!record(value)) return false;
  if (part === "options") return Array.isArray(value.providers) &&
    typeof value.provider === "string" && typeof value.model === "string" &&
    value.providers.every((p) => record(p) && typeof p.slug === "string" &&
      typeof p.name === "string" && Array.isArray(p.models) && p.models.every((m) => typeof m === "string"));
  if (part === "auxiliary") return Array.isArray(value.tasks) && routePair(value.main) &&
    value.tasks.every((t) => routePair(t) && typeof t.task === "string" && typeof t.base_url === "string");
  if (part === "moa") return typeof value.default_preset === "string" &&
    typeof value.active_preset === "string" && record(value.presets) &&
    Object.values(value.presets).every((preset) => record(preset) &&
      Array.isArray(preset.reference_models) && preset.reference_models.every(routePair) && routePair(preset.aggregator));
  return false;
}

// Fail closed before an authenticated write. Body profile overrides query on
// the host; a preconfirmed body would bypass the user's confirmation dialog.
export function validateHostCall(call, operationId) {
  const fail = () => { throw new ApiError("Invalid host step", { status: 0 }); };
  if (!record(call) || call.opId !== operationId || !Number.isInteger(call.seq) || call.seq < 1 ||
      typeof call.token !== "string" || !call.token || call.method !== "POST" || call.path !== HOST_ROUTES.set ||
      !record(call.query) || Object.keys(call.query).some((k) => k !== "profile") ||
      typeof call.query.profile !== "string" || !call.query.profile || !record(call.body)) fail();
  const b = call.body;
  const allowed = ["scope", "provider", "model", "task", "base_url", "reasoning_effort"];
  if (Object.keys(b).some((k) => !allowed.includes(k)) || !["main", "auxiliary"].includes(b.scope) ||
      typeof b.provider !== "string" || typeof b.model !== "string") fail();
  if (b.scope === "main" && ("base_url" in b || "task" in b || "reasoning_effort" in b)) fail();
  if (b.scope === "auxiliary" && (typeof b.task !== "string" || !b.task)) fail();
  if ("base_url" in b) {
    try { const u = new URL(b.base_url); if (u.username || u.password || u.search || u.hash || !["http:", "https:"].includes(u.protocol)) fail(); }
    catch { fail(); }
  }
  return call;
}

// One host request. Always resolves to the parsed body or throws ApiError
// (with a numeric status); the caller decides what a failure means.
export async function hostJSON(path, init) {
  try {
    return await hostTransport()(path, {
      ...init, cache: "no-store",
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    });
  } catch (e) {
    if (e instanceof ApiError) throw e;
    throw new ApiError("HTTP " + hostStatusOf(e), { status: hostStatusOf(e) });
  }
}

// Every host part one profile can contribute, keyed exactly as the backend's
// `host` bundle expects. A missing part stays `null` (never `{}`): the backend
// reads null as "no evidence", which is different from "empty evidence".
export const HOST_PARTS = ["options", "auxiliary", "moa"];

export function emptyHostBundle() {
  return { profiles: {} };
}

// Host bundle for a set of profiles: at most `limit` (3) profiles in flight,
// one request at a time inside a profile. Never throws: unreachable routes
// degrade to `null` plus one entry in `failures`, so a 404 on an older Hermes
// can never block the tab.
export async function loadHostBundle(profileIds, { refresh = false, limit = 3 } = {}) {
  const ids = [...new Set((profileIds || []).filter((id) => typeof id === "string" && id))];
  const host = emptyHostBundle();
  const failures = [];
  const pathFor = (part, pid) => (part === "options" ? hostOptionsPath(pid, { refresh })
    : part === "auxiliary" ? hostAuxiliaryPath(pid) : hostMoaPath(pid));
  let cursor = 0;
  const worker = async () => {
    while (cursor < ids.length) {
      const pid = ids[cursor];
      cursor += 1;
      const parts = {};
      for (const part of HOST_PARTS) {
        try {
          const value = await hostJSON(pathFor(part, pid));
          if (!validHostPart(part, value)) throw new ApiError("Malformed host data", { status: 200 });
          parts[part] = value;
        } catch (e) {
          parts[part] = null;
          failures.push({ profileId: pid, part, status: hostStatusOf(e) });
        }
      }
      host.profiles[pid] = parts;
    }
  };
  const width = Math.max(1, Math.min(limit, ids.length || 1));
  await Promise.all(Array.from({ length: ids.length ? width : 0 }, worker));
  return { host, failures };
}

// `POST /catalog` / `POST /models` body: the profile, the optional provider
// filter, the explicit refresh flag, and the host OPTIONS payload for that
// profile (the catalog is a pure transform of it). The whole host bundle rides
// along so the backend can evidence credentials without another round trip.
export function catalogBody({ profileId, provider, refresh, host } = {}) {
  const pid = profileId || "";
  const body = {
    profile: pid,
    options: (host && host.profiles && host.profiles[pid] && host.profiles[pid].options) || null,
    host: host || null,
  };
  if (provider) body.provider = provider;
  if (refresh) body.refresh = true;
  return body;
}

export function modelsBody(opts) {
  return catalogBody(opts);
}

export const api = {
  // `GET /state` is config-only; `POST /state {host}` returns the same DTO
  // enriched with host-derived rows (plugin auxiliary tasks, credential
  // status). The UI paints from GET first and upgrades when host data lands.
  loadState: (host) => (host && host.profiles
    ? request("/state", { method: "POST", body: JSON.stringify({ host }) })
    : request("/state")),
  preview: (changes, host) => request("/preview", {
    method: "POST", body: JSON.stringify(host && host.profiles ? { changes, host } : { changes }) }),
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
  recover: (id) => request("/operations/" + encodeURIComponent(id) + "/recover",
    { method: "POST", preserveOperationFailure: true }),
  undo: (id) => request("/operations/" + encodeURIComponent(id) + "/undo",
    { method: "POST", preserveOperationFailure: true }),
  // `POST /models {profile, provider, options, refresh, host}` →
  // { ok:true, provider, models:[{id,name}], fetchedAt, source, stale, error }
  getModels: (opts) => request("/models", { method: "POST", body: JSON.stringify(modelsBody(opts)) }),
  // `POST /catalog {profile, provider, options, refresh, host}` → frozen
  // aggregate DTO {ok, profileId, providers:[...], cost:{...}, error}.
  getCatalog: (opts) => request("/catalog", { method: "POST", body: JSON.stringify(catalogBody(opts)) }),
  // One step of the host half of a backend operation. `call` is the envelope's
  // `hostCall` (`{opId, seq, token, method, path, query, body}`); the profile is
  // the query value, URL-encoded, and the body is sent as JSON.
  hostCall: (call, { confirmExpensiveModel = false } = {}) => {
    validateHostCall(call, call && call.opId);
    const qs = new URLSearchParams();
    const query = call.query && typeof call.query === "object" ? call.query : {};
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === "") continue;
      qs.set(key, String(value));
    }
    const search = qs.toString();
    const body = { ...(call.body && typeof call.body === "object" ? call.body : {}) };
    if (confirmExpensiveModel) body.confirm_expensive_model = true;
    return hostJSON(String(call.path) + (search ? "?" + search : ""), {
      method: call.method || "POST",
      body: JSON.stringify(body),
    });
  },
  // Report the host step's outcome. `status` is the numeric HTTP status only —
  // the backend classifies it; host response text is never forwarded.
  hostResult: (opId, { seq, token, outcome, status }) => request(
    "/operations/" + encodeURIComponent(opId) + "/host-result",
    {
      method: "POST",
      body: JSON.stringify(status ? { seq, token, outcome, status } : { seq, token, outcome }),
      preserveOperationFailure: true,
    }),
};
