export default {
  testDir: ".",
  timeout: 20000,
  use: { baseURL: "http://localhost:8931" },
  webServer: {
    command: "node server.mjs",
    url: "http://localhost:8931/fixture.html",
    reuseExistingServer: true,
    timeout: 10000,
  },
};
