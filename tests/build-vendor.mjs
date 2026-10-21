// Build the local Playwright harness's vendor.js: host-like React/ReactDOM
// globals exactly as the real Dashboard shell provides them. The plugin
// bundle itself NEVER includes React — this file only equips the test fixture.
import * as esbuild from "esbuild";
import { fileURLToPath } from "node:url";

await esbuild.build({
  stdin: {
    contents: `
      import * as React from "react";
      import * as ReactDOM from "react-dom";
      import * as ReactDOMClient from "react-dom/client";
      globalThis.React = React;
      globalThis.ReactDOM = Object.assign({}, ReactDOM, ReactDOMClient);
    `,
    resolveDir: fileURLToPath(new URL("..", import.meta.url)),
    loader: "js",
  },
  bundle: true,
  format: "iife",
  outfile: "tests/vendor.js",
  minify: true,
  logLevel: "info",
});
console.log("built tests/vendor.js");

// --- Dashboard host: window.__HERMES_PLUGIN_SDK__ + window.__HERMES_PLUGINS__ -
//
// Mirrors the real Dashboard shell (hermes-agent web/src/plugins/registry.ts
// exposePluginSDK + the window.__HERMES_PLUGINS__ registry). It deliberately
// does NOT set globalThis.React / globalThis.ReactDOM — the real Dashboard never
// does, which is exactly what crashed the tab:
//   "Cannot destructure property 'useState' of 'q' as it is undefined".
await esbuild.build({
  stdin: {
    contents: `
      import * as React from "react";
      import * as ReactDOMClient from "react-dom/client";

      // Faithful to the host: fetchJSON/authedFetch are the ONLY sanctioned way
      // for a plugin to reach its own API — they carry the shell's auth (loopback
      // session-token header / gated cookie) and base path. A plugin that calls
      // bare fetch() has no auth at all and 401s on a real dashboard, so the
      // fixture counts SDK-routed calls and fails the suite if the plugin ever
      // goes around them.
      window.__sdkFetchCalls = { authedFetch: 0, fetchJSON: 0 };
      window.__HERMES_PLUGIN_SDK__ = {
        sdkVersion: "1.1.0",
        React,
        hooks: {
          useState: React.useState,
          useEffect: React.useEffect,
          useCallback: React.useCallback,
          useMemo: React.useMemo,
          useRef: React.useRef,
          useContext: React.useContext,
          createContext: React.createContext,
        },
        api: {},
        fetchJSON: async (url, init) => {
          window.__sdkFetchCalls.fetchJSON += 1;
          const res = await fetch(url, { ...init, credentials: "include",
            headers: { ...init?.headers, "X-Hermes-Session-Token": "fixture-session" } });
          if (!res.ok) {
            const text = await res.text().catch(() => res.statusText);
            throw new Error(res.status + ": " + (text || res.statusText));
          }
          return res.json();
        },
        authedFetch: async (url, init) => {
          window.__sdkFetchCalls.authedFetch += 1;
          return fetch(url, { ...init, credentials: "include" });
        },
        buildWsUrl: async () => "",
        buildWsAuthParam: async () => ["", ""],
        components: {},
        utils: { cn: (...parts) => parts.filter(Boolean).join(" ") },
        useI18n: () => ({ t: (key) => key }),
      };

      window.__HERMES_PLUGINS__ = {
        registered: {},
        slots: {},
        register(name, Component) { this.registered[name] = Component; },
        registerSlot(slot, name, Component) { this.slots[slot + ":" + name] = Component; },
      };

      // What the real shell does once the entry script has loaded: it renders the
      // registered component into the tab pane. The plugin must not mount itself.
      window.__mountRegisteredTab = function (name, el) {
        const Component = window.__HERMES_PLUGINS__.registered[name];
        if (!Component) throw new Error("no component registered for " + name);
        ReactDOMClient.createRoot(el).render(React.createElement(Component));
      };
    `,
    resolveDir: fileURLToPath(new URL("..", import.meta.url)),
    loader: "js",
  },
  bundle: true,
  format: "iife",
  outfile: "tests/vendor-sdk.js",
  minify: true,
  logLevel: "info",
});
console.log("built tests/vendor-sdk.js");
