// react-dom/client shim — LEGACY self-mount path only.
//
// The Dashboard contract exposes no ReactDOM: a dashboard plugin registers a
// component (window.__HERMES_PLUGINS__.register) and the HOST mounts it inside
// the tab. This shim is therefore only reachable when no host registry exists —
// a standalone host, or this repo's local Playwright harness, which sets
// globalThis.ReactDOM. It fails loudly instead of "Cannot read properties of
// undefined" when neither host is present.
export function createRoot(...args) {
  const ReactDOM = globalThis.ReactDOM;
  if (!ReactDOM || typeof ReactDOM.createRoot !== "function") {
    throw new Error(
      "hermes-model-switcher: createRoot unavailable — the Hermes Dashboard mounts " +
        "registered components itself; globalThis.ReactDOM exists only for standalone hosts.",
    );
  }
  return ReactDOM.createRoot(...args);
}
