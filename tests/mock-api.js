// In-page mock of the plugin backend. Injected before the bundle loads.
// Test hooks: window.__mockApi = { failState, failReadback, failUndo, okFalse,
// rollbackMode: "compensated"|"partial"|"recovery_required", noWrite,
// statusOverride, overrideState }
(function () {
  const BASE = "/api/plugins/hermes-model-switcher";

  window.__mockApi = Object.assign(
    { failState: false, failReadback: false, failUndo: false, failSavePreset: false, failStateHttp: false, blockedApply: false },
    JSON.parse(sessionStorage.getItem("hms-mock-flags") || "{}"),
  );
  // Recorded API calls: [{ path, method, body }] — lets specs assert exact request bodies.
  window.__mockApi.requests = [];

  const profiles = [
    { id: "default", name: "Default", writable: true },
    { id: "research", name: "Research", writable: true },
    { id: "archived", name: "Archived", writable: false },
  ];

  const mk = (profileId, slotId, family, provider, model, extra) => Object.assign({
    profileId, slotId, family, provider, model, state: "configured", writable: true,
    storagePath: "config.yaml", lastObserved: { provider, model, at: "2026-10-12T10:00:00Z" },
  }, extra || {});

  const slots = [
    mk("default", "main", "main", "anthropic", "claude-sonnet-4-6"),
    mk("default", "auxiliary.goal_judge", "judges", "anthropic", "claude-haiku-4-5",
      { protocol: "typed-decision", note: "Typed decision model — not interchangeable with chat models" }),
    mk("default", "cron.model", "automation", "openai", "gpt-5.1", { storagePath: "cron jobs store" }),
    mk("research", "main", "main", "openai", "gpt-5.1"),
    mk("research", "delegation.fallback_providers", "fallback", "openai", "gpt-5-mini"),
    mk("archived", "main", "main", "anthropic", "claude-sonnet-4-6", { writable: false }),
  ];

  const presets = [{
    id: "p-fast", name: "Fast lane",
    profileIds: ["default", "research"], families: ["main"],
    assignments: { main: { provider: "zhipu", model: "glm-5.3-flash" } },
    billingPolicy: "metered-approved",
  }];

  let previewSeq = 0, opSeq = 0;
  let lastPreview = null;
  const operations = {};

  function failUnless(check) {
    return check; // readability helper
  }

  async function handle(path, method, body) {
    const M = window.__mockApi;
    if (path === BASE + "/state" && method === "GET") {
      if (M.failState) return { status: 200, body: { ok: false, error: "raw config unreadable" } };
      if (M.failStateHttp) return { status: 500, body: { error: "internal error" } };
      return { status: 200, body: { ok: true, profiles, slots, presets } };
    }
    if (path === BASE + "/preview" && method === "POST") {
      const changes = (body.changes || []).map((c) => ({
        ...c,
        sideEffects: c.action === "set" && c.to.provider === "zhipu" ? ["canonical tool defaults seeded"] : [],
      }));
      const blocked = changes.filter((c) => !profiles.find((p) => p.id === c.profileId && p.writable))
        .map((c) => ({ profileId: c.profileId, reason: "profile is read-only" }));
      if (blocked.length) return { status: 200, body: { ok: true, preview: { id: "prev-x", changes: [], blocked } } };
      const preview = { id: "prev-" + (++previewSeq), changes };
      lastPreview = preview;
      return { status: 200, body: { ok: true, preview } };
    }
    if (path === BASE + "/presets" && method === "POST") {
      if (M.failSavePreset) return { status: 200, body: { ok: false, error: "journal write failed" } };
      const preset = { id: "p-" + (presets.length + 1), name: body.name, profileIds: ["default", "research"], families: ["main"], billingPolicy: "reviewed" };
      presets.push(preset);
      return { status: 200, body: { ok: true, preset } };
    }
    if (path === BASE + "/operations" && method === "POST") {
      // Host-shaped 409: FastAPI wraps the engine's refusal under ``detail``.
      // The UI must show that sentence, not a bare status code.
      if (M.blockedApply) return { status: 409, body: { detail: {
        ok: false, blocked: true,
        error: "apply blocked: the canonical route was rejected",
        reasons: ["Nous Portal is not connected: no API key or login was found for it."],
      } } };
      const preview = lastPreview;
      if (!preview || (body.previewId && body.previewId !== preview.id))
        return { status: 200, body: { ok: false, error: "unknown or stale preview" } };
      const failed = Boolean(M.failReadback);
      const rollback = M.rollbackMode || null; // "compensated" | "partial" | "recovery_required"
      const noWrite = Boolean(M.noWrite); // F1 known no-write conflict
      const id = "op-" + (++opSeq);
      // Mirror the REAL POST /operations DTO (_op_dto): id / state / results /
      // undoable, and — on a failed apply — a SAFE failure category plus
      // final_state. The real backend never sends a per-step ``readback`` object
      // nor raw exception text, so neither does this mock: the UI must render
      // failures from ``results[].status`` / ``operation.failure`` alone.
      //
      // ``rollbackMode`` covers an apply that fails MID-operation and then
      // compensates: the per-step status is the same ``compensated`` the undo
      // path uses, so the drawer must not read it as a verified undo.
      // ``noWrite`` covers the F1 revision-conflict case where nothing is
      // written at all (status ``not_written``, state ``pending``).
      const pids = preview.changes.length ? dedupeProfiles(preview.changes) : [];
      let state = "committed", failure = null, statusFor = () => "applied";
      if (M.statusOverride) {
        // Unknown/extra per-step status the backend may add later: the drawer
        // must render it NEUTRAL (never green) instead of guessing success.
        state = M.overrideState || "recovery_required";
        statusFor = () => M.statusOverride;
      } else if (noWrite) {
        state = "pending"; failure = "revision_conflict";
        statusFor = () => "not_written";
      } else if (rollback === "compensated") {
        // Every step rolled back, no failure category — the rollback is the
        // whole outcome (this is the case that used to read "0 · 0").
        state = "compensated";
        statusFor = () => "compensated";
      } else if (rollback === "partial") {
        // Mixed: some steps rolled back, some never verified.
        state = "partial";
        statusFor = (i) => (i % 2 === 0 ? "compensated" : "failed");
      } else if (rollback === "recovery_required") {
        state = "recovery_required"; failure = "write_verification_failed";
        statusFor = () => "failed";
      } else if (failed) {
        state = "recovery_required"; failure = "write_verification_failed";
        statusFor = () => "failed";
      }
      const committed = state === "committed";
      const results = pids.map((pid, i) => {
        const status = statusFor(i);
        const changed = status === "applied";
        return {
          profileId: pid, status,
          revisionBefore: "rev" + pid + "-a",
          revisionAfter: changed ? "rev" + pid + "-b" : "rev" + pid + "-a",
        };
      });
      const operation = {
        id,
        state,
        results,
        // A non-committed operation is not undoable through the drawer; the
        // backend exposes that as ``undoable: false``.
        undoable: results.some((r) => r.status === "applied"),
        // Terminal classification + a FIXED failure category (never raw text).
        final_state: state,
        failure,
      };
      operations[id] = operation;
      // Envelope semantics: top-level ``ok`` is true only for committed/undone,
      // so a failed apply is HTTP 200 with ok:false AND the operation DTO — the
      // exact shape the UI must NOT discard.
      return { status: 200, body: { ok: committed, operation } };
    }
    const opUndo = path.match(new RegExp("^" + BASE + "/operations/([^/]+)/undo$"));
    if (opUndo && method === "POST") {
      const op = operations[opUndo[1]];
      if (!op) return { status: 200, body: { ok: false, error: "unknown operation" } };
      if (M.failUndo) return { status: 200, body: { ok: false, error: "revision conflict: profile edited after apply" } };
      // Real undo compensates each applied step: step state "compensated"
      // (rendered "Restored (undo verified)"), operation state "undone".
      const operation = {
        id: op.id,
        state: "undone",
        results: op.results.map((r) => ({
          ...r,
          status: r.status === "applied" ? "compensated" : r.status,
          revisionAfter: "rev" + r.profileId + "-a",
        })),
        undoable: false,
        final_state: "undone",
        failure: null,
      };
      operations[op.id] = operation;
      return { status: 200, body: { ok: true, operation } };
    }
    const opGet = path.match(new RegExp("^" + BASE + "/operations/([^/]+)$"));
    if (opGet && method === "GET") {
      const op = operations[opGet[1]];
      if (!op) return { status: 200, body: { ok: false, error: "unknown operation" } };
      // ok iff committed/undone, mirroring the backend envelope.
      return { status: 200, body: { ok: op.state === "committed" || op.state === "undone", operation: op } };
    }
    return { status: 404, body: { error: "not found: " + path } };
  }

  function dedupeProfiles(changes) {
    return [...new Set(changes.map((c) => c.profileId))];
  }

  const origFetch = window.fetch;
  window.fetch = async function (url, opts) {
    const u = new URL(url, location.origin);
    if (u.pathname.startsWith(BASE)) {
      const method = (opts && opts.method) || "GET";
      let body = {};
      try { body = opts && opts.body ? JSON.parse(opts.body) : {}; } catch (e) {}
      window.__mockApi.requests.push({ path: u.pathname, method, body });
      const res = await handle(u.pathname, method, body);
      return new Response(JSON.stringify(res.body), {
        status: res.status, headers: { "Content-Type": "application/json" },
      });
    }
    return origFetch.apply(this, arguments);
  };
})();
