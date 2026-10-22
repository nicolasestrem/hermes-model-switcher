// Multi-lab picker fixture: catalog providers shaped like the real installed
// ones (openrouter: 30 namespaced models across five labs, anthropic and openai
// first; primalabs: one lab, no published prices; opencode-go: 46 bare ids).
//
// It is installed by wrapping window.fetch AFTER tests/mock-api.js has run (that
// script replaces window.fetch at parse time) and rewriting the aggregate
// /catalog DTO's provider list. Nothing else changes: /state, /preview,
// /operations and the host routes stay the shared mock, so a spec can still
// assert the exact write payload.
//
// Counts are deliberate and self-consistent:
//   openrouter  30 = anthropic 9, deepseek 5, openai 11, openrouter 1, z-ai 4
//   primalabs    7 = primalabs-ai 7   (single lab, every price unknown)
//   opencode-go 46 bare ids           (no namespace, long flat list)

const priced = (input, output) => ({
  unit: "USD_per_1M_tokens",
  input,
  output,
  cacheRead: null,
  display: {
    input: `$${input.toFixed(2)}`,
    output: `$${output.toFixed(2)}`,
    cache: null,
  },
  free: false,
  source: "picker",
  pricingVersion: "2026-10",
});

const unknownModel = (id, label) => ({ id, label, pricingStatus: "unknown", cost: null });

// Nine Anthropic models; the first is priced so the real-price suffix is
// exercised next to rows that carry no suffix at all.
function anthropicModels() {
  const rows = [
    { id: "anthropic/claude-opus-5.5", label: "Claude Opus 5.5", pricingStatus: "priced", cost: priced(15, 75) },
    { id: "anthropic/claude-sonnet-5", label: "Claude Sonnet 5", pricingStatus: "free", cost: null },
    { id: "anthropic/claude-haiku-5", label: "Claude Haiku 5", pricingStatus: "included", cost: null },
  ];
  for (let n = 1; n <= 6; n += 1) rows.push(unknownModel(`anthropic/claude-filler-${n}`, `Claude Filler ${n}`));
  return rows;
}

// Eleven OpenAI models, opened first in the real list.
function openaiModels() {
  const rows = [];
  for (let n = 1; n <= 11; n += 1) {
    rows.push(unknownModel(`openai/gpt-5.${n}`, `GPT 5.${n}`));
  }
  return rows;
}

// Five DeepSeek models; one carries a `:free` variant id.
function deepseekModels() {
  return [
    { id: "deepseek/deepseek-v4.1-flash", label: "DeepSeek V4.1 Flash", pricingStatus: "priced", cost: priced(0.1, 0.5) },
    unknownModel("deepseek/deepseek-v4.1", "DeepSeek V4.1"),
    unknownModel("deepseek/deepseek-v4.1-pro", "DeepSeek V4.1 Pro"),
    unknownModel("deepseek/deepseek-r2", "DeepSeek R2"),
    { id: "deepseek/deepseek-r1:free", label: "DeepSeek R1 (free)", pricingStatus: "free", cost: null },
  ];
}

// Four Z.ai models — the "glm" filter target across labs.
function zaiModels() {
  return [
    unknownModel("z-ai/glm-5.3", "GLM 5.3"),
    unknownModel("z-ai/glm-5.3-flash", "GLM 5.3 Flash"),
    unknownModel("z-ai/glm-5-air", "GLM 5 Air"),
    unknownModel("z-ai/glm-4.7", "GLM 4.7"),
  ];
}

function openrouterProvider() {
  return {
    id: "openrouter",
    label: "OpenRouter",
    authenticated: true,
    configured: true,
    isCurrent: false,
    fetchedAt: "2026-10-18T12:00:00Z",
    stale: false,
    warning: null,
    models: [
      ...anthropicModels(),
      ...openaiModels(),
      ...deepseekModels(),
      ...zaiModels(),
      // The aggregator's own passthrough id: a fifth lab, not a vendor.
      unknownModel("openrouter/pareto-code", "Pareto Code"),
    ],
  };
}

