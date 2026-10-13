// In-page mock of the plugin backend. Injected before the bundle loads.
// Test hooks: window.__mockApi = { failState, failReadback, failUndo, okFalse }
(function () {
  const BASE = "/api/plugins/hermes-model-switcher";

  window.__mockApi = Object.assign(
    { failState: false, failReadback: false, failUndo: false, failSavePreset: false, failStateHttp: false },
    JSON.parse(sessionStorage.getItem("hms-mock-flags") || "{}"),
  );

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
      const preview = lastPreview;
      if (!preview || (body.previewId && body.previewId !== preview.id))
        return { status: 200, body: { ok: false, error: "unknown or stale preview" } };
      const id = "op-" + (++opSeq);
      const results = preview.changes.length
        ? dedupeProfiles(preview.changes).map((pid) => ({
            profileId: pid, status: M.failReadback ? "failed" : "applied",
            revisionBefore: "rev" + pid + "-a", revisionAfter: M.failReadback ? "rev" + pid + "-a" : "rev" + pid + "-b",
            readback: M.failReadback ? { ok: false, error: "readback failed: config unreadable" } : { ok: true },
            error: M.failReadback ? "readback failed: config unreadable" : null,
          }))
        : [];
      const operation = { id, results };
      operations[id] = operation;
      return { status: 200, body: { ok: true, operation } };
    }
    const opUndo = path.match(new RegExp("^" + BASE + "/operations/([^/]+)/undo$"));
    if (opUndo && method === "POST") {
      const op = operations[opUndo[1]];
      if (!op) return { status: 200, body: { ok: false, error: "unknown operation" } };
      if (M.failUndo) return { status: 200, body: { ok: false, error: "revision conflict: profile edited after apply" } };
      const operation = {
        id: op.id,
        results: op.results.map((r) => ({
          ...r,
          status: r.status === "applied" ? "restored" : r.status,
          revisionAfter: "rev" + r.profileId + "-a",
        })),
      };
      operations[op.id] = operation;
      return { status: 200, body: { ok: true, operation } };
    }
    const opGet = path.match(new RegExp("^" + BASE + "/operations/([^/]+)$"));
    if (opGet && method === "GET") {
      const op = operations[opGet[1]];
      return op ? { status: 200, body: { ok: true, operation: op } }
        : { status: 200, body: { ok: false, error: "unknown operation" } };
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
      const res = await handle(u.pathname, method, body);
      return new Response(JSON.stringify(res.body), {
        status: res.status, headers: { "Content-Type": "application/json" },
      });
    }
    return origFetch.apply(this, arguments);
  };
})();
