// Host React shim — resolves the Dashboard's documented SDK first.
//
// Dashboard (docs: extending-the-dashboard — "React is never bundled; it comes
// from SDK.React"): window.__HERMES_PLUGIN_SDK__.React.
// Standalone/legacy host: a host-provided globalThis.React.
//
// The web Dashboard does NOT set globalThis.React, so resolving that first made
// every destructure below crash the tab the moment the entry script ran:
// "Cannot destructure property 'useState' of 'q' as it is undefined".
const SDK = typeof window !== "undefined" ? window.__HERMES_PLUGIN_SDK__ : undefined;
const React = (SDK && SDK.React) || globalThis.React;

if (!React) {
  throw new Error(
    "hermes-model-switcher: no React host — expected window.__HERMES_PLUGIN_SDK__.React " +
      "(Hermes Dashboard) or a host-provided globalThis.React.",
  );
}

export default React;
export const { useState, useEffect, useMemo, useRef, Fragment, createElement } = React;
