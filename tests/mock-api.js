// In-page mock of the plugin backend. Injected before the bundle loads.
// Test hooks: window.__mockApi = { failState, failReadback, failUndo, okFalse,
// rollbackMode: "compensated"|"partial"|"recovery_required", noWrite,
// statusOverride, overrideState, undoPartial, undoConflict }
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
      catalogNoPrices: false, catalogZeroPriced: false,
      // Real provider catalogs (e.g. Nous Portal) send label === id (true);
      // "missing" drops the label entirely.
      catalogIdLabels: false,
      // ---- host route emulation (Hermes' own /api/model/*) ----------------
      // `hostMissing` / `hostMissingParts` = the route is not served at all
      // (an older host, or a plugin running without it). `hostSetHttp` = the
      // assignment route answers that HTTP status (the UI must report the
      // NUMBER only). `hostNoWrite` = the host answers ok but wrote nothing
      // (the backend's `host_no_write` category). One model is priced above
      // the host's cost guard so /api/model/set asks for a confirmation.
      hostMissing: false, hostMissingParts: [], hostSetHttp: 0, hostNoWrite: false,
      hostDelayMs: 0,
      hostExpensiveModel: "hermes-4-405b-max",
      hostConfirmMessage: "This route is priced at $40.00 in / $200.00 out per 1M tokens. " +
        "<b>Confirm</b> to assign it anyway — <img src=x onerror=alert(1)>.",
      hostAckCount: 0 },
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
  // These optional rows are real mutable fixture assignments, not fresh copies
  // appended to each read (host writes and recovery must remain observable).
  if (window.__mockApi.catalogLinkedSlots) slots.push(
    mk("default", "auxiliary.vision", "auxiliary", "nous", "hermes-4-405b"),
    mk("research", "cron.model", "automation", "nous", "hermes-4-405b"),
    mk("research", "auxiliary.vision", "auxiliary", "nous", "hermes-4-405b"),
  );

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
          id: m.id, label: M.catalogIdLabels === "missing" ? undefined : M.catalogIdLabels ? m.id : m.label, pricingStatus: m.pricingStatus, cost: m.cost,
        };
        // Every row unknown: exercises the "no cheapest tile when no prices"
        // branch without inventing a provider-specific shape.
        if (M.catalogNoPrices) return { ...mapped, pricingStatus: "unknown", cost: null };
        if (M.catalogNullNumericModel === m.id && mapped.cost) return {
          ...mapped, cost: { ...mapped.cost, input: null, output: null, cacheRead: null },
        };
        return mapped;
      });
      return {
        id: p.id, label: p.label, authenticated: p.authenticated, configured: p.configured,
        isCurrent: p.isCurrent, modelCount: models.length,
        fetchedAt: M.catalogPickerCache ? null : refresh ? "2026-10-18T18:00:00Z" : p.fetchedAt,
        stale: M.catalogPickerCache ? true : refresh ? false : p.stale,
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
      return { ok: false, provider, providers: [], models: [], fetchedAt: null, source: "unknown", stale: true,
        error: `provider '${provider}' is not available in this profile's catalog (not configured or not authenticated)` };
    }
    const extra = provider === "nous" && window.__mockApi.catalogZeroPriced
      ? zeroPricedModels : [];
    const models = [...(row.models || []), ...extra].map((m) => ({ id: m.id, name: m.label }));
    return {
      ok: true, provider, models, providers: catalogProviders.map((p) => ({ id: p.id, name: p.label })),
      fetchedAt: row.fetchedAt, source: "picker", stale: row.stale,
      error: row.warning || (models.length ? null : `provider '${provider}' reported no models`),
    };
  }

  // ---- host route payloads (Hermes' /api/model/*) -----------------------
  // Shapes are the host's own: `hermes_cli/inventory.py::build_models_payload`
  // provider rows for /options, `AuxiliaryModelsResponse` for /auxiliary, and
  // `normalize_moa_config` for /moa. Not invented fields.
  function mainFor(profileId) {
    const slot = slots.find((s) => s.profileId === profileId && s.slotId === "main");
    return { provider: (slot && slot.provider) || "", model: (slot && slot.model) || "" };
  }

  function hostOptionsFor(profileId) {
    const providers = catalogProviders.map((p) => {
      const models = (p.models || []).map((m) => m.id);
      const pricing = {};
      const capabilities = {};
      for (const m of p.models || []) {
        const display = (m.cost && m.cost.display) || {};
        pricing[m.id] = {
          input: display.input || "", output: display.output || "",
          cache: display.cache === undefined ? null : display.cache,
          free: Boolean(m.cost && m.cost.free),
        };
        capabilities[m.id] = { fast: true, reasoning: false };
      }
      return {
        slug: p.id, name: p.label, models, total_models: models.length,
        is_current: Boolean(p.isCurrent), is_user_defined: /^custom:/.test(p.id),
        source: "canonical", aliases: [], api_url: null, auth_type: "api_key",
        authenticated: p.authenticated !== false,
        key_env: null, warning: p.warning || null,
        featured_models: models.slice(0, 2), capabilities, pricing,
        pricing_pending: p.id === "ollama-local",
        free_tier: Boolean(p.id === "nous"), free_tier_pending: false,
        unavailable_models: [],
      };
    });
    // The host prices one tier above the cost guard's threshold. The frozen
    // /catalog fixture predates it on purpose: the editor reaches it through
    // the exact-id "Other" field, which is the real escape hatch for a model a
    // profile's catalog has not listed yet.
    const expensive = window.__mockApi.hostExpensiveModel;
    const nous = providers.find((p) => p.slug === "nous");
    if (nous && expensive && !nous.models.includes(expensive)) {
      nous.models = [...nous.models, expensive];
      nous.total_models = nous.models.length;
      nous.capabilities[expensive] = { fast: false, reasoning: true };
      nous.pricing[expensive] = { input: "$40.00", output: "$200.00", cache: null, free: false };
      nous.featured_models = [...nous.featured_models, expensive];
    }
    const main = mainFor(profileId);
    return { providers, provider: main.provider, model: main.model };
  }

  function hostAuxiliaryFor(profileId) {
    const main = mainFor(profileId);
    const tasks = slots
      .filter((s) => s.profileId === profileId && s.slotId.startsWith("auxiliary."))
      .map((s) => ({
        task: s.slotId.slice("auxiliary.".length), label: s.slotId,
        provider: s.provider, model: s.model, base_url: s.base_url || "",
        reasoning_effort: null, local_endpoint: false, plugin: false, inherit_from: null,
      }));
    return { tasks, main: { provider: main.provider, model: main.model } };
  }

  function hostMoaFor(profileId) {
    const main = mainFor(profileId);
    return {
      default_preset: "default",
      active_preset: "",
      presets: {
        default: {
          reference_models: [{ provider: main.provider, model: main.model }],
          aggregator: { provider: main.provider, model: main.model },
          reference_temperature: 0.6,
          aggregator_temperature: 0.3,
          reference_timeout: null,
          degraded_reference_policy: "loud",
          enabled: false,
          fanout: "user_turn",
        },
      },
      reference_models: [{ provider: main.provider, model: main.model }],
      aggregator: { provider: main.provider, model: main.model },
      reference_temperature: 0.6,
      aggregator_temperature: 0.3,
      reference_timeout: null,
      degraded_reference_policy: "loud",
      enabled: false,
      privacy_filter: "off",
      fanout: "user_turn",
    };
  }

  // POST /api/model/set?profile=… — the host's own assignment route, including
  // its cost guard: an expensive model without `confirm_expensive_model` is
  // refused with `confirm_required: true` + a human `confirm_message`.
  async function hostSet(body, params) {
    const M = window.__mockApi;
    if (M.hostSetDelayMs) await new Promise((resolve) => setTimeout(resolve, M.hostSetDelayMs));
    M.hostSetCount = (M.hostSetCount || 0) + 1;
    if (M.hostFailAfter && M.hostSetCount > M.hostFailAfter) return { status: 503, body: { detail: "private host failure", ok: false } };
    const scope = body.scope || "main";
    const profileId = body.profile || params.get("profile") || "default";
    if (M.hostSetBody) return { status: 200, body: M.hostSetBody };
    if (M.hostSetHttp) {
      return { status: M.hostSetHttp, body: { detail: "host assignment failed", ok: false } };
    }
    if (body.model && body.model === M.hostExpensiveModel && !body.confirm_expensive_model) {
      return { status: 200, body: {
        ok: false, scope, provider: body.provider || "", model: body.model,
        confirm_required: true, confirm_message: M.hostConfirmMessage,
      } };
    }
    if (body.confirm_expensive_model) M.hostAckCount += 1;
    // The mock host "writes" by moving the in-memory main/aux slot, so the
    // refreshed /state shows the new route exactly like the real host would.
    const slotId = scope === "main" ? "main" : `auxiliary.${body.task || ""}`;
    const target = slots.find((s) => s.profileId === profileId && s.slotId === slotId);
    if (target && !M.hostNoWrite) {
      target.provider = body.provider || target.provider; target.model = body.model || target.model;
      if (scope === "auxiliary" && body.base_url) target.base_url = body.base_url;
    }
    return { status: 200, body: {
      ok: true, scope, provider: body.provider || "", model: body.model || "",
      tasks: scope === "auxiliary" ? [body.task || ""] : [],
      stale_aux: [],
    } };
  }

  async function handleHost(path, method, body, params) {
    const M = window.__mockApi;
    if (M.hostDelayMs) await new Promise((resolve) => setTimeout(resolve, M.hostDelayMs));
    const part = path.replace("/api/model/", "");
    const parts = Array.isArray(M.hostMissingParts) ? M.hostMissingParts : [];
    if (M.hostMissing === true || parts.includes(part)) {
      // A host without the route: 404, exactly what an older Hermes answers.
      return { status: 404, body: { detail: "Not Found", ok: false } };
    }
    const profileId = params.get("profile") || "default";
    if (M.hostWrongShapeParts?.includes(part)) return { status: 200, body: { detail: "not host evidence" } };
    if (part === "options" && method === "GET") return { status: 200, body: hostOptionsFor(profileId) };
    if (part === "auxiliary" && method === "GET") return { status: 200, body: hostAuxiliaryFor(profileId) };
    if (part === "moa" && method === "GET") return { status: 200, body: hostMoaFor(profileId) };
    if (part === "set" && method === "POST") return hostSet(body, params);
    return { status: 405, body: { detail: "Method Not Allowed", ok: false } };
  }

  // ---- host-step protocol (operation → hostCall → host-result → …) ------
  // Match plugin_api.py::aux_host_route/_host_execution_plan and the engine's
  // ONE step per (profile, canonical_config), not one step per selected slot.
  function hostBodyFor(change) {
    const to = change.to;
    if (change.action === "remove" || !to || typeof to !== "object" || Array.isArray(to)) return null;
    const target = slots.find((s) => s.profileId === change.profileId && s.slotId === change.slotId);
    if (!target || !target.writable || target.protected) return null;
    const provider = typeof to.provider === "string" ? to.provider.trim() : "";
    const model = typeof to.model === "string" ? to.model.trim() : "";
    if (!provider || !model) return null;
    if (change.slotId === "main") return { scope: "main", provider, model };
    if (target.family !== "auxiliary" || !/^auxiliary\.[^.]+$/.test(change.slotId) || change.slotId === "auxiliary.openrouter_model") return null;
    if (Object.keys(to).some((key) => !["provider", "model", "base_url"].includes(key))) return null;
    const body = { scope: "auxiliary", provider, model, task: change.slotId.slice("auxiliary.".length) };
    if (to.base_url !== undefined && to.base_url !== null && to.base_url !== "") {
      const base = String(to.base_url).trim();
      try {
        const url = new URL(base);
        if (!["http:", "https:"].includes(url.protocol) || !url.host || url.username || url.password || url.search || url.hash) return null;
      } catch { return null; }
      body.base_url = base;
    } else {
      if (Object.hasOwn(to, "base_url")) return null; // explicit clear, not assignment
      if (provider.toLowerCase() !== String(target.provider || "").trim().toLowerCase() && provider !== "custom") return null;
    }
    return body;
  }

  function executionPlan(changes) {
    const planned = (changes || []).map((c) => ({ ...c, executor: hostBodyFor(c) ? "host" : "backend" }));
    const blocked = [];
    const refused = new Set();
    for (const profileId of dedupeProfiles(planned)) {
      const group = planned.filter((c) => c.profileId === profileId);
      const host = group.filter((c) => c.executor === "host");
      if (!host.length || group.length === 1) continue;
      if (host.some((c) => c.slotId === "main")) {
        refused.add(profileId);
        blocked.push({ profileId, slotId: "main", kind: "host_scope", reason:
          "the main assignment must be applied on its own for this profile: a host assignment and another config change cannot share one atomic step — apply them separately" });
      } else {
        for (const c of group) c.executor = "backend";
      }
    }
    return { changes: planned.filter((c) => !refused.has(c.profileId)), blocked };
  }

  function writeChange(change) {
    const target = slots.find((s) => s.profileId === change.profileId && s.slotId === change.slotId);
    if (!target) return;
    if (change.action === "remove") {
      target.provider = ""; target.model = "";
      delete target.base_url; delete target.api_mode;
      return;
    }
    const to = change.to || {};
    const route = to.provider ? to : (Array.isArray(to.chain) && to.chain.length ? to.chain[0] : {});
    Object.assign(target, route);
  }

  function stepChanged(step) {
    return step.snapshots.some(({ index, value }) => JSON.stringify(slots[index]) !== JSON.stringify(value));
  }

  function restoreStep(step) {
    for (const { index, value } of step.snapshots) slots[index] = structuredClone(value);
    step.status = "compensated";
  }

  function failHostOperation(op, category) {
    const step = op.steps.find((s) => s.status === "awaiting_host");
    step.status = "failed";
    // A lost/error response may follow a successful host write. Treat that
    // written-but-failed step as compensation-owned too (engine.py:518-523).
    const written = op.steps.filter((s) => s.status === "applied" || (s === step && stepChanged(s)));
    for (const s of written.reverse()) { s.postWritten = true; restoreStep(s); }
    op.awaiting = null; op.token = null;
    op.state = written.length ? "compensated" : "pending";
    op.finalState = op.state; op.failure = category;
    return hostEnvelope(op, null);
  }

  // Public journal-step projection, matching plugin_api.py::_op_dto.
  function operationDto(op) {
    if (!op || !Array.isArray(op.steps)) return op; // already a plain DTO
    const results = op.steps.map((step, seq) => ({
      profileId: step.profileId, status: step.status, seq, store: "canonical_config",
      revisionBefore: "rev" + step.profileId + "-a",
      revisionAfter: step.postWritten ? "rev" + step.profileId + "-b" : null,
    }));
    return {
      id: op.id, state: op.state, results,
      undoable: op.state === "committed" && results.some((r) => r.status === "applied"),
      final_state: op.finalState || op.state,
      failure: op.failure || null,
    };
  }

  function hostEnvelope(op, hostCall) {
    const body = {
      ok: op.state === "committed" || op.state === "undone",
      operation: operationDto(op),
    };
    if (hostCall) body.hostCall = Object.assign({ opId: op.id }, hostCall);
    return { status: 200, body };
  }

  function startHostOperation(id, changes) {
    const op = {
      id, state: "pending", finalState: "pending", failure: null,
      steps: dedupeProfiles(changes || []).map((profileId) => {
        const group = changes.filter((c) => c.profileId === profileId);
        return {
          profileId, changes: group,
          executor: group[0].executor, hostBody: group[0].executor === "host" ? hostBodyFor(group[0]) : null,
          snapshots: slots.flatMap((row, index) => row.profileId === profileId ? [{ index, value: structuredClone(row) }] : []),
          status: "pending",
        };
      }),
      seq: 0, token: null, awaiting: null,
    };
    operations[id] = op;
    return advanceHostOperation(op);
  }

  // Run backend steps inline; stop and hand back the next host step. Returns a
  // `{ok, operation, hostCall?}` envelope.
  function advanceHostOperation(op) {
    for (;;) {
      const next = op.steps.find((s) => s.status === "pending");
      if (!next) break;
      if (next.executor === "host") {
        op.seq = op.steps.indexOf(next);
        next.status = "awaiting_host";
        op.state = "awaiting_host";
        op.finalState = "awaiting_host";
        op.token = "tok-" + op.id + "-" + op.seq;
        op.awaiting = { seq: op.seq, token: op.token };
        return hostEnvelope(op, {
          seq: op.seq, token: op.token, method: "POST",
          path: "/api/model/set",
          query: { profile: next.profileId },
          body: next.hostBody,
        });
      }
      for (const c of next.changes) writeChange(c);
      next.postWritten = true;
      next.status = "applied";
    }
    op.awaiting = null;
    op.token = null;
    if (!op.failure) { op.state = "committed"; op.finalState = "committed"; }
    return hostEnvelope(op, null);
  }

  let previewSeq = 0, opSeq = 0;
  let lastPreview = null;
  const operations = {};

  function failUnless(check) {
    return check; // readability helper
  }

  async function handle(path, method, body, params) {
    const M = window.__mockApi;
    // Protocol: /catalog and /models are POST-only transforms of a host
    // options payload. The GET forms are retired and answer the contract's 410.
    if (path === BASE + "/catalog" && method !== "POST") {
      return { status: 410, body: { ok: false, reason: "hostCatalogRequired" } };
    }
    if (path === BASE + "/models" && method !== "POST") {
      return { status: 410, body: { ok: false, reason: "hostCatalogRequired" } };
    }
    if (path === BASE + "/catalog" && method === "POST") {
      if (M.catalogDelayMs) await new Promise((resolve) => setTimeout(resolve, M.catalogDelayMs));
      if (M.catalogFail) return { status: 200, body: { ok: false, error: "catalog unavailable: upstream timeout" } };
      // No host evidence for that profile: truthful refusal, never an invented
      // (empty) provider list.
      if (!body.options) {
        return { status: 200, body: {
          ok: false, reason: "hostCatalogUnavailable", error: "hostCatalogUnavailable",
          profileId: body.profile || null, providers: [],
          cost: { pending: false, unavailableProviders: [] } } };
      }
      return { status: 200, body: catalogFor(body.profile, body.provider, Boolean(body.refresh)) };
    }
    if (path === BASE + "/models" && method === "POST") {
      if (M.modelsDelayMs) await new Promise((resolve) => setTimeout(resolve, M.modelsDelayMs));
      if (M.modelsFail) {
        return { status: 200, body: { ok: false, provider: body.provider || "", models: [],
          fetchedAt: null, source: "unknown", stale: true, error: "model catalog unavailable: upstream timeout" } };
      }
      if (!body.options) {
        return { status: 200, body: { ok: false, provider: body.provider || "", models: [],
          fetchedAt: null, source: "host", stale: true, error: "hostModelsUnavailable" } };
      }
      return { status: 200, body: modelsFor(body.provider || "") };
    }
    const stateBody = () => {
      return { ok: true, profiles, slots: [...slots], presets };
    };
    if (path === BASE + "/state" && method === "GET") {
      if (M.failState) return { status: 200, body: { ok: false, error: "raw config unreadable" } };
      if (M.failStateHttp) return { status: 500, body: { error: "internal error" } };
      return { status: 200, body: stateBody() };
    }
    if (path === BASE + "/state" && method === "POST") {
      // `POST /state {host}`: the same DTO, enriched with host-derived rows.
      // The mock's extra rows come from the host's auxiliary payload, which is
      // the one host shape that adds slots a config-only read cannot see.
      if (M.failState) return { status: 200, body: { ok: false, error: "raw config unreadable" } };
      if (M.failStateHttp) return { status: 500, body: { error: "internal error" } };
      const dto = stateBody();
      const host = body && body.host && body.host.profiles ? body.host.profiles : {};
      const extra = [];
      for (const [pid, parts] of Object.entries(host)) {
        const aux = parts && parts.auxiliary;
        for (const task of (aux && Array.isArray(aux.tasks)) ? aux.tasks : []) {
          if (task.plugin && task.inherit_from === null) {
            extra.push(mk(pid, `auxiliary.${task.task}`, "auxiliary", task.provider, task.model,
              { state: "inherited", note: "registered by a plugin" }));
          }
        }
      }
      dto.slots = [...dto.slots, ...extra];
      dto.hostEnriched = true;
      return { status: 200, body: dto };
    }
    if (path === BASE + "/preview" && method === "POST") {
      const plan = executionPlan(body.changes || []);
      const changes = plan.changes.map((c) => ({
        ...c,
        sideEffects: c.action === "set" && c.to?.provider === "zhipu" ? ["canonical tool defaults seeded"] : [],
      }));
      const blocked = [...plan.blocked, ...changes.filter((c) => !profiles.find((p) => p.id === c.profileId && p.writable))
        .map((c) => ({ profileId: c.profileId, reason: "profile is read-only" }))];
      const preview = { id: "prev-" + (++previewSeq), changes: changes.filter((c) => !blocked.some((b) => b.profileId === c.profileId)), blocked };
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
      const preview = body.previewId ? lastPreview : body.changes ? executionPlan(body.changes) : lastPreview;
      if (!preview || (body.previewId && body.previewId !== preview.id))
        return { status: 200, body: { ok: false, error: "unknown or stale preview" } };
      if (preview.blocked?.length) return body.previewId
        ? { status: 409, body: { detail: { ok: false, blocked: true,
          error: "apply blocked: the preview contained blocked changes", reasons: preview.blocked.map((b) => b.reason) } } }
        : { status: 422, body: { detail: { ok: false, error: "preview blocked for some profiles", blocked: preview.blocked } } };
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
      // Happy path: the engine journals host steps for the host-owned slots
      // and hands each one back as a `hostCall`, so the browser performs the
      // host route and reports the outcome. The failure-injection flags above
      // keep the single-shot envelope (they model an operation that dies
      // before any host step runs or after it).
      if (committed && !M.statusOverride && !noWrite && !rollback && !failed) {
        return startHostOperation(id, preview.changes);
      }
      const results = pids.map((pid, i) => {
        const status = statusFor(i);
        const changed = status === "applied";
        return {
          profileId: pid, status, seq: i, store: "canonical_config",
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
        undoable: state === "committed" && results.some((r) => r.status === "applied"),
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
    const opHostResult = path.match(new RegExp("^" + BASE + "/operations/([^/]+)/host-result$"));
    if (opHostResult && method === "POST") {
      const op = operations[opHostResult[1]];
      if (!op || !Array.isArray(op.steps)) return { status: 200, body: { ok: false, error: "unknown operation" } };
      // Single-use token: a replay or a foreign token changes no state.
      if (!op.awaiting || body.seq !== op.awaiting.seq || body.token !== op.awaiting.token) {
        return { status: 409, body: { detail: { ok: false, error: "host_token_mismatch", failure: "host_token_mismatch" } } };
      }
      const step = op.steps.find((s) => s.status === "awaiting_host");
      if (body.outcome === "ok") {
        // Readback, not the reported HTTP outcome, proves a write landed.
        if (!stepChanged(step)) return failHostOperation(op, "host_no_write");
        step.postWritten = true;
        step.status = "applied";
        op.awaiting = null; op.token = null;
        return advanceHostOperation(op);
      }
      return failHostOperation(op, body.outcome === "declined" ? "host_declined" : "host_error");
    }
    const opRecover = path.match(new RegExp("^" + BASE + "/operations/([^/]+)/recover$"));
    if (opRecover && method === "POST") {
      const op = operations[opRecover[1]];
      if (!op) return { status: 404, body: { ok: false, error: "unknown operation" } };
      if (op.awaiting) return failHostOperation(op, "host_error");
      return hostEnvelope(op, null);
    }
    const opUndo = path.match(new RegExp("^" + BASE + "/operations/([^/]+)/undo$"));
    if (opUndo && method === "POST") {
      const op = operations[opUndo[1]];
      if (!op) return { status: 200, body: { ok: false, error: "unknown operation" } };
      if (M.failUndo) return { status: 200, body: { ok: false, error: "revision conflict: profile edited after apply" } };
      // A CONFLICTED undo: every applied profile moved since the apply (e.g. a
      // host save renamed legacy model keys, or any external edit), so no
      // snapshot may be restored over it. The whole operation ends
      // ``recovery_required`` with ok:false and every step ``conflict``.
      // ``failure`` is "external_edit": the engine's undo path records that
      // fixed category when a compensate is refused, and _op_dto projects it.
      if (M.undoConflict) {
        const operation = {
          id: op.id,
          state: "recovery_required",
          results: operationDto(op).results.map((r) => ({ ...r, status: "conflict" })),
          undoable: false,
          final_state: "recovery_required",
          failure: "external_edit",
        };
        operations[op.id] = operation;
        return { status: 200, body: { ok: false, operation } };
      }
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
          results: operationDto(op).results.map((r) => ({
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
        results: operationDto(op).results.map((r) => ({
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
      return { status: 200, body: { ok: op.state === "committed" || op.state === "undone",
        operation: operationDto(op) } };
    }
    return { status: 404, body: { error: "not found: " + path } };
  }

  function dedupeProfiles(changes) {
    return [...new Set(changes.map((c) => c.profileId))];
  }

  const origFetch = window.fetch;
  window.fetch = async function (url, opts) {
    const u = new URL(url, location.origin);
    if (u.pathname.startsWith("/api/model/")) {
      const method = (opts && opts.method) || "GET";
      let body = {};
      try { body = opts && opts.body ? JSON.parse(opts.body) : {}; } catch (e) {}
      const sessionHeader = new Headers(opts?.headers).get("X-Hermes-Session-Token");
      window.__mockApi.requests.push({ path: u.pathname, search: u.search, method, body, host: true, sessionHeader });
      if (sessionHeader !== "fixture-session") return new Response(JSON.stringify({detail: "Unauthorized"}), { status: 401 });
      const res = await handleHost(u.pathname, method, body, u.searchParams);
      return new Response(JSON.stringify(res.body), {
        status: res.status, headers: { "Content-Type": "application/json" },
      });
    }
    if (u.pathname.startsWith(BASE)) {
      const method = (opts && opts.method) || "GET";
      let body = {};
      try { body = opts && opts.body ? JSON.parse(opts.body) : {}; } catch (e) {}
      window.__mockApi.requests.push({ path: u.pathname, search: u.search, method, body });
      const res = await handle(u.pathname, method, body, u.searchParams);
      if (u.pathname.endsWith("/host-result") && window.__mockApi.hostResultLoseOnce) {
        window.__mockApi.hostResultLoseOnce = false;
        throw new TypeError("Synthetic lost continuation response");
      }
      if (u.pathname === BASE + "/catalog" && method === "POST") {
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
