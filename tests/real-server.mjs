// Real-router harness: seed a throwaway lab Hermes home under /tmp, then run
// the ACTUAL plugin backend (FastAPI/uvicorn, dashboard/plugin_api.py router)
// against it. Mock exists only at the credential level inside the lab home.
// Cleans up: lab home, uvicorn process, real-home.json.
import { spawn, execFileSync } from "node:child_process";
import { rmSync, readFileSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "..");
const HOME_FILE = join(HERE, "real-home.json");
const HOME_FILE_OUT = process.env.HMS_HOME_FILE_OUT || HOME_FILE;

// Portable interpreter resolution, in order of precedence:
//   1. an explicit HMS_PYTHON override,
//   2. this repo's local virtualenv (<repo>/.venv/bin/python),
//   3. the bare `python3` on PATH.
// A bare `npm test` therefore works without exporting HMS_PYTHON, while a
// caller that needs a specific venv (e.g. the main checkout's) can still pin it.
export function resolvePython(env = process.env, repo = REPO) {
  if (env.HMS_PYTHON) return env.HMS_PYTHON;
  const localVenv = join(repo, ".venv", "bin", "python");
  if (existsSync(localVenv)) return localVenv;
  return "python3";
}

async function main() {
  const venvPython = resolvePython();

  // 1) seed the lab home (never under ~/.hermes; /tmp only)
  execFileSync(venvPython, [join(HERE, "real_app.py"), "--seed", "auto",
                            "--home-file", HOME_FILE_OUT],
               { cwd: REPO, stdio: "inherit" });
  const labHome = JSON.parse(readFileSync(HOME_FILE_OUT, "utf8")).home;
  console.log("lab home:", labHome);

  // 2) uvicorn on 8932 with HERMES_HOME pointed at the lab home
  const proc = spawn(venvPython,
    ["-m", "uvicorn", "real_app:app", "--host", "127.0.0.1", "--port", "8932",
     "--log-level", "warning"],
    {
      cwd: HERE,
      env: {
        ...process.env,
        PYTHONPATH: `${HERE}:${REPO}:${process.env.HMS_HERMES_SRC || ""}`,
        HERMES_HOME: labHome,
        PYTHONUNBUFFERED: "1",
      },
      stdio: "inherit",
    });

  const shutdown = (code) => {
    proc.kill("SIGTERM");
    try { rmSync(labHome, { recursive: true, force: true }); } catch {}
    if (existsSync(HOME_FILE_OUT)) {
      try { rmSync(HOME_FILE_OUT); } catch {}
    }
    process.exit(code);
  };
  process.on("exit", () => { try { proc.kill("SIGTERM"); } catch {} });
  process.on("SIGINT", () => shutdown(130));
  process.on("SIGTERM", () => shutdown(143));

  // 3) wait for readiness
  const deadline = Date.now() + 30000;
  let ready = false;
  while (Date.now() < deadline) {
    try {
      const r = await fetch("http://localhost:8932/healthz");
      if (r.ok) { ready = true; break; }
    } catch {}
    await new Promise((r) => setTimeout(r, 200));
  }
  if (!ready) {
    console.error("real router failed to become ready");
    shutdown(1);
  }
  console.log("real router ready on http://localhost:8932");

  // Keep the harness process alive; Playwright/webServer kills it with SIGTERM
  // after the run, which triggers lab-home cleanup in the handlers above.
  await new Promise(() => {});
}

// Only start the server when this file is the entry point; importing it (to
// exercise resolvePython in a test) must not bind ports or spawn uvicorn.
if (process.argv[1] &&
    import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await main();
}
