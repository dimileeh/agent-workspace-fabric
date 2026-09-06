import { readFileSync } from "node:fs";
import { join } from "node:path";

import type { Page, Route } from "@playwright/test";

export function loadConsoleFixture<T = unknown>(name: string): T {
  // Playwright runs from apps/console; also tolerate repo-root cwd.
  const candidates = [
    join(process.cwd(), "../../docs/console/fixtures/v1", name),
    join(process.cwd(), "docs/console/fixtures/v1", name),
    join(process.cwd(), "../docs/console/fixtures/v1", name),
  ];
  for (const candidate of candidates) {
    try {
      return JSON.parse(readFileSync(candidate, "utf8")) as T;
    } catch {
      // try next
    }
  }
  throw new Error(`Console fixture not found: ${name} (cwd=${process.cwd()})`);
}

export async function fulfillJson(
  route: Route,
  body: unknown,
  status = 200,
): Promise<void> {
  await route.fulfill({
    status,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function listEnvelope<T>(items: T[]) {
  return { items, next_cursor: null, has_more: false };
}

const now = "2026-05-02T12:00:00.000Z";

/** Minimal local capabilities matching Core advertisement. */
export function localCapabilities() {
  return loadConsoleFixture("capabilities.local.json");
}

export function hostedCapabilities() {
  return loadConsoleFixture("capabilities.hosted.json");
}

export function localDashboardSummary(overrides?: Record<string, unknown>) {
  const base = loadConsoleFixture<Record<string, unknown>>("dashboard-summary.local.json");
  return {
    ...base,
    ...overrides,
    counts: {
      ...(base.counts as Record<string, unknown>),
      ...((overrides?.counts as Record<string, unknown> | undefined) ?? {}),
    },
  };
}

export function hostedDashboardSummary() {
  return loadConsoleFixture("dashboard-summary.hosted.json");
}

export function hostedCloudRuntime() {
  return loadConsoleFixture("cloud-runtime.hosted.json");
}

/** Saturation fixture aligned with running-kpi regression counts. */
export function resourceSaturationForRunningKpi() {
  return {
    generated_at: now,
    workspace_counts: {
      by_status: {
        running: 2,
        validating: 1,
        pushing: 1,
        monitoring_pr: 0,
        blocked: 1,
      },
      active_total: 5,
      requested: 0,
      provisioning: 0,
      ready: 0,
      running: 2,
      validating: 1,
      pushing: 1,
      monitoring_pr: 0,
      blocked: 1,
      recovering: 0,
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

export function workspaceReliabilityEmpty() {
  return {
    generated_at: now,
    window_start: now,
    since_hours: 24,
    status_counts: {},
    failure_reason_counts: {},
    active_count: 0,
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

export type MockAwfOptions = {
  mode?: "local" | "hosted";
  capabilities?: unknown;
  capabilitiesStatus?: number;
  dashboardSummary?: unknown;
  overviewItems?: unknown[];
  onRequest?: (path: string) => void;
};

/**
 * Shared AWF API mock for Stage 2 console browser tests.
 * Always serves capabilities + dashboard-summary for local mode by default.
 */
export async function mockAwfConsoleApi(page: Page, options: MockAwfOptions = {}) {
  const mode = options.mode ?? "local";
  const caps =
    options.capabilities ??
    (mode === "hosted" ? hostedCapabilities() : localCapabilities());
  const summary =
    options.dashboardSummary ??
    (mode === "hosted"
      ? hostedDashboardSummary()
      : localDashboardSummary({
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
        }));

  await page.route("**/api/awf/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    options.onRequest?.(path);

    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      if (options.capabilitiesStatus && options.capabilitiesStatus !== 200) {
        await fulfillJson(
          route,
          options.capabilities ?? { detail: { error_code: "UNAUTHORIZED", message: "denied" } },
          options.capabilitiesStatus,
        );
        return;
      }
      await fulfillJson(route, caps);
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, summary);
      return;
    }
    if (path === "/api/awf/console/cloud-runtime") {
      await fulfillJson(route, hostedCloudRuntime());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope(options.overviewItems ?? []));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturationForRunningKpi());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliabilityEmpty());
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
