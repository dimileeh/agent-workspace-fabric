import { expect, type Page, test } from "@playwright/test";

import {
  localDashboardSummary,
  mockAwfConsoleApi,
} from "./fixtures/console-api";

// Regression: the fleet-summary "Running" KPI must count the active-execution
// phase (running + validating + pushing), not only the literal "running" status.
// Counts now come from dashboard-summary (independent of saturation).

test.beforeEach(async ({ page }) => {
  await mockAwfConsoleApi(page, {
    dashboardSummary: localDashboardSummary({
      counts: {
        active: 5,
        executing: 4,
        monitoring_pr: 0,
        awaiting_operator: 1,
        awaiting_human: 0,
        retrying: 0,
        queued: 0,
        completed_last_window: 0,
        cancelled_last_window: 0,
        failed_last_window: 0,
      },
    }),
  });
});

test("Running KPI sums running, validating, and pushing workspaces", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  const runningKpi = page.getByText("Running", { exact: true }).locator("..").filter({ has: page.locator(".kpi-value") });
  await expect(runningKpi).toBeVisible();
  await expect(runningKpi.locator(".kpi-value")).toHaveText("4");
});

test("a blocked workspace lands in Active but never in the Running KPI", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  const kpi = (label: string) =>
    page.getByText(label, { exact: true }).locator("..").filter({ has: page.locator(".kpi-value") });

  await expect(kpi("Running").locator(".kpi-value")).toHaveText("4");
  await expect(kpi("Active").locator(".kpi-value")).toHaveText("5");
  await expect(kpi("Awaiting operator").locator(".kpi-value")).toHaveText("1");
});

async function waitForConsoleReady(page: Page) {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}
