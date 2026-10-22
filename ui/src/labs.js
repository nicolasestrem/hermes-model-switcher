// Lab (model vendor) derivation shared by the catalog view and the routing
// picker. A "lab" is the vendor namespace a model id carries before its first
// slash (`anthropic/claude-opus-5.5` -> `anthropic`). Ids without a namespace
// (opencode-go, copilot, nous-native ids) belong to the serving provider, so
// the provider slug is the honest fallback; nothing is guessed from the name.

// Display names for namespaces whose slug does not title-case cleanly. Unknown
// namespaces fall back to the raw slug — never an invented brand.
const LAB_NAMES = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  google: "Google",
  deepseek: "DeepSeek",
  "z-ai": "Z.ai",
  "x-ai": "xAI",
  meta: "Meta",
  nvidia: "NVIDIA",
  qwen: "Qwen",
  moonshotai: "Moonshot AI",
  minimax: "MiniMax",
  xiaomi: "Xiaomi",
  tencent: "Tencent",
  stepfun: "StepFun",
  upstage: "Upstage",
  poolside: "Poolside",
  sakana: "Sakana AI",
  thinkingmachines: "Thinking Machines",
  "primalabs-ai": "PrimaLabs",
  openrouter: "OpenRouter",
  "deepseek-ai": "DeepSeek",
};

// Two keys can share one display label (`deepseek` and `deepseek-ai` both read
// "DeepSeek"). Left alone they become two Lab options with identical text — an
// unusable select, and a filter that hides half the vendor's rows. Fold every
// duplicate onto the first key declared above, so the canonical key is stable
// (declaration order, not catalog order) whichever slug a catalog happens to
// use. Derived from LAB_NAMES so a future duplicate is joined automatically.
const LAB_ALIASES = (() => {
  const canonicalByLabel = new Map();
  const aliases = new Map();
  for (const [key, label] of Object.entries(LAB_NAMES)) {
    const canonical = canonicalByLabel.get(label);
    if (canonical) aliases.set(key, canonical);
    else canonicalByLabel.set(label, key);
  }
  return aliases;
})();

/** Canonical key for a lab: duplicate-label slugs join their first key. */
export function canonicalLab(lab) {
  const key = String(lab || "").toLowerCase();
  return LAB_ALIASES.get(key) || key;
}

/** Model id with surrounding whitespace removed (never null/undefined). */
export function normalizeModelId(modelId) {
  return typeof modelId === "string" ? modelId.trim() : "";
}

/** Vendor namespace of a model id, or "" when the id carries none. */
export function modelNamespace(modelId) {
  const id = normalizeModelId(modelId);
  const cut = id.indexOf("/");
  return cut > 0 && cut < id.length - 1 ? id.slice(0, cut) : "";
}

/**
 * Lab key for one model: its namespace, else the serving provider's slug.
 *
 * Keys are lower-cased so one lab is one key: a catalog that sends
 * `Anthropic/x` and `anthropic/y` must not offer two options that both read
 * "Anthropic". Model ids are case-insensitive in practice, so folding here
 * loses nothing. Duplicate-label slugs are joined too (see LAB_ALIASES), so
 * `deepseek-ai/x` and `deepseek/y` are one lab, not two.
 */
export function labOf(providerId, modelId) {
  const lab = modelNamespace(modelId) || normalizeModelId(providerId);
  return canonicalLab(lab);
}

/**
 * Human label for a lab key; `providerLabel` names a provider-slug fallback.
 * A known namespace always wins over a provider label: the brand belongs to
 * the lab, not to whichever provider happens to serve it.
 */
export function labLabel(lab, providerLabel) {
  const key = String(lab || "").toLowerCase();
  if (LAB_NAMES[key]) return LAB_NAMES[key];
  return providerLabel ? String(providerLabel) : key;
}

/** Model id without its vendor namespace, for rows already grouped by lab. */
export function modelLeaf(modelId) {
  // Slice the trimmed id: a padded id must not render its own prefix because
  // the namespace was measured on the trimmed string.
  const id = normalizeModelId(modelId);
  const ns = modelNamespace(id);
  return ns ? id.slice(ns.length + 1) : id;
}

/**
 * True when a provider's list spans two or more labs and every id is
 * namespaced — an aggregator such as openrouter or nous. Single-lab or bare-id
 * providers keep their flat list.
 */
export function isMultiLab(modelIds) {
  const ids = Array.isArray(modelIds) ? modelIds : [];
  const labs = new Set();
  for (const id of ids) {
    const ns = modelNamespace(id).toLowerCase();
    if (!ns) return false;
    labs.add(ns);
  }
  return labs.size >= 2;
}

/** [{lab, label, count}] sorted by label, for a list of {providerId, modelId}. */
export function labCounts(entries, providerLabels = {}) {
  const counts = new Map();
  for (const { providerId, modelId } of entries || []) {
    const lab = labOf(providerId, modelId);
    if (!lab) continue;
    counts.set(lab, (counts.get(lab) || 0) + 1);
  }
  return [...counts.entries()]
    .map(([lab, count]) => ({ lab, count, label: labLabel(lab, providerLabels[lab]) }))
    .sort((a, b) => a.label.localeCompare(b.label) || a.lab.localeCompare(b.lab));
}
