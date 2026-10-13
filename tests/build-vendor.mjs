// Build the local Playwright harness's vendor.js: host-like React/ReactDOM
// globals exactly as the real Dashboard shell provides them. The plugin
// bundle itself NEVER includes React — this file only equips the test fixture.
import * as esbuild from "esbuild";

await esbuild.build({
  stdin: {
    contents: `
      import * as React from "react";
      import * as ReactDOM from "react-dom";
      import * as ReactDOMClient from "react-dom/client";
      globalThis.React = React;
      globalThis.ReactDOM = Object.assign({}, ReactDOM, ReactDOMClient);
    `,
    resolveDir: new URL("..", import.meta.url).pathname,
    loader: "js",
  },
  bundle: true,
  format: "iife",
  outfile: "tests/vendor.js",
  minify: false,
  logLevel: "info",
});
console.log("built tests/vendor.js");
