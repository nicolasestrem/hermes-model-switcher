import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../", import.meta.url));
const PORT = Number(process.env.HMS_FXT_PORT || 8931);
const HERMES_REPO = process.env.HERMES_MODEL_SWITCHER_HERMES_REPO
  || join(process.env.HOME || "", ".hermes", "hermes-agent");

async function hostStylesheet() {
  const { readdir } = await import("node:fs/promises");
  const dir = join(HERMES_REPO, "hermes_cli", "web_dist", "assets");
  const css = (await readdir(dir)).filter((f) => /^index-.*\.css$/.test(f));
  if (css.length !== 1) throw new Error(`expected one index-*.css in ${dir}, found ${css.length}`);
  return readFile(join(dir, css[0]));
}

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };

createServer(async (req, res) => {
  let p = decodeURIComponent(new URL(req.url, "http://x").pathname);
  if (p === "/" || p === "/fixture.html") p = "/tests/fixture.html";
  if (p === "/fixture-sdk.html") p = "/tests/fixture-sdk.html";
  // The REAL host dashboard stylesheet from the pinned Hermes checkout, so
  // specs see the host's global rules (preflight, bare `code`, ...) instead of
  // a hand-copied subset. Missing checkout = loud 500, never a silent pass.
  if (p === "/host/index.css") {
    try {
      const css = await hostStylesheet();
      res.writeHead(200, { "Content-Type": "text/css" });
      return res.end(css);
    } catch (err) {
      res.writeHead(500); return res.end(`host stylesheet unavailable: ${err.message}`);
    }
  }
  const file = normalize(join(ROOT, p));
  if (!file.startsWith(ROOT)) { res.writeHead(403); return res.end(); }
  try {
    const data = await readFile(file);
    res.writeHead(200, { "Content-Type": TYPES[extname(file)] || "application/octet-stream" });
    res.end(data);
  } catch {
    res.writeHead(404); res.end("not found");
  }
}).listen(PORT, () => console.log(`fixture server on http://localhost:${PORT}`));
