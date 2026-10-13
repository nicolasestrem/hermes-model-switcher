export default {
  testDir: ".",
  timeout: 20000,
  use: { baseURL: `http://localhost:${process.env.HMS_FXT_PORT || 8931}` },
  webServer: {
    command: "node server.mjs",
    url: `http://localhost:${process.env.HMS_FXT_PORT || 8931}/fixture.html`,
    env: { ...process.env },
    reuseExistingServer: true,
    timeout: 10000,
  },
};
