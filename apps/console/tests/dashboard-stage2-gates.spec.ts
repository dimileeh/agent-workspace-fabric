import { expect, type Page, test } from "@playwright/test";

import {
  fulfillJson,
  hostedCapabilities,
  loadConsoleFixture,
  localCapabilities,
  localDashboardSummary,
  mockAwfConsoleApi,
} from "./fixtures/console-api";

async function waitForConsoleReady(page: Page) {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

const kpi = (page: Page, label: string) =>
  page.getByText(label, { exact: true }).locator("..").filter({ has: page.locator(".kpi-value") });

function presentationOverview(): Record<string, unknown> {
  const sample = loadConsoleFixture<Record<string, unknown>>("workspace-presentation.sample.json");
  const { notes: _notes, ...fields } = sample;
  return {
    ...fields,
    task_prompt: "Populate console presentation metadata",
    network_posture: "restricted",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
    coordination_warnings: [],
    provider_readiness_preflight: null,
    subphase: null,
    last_log_at: sample.last_activity_at,
    is_stale_running: false,
    current_phase: "monitoring_pr",
    active_operation: null,
    last_event: null,
    pr_number: 1,
    failure_reason: null,
    failure_message: null,
  };
}

function capabilitiesWithUnsupportedCancel() {
  const caps = localCapabilities() as {
    controls: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  return {
    ...caps,
    controls: caps.controls.map((control) =>
      control.id === "cancel"
        ? {
            ...control,
            availability: "unsupported",
            reason_code: "policy_disabled",
            message: "Cancel is not available on this backend.",
          }
        : control,
    ),
  };
}

test("unsupported controls are disabled with capability reason", async ({ page }) => {
  const overview = {
    ...presentationOverview(),
    status: "running",
    current_phase: "running",
    pr_url: null,
    pr_number: null,
    native_runtime_finished_at: null,
  } as Record<string, unknown>;
  await mockAwfConsoleApi(page, {
    capabilities: capabilitiesWithUnsupportedCancel(),
    overviewItems: [overview],
  });
  await page.route("**/api/awf/workspaces/ws_presentation_sample**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/workspaces/ws_presentation_sample") {
      await fulfillJson(route, {
        ...overview,
        id: overview.workspace_id,
        version: 1,
      });
      return;
    }
    if (path.endsWith("/runtime")) {
      await fulfillJson(route, { status: "running" });
      return;
    }
    if (path.includes("/events") || path.includes("/operations") || path.includes("/logs")) {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId("workspace-card-ws_presentation_sample").click();
  const cancel = page.getByRole("button", { name: "Cancel" });
  await expect(cancel).toBeVisible();
  await expect(cancel).toBeDisabled();
  await cancel.locator("..").hover();
  await expect(page.getByRole("tooltip")).toContainText("Cancel is not available on this backend.");
});

test("dashboard-summary outage keeps last-successful KPIs with stale marker", async ({ page }) => {
  let summaryOutage = false;
  const summary = localDashboardSummary({
    counts: {
      active: 9,
      executing: 7,
      monitoring_pr: 1,
      awaiting_operator: 0,
      awaiting_human: 0,
      retrying: 0,
      queued: 0,
      completed_last_window: 0,
      cancelled_last_window: 0,
      failed_last_window: 0,
    },
  });

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      if (summaryOutage) {
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "summary outage" } },
          503,
        );
        return;
      }
      await fulfillJson(route, summary);
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, {
        generated_at: "2026-09-06T17:00:00Z",
        since_hours: 24,
        completed_count: 0,
        failed_count: 0,
        cancelled_count: 0,
        stuck_count: 0,
        actionable_reason_count: 0,
        unactionable_reason_count: 0,
        active_count: 0,
        destroying_count: 0,
        destroyed_count: 0,
        cleanup_failure_count: 0,
        status_counts: {},
        failure_reason_counts: {},
        window_start: "2026-09-05T17:00:00Z",
      });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("9");

  summaryOutage = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/last snapshot|may be stale/i)).toBeVisible({ timeout: 10_000 });
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("9");
  await expect(kpi(page, "Active")).toHaveAttribute("data-awf-stale", "true");
});

