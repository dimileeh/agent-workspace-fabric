import { expect, type Page, test } from "@playwright/test";

import { localDashboardSummary, mockAwfConsoleApi } from "./fixtures/console-api";

async function waitForConsoleReady(page: Page) {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

const kpi = (page: Page, label: string) =>
  page.getByText(label, { exact: true }).locator("..").filter({ has: page.locator(".kpi-value") });

test("KPI values come from dashboard-summary when saturation absent", async ({ page }) => {
  const requested: string[] = [];
  await mockAwfConsoleApi(page, {
    mode: "hosted",
    dashboardSummary: localDashboardSummary({
      counts: {
        active: 12,
        executing: 7,
        monitoring_pr: 3,
        awaiting_operator: 0,
        awaiting_human: 2,
        retrying: 1,
        queued: 4,
        completed_last_window: 9,
        cancelled_last_window: 2,
        failed_last_window: 1,
      },
    }),
    onRequest: (path) => requested.push(path),
  });

  await page.goto("/");
  await waitForConsoleReady(page);

  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("12");
  await expect(kpi(page, "Running").locator(".kpi-value")).toHaveText("7");
  await expect(kpi(page, "Monitoring PR").locator(".kpi-value")).toHaveText("3");
  await expect(kpi(page, "Awaiting human").locator(".kpi-value")).toHaveText("2");
  await expect(kpi(page, "Auto-retrying").locator(".kpi-value")).toHaveText("1");
  await expect(kpi(page, "Queued").locator(".kpi-value")).toHaveText("4");
  await expect(kpi(page, "Completed").locator(".kpi-value")).toHaveText("9");
  await expect(kpi(page, "Cancelled").locator(".kpi-value")).toHaveText("2");
  await expect(kpi(page, "Failed").locator(".kpi-value")).toHaveText("1");
  await expect(kpi(page, "Capacity")).toHaveCount(0);

  expect(requested.some((path) => path.includes("/metrics/resources/saturation"))).toBe(false);
});

test("null dashboard counts render as dash not zero", async ({ page }) => {
  await mockAwfConsoleApi(page, {
    dashboardSummary: localDashboardSummary({
      coverage: { status: "partial", notes: ["queued_count_unavailable"] },
      counts: {
        active: 3,
        executing: 2,
        monitoring_pr: 1,
        awaiting_operator: 0,
        awaiting_human: 0,
        retrying: 0,
        queued: null,
        completed_last_window: 1,
        cancelled_last_window: null,
        failed_last_window: 0,
      },
    }),
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(kpi(page, "Queued").locator(".kpi-value")).toHaveText("—");
  await expect(kpi(page, "Cancelled").locator(".kpi-value")).toHaveText("—");
  await expect(kpi(page, "Failed").locator(".kpi-value")).toHaveText("0");
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("3");
});

test("status counters stay consistent for escalation/retry/terminal fixtures", async ({ page }) => {
  await mockAwfConsoleApi(page, {
    dashboardSummary: localDashboardSummary({
      counts: {
        active: 8,
        executing: 3,
        monitoring_pr: 2,
        awaiting_operator: 1,
        awaiting_human: 1,
        retrying: 1,
        queued: 2,
        completed_last_window: 3,
        cancelled_last_window: 1,
        failed_last_window: 2,
      },
    }),
  });
  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(kpi(page, "Awaiting operator").locator(".kpi-value")).toHaveText("1");
  await expect(kpi(page, "Awaiting human").locator(".kpi-value")).toHaveText("1");
  await expect(kpi(page, "Auto-retrying").locator(".kpi-value")).toHaveText("1");
  await expect(kpi(page, "Completed").locator(".kpi-value")).toHaveText("3");
  await page.screenshot({ path: "test-results/dashboard-summary-kpis-desktop.png", fullPage: true });
});
