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

function localExactCountSummary(): ConsoleDashboardSummary {
  return localDashboardSummary({
    counts: {
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
  });
}

/** Exact counts win; confirmed evidence fills only null exact fields. */
function mixedExactAndConfirmedSummary(): ConsoleDashboardSummary {
  // Parser invariants: unknown statuses must be 0 when any exact count is
  // non-null, and every non-null exact value must equal confirmed_counts.
  // Null exact fields remain so confirmed fallback is still exercised.
  return localDashboardSummary({
    coverage: {
      status: "partial",
      notes: ["some_counts_unavailable"],
    },
    counts: {
      active: 7,
      executing: null,
      monitoring_pr: 2,
      awaiting_operator: null,
      awaiting_human: 0,
      retrying: null,
      queued: 1,
      completed_last_window: null,
      cancelled_last_window: null,
      failed_last_window: 0,
    },
    count_evidence: {
      total_workspaces: 11,
      status_known_workspaces: 11,
      status_unknown_workspaces: 0,
      confirmed_counts: {
        active: 7,
        executing: 3,
        monitoring_pr: 2,
        awaiting_operator: 1,
        awaiting_human: 0,
        retrying: 0,
        queued: 1,
        completed_last_window: 4,
        cancelled_last_window: 0,
        failed_last_window: 0,
      },
    },
  });
}

/** Page + Fleet health strip must not overflow on any count-selection fixture. */
async function assertNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => {
    const root = document.documentElement;
    const body = document.body;
    const viewport = window.innerWidth;
    const amount = Math.max(root.scrollWidth, body.scrollWidth) - viewport;
    const strip = document.querySelector('[aria-label="Fleet health"]');
    const stripOverflow =
      strip instanceof HTMLElement ? strip.scrollWidth > strip.clientWidth + 1 : false;
    return { amount, stripOverflow, viewport };
  });
  expect(overflow.amount, JSON.stringify(overflow)).toBeLessThanOrEqual(1);
  expect(overflow.stripOverflow, JSON.stringify(overflow)).toBe(false);
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

