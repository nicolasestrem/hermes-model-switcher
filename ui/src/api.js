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

async function request(path, opts = {}) {
  let res;
  try {
    res = await fetch(BASE + path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
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
    throw new ApiError(
      (body && body.error) || "HTTP " + res.status,
      { status: res.status, body },
    );
  }
  if (!body || body.ok !== true) {
    // ok:false (or missing ok) is a failure, not empty data.
    throw new ApiError(
      (body && body.error) || "Response carried ok:false", { status: res.status, body },
    );
  }
  return body;
}

export const api = {
  loadState: () => request("/state"),
  preview: (changes) => request("/preview", { method: "POST", body: JSON.stringify({ changes }) }),
  savePreset: (preset) => request("/presets", { method: "POST", body: JSON.stringify(preset) }),
  apply: (payload) => request("/operations", { method: "POST", body: JSON.stringify(payload) }),
  getOperation: (id) => request("/operations/" + encodeURIComponent(id)),
  undo: (id) => request("/operations/" + encodeURIComponent(id) + "/undo", { method: "POST" }),
};
