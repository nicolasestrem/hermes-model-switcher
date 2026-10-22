// Fallback-chain usage for the Model Catalog: /state slots whose routes live in
// `chain` instead of the top-level provider/model.
//
// The DTO this mirrors (dashboard/plugin_api.py, the `_CHAIN_STORAGE_PATHS`
// branch of /state plus `_chain_slot_state`) emits an ordered-chain slot with a
// BLANK top-level route and its own stored list under `chain`, and only for one
// state:
//   configured  a stored list — the slot's OWN routes;
//   inherit     the chain is the PARENT's effective list, not this slot's
//               assignment, so it is not configuration of this slot;
//   disabled    `[]`, explicitly no fallback;
//   unset       null, nothing stored at this level.
// `lastObserved` is always null in this plugin (no runtime usage evidence), so
// the only truthful usage source is the configured route(s).
//
// Entry shapes: the runtime inventory (route_inventory.py) drops bare
// `provider/model` STRINGS from chains, and an object missing either half is a
// broken row rather than a route. Those variants are here on purpose — one
// malformed entry must not hide the valid routes beside it.
//
// Serialisable data only: `catalog-chain-usage.spec.mjs` injects `slots` into
// the page and merges them into the mocked GET/POST /state. No real profile,
// credential or provider payload is copied here.

import { realisticFixture } from "./catalog-realistic.js";

export function chainUsageScenario() {
  const fixture = realisticFixture();
  const modelIds = (providerId) => {
    const provider = fixture.providers.default.find((p) => p.id === providerId);
    if (!provider) throw new Error(`fixture provider ${providerId} missing`);
    return provider.models.map((m) => m.id);
  };
  // Catalog ids from the shared realistic fixture, so every expectation below
  // points at a row that actually renders.
  const anthropic = modelIds("anthropic");
  const codex = modelIds("openai-codex");
  const nous = modelIds("nous");
  const [A0, A1, A2, A3, A4, A5, A6] = anthropic;
  const C0 = codex[0];
  // Index 2 deliberately: the realistic fixture's own `default/main` slot is
  // `nous.models[0]`, and this scenario must not borrow a route the base
  // fixture already provides.
  const N0 = nous[2];

  const unmatched = {
    text: "—",
    title: "No loaded profile slot routes to this provider/model.",
  };

  const slots = [
    // The main case: a configured `fallback.main_chain` with blank top-level
    // route, a repeated route, malformed entries around the valid ones, and a
    // route the catalog serves under a DIFFERENT provider.
    {
      profileId: "default", slotId: "fallback.main_chain", family: "fallback",
      provider: "", model: "", storagePath: "fallback_providers",
      state: "configured", writable: true, lastObserved: null,
      chain: [
        { provider: "anthropic", model: A0 },
        { provider: "anthropic", model: A0 },        // repeated: counts once
        `${"anthropic"}/${A0}`,                      // bare string: dropped
        { provider: "anthropic" },                   // half-filled: skipped
        { model: A1 },                               // half-filled: skipped
        { provider: "   ", model: A6 },              // blank provider: skipped
        { provider: "anthropic", model: "   " },     // blank model: skipped
        { provider: 42, model: A6 },                 // non-string id: skipped
        ["anthropic", A6],                           // array entry: skipped
        { provider: "openai-codex", model: C0 },     // valid delegation route
        { provider: "openrouter", model: A3 },       // same model, other provider
        { provider: "anthropic", model: A1 },        // valid, after the noise
      ],
    },
    // Unset at the top level: nothing configured, no chain.
    {
      profileId: "default", slotId: "delegation.fallback_chain", family: "fallback",
      provider: "", model: "", storagePath: "delegation.fallback_providers",
      state: "unset", writable: true, lastObserved: null, chain: null,
    },
    // Disabled. A disabled chain is `[]` in practice; the stale list here is
    // deliberate — only the STATE decides, never the length of the array.
    {
      profileId: "research", slotId: "fallback.main_chain", family: "fallback",
      provider: "", model: "", storagePath: "fallback_providers",
      state: "disabled", writable: true, lastObserved: null,
      chain: [{ provider: "anthropic", model: A4 }],
    },
    // A configured delegation chain that ALSO carries a top-level route: the
    // slot is one slot, so the overlapping route counts once for it.
    {
      profileId: "research", slotId: "delegation.fallback_chain", family: "fallback",
      provider: "anthropic", model: A0, storagePath: "delegation.fallback_providers",
      state: "configured", writable: true, lastObserved: null,
      chain: [
        { provider: "anthropic", model: A0 },
        { provider: "nous", model: N0 },
      ],
    },
    // Inherited: the list is the parent's effective chain, not this slot's own
    // configuration.
    {
      profileId: "moa", slotId: "fallback.main_chain", family: "fallback",
      provider: "", model: "", storagePath: "fallback_providers",
      state: "inherit", writable: true, lastObserved: null,
      chain: [{ provider: "anthropic", model: A2 }],
    },
    // Usage evidence only: unset, nothing configured, lastObserved points at a
    // catalog row that must stay unmatched.
    {
      profileId: "moa", slotId: "delegation.fallback_chain", family: "fallback",
      provider: "", model: "", storagePath: "delegation.fallback_providers",
      state: "unset", writable: true, chain: null,
      lastObserved: { provider: "anthropic", model: A5, at: "2026-10-21T09:00:00Z" },
    },
  ];

  // Every route the /state above really configures, stated once. The spec joins
  // the default catalog against this set for the In-use row list, so a route
  // this fixture forgot to declare can never be excused by the same code the
  // view runs.
  const usedRoutes = [
    { providerId: "anthropic", modelId: A0 },
    { providerId: "anthropic", modelId: A1 },
    { providerId: "openai-codex", modelId: C0 },
    { providerId: "nous", modelId: N0 },
  ];

  // "Configured in" cell text and title per row, stated literally. A0 is in two
  // slots and says so once each; A1 is repeated inside one chain and reads one
  // slot; the rest are the states that must NOT count.
  const expectations = [
    {
      label: "a route repeated inside one configured chain, and carried by a second profile's configured delegation chain",
      providerId: "anthropic", modelId: A0,
      text: "2 slots · default, research",
      title: "default/fallback.main_chain, research/delegation.fallback_chain",
    },
    {
      label: "a route that follows malformed and string entries in the same chain",
      providerId: "anthropic", modelId: A1,
      text: "1 slot · default", title: "default/fallback.main_chain",
    },
    {
      label: "a route only the configured delegation chain lists",
      providerId: "openai-codex", modelId: C0,
      text: "1 slot · default", title: "default/fallback.main_chain",
    },
    {
      label: "a route only a research chain lists",
      providerId: "nous", modelId: N0,
      text: "1 slot · research", title: "research/delegation.fallback_chain",
    },
    { label: "an inherited chain (the parent's effective list)", providerId: "anthropic", modelId: A2, ...unmatched },
    { label: "the same model under a different provider", providerId: "anthropic", modelId: A3, ...unmatched },
    { label: "a disabled chain", providerId: "anthropic", modelId: A4, ...unmatched },
    { label: "observed-only usage (lastObserved, state unset)", providerId: "anthropic", modelId: A5, ...unmatched },
    { label: "malformed and string entries only", providerId: "anthropic", modelId: A6, ...unmatched },
  ];

  return { fixture, slots, usedRoutes, expectations };
}
