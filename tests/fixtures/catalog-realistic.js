// Realistic Model Catalog fixture, shaped like the audited live default
// profile: 54 providers, of which only 9 are authenticated and publish models
// (258 total), the other 45 are `authenticated: false` with zero models.
// Every pricingStatus is "unknown" (the audit found zero priced/free/included
// rows across 1,584 models), OpenRouter-style ids carry a `:free` suffix that
// says nothing about price, and a few rows carry the `capabilities` object the
// backend adds in parallel.
//
// Serialisable data only: the spec injects the return value of
// `realisticFixture()` into the page, and tests/mock-api.js serves it for
// /catalog, /models and /api/model/options under its `catalogRealistic` flag.
// No real profile, credential or provider payload is copied here.

const LAB_CYCLE = [
  "openai", "anthropic", "google", "deepseek", "z-ai", "x-ai", "meta", "nvidia",
  "qwen", "moonshotai", "minimax", "xiaomi", "tencent", "stepfun", "upstage",
  "poolside", "sakana", "thinkingmachines", "primalabs-ai",
];

const USD = (input, output) => ({
  unit: "USD_per_1M_tokens",
  input,
  output,
  cacheRead: null,
  display: {
    input: input === null ? null : `$${input.toFixed(2)}`,
    output: output === null ? null : `$${output.toFixed(2)}`,
    cache: null,
  },
  free: false,
  source: "picker",
  pricingVersion: "2026-10",
});

const FREE_COST = {
  unit: "USD_per_1M_tokens",
  input: 0,
  output: 0,
  cacheRead: 0,
  display: { input: "$0.00", output: "$0.00", cache: "$0.00" },
  free: true,
  source: "subscription_route",
  pricingVersion: null,
};

// 45 provider slugs that are authenticated:false with no published models in
// the audited profile. Plausible gateway/cloud names only — no private data.
const NOT_SET_UP_SLUGS = [
  "azure-openai", "aws-bedrock", "google-vertex", "groq", "together",
  "fireworks", "deepinfra", "cerebras", "sambanova", "hyperbolic",
  "novita", "lambda", "perplexity", "cohere", "ai21",
  "mistral", "ollama-local", "lmstudio", "vllm", "llamacpp",
  "openai", "google", "deepseek", "zhipu", "moonshot",
  "minimax", "baidu", "alibaba", "bytedance", "tencent-cloud",
  "upstage-cloud", "sakana-cloud", "reka", "inflection", "writer",
  "ai2", "nvidia-nim", "databricks", "snowflake", "modal",
  "replicate", "huggingface", "cloudflare", "vercel-ai", "portkey",
];

// `lab` is the vendor namespace the provider's ids carry. Aggregators (nous,
// openrouter) span many labs; a first-party provider serves its own; bare-id
// providers (opencode-go, copilot) have no namespace so labOf falls back to the
// provider slug. This is what makes "Lab" a genuinely cross-provider grouping.
const SET_UP_SPECS = [
  { id: "nous", label: "Nous", count: 67, isCurrent: true, labs: "cycle" },
  { id: "openrouter", label: "OpenRouter", count: 62, labs: "cycle" },
  { id: "anthropic", label: "Anthropic", count: 17, lab: "anthropic" },
  { id: "copilot", label: "GitHub Copilot", count: 17, bare: true },
  { id: "copilot-acp", label: "Copilot ACP", count: 17, bare: true },
  { id: "openai-codex", label: "OpenAI Codex", count: 16, lab: "openai" },
  { id: "opencode-go", label: "opencode go", count: 46, bare: true },
  { id: "xai-oauth", label: "xAI (OAuth)", count: 9, lab: "x-ai" },
  { id: "primalabs", label: "PrimaLabs", count: 7, lab: "primalabs-ai" },
];

// Host order with the set-up providers interleaved through the empty ones, as
// the live host returns them (opencode-go is 34th of 54 there, absurd to dig
// out of a flat list of empty headers).
const SET_UP_HOST_POSITIONS = [0, 5, 9, 14, 19, 24, 33, 40, 48];

function buildModels(spec, offset) {
  const models = [];
  for (let i = 0; i < spec.count; i += 1) {
    const lab = spec.labs === "cycle" ? LAB_CYCLE[(offset + i) % LAB_CYCLE.length] : spec.lab;
    const bare = Boolean(spec.bare);
    const baseId = bare ? `${spec.id}-model-${i + 1}` : `${lab}/${spec.id}-model-${i + 1}`;
    // OpenRouter free-tier naming: a `:free` id suffix. Deliberately not a
    // price field — pricingStatus below stays "unknown" (as in the audit).
    const freeVariant = !bare && (spec.id === "openrouter" ? i % 2 === 0 : spec.id === "nous" ? i % 3 === 0 : false);
    models.push({
      id: freeVariant ? `${baseId}:free` : baseId,
      label: `${spec.label} ${i + 1}`,
      pricingStatus: "unknown",
      cost: null,
    });
  }
  return models;
}

function providerEntry(spec, models) {
  return {
    id: spec.id,
    label: spec.label,
    authenticated: true,
    configured: true,
    isCurrent: Boolean(spec.isCurrent),
    fetchedAt: "2026-10-20T08:00:00Z",
    stale: false,
    warning: null,
    models,
  };
}

function notSetUpEntry(slug) {
  return {
    id: slug,
    label: slug,
    authenticated: false,
    configured: false,
    isCurrent: false,
    fetchedAt: null,
    stale: false,
    warning: "Sign-in required",
    models: [],
  };
}

