import { defineConfig, devices } from "@playwright/test";

const ciBrowserChannel = process.env.CI ? { channel: "chrome" as const } : {};

const hostedEnv = {
  ...process.env,
  AWF_CONSOLE_DIST_DIR: ".next-hosted",
  NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH: "/workspaces",
  NEXT_PUBLIC_AWF_CONSOLE_API_BASE: "/api/core-console",
  NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE: "/api/core-console",
  NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS: "org_id,project_id",
};

export default defineConfig({
  testDir: "./tests",
  outputDir: "./test-results",
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: "http://127.0.0.1:3100",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "npm run dev -- --hostname 127.0.0.1 --port 3100",
      url: "http://127.0.0.1:3100",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      // Registers page.harness.tsx via next.config pageExtensions for /test-harness/*.
      env: {
        ...process.env,
        AWF_CONSOLE_TEST_HARNESS: "1",
      },
    },
    {
      command: "npm run dev -- --hostname 127.0.0.1 --port 3101",
      url: "http://127.0.0.1:3101/workspaces",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        ...hostedEnv,
        AWF_CONSOLE_TEST_HARNESS: "1",
      },
    },
  ],
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], ...ciBrowserChannel },
    },
  ],
});
