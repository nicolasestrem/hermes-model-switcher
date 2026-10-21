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

/** Vendor namespace of a model id, or "" when the id carries none. */
export function modelNamespace(modelId) {
  const id = typeof modelId === "string" ? modelId.trim() : "";
  const cut = id.indexOf("/");
  return cut > 0 && cut < id.length - 1 ? id.slice(0, cut) : "";
}

/** Lab key for one model: its namespace, else the serving provider's slug. */
export function labOf(providerId, modelId) {
  return modelNamespace(modelId) || String(providerId || "").trim();
}

/** Human label for a lab key; `providerLabel` names a provider-slug fallback. */
export function labLabel(lab, providerLabel) {
  const key = String(lab || "");
  if (LAB_NAMES[key]) return LAB_NAMES[key];
  return providerLabel ? String(providerLabel) : key;
}

/** Model id without its vendor namespace, for rows already grouped by lab. */
export function modelLeaf(modelId) {
  const id = String(modelId || "");
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
    const ns = modelNamespace(id);
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