/**
 * Build the fixture. `prices: true` flips a few rows to priced/free so the
 * price controls have something honest to act on; the default (like the live
 * profile) has no priced or free model at all. `signedOutModels: true` gives
 * one `authenticated: false` provider two `anthropic/*` ids — the shape a
 * pasted/aggregated catalog can have, and the only way the Lab filter and the
 * not-set-up section can be asked the same question.
 */
export function realisticFixture({ prices = false, signedOutModels = false } = {}) {
  const setUp = SET_UP_SPECS.map((spec, i) => providerEntry(spec, buildModels(spec, i * 3)));
  const notSetUp = NOT_SET_UP_SLUGS.map(notSetUpEntry);

  // Interleave in host order.
  const hostOrder = [];
  let setUpIndex = 0;
  let notSetUpIndex = 0;
  for (let position = 0; position < setUp.length + notSetUp.length; position += 1) {
    if (SET_UP_HOST_POSITIONS.includes(position)) hostOrder.push(setUp[setUpIndex++]);
    else hostOrder.push(notSetUp[notSetUpIndex++]);
  }

  if (signedOutModels) {
    // A signed-out provider that still lists a vendor's ids: a pasted or
    // aggregated catalog can publish rows before any credential exists. Its
    // rows are unroutable, not invisible — Lab = anthropic must find them.
    const azure = hostOrder.find((p) => p.id === "azure-openai");
    azure.models = [
      { id: "anthropic/claude-sonnet-5", label: "Claude Sonnet 5 (Azure)", pricingStatus: "unknown", cost: null },
      { id: "anthropic/claude-haiku-5", label: "Claude Haiku 5 (Azure)", pricingStatus: "unknown", cost: null },
    ];
  }

  if (prices) {
    const nous = hostOrder.find((p) => p.id === "nous");
    const anthropic = hostOrder.find((p) => p.id === "anthropic");
    const opencode = hostOrder.find((p) => p.id === "opencode-go");
    const openrouter = hostOrder.find((p) => p.id === "openrouter");
    Object.assign(nous.models[1], { pricingStatus: "priced", cost: USD(3, 15) });
    Object.assign(anthropic.models[1], { pricingStatus: "priced", cost: USD(5, 25) });
    Object.assign(opencode.models[1], { pricingStatus: "priced", cost: USD(0.5, 2) });
    Object.assign(openrouter.models[1], { pricingStatus: "free", cost: FREE_COST });
  }

  // The parallel backend work adds `capabilities` to catalog model entries in
  // the {fast, reasoning, canDisableReasoning} shape. The UI must render those
  // toggles only when the field is present, so the fixture carries a few.
  const nous = hostOrder.find((p) => p.id === "nous");
  const openrouter = hostOrder.find((p) => p.id === "openrouter");
  const opencode = hostOrder.find((p) => p.id === "opencode-go");
  const anthropic = hostOrder.find((p) => p.id === "anthropic");
  nous.models[5].capabilities = { fast: true, reasoning: true, canDisableReasoning: true };
  openrouter.models[7].capabilities = { fast: true, reasoning: false, canDisableReasoning: false };
  opencode.models[9].capabilities = { fast: false, reasoning: true, canDisableReasoning: true };
  anthropic.models[0].capabilities = { fast: true, reasoning: true, canDisableReasoning: false };

  // One extra provider that exists ONLY outside the default profile, so the
  // profile select has a visibly different catalog to switch to.
  const moaGateway = {
    id: "moa-gateway",
    label: "MoA Gateway",
    authenticated: true,
    configured: true,
    isCurrent: true,
    fetchedAt: "2026-10-20T08:00:00Z",
    stale: false,
    warning: null,
    models: [
      { id: "moa/ensemble-fast", label: "Ensemble Fast", pricingStatus: "unknown", cost: null },
      { id: "moa/ensemble-deep", label: "Ensemble Deep", pricingStatus: "unknown", cost: null },
    ],
  };

  const profiles = [
    { id: "default", name: "Default", writable: true },
    { id: "research", name: "Research", writable: true },
    { id: "moa", name: "MoA Lab", writable: true },
  ];

  const slots = [
    { profileId: "default", slotId: "main", family: "main", provider: "nous", model: nous.models[0].id },
    { profileId: "default", slotId: "auxiliary.vision", family: "auxiliary", provider: "opencode-go", model: opencode.models[0].id },
    { profileId: "research", slotId: "auxiliary.scout", family: "auxiliary", provider: "openrouter", model: openrouter.models[4].id },
    { profileId: "moa", slotId: "main", family: "main", provider: "moa-gateway", model: "moa/ensemble-fast" },
  ];

  return {
    profiles,
    providers: {
      default: hostOrder,
      research: hostOrder,
      moa: [...hostOrder, moaGateway],
    },
    slots,
  };
}

// Counts the specs assert against without hard-coding them twice.
export const REALISTIC = {
  setUpProviderIds: SET_UP_SPECS.map((spec) => spec.id),
  notSetUpProviderCount: NOT_SET_UP_SLUGS.length,
  modelCount: SET_UP_SPECS.reduce((n, spec) => n + spec.count, 0),
  opencodeGoModels: 46,
};

// The one signed-out provider `realisticFixture({ signedOutModels: true })`
// gives published rows, and how many it gets.
export const SIGNED_OUT_PUBLISHER = {
  providerId: "azure-openai",
  lab: "anthropic",
  modelCount: 2,
};