test("null dashboard counts render as dash not zero with coverage banner", async ({ page }) => {
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

test("unknown dashboard coverage renders an explicit notice without a request error", async ({
  page,
}) => {
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

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile-390", width: 390, height: 844 },
  { name: "mobile-375", width: 375, height: 812 },
] as const;

type CountScenario = {
  name: string;
  mode: "local" | "hosted";
  summary: () => ConsoleDashboardSummary;
  assertKpis: (page: Page) => Promise<void>;
  expectCoverage?: (page: Page) => Promise<void>;
};

const COUNT_SCENARIOS: CountScenario[] = [
  {
    name: "hosted-confirmed",
    mode: "hosted",
    summary: hostedCountEvidenceSummary,
    assertKpis: async (page) => {
      // Unknown workflow statuses leave exact counts null; confirmed evidence shows
      // visibly qualified lower bounds (`N confirmed`) per CONSOLE_BACKEND_CONTRACT.
      await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("1 confirmed");
      await expect(kpi(page, "Awaiting operator").locator(".kpi-value")).toHaveText("0 confirmed");
      await expect(kpi(page, "Running").locator(".kpi-value")).toHaveText("1 confirmed");
      await expect(kpi(page, "Monitoring PR").locator(".kpi-value")).toHaveText("0 confirmed");
      await expect(kpi(page, "Completed").locator(".kpi-value")).toHaveText("0 confirmed");
      await expect(kpi(page, "Running")).toContainText("exact metric count is incomplete");
      await expect(kpi(page, "Completed")).toContainText("last 24h");
      await expect(kpi(page, "Completed")).toContainText("exact metric count is incomplete");
    },
    expectCoverage: async (page) => {
      const coverage = page.getByTestId("dashboard-summary-coverage");
      await expect(coverage).toContainText("24 of 29 workflow statuses known; 5 unknown");
      await expect(coverage).toContainText("terminal timestamp unavailable");
      await expect(coverage).toContainText("attention evidence unavailable");
      await expect(page.getByTestId("dashboard-summary-error")).toHaveCount(0);
    },
  },
  {
    name: "local-exact",
    mode: "local",
    summary: localExactCountSummary,
    assertKpis: async (page) => {
      await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("9");
      await expect(kpi(page, "Running").locator(".kpi-value")).toHaveText("3");
      await expect(kpi(page, "Monitoring PR").locator(".kpi-value")).toHaveText("2");
      await expect(kpi(page, "Awaiting operator").locator(".kpi-value")).toHaveText("1");
      await expect(kpi(page, "Awaiting human").locator(".kpi-value")).toHaveText("1");
      await expect(kpi(page, "Auto-retrying").locator(".kpi-value")).toHaveText("1");
      await expect(kpi(page, "Queued").locator(".kpi-value")).toHaveText("2");
      await expect(kpi(page, "Completed").locator(".kpi-value")).toHaveText("3");
      await expect(kpi(page, "Cancelled").locator(".kpi-value")).toHaveText("1");
      await expect(kpi(page, "Failed").locator(".kpi-value")).toHaveText("2");
      await expect(kpi(page, "Completed")).toContainText("last 24h");
    },
    expectCoverage: async (page) => {
      await expect(page.getByTestId("dashboard-summary-coverage")).toHaveCount(0);
    },
  },
  {
    name: "mixed-exact-confirmed",
    mode: "local",
    summary: mixedExactAndConfirmedSummary,
    assertKpis: async (page) => {
      // Exact wins when both present; confirmed fills nulls with `N confirmed`.
      await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("7");
      await expect(kpi(page, "Running").locator(".kpi-value")).toHaveText("3 confirmed");
      await expect(kpi(page, "Monitoring PR").locator(".kpi-value")).toHaveText("2");
      await expect(kpi(page, "Awaiting operator").locator(".kpi-value")).toHaveText("1 confirmed");
      await expect(kpi(page, "Awaiting human").locator(".kpi-value")).toHaveText("0");
      await expect(kpi(page, "Auto-retrying").locator(".kpi-value")).toHaveText("0 confirmed");
      await expect(kpi(page, "Queued").locator(".kpi-value")).toHaveText("1");
      await expect(kpi(page, "Completed").locator(".kpi-value")).toHaveText("4 confirmed");
      await expect(kpi(page, "Cancelled").locator(".kpi-value")).toHaveText("0 confirmed");
      await expect(kpi(page, "Failed").locator(".kpi-value")).toHaveText("0");
      await expect(kpi(page, "Completed")).toContainText("last 24h");
      await expect(kpi(page, "Running")).toContainText("exact metric count is incomplete");
    },
    expectCoverage: async (page) => {
      const coverage = page.getByTestId("dashboard-summary-coverage");
      await expect(coverage).toBeVisible();
      await expect(coverage).toContainText("partial coverage");
      await expect(coverage).toContainText("11 of 11 workflow statuses known; 0 unknown");
      await expect(coverage).toContainText("some counts unavailable");
    },
  },
];

for (const viewport of VIEWPORTS) {
  for (const scenario of COUNT_SCENARIOS) {
    test(`${scenario.name} stays contract-qualified on ${viewport.name}`, async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await mockAwfConsoleApi(page, {
        mode: scenario.mode,
        dashboardSummary: scenario.summary(),
      });

      await page.goto("/");
      await waitForConsoleReady(page);
      await scenario.assertKpis(page);
      if (scenario.expectCoverage) {
        await scenario.expectCoverage(page);
      }
      await assertNoHorizontalOverflow(page);

      await page.screenshot({
        path: `test-results/dashboard-summary-kpis-${scenario.name}-${viewport.name}.png`,
        fullPage: true,
      });
    });
  }
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
  await expect(page.getByTestId("dashboard-summary-coverage")).toHaveCount(0);
  await page.screenshot({ path: "test-results/dashboard-summary-kpis-desktop.png", fullPage: true });
});
