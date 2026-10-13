// Build the IIFE plugin bundle. React/react-dom are externals resolved to the
// host's globals via shims — no second React is bundled.
import * as esbuild from "esbuild";

await esbuild.build({
  entryPoints: ["ui/src/main.jsx"],
  bundle: true,
  format: "iife",
  globalName: "HermesModelSwitcher",
  outfile: "ui/dist/plugin.js",
  alias: {
    "shims/react.js": "./ui/src/shims/react.js",
    
    "shims/react-dom-client.js": "./ui/src/shims/react-dom-client.js",
  },
  minify: true,
  sourcemap: false,
  logLevel: "info",
});
console.log("built ui/dist/plugin.js");

// Also emit the official dashboard-plugin layout (dashboard/manifest.json
// expects dashboard/dist/index.js). Same bytes, both locations.
import { copyFileSync, mkdirSync } from "node:fs";
mkdirSync(new URL("../dashboard/dist", import.meta.url), { recursive: true });
copyFileSync(
  new URL("../ui/dist/plugin.js", import.meta.url),
  new URL("../dashboard/dist/index.js", import.meta.url),
);
console.log("built dashboard/dist/index.js");

// Test-harness vendor bundle (host-like React globals) — test asset only.
await import(new URL("../tests/build-vendor.mjs", import.meta.url).pathname);