test("backend identity change clears authorized in-memory feeds", async ({ page }) => {
  let useHostedTenant = false;
  const localSummary = localDashboardSummary({
    counts: {
      active: 9,
      executing: 4,
      monitoring_pr: 1,
      awaiting_operator: 0,
      awaiting_human: 0,
      retrying: 0,
      queued: 0,
      completed_last_window: 0,
      cancelled_last_window: 0,
      failed_last_window: 0,
    },
  });
  const hostedCaps = {
    ...(hostedCapabilities() as Record<string, unknown>),
    identity: {
      backend_id: "awf-cloud-tenant-b",
      scope: "tenant",
      tenant_id: "tenant_b",
    },
  };

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, useHostedTenant ? hostedCaps : localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      if (useHostedTenant) {
        // Fail after identity switch so cleared state cannot fall back to local 9.
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "tenant summary unavailable" } },
          503,
        );
        return;
      }
      await fulfillJson(route, localSummary);
      return;
    }
    if (path === "/api/awf/console/cloud-runtime") {
      await fulfillJson(route, loadConsoleFixture("cloud-runtime.hosted.json"));
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, {
        generated_at: "2026-09-06T17:00:00Z",
        since_hours: 24,
        completed_count: 0,
        failed_count: 0,
        cancelled_count: 0,
        stuck_count: 0,
        actionable_reason_count: 0,
        unactionable_reason_count: 0,
        active_count: 0,
        destroying_count: 0,
        destroyed_count: 0,
        cleanup_failure_count: 0,
        status_counts: {},
        failure_reason_counts: {},
        window_start: "2026-09-05T17:00:00Z",
      });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("9");
  await expect(page.getByText("Resource / Runtime Capacity")).toBeVisible();

  useHostedTenant = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("—", { timeout: 10_000 });
  await expect(page.getByText("Resource / Runtime Capacity")).toHaveCount(0);
});

test("presentation fixture renders requested vs confirmed and distinct finish facts", async ({ page }) => {
  const overview = presentationOverview();
  await mockAwfConsoleApi(page, { overviewItems: [overview] });
  await page.route("**/api/awf/workspaces/ws_presentation_sample**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/workspaces/ws_presentation_sample") {
      await fulfillJson(route, {
        ...overview,
        id: overview.workspace_id,
        version: 1,
      });
      return;
    }
    if (path.endsWith("/runtime")) {
      await fulfillJson(route, { status: "monitoring_pr" });
      return;
    }
    if (path.includes("/events") || path.includes("/operations") || path.includes("/logs")) {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByTestId("workspace-task-key-ws_presentation_sample")).toHaveText(
    "CONSOLE-PRESENTATION",
  );
  await expect(page.getByTestId("workspace-last-activity-ws_presentation_sample")).toBeVisible();

  await page.getByTestId("workspace-card-ws_presentation_sample").click();
  await expect(page.getByText("Requested model", { exact: true })).toBeVisible();
  await expect(page.getByText("gpt-5.5 (task_policy)", { exact: true })).toBeVisible();
  await expect(page.getByText("Confirmed model", { exact: true })).toBeVisible();
  await expect(page.getByText("gpt-5.5-2026-08-07 (execution_evidence)", { exact: true })).toBeVisible();
  await expect(page.getByText("Native runtime finished", { exact: true })).toBeVisible();
  await expect(page.getByText("Workflow finished", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Task key", { exact: true })).toBeVisible();
  await expect(page.getByText("Last activity", { exact: true })).toBeVisible();
});

test("capability 403 clears stale summary KPIs", async ({ page }) => {
  let authDenied = false;
  const summary = localDashboardSummary({
    counts: {
      active: 9,
      executing: 7,
      monitoring_pr: 1,
      awaiting_operator: 0,
      awaiting_human: 0,
      retrying: 0,
      queued: 0,
      completed_last_window: 0,
      cancelled_last_window: 0,
      failed_last_window: 0,
    },
  });

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      if (authDenied) {
        await fulfillJson(
          route,
          { detail: { error_code: "FORBIDDEN", message: "AWF API token lacks console access." } },
          403,
        );
        return;
      }
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, summary);
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, {
        generated_at: "2026-09-06T17:00:00Z",
        since_hours: 24,
        completed_count: 0,
        failed_count: 0,
        cancelled_count: 0,
        stuck_count: 0,
        actionable_reason_count: 0,
        unactionable_reason_count: 0,
        active_count: 0,
        destroying_count: 0,
        destroyed_count: 0,
        cleanup_failure_count: 0,
        status_counts: {},
        failure_reason_counts: {},
        window_start: "2026-09-05T17:00:00Z",
      });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("9");

  authDenied = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/forbidden|authorization denied|denied|lacks console/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("—");
});
