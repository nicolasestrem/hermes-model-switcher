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
