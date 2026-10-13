export default {
  testDir: ".",
  timeout: 30000,
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
