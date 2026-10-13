// Vendor bundle for the test fixture: exposes React and ReactDOM as globals,
// mirroring what the real Dashboard host page provides.
import * as esbuild from "esbuild";

await esbuild.build({
  entryPoints: ["tests/vendor-entry.js"],
  bundle: true,
  format: "iife",
  globalName: "Vendor",
  outfile: "tests/vendor.js",
  minify: false,
  logLevel: "warning",
});
console.log("built tests/vendor.js");