function primalabsProvider() {
  const models = Array.from({ length: 7 }, (_, i) => unknownModel(
    `primalabs-ai/Primalabs-Model-${i + 1}-1017`,
    `Primalabs Model ${i + 1}`,
  ));
  return {
    id: "primalabs",
    label: "Primalabs",
    authenticated: true,
    configured: true,
    isCurrent: false,
    fetchedAt: "2026-10-18T12:00:00Z",
    stale: false,
    warning: null,
    models,
  };
}

function opencodeGoProvider() {
  // Bare ids: the serving provider IS the lab, so the list stays flat and keeps
  // its full-id row text. 46 rows is above the filter threshold.
  const models = Array.from({ length: 46 }, (_, i) => unknownModel(
    `opencode-model-${String(i + 1).padStart(2, "0")}`,
    `OpenCode Model ${i + 1}`,
  ));
  models[0] = { id: "opencode-model-01", label: "OpenCode Model 1", pricingStatus: "priced", cost: priced(0.05, 0.2) };
  return {
    id: "opencode-go",
    label: "OpenCode Go",
    authenticated: true,
    configured: true,
    isCurrent: false,
    fetchedAt: "2026-10-18T12:00:00Z",
    stale: false,
    warning: null,
    models,
  };
}

export const PICKER_PROVIDERS = [openrouterProvider(), primalabsProvider(), opencodeGoProvider()];

export const LAB_COUNTS = { anthropic: 9, deepseek: 5, openai: 11, openrouter: 1, "z-ai": 4 };

// The same catalog with openrouter's Z.ai rows withdrawn: openrouter still spans
// several labs (anthropic 9, deepseek 5, openai 11, openrouter 1 = 26 rows), it
// simply no longer offers `z-ai`. That is the shape a catalog scope switch or a
// refresh produces for a lab pick that was legitimate a moment ago.
export function withdrawnZaiProviders() {
  return PICKER_PROVIDERS.map((provider) => (provider.id !== "openrouter" ? provider : {
    ...provider,
    models: provider.models.filter((model) => !model.id.startsWith("z-ai/")),
  }));
}

// Install the fixture catalog. Call after page.goto("/fixture.html") and before
// any dropdown interaction: the catalog is fetched lazily on first focus.
//
// `profiles` serves a different provider list per catalog profile (`{research:
// withdrawnZaiProviders()}`), and `refreshed` serves a different list after an
// explicit refresh (`refresh: true` in the request body) — the two ways a lab
// option can disappear while the same provider stays selected.
export async function installPickerMultilab(page, { profiles = {}, refreshed = {} } = {}) {
  await page.evaluate(({ defaultProviders, byProfile, byRefresh }) => {
    if (window.__pickerMultilab) return;
    const original = window.fetch;
    window.__pickerMultilab = { served: null };
    const providersFor = (request) => {
      const body = request && request.body ? JSON.parse(request.body) : {};
      const profile = body.profile || "default";
      const table = body.refresh ? byRefresh : byProfile;
      return table[profile] || byProfile[profile] || defaultProviders;
    };
    window.fetch = async function patchedFetch(url, opts) {
      const res = await original.call(this, url, opts);
      const path = new URL(url, location.origin).pathname;
      const method = (opts && opts.method) || "GET";
      if (!path.endsWith("/catalog") || method !== "POST") return res;
      const body = await res.clone().json();
      if (!body || body.ok !== true) return res;
      const providers = providersFor(opts);
      const served = {
        ...body,
        providers: providers.map((p) => ({ ...p, modelCount: p.models.length })),
        cost: { pending: false, unavailableProviders: [] },
      };
      window.__pickerMultilab.served = served;
      return new Response(JSON.stringify(served), {
        status: res.status,
        headers: { "Content-Type": "application/json" },
      });
    };
  }, { defaultProviders: PICKER_PROVIDERS, byProfile: profiles, byRefresh: refreshed });
}
