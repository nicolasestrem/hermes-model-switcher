// In-page mock of the plugin backend. Injected before the bundle loads.
// Test hooks: window.__mockApi = { failState, failReadback, failUndo, okFalse,
// rollbackMode: "compensated"|"partial"|"recovery_required", noWrite,
// statusOverride, overrideState, undoPartial }
(function () {
  const BASE = "/api/plugins/hermes-model-switcher";

  window.__mockApi = Object.assign(
    { failState: false, failReadback: false, failUndo: false, failSavePreset: false, failStateHttp: false, blockedApply: false,
      catalogFail: false, catalogDelayMs: 0, modelsFail: false, modelsDelayMs: 0,
      // Catalog-view scenarios (all off by default so existing fixtures keep
      // their exact totals): a pending cost probe, an empty catalog, a model id
      // duplicated across providers, a >50-model provider, models on the
      // unauthenticated provider, and /state slots that join to catalog rows.
      catalogPending: false, catalogEmpty: false, catalogDuplicateModel: false,
      catalogManyModels: false, catalogPortalModels: false, catalogLinkedSlots: false,
      catalogNoPrices: false, catalogZeroPriced: false },
    JSON.parse(sessionStorage.getItem("hms-mock-flags") || "{}"),
  );
  // Recorded API calls: [{ path, search, method, body }] — lets specs assert
  // exact request bodies and query parameters (e.g. catalog refresh=1).
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

  // ---- aggregate/mock model catalog (frozen GET /catalog DTO) -------------
  // Synthetic but contract-exact: one priced row, one free row, one included,
  // one unknown (never "$0.00"/free), one unauthenticated provider and one
  // provider whose catalog refresh failed (warning row).
  const usd = (v) => (v === null || v === undefined ? null : `$${Number(v).toFixed(2)}`);
  const pricedCost = (input, output) => ({
    unit: "USD_per_1M_tokens", input, output, cacheRead: 0.25,
    display: { input: usd(input), output: usd(output), cache: usd(0.25) },
    free: false, source: "picker", pricingVersion: "2026-10",
  });
  const freeCost = () => ({
    unit: "USD_per_1M_tokens", input: 0, output: 0, cacheRead: 0,
    display: { input: "$0.00", output: "$0.00", cache: "$0.00" },
    free: true, source: "subscription_route", pricingVersion: null,
  });
  // A priced route may legitimately state a zero (or "free") rate for ONE
  // direction while charging for the other. These are real provider display
  // strings, not unknown data, and must reach the page verbatim.
  const zeroInputCost = () => ({
    unit: "USD_per_1M_tokens", input: 0, output: 0.5, cacheRead: null,
    display: { input: "$0.00", output: usd(0.5), cache: null },
    free: false, source: "picker", pricingVersion: "2026-10",
  });
  const freeInputCost = () => ({
    unit: "USD_per_1M_tokens", input: null, output: 1, cacheRead: null,
    display: { input: "free", output: usd(1), cache: null },
    free: false, source: "picker", pricingVersion: "2026-10",
  });
  const includedCost = () => ({
    unit: "USD_per_1M_tokens", input: null, output: null, cacheRead: null,
    display: { input: null, output: null, cache: null },
    free: false, source: "subscription_route", pricingVersion: null,
  });

  const catalogProviders = [
    { id: "nous", label: "Nous", authenticated: true, configured: true, isCurrent: true,
      fetchedAt: "2026-10-18T12:00:00Z", stale: false, warning: null,
      models: [
        { id: "hermes-4-405b", label: "Hermes 4 405B", pricingStatus: "priced", cost: pricedCost(10, 50) },
        { id: "hermes-4-70b", label: "Hermes 4 70B", pricingStatus: "free", cost: freeCost() },
        { id: "hermes-4-mini", label: "Hermes 4 Mini", pricingStatus: "included", cost: includedCost() },
        { id: "hermes-unknown", label: "Hermes Unknown", pricingStatus: "unknown", cost: null },
        ...[5, 6, 7, 8, 9, 10, 11, 12].map((n) => ({
          id: `hermes-filler-${n}`, label: `Hermes Filler ${n}`,
          pricingStatus: "priced", cost: pricedCost(1, n),
        })),
      ] },
    { id: "portal", label: "Nous Portal", authenticated: false, configured: false, isCurrent: false,
      fetchedAt: null, stale: true, warning: "Sign-in required: no Portal session found", models: [] },
    { id: "opencode-go", label: "OpenCode Go", authenticated: true, configured: true, isCurrent: false,
      fetchedAt: "2026-10-17T09:30:00Z", stale: true, warning: null,
      models: [
        { id: "deepseek-v4.1-flash", label: "DeepSeek V4.1 Flash", pricingStatus: "priced", cost: pricedCost(0.1, 0.5) },
        { id: "another-model", label: "Another Model", pricingStatus: "unknown", cost: null },
      ] },
    { id: "ollama-local", label: "Ollama (local)", authenticated: true, configured: true, isCurrent: false,
      fetchedAt: "2026-10-16T18:00:00Z", stale: false, warning: null,
      models: [
        { id: "llama3.3-local", label: "Llama 3.3 Local", pricingStatus: "unknown", cost: null },
      ] },
    { id: "broken-provider", label: "Broken Provider", authenticated: true, configured: true, isCurrent: false,
      fetchedAt: "2026-10-18T11:00:00Z", stale: true,
      warning: "catalog fetch failed: upstream timeout; retry or type an exact model id",
      models: [] },
  ];

  const bulkModels = Array.from({ length: 60 }, (_, i) => {
    const n = i + 1;
    return {
      id: `bulk-model-${String(n).padStart(2, "0")}`, label: `Bulk Model ${n}`,
      pricingStatus: "priced", cost: pricedCost(0.5 + n / 100, 1 + n / 100),
    };
  });
  const gatewayProvider = {
    id: "gateway", label: "Gateway", authenticated: true, configured: true, isCurrent: false,
    fetchedAt: "2026-10-18T13:00:00Z", stale: false, warning: null,
    models: [
      { id: "hermes-4-405b", label: "Hermes 4 405B (gateway)", pricingStatus: "priced", cost: pricedCost(99, 199) },
    ],
  };
  const portalModel = {
    id: "portal-chat", label: "Portal Chat", pricingStatus: "priced", cost: pricedCost(2, 4),
  };
  const zeroPricedModels = [
    { id: "zero-input-paid-output", label: "Zero Input, Paid Output",
      pricingStatus: "priced", cost: zeroInputCost() },
    { id: "free-input-paid-output", label: "Free Input, Paid Output",
      pricingStatus: "priced", cost: freeInputCost() },
  ];

  function catalogFor(profileId, provider, refresh) {
    const M = window.__mockApi;
    let base = M.catalogEmpty
      ? []
      : catalogProviders.map((p) => {
        if (p.id === "nous" && M.catalogManyModels) return { ...p, models: [...p.models, ...bulkModels] };
        if (p.id === "nous" && M.catalogZeroPriced) return { ...p, models: [...p.models, ...zeroPricedModels] };
        if (p.id === "portal" && M.catalogPortalModels) return { ...p, models: [...p.models, portalModel] };
        return p;
      });
    if (M.catalogDuplicateModel && !M.catalogEmpty) base = [...base, gatewayProvider];
    const rows = provider
      ? base.filter((p) => p.id === provider)
      : base;
    const providers = rows.map((p) => {
      const models = (p.models || []).map((m) => {
        const mapped = {
          id: m.id, label: m.label, pricingStatus: m.pricingStatus, cost: m.cost,
        };
        // Every row unknown: exercises the "no cheapest tile when no prices"
        // branch without inventing a provider-specific shape.
        if (M.catalogNoPrices) return { ...mapped, pricingStatus: "unknown", cost: null };
        return mapped;
      });
      return {
        id: p.id, label: p.label, authenticated: p.authenticated, configured: p.configured,
        isCurrent: p.isCurrent, modelCount: models.length,
        fetchedAt: refresh ? "2026-10-18T18:00:00Z" : p.fetchedAt,
        stale: refresh ? false : p.stale,
        warning: p.warning, models,
      };
    });
    return {
      ok: true,
      profileId: profileId || null,
      providers,
      cost: {
        pending: Boolean(M.catalogPending),
        unavailableProviders: providers.filter((p) => !p.authenticated || p.warning).map((p) => p.id),
      },
      error: null,
    };
  }

  function modelsFor(provider) {
    const row = catalogProviders.find((p) => p.id === provider);
    if (!row || row.authenticated === false) {
      return { ok: false, provider, models: [], fetchedAt: null, source: "unknown", stale: true,
        error: `provider '${provider}' is not available in this profile's catalog (not configured or not authenticated)` };
    }
    const extra = provider === "nous" && window.__mockApi.catalogZeroPriced
      ? zeroPricedModels : [];
    const models = [...(row.models || []), ...extra].map((m) => ({ id: m.id, name: m.label }));
    return {
      ok: true, provider, models, fetchedAt: row.fetchedAt, source: "picker", stale: row.stale,
      error: row.warning || (models.length ? null : `provider '${provider}' reported no models`),
    };
  }

  let previewSeq = 0, opSeq = 0;
  let lastPreview = null;
  const operations = {};

  function failUnless(check) {
    return check; // readability helper
  }

  async function handle(path, method, body, params) {
    const M = window.__mockApi;
    if (path === BASE + "/catalog" && method === "GET") {
      if (M.catalogDelayMs) await new Promise((resolve) => setTimeout(resolve, M.catalogDelayMs));
      if (M.catalogFail) return { status: 200, body: { ok: false, error: "catalog unavailable: upstream timeout" } };
      return { status: 200, body: catalogFor(params.get("profileId"), params.get("provider"), params.has("refresh")) };
    }
    if (path === BASE + "/models" && method === "GET") {
      if (M.modelsDelayMs) await new Promise((resolve) => setTimeout(resolve, M.modelsDelayMs));
      if (M.modelsFail) {
        return { status: 200, body: { ok: false, provider: params.get("provider") || "", models: [],
          fetchedAt: null, source: "unknown", stale: true, error: "model catalog unavailable: upstream timeout" } };
      }
      return { status: 200, body: modelsFor(params.get("provider") || "") };
    }
    if (path === BASE + "/state" && method === "GET") {
      if (M.failState) return { status: 200, body: { ok: false, error: "raw config unreadable" } };
      if (M.failStateHttp) return { status: 500, body: { error: "internal error" } };
      const linked = M.catalogLinkedSlots
        ? [
          mk("default", "auxiliary.vision", "auxiliary", "nous", "hermes-4-405b"),
          mk("research", "cron.model", "automation", "nous", "hermes-4-405b"),
          mk("research", "auxiliary.vision", "auxiliary", "nous", "hermes-4-405b"),
        ]
        : [];
      return { status: 200, body: { ok: true, profiles, slots: [...slots, ...linked], presets } };
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
      // path uses, so the drawer must not read it as a verified undo — and the
      // engine persists the SAFE category BEFORE compensating (engine.py), so
      // the real DTO carries ``failure`` alongside state ``compensated``.
      // ``noWrite`` covers the F1 revision-conflict case where nothing is
      // written: the FIRST step fails its pre-write recheck and the steps
      // behind it stay ``pending`` (journal step states are only
      // pending|applied|failed|compensated|conflict — never ``not_written``),
      // op state ``pending`` with category ``revision_conflict``.
      const pids = preview.changes.length ? dedupeProfiles(preview.changes) : [];
      let state = "committed", failure = null, statusFor = () => "applied";
      if (M.statusOverride) {
        // Unknown/extra per-step status the backend may add later: the drawer
        // must render it NEUTRAL (never green) instead of guessing success.
        state = M.overrideState || "recovery_required";
        statusFor = () => M.statusOverride;
      } else if (noWrite) {
        // F1: the first step fails its pre-write revision recheck; the steps
        // behind it are never attempted and stay ``pending``. The op stays
        // retryable: state/final_state ``pending`` + category
        // ``revision_conflict``.
        state = "pending"; failure = "revision_conflict";
        statusFor = (i) => (i === 0 ? "failed" : "pending");
      } else if (rollback === "compensated") {
        // Every step was written then rolled back. The engine records the safe
        // category BEFORE compensating (engine.py:306-307), so the real DTO is
        // state/final_state ``compensated`` + failure
        // ``write_verification_failed`` (test_backend_reliability_fixes.py).
        state = "compensated"; failure = "write_verification_failed";
        statusFor = () => "compensated";
      } else if (rollback === "partial") {
        // Mixed: one step rolled back, one never verified; the safe category
        // for the failing step rides along with state/final_state ``partial``.
        state = "partial"; failure = "io_error";
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
      // A PARTIAL undo: the first applied step's previous bytes are restored
      // (``compensated``), but a later profile's revision changed since the apply
      // so its undo hits a ``conflict``. The WHOLE operation is therefore
      // ``recovery_required`` with ok:false — yet the restored profile's bytes
      // ARE verified back. ``failure`` is null: the conflicted STEP carries the
      // reason, and the real DTO sends no op-level safe category for this shape.
      if (M.undoPartial) {
        const operation = {
          id: op.id,
          state: "recovery_required",
          results: op.results.map((r) => ({
            ...r,
            status: r.profileId === "default" ? "compensated" : "conflict",
            revisionAfter: r.profileId === "default" ? "rev" + r.profileId + "-a" : r.revisionAfter,
          })),
          undoable: false,
          final_state: "recovery_required",
          failure: null,
        };
        operations[op.id] = operation;
        return { status: 200, body: { ok: false, operation } };
      }
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
      window.__mockApi.requests.push({ path: u.pathname, search: u.search, method, body });
      const res = await handle(u.pathname, method, body, u.searchParams);
      if (u.pathname === BASE + "/catalog" && method === "GET") {
        // Expose the exact served aggregate for specs that assert the rendered
        // group/row matrix against the API payload without a second request.
        window.__mockApi.lastCatalog = res.body;
      }
      return new Response(JSON.stringify(res.body), {
        status: res.status, headers: { "Content-Type": "application/json" },
      });
    }
    return origFetch.apply(this, arguments);
  };
})();
