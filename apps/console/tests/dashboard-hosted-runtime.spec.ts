import { expect, type Page, test } from "@playwright/test";

import { loadConsoleFixture, localCapabilities, mockAwfConsoleApi } from "./fixtures/console-api";

async function waitForConsoleReady(page: Page) {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

test("hosted mode shows Cloud Runtime and omits local capacity", async ({ page }) => {
  await mockAwfConsoleApi(page, { mode: "hosted" });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await waitForConsoleReady(page);

  await expect(page.getByRole("heading", { name: "Cloud Runtime" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Reliability" })).toBeVisible();
  await expect(page.getByText("Stuck", { exact: true })).toBeVisible();
  await expect(page.getByText("within_quota")).toBeVisible();
  await expect(page.getByText(/Cost|Billing|\$/i)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Resource / Runtime Capacity" })).toHaveCount(0);

  await page.screenshot({ path: "test-results/hosted-runtime-desktop.png", fullPage: true });
});

test("hosted mode mobile screenshot", async ({ page }) => {
  await mockAwfConsoleApi(page, { mode: "hosted" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByRole("heading", { name: "Cloud Runtime" })).toBeVisible();
  await page.screenshot({ path: "test-results/hosted-runtime-mobile.png", fullPage: true });
});

test("local mode keeps resource capacity panel", async ({ page }) => {
  await mockAwfConsoleApi(page, { mode: "local" });
  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.locator("#awf-capacity")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Cloud Runtime" })).toHaveCount(0);
});

test("local mobile section nav links only mounted targets", async ({ page }) => {
  await mockAwfConsoleApi(page, { mode: "local" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await waitForConsoleReady(page);

  const sectionNav = page.getByRole("navigation", { name: "Jump to section" });
  await expect(sectionNav.getByRole("button", { name: "Workspaces" })).toBeVisible();
  await expect(sectionNav.getByRole("button", { name: "Capacity" })).toBeVisible();
  await expect(sectionNav.getByRole("button", { name: "Merge queue" })).toBeVisible();
  await expect(sectionNav.getByRole("button", { name: "Failures" })).toBeVisible();
  await expect(page.locator("#awf-workspaces")).toBeVisible();
  await expect(page.locator("#awf-capacity")).toBeVisible();
  await expect(page.locator("#awf-merge-queue")).toBeVisible();
  await expect(page.locator("#awf-failures")).toBeVisible();
});

test("section nav omits jump links for unavailable widgets", async ({ page }) => {
  const caps = localCapabilities() as {
    diagnostics: Array<Record<string, unknown>>;
    widgets: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const gated = {
    ...caps,
    widgets: caps.widgets.map((item) =>
      item.id === "resource_capacity"
        ? {
            id: "resource_capacity",
            availability: "unsupported",
            reason_code: "backend_kind_hosted",
            message: "Local capacity unavailable",
            semantics: "Local Docker/disk/runtime slot saturation.",
          }
        : item,
    ),
    diagnostics: caps.diagnostics.map((item) =>
      item.id === "merge_queue" || item.id === "failures" || item.id === "reliability"
        ? {
            id: item.id,
            availability: "unsupported",
            reason_code: "not_implemented",
            message: `${String(item.id)} unavailable`,
            semantics: String(item.semantics ?? item.id),
          }
        : item,
    ),
  };
  await mockAwfConsoleApi(page, { capabilities: gated });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await waitForConsoleReady(page);

  const sectionNav = page.getByRole("navigation", { name: "Jump to section" });
  await expect(sectionNav).toBeVisible();
  await expect(sectionNav.getByRole("button", { name: "Workspaces" })).toBeVisible();
  await expect(sectionNav.getByRole("button", { name: "Capacity" })).toHaveCount(0);
  await expect(sectionNav.getByRole("button", { name: "Merge queue" })).toHaveCount(0);
  await expect(sectionNav.getByRole("button", { name: "Failures" })).toHaveCount(0);
  await expect(page.locator("#awf-capacity")).toHaveCount(0);
  await expect(page.locator("#awf-merge-queue")).toHaveCount(0);
  await expect(page.locator("#awf-failures")).toHaveCount(0);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6fzZ14: when merge_queue
// is the only advertised grid sibling, 2xl absolute overlay must not collapse
// the panel out of view — stay in normal flow without a capacity column.
test("merge queue stays visible at 2xl when capacity panels are unsupported", async ({ page }) => {
  const caps = localCapabilities() as {
    diagnostics: Array<Record<string, unknown>>;
    widgets: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const gated = {
    ...caps,
    widgets: caps.widgets.map((item) =>
      item.id === "resource_capacity" || item.id === "cloud_runtime"
        ? {
            id: item.id,
            availability: "unsupported",
            reason_code: "not_implemented",
            message: `${String(item.id)} unavailable`,
            semantics: String(item.semantics ?? item.id),
          }
        : item,
    ),
    diagnostics: caps.diagnostics.map((item) =>
      item.id === "reliability"
        ? {
            id: "reliability",
            availability: "unsupported",
            reason_code: "not_implemented",
            message: "reliability unavailable",
            semantics: String(item.semantics ?? "reliability"),
          }
        : item,
    ),
  };
  await mockAwfConsoleApi(page, { capabilities: gated });
  // Tailwind 2xl = 1536px; absolute overlay only applies at this breakpoint.
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await waitForConsoleReady(page);

  await expect(page.locator("#awf-capacity")).toHaveCount(0);
  const mergeQueue = page.locator("#awf-merge-queue");
  await expect(mergeQueue).toBeVisible();
  await expect(page.getByRole("heading", { name: "Merge Queue" })).toBeVisible();
  const box = await mergeQueue.boundingBox();
  expect(box?.height ?? 0).toBeGreaterThan(40);
  // Failures span the full row; without capacity the queue must match that
  // width instead of occupying only the first 2xl track.
  const failures = page.locator("#awf-failures");
  await expect(failures).toBeVisible();
  const failuresBox = await failures.boundingBox();
  expect(Math.abs((box?.width ?? 0) - (failuresBox?.width ?? 0))).toBeLessThan(8);
});

test("hosted mobile section nav keeps capacity when cloud runtime is available", async ({ page }) => {
  await mockAwfConsoleApi(page, { mode: "hosted" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await waitForConsoleReady(page);

  const sectionNav = page.getByRole("navigation", { name: "Jump to section" });
  await expect(sectionNav.getByRole("button", { name: "Workspaces" })).toBeVisible();
  await expect(sectionNav.getByRole("button", { name: "Capacity" })).toBeVisible();
  await expect(sectionNav.getByRole("button", { name: "Merge queue" })).toBeVisible();
  await expect(sectionNav.getByRole("button", { name: "Failures" })).toBeVisible();
  await expect(page.locator("#awf-capacity")).toBeVisible();
  await expect(page.locator("#awf-merge-queue")).toBeVisible();
  await expect(page.locator("#awf-failures")).toBeVisible();
});

test("section nav omits only the unavailable merge-queue jump link", async ({ page }) => {
  const caps = localCapabilities() as {
    diagnostics: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const gated = {
    ...caps,
    diagnostics: caps.diagnostics.map((item) =>
      item.id === "merge_queue"
        ? {
            id: "merge_queue",
            availability: "unsupported",
            reason_code: "not_implemented",
            message: "Merge queue unavailable",
            semantics: String(item.semantics ?? "merge_queue"),
          }
        : item,
    ),
  };
  await mockAwfConsoleApi(page, { capabilities: gated });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await waitForConsoleReady(page);

  const sectionNav = page.getByRole("navigation", { name: "Jump to section" });
  await expect(sectionNav.getByRole("button", { name: "Workspaces" })).toBeVisible();
  await expect(sectionNav.getByRole("button", { name: "Capacity" })).toBeVisible();
  await expect(sectionNav.getByRole("button", { name: "Merge queue" })).toHaveCount(0);
  await expect(sectionNav.getByRole("button", { name: "Failures" })).toBeVisible();
  await expect(page.locator("#awf-merge-queue")).toHaveCount(0);
  await expect(page.locator("#awf-capacity")).toBeVisible();
  await expect(page.locator("#awf-failures")).toBeVisible();
});

test("capability failure omits section nav links without mounted targets", async ({ page }) => {
  await mockAwfConsoleApi(page, {
    capabilities: loadConsoleFixture("capabilities.malformed.json"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByText(/malformed|capabilities/i).first()).toBeVisible();

  const sectionNav = page.getByRole("navigation", { name: "Jump to section" });
  await expect(sectionNav.getByRole("button", { name: "Workspaces" })).toBeVisible();
  await expect(sectionNav.getByRole("button", { name: "Capacity" })).toHaveCount(0);
  await expect(sectionNav.getByRole("button", { name: "Merge queue" })).toHaveCount(0);
  await expect(sectionNav.getByRole("button", { name: "Failures" })).toHaveCount(0);
  await expect(page.locator("#awf-workspaces")).toBeVisible();
  await expect(page.locator("#awf-capacity")).toHaveCount(0);
  await expect(page.locator("#awf-merge-queue")).toHaveCount(0);
  await expect(page.locator("#awf-failures")).toHaveCount(0);
});
