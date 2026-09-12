import { expect, type Page, test } from "@playwright/test";

import type { ConsoleDashboardSummary } from "@/lib/types";

import { localDashboardSummary, hostedDashboardSummary, mockAwfConsoleApi } from "./fixtures/console-api";

async function waitForConsoleReady(page: Page) {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

const kpi = (page: Page, label: string) =>
  page.getByText(label, { exact: true }).locator("..").filter({ has: page.locator(".kpi-value") });

function hostedCountEvidenceSummary(): ConsoleDashboardSummary {
  const base = hostedDashboardSummary() as ConsoleDashboardSummary;
  return {
    ...base,
    coverage: {
      status: "partial",
      notes: ["terminal_timestamp_unavailable", "attention_evidence_unavailable"],
    },
    counts: {
      active: null,
      executing: null,
      monitoring_pr: null,
      awaiting_operator: null,
      awaiting_human: null,
      retrying: null,
      queued: null,
      completed_last_window: null,
      cancelled_last_window: null,
      failed_last_window: null,
    },
    count_evidence: {
      total_workspaces: 30,
      status_known_workspaces: 25,
      status_unknown_workspaces: 5,
      confirmed_counts: {
        active: 1,
        executing: 1,
        monitoring_pr: 0,
        awaiting_operator: 0,
        awaiting_human: 0,
        retrying: 0,
        queued: 0,
        completed_last_window: 0,
        cancelled_last_window: 0,
        failed_last_window: 0,
      },
    },
  };
}

test("KPI values come from dashboard-summary when saturation absent", async ({ page }) => {
  const requested: string[] = [];
  await mockAwfConsoleApi(page, {
    mode: "hosted",
    // Hosted capabilities require tenant-scope summary; localDashboardSummary is rejected.
    dashboardSummary: hostedDashboardSummary(),
    onRequest: (path) => requested.push(path),
  });

  await page.goto("/");
  await waitForConsoleReady(page);

  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("15");
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
  await expect(page.getByTestId("dashboard-summary-coverage")).toHaveCount(0);
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

  // HTTP 200 clears the request error; partial coverage must still be explicit.
  const coverage = page.getByTestId("dashboard-summary-coverage");
  await expect(coverage).toBeVisible();
  await expect(coverage).toContainText("partial coverage");
  await expect(coverage).toContainText("queued count unavailable");
  await expect(coverage).toContainText("last complete 2026-09-06T17:00:00Z");
  await expect(page.getByTestId("dashboard-summary-error")).toHaveCount(0);
});

test("unknown dashboard coverage renders an explicit notice without a request error", async ({ page }) => {
  await mockAwfConsoleApi(page, {
    dashboardSummary: localDashboardSummary({
      coverage: { status: "unknown", notes: ["provider_lag"] },
      counts: {
        active: 3,
        executing: null,
        monitoring_pr: 1,
        awaiting_operator: 0,
        awaiting_human: 0,
        retrying: 0,
        queued: null,
        completed_last_window: 1,
        cancelled_last_window: 0,
        failed_last_window: 0,
      },
    }),
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("3");
  await expect(kpi(page, "Running").locator(".kpi-value")).toHaveText("—");
  const coverage = page.getByTestId("dashboard-summary-coverage");
  await expect(coverage).toBeVisible();
  await expect(coverage).toContainText("coverage unknown");
  await expect(coverage).toContainText("provider lag");
  await expect(page.getByTestId("dashboard-summary-error")).toHaveCount(0);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`count evidence stays qualified and readable on ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockAwfConsoleApi(page, {
      mode: "hosted",
      dashboardSummary: hostedCountEvidenceSummary(),
    });

    await page.goto("/");
    await waitForConsoleReady(page);

    // Unknown workflow statuses make every exact count unproven, so the fixture
    // exposes only explicitly qualified lower bounds, including zero.
    await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("1 confirmed");
    await expect(kpi(page, "Awaiting operator").locator(".kpi-value")).toHaveText("0 confirmed");
    await expect(kpi(page, "Running").locator(".kpi-value")).toHaveText("1 confirmed");
    await expect(kpi(page, "Monitoring PR").locator(".kpi-value")).toHaveText("0 confirmed");
    await expect(kpi(page, "Completed").locator(".kpi-value")).toHaveText("0 confirmed");
    await expect(kpi(page, "Running")).toContainText("exact metric count is incomplete");
    await expect(kpi(page, "Completed")).toContainText("last 24h");
    await expect(kpi(page, "Completed")).toContainText("exact metric count is incomplete");

    const coverage = page.getByTestId("dashboard-summary-coverage");
    await expect(coverage).toContainText("25 of 30 workflow statuses known; 5 unknown");
    await expect(coverage).toContainText("terminal timestamp unavailable");
    await expect(coverage).toContainText("attention evidence unavailable");
    await expect(page.getByTestId("dashboard-summary-error")).toHaveCount(0);

    for (const label of ["Running", "Monitoring PR", "Completed"]) {
      const card = kpi(page, label);
      const valueBox = await card.locator(".kpi-value").boundingBox();
      const hintBox = await card.getByText(/exact metric count is incomplete/).boundingBox();
      expect(valueBox, `${label} value is measurable`).not.toBeNull();
      expect(hintBox, `${label} incomplete-metric hint is measurable`).not.toBeNull();
      expect(valueBox!.y + valueBox!.height, `${label} value does not overlap its hint`).toBeLessThanOrEqual(
        hintBox!.y + 1,
      );
    }
    const hasHorizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(hasHorizontalOverflow).toBe(false);
  });
}

test("status counters stay consistent for escalation/retry/terminal fixtures", async ({ page }) => {
  await mockAwfConsoleApi(page, {
    dashboardSummary: localDashboardSummary({
      counts: {
        // executing+monitoring_pr+queued+awaiting_operator+retrying = 9 ⊆ active
        active: 9,
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
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("9");
  await expect(kpi(page, "Awaiting operator").locator(".kpi-value")).toHaveText("1");
  await expect(kpi(page, "Awaiting human").locator(".kpi-value")).toHaveText("1");
  await expect(kpi(page, "Auto-retrying").locator(".kpi-value")).toHaveText("1");
  await expect(kpi(page, "Completed").locator(".kpi-value")).toHaveText("3");
  await page.screenshot({ path: "test-results/dashboard-summary-kpis-desktop.png", fullPage: true });
});
