export default {
  testDir: ".",
  timeout: 30000,
  // Serialize: every real-plugin spec (real-router, granular-review, ...) reads
  // and writes ONE shared, mutable Hermes Lab backend (tests/real-server.mjs
  // over the throwaway lab home). Running them in parallel lets one spec's
  // writes leak into another's byte-exact seed comparison — e.g. a concurrent
  // granular-review run adds delegation.model=glm / provider=zhipu that then
  // mismatches real-router's seeded bytes. Parallelism returns only with
  // per-worker, separate lab homes.
  workers: 1,
  use: { baseURL: `http://localhost:${process.env.HMS_FXT_PORT || 8931}` },
  webServer: [
    {
      command: "node server.mjs",
      url: `http://localhost:${process.env.HMS_FXT_PORT || 8931}/fixture.html`,
      env: { ...process.env },
      reuseExistingServer: true,
      timeout: 10000,
    },
    {
      // Real plugin backend (FastAPI/uvicorn) over a throwaway lab home;
      // seeds tests/real-home.json the specs read for on-disk assertions.
      command: "node real-server.mjs",
      url: "http://localhost:8932/healthz",
      reuseExistingServer: false,
      timeout: 45000,
    },
  ],
};
