import { expect, type Page, test } from "@playwright/test";

import type { ConsoleDashboardSummary } from "@/lib/types";

import { localDashboardSummary, hostedDashboardSummary, mockAwfConsoleApi } from "./fixtures/console-api";

async function waitForConsoleReady(page: Page) {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

const kpi = (page: Page, label: string) =>
  page.getByText(label, { exact: true }).locator("..").filter({ has: page.locator(".kpi-value") });

const COVERAGE_OR_CONFIRMED_COPY =
  /confirmed|lower bound|partial coverage|coverage unknown|workflow statuses known/i;

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
      // Production-shaped acceptance fixture: 29 total / 24 known / 5 unknown.
      total_workspaces: 29,
      status_known_workspaces: 24,
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

async function assertNoCoverageChrome(page: Page) {
  await expect(page.getByTestId("dashboard-summary-coverage")).toHaveCount(0);
  await expect(page.getByTestId("dashboard-summary-error")).toHaveCount(0);
  const strip = page.getByLabel("Fleet health");
  await expect(strip).not.toContainText(COVERAGE_OR_CONFIRMED_COPY);
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
  await assertNoCoverageChrome(page);
});

test("null dashboard counts render as dash not zero without coverage banner", async ({ page }) => {
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

  // HTTP 200 clears the request error; partial coverage alone must not surface a banner.
  await assertNoCoverageChrome(page);
});

test("unknown dashboard coverage stays silent without a request error", async ({ page }) => {
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
  await assertNoCoverageChrome(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-390", width: 390, height: 844 },
  { name: "mobile-375", width: 375, height: 812 },
]) {
  test(`count evidence renders plain numbers on ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockAwfConsoleApi(page, {
      mode: "hosted",
      dashboardSummary: hostedCountEvidenceSummary(),
    });

    await page.goto("/");
    await waitForConsoleReady(page);

    // Unknown workflow statuses leave exact counts null; confirmed evidence still shows
    // plain numbers (including evidenced zeros) — never "N confirmed" or lower-bound copy.
    await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("1");
    await expect(kpi(page, "Awaiting operator").locator(".kpi-value")).toHaveText("0");
    await expect(kpi(page, "Running").locator(".kpi-value")).toHaveText("1");
    await expect(kpi(page, "Monitoring PR").locator(".kpi-value")).toHaveText("0");
    await expect(kpi(page, "Completed").locator(".kpi-value")).toHaveText("0");
    await expect(kpi(page, "Completed")).toContainText("last 24h");
    await expect(kpi(page, "Completed")).not.toContainText(COVERAGE_OR_CONFIRMED_COPY);
    await expect(kpi(page, "Running")).not.toContainText(COVERAGE_OR_CONFIRMED_COPY);

    await assertNoCoverageChrome(page);

    const hasHorizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(hasHorizontalOverflow).toBe(false);

    await page.screenshot({
      path: `test-results/dashboard-summary-kpis-count-evidence-${viewport.name}.png`,
      fullPage: true,
    });
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
  await expect(kpi(page, "Completed")).toContainText("last 24h");
  await assertNoCoverageChrome(page);
  await page.screenshot({ path: "test-results/dashboard-summary-kpis-desktop.png", fullPage: true });
});
