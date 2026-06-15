import { expect, type Page, test } from "@playwright/test";

// Regression: the fleet-summary "Running" KPI must count the active-execution
// phase (running + validating + pushing), not only the literal "running" status.
// A workspace in validating or pushing should still contribute to Running.

const now = "2026-05-02T12:00:00.000Z";

test.beforeEach(async ({ page }) => {
  await mockAwfApi(page);
});

test("Running KPI sums running, validating, and pushing workspaces", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  // Find the Running KPI card and assert its rendered numeric value.
  const runningKpi = page.getByText("Running", { exact: true }).locator("..").filter({ has: page.locator(".kpi-value") });
  await expect(runningKpi).toBeVisible();

  // running:2 + validating:1 + pushing:1 => 4
  await expect(runningKpi.locator(".kpi-value")).toHaveText("4");
});

async function waitForConsoleReady(page: Page) {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

async function mockAwfApi(page: Page) {
  await page.route("**/api/awf/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }

    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });
}

async function fulfillJson(route: Parameters<Parameters<Page["route"]>[1]>[0], body: unknown, status = 200) {
  await route.fulfill({ status, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
}

function listEnvelope<T>(items: T[]) {
  return { items, next_cursor: null, has_more: false };
}

function resourceSaturation() {
  return {
    generated_at: now,
    workspace_counts: {
      by_status: {
        running: 2,
        validating: 1,
        pushing: 1,
        monitoring_pr: 0,
      },
      active_total: 4,
      requested: 0,
      provisioning: 0,
      ready: 0,
      running: 2,
      validating: 1,
      pushing: 1,
      monitoring_pr: 0,
      destroying: 0,
      completed: 0,
      failed: 0,
      cancelled: 0,
      destroyed: 0,
    },
    worker: { max_concurrent_provisions: 2, max_concurrent_executions: 4 },
    local_capacity: { cpu_cores: 8, memory_gb: 32, source: "docker", reason_code: null, detail: null },
    resource_defaults: { steady_cpu: 1, steady_memory_gb: 2, peak_cpu: 2, peak_memory_gb: 4 },
    reserved_resources: {
      active_workspace_count: 4,
      steady_cpu: 4,
      steady_memory_gb: 8,
      peak_cpu: 8,
      peak_memory_gb: 16,
      disk_mb: 10240,
      dind_slots: 0,
    },
    capacity: {
      steady_cpu: { limit: null, reserved: 4, available: 4, available_after_next_default: 2, reason_code: null },
      peak_cpu: { limit: null, reserved: 8, available: 8, available_after_next_default: 4, reason_code: null },
      steady_memory_gb: { limit: null, reserved: 8, available: 24, available_after_next_default: 20, reason_code: null },
      peak_memory_gb: { limit: null, reserved: 16, available: 16, available_after_next_default: 12, reason_code: null },
      disk_mb: { limit: null, reserved: 10240, available: 1048576, available_after_next_default: 1038336, reason_code: null },
      dind_slots: { limit: null, reserved: 0, available: null, available_after_next_default: null, reason_code: null },
      pressure_reasons: [],
    },
    allocated_resources: {
      active_workspace_count: 0,
      steady_cpu: 0,
      steady_memory_gb: 0,
      peak_cpu: 0,
      peak_memory_gb: 0,
      disk_mb: 0,
      dind_slots: 0,
    },
    allocated_capacity: {
      steady_cpu: { limit: null, reserved: 0, available: 8, available_after_next_default: 6, reason_code: null },
      peak_cpu: { limit: null, reserved: 0, available: 16, available_after_next_default: 12, reason_code: null },
      steady_memory_gb: { limit: null, reserved: 0, available: 32, available_after_next_default: 28, reason_code: null },
      peak_memory_gb: { limit: null, reserved: 0, available: 32, available_after_next_default: 28, reason_code: null },
      disk_mb: { limit: null, reserved: 0, available: 1060864, available_after_next_default: 1049600, reason_code: null },
      dind_slots: { limit: null, reserved: 0, available: null, available_after_next_default: null, reason_code: null },
      pressure_reasons: [],
    },
    capacity_queue: {
      queued_workspace_count: 0,
      oldest_workspace_id: null,
      oldest_wait_seconds: null,
      planned_resources: { steady_cpu: 0, steady_memory_gb: 0, peak_cpu: 0, peak_memory_gb: 0, disk_mb: 0, dind_slots: 0 },
      blocked_reason_counts: {},
    },
    concurrency: {
      provision: { limit: 2, in_use: 0, queued: 0, available: 2 },
      execution: { limit: 4, in_use: 4, queued: 0, available: 0 },
    },
    disk: {
      path: "/workspace",
      checked_path: "/workspace",
      total_bytes: 107374182400,
      used_bytes: 21474836480,
      free_bytes: 85899345920,
      percent_free: 80,
      threshold_bytes: 10737418240,
      ok: true,
      status: "ok",
      reason: "healthy",
      detail: null,
    },
    admission: { ok: true, status: "ok", reason: "healthy", detail: null },
  };
}

function workspaceReliability() {
  return {
    generated_at: now,
    window_start: now,
    since_hours: 24,
    status_counts: { running: 2, validating: 1, pushing: 1 },
    failure_reason_counts: {},
    active_count: 4,
    destroying_count: 0,
    completed_count: 0,
    failed_count: 0,
    cancelled_count: 0,
    destroyed_count: 0,
    cleanup_failure_count: 0,
    stuck_count: 0,
    actionable_reason_count: 0,
    unactionable_reason_count: 0,
  };
}
