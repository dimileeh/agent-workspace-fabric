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

// Dedicated ports (not 3100/3101): a developer's plain `npm run dev` must not
// be reused — it lacks AWF_CONSOLE_TEST_HARNESS=1, so /test-harness/* 404s.
const localHarnessOrigin = "http://127.0.0.1:3190";
const hostedHarnessOrigin = "http://127.0.0.1:3191";

export default defineConfig({
  testDir: "./tests",
  outputDir: "./test-results",
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: localHarnessOrigin,
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "npm run dev -- --hostname 127.0.0.1 --port 3190",
      url: localHarnessOrigin,
      reuseExistingServer: false,
      timeout: 120_000,
      // Registers page.harness.tsx via next.config pageExtensions for /test-harness/*.
      env: {
        ...process.env,
        AWF_CONSOLE_TEST_HARNESS: "1",
      },
    },
    {
      command: "npm run dev -- --hostname 127.0.0.1 --port 3191",
      url: `${hostedHarnessOrigin}/workspaces`,
      reuseExistingServer: false,
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
