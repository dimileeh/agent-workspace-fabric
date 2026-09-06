import { expect, type Page, test } from "@playwright/test";

// Regression for PR #482 review thread PRRT_kwDOSJAM6s6IQpHH: the Resource /
// Runtime Capacity panel must keep rendering the reliability-summary facts
// (Stuck, Reason Coverage) even when the saturation feed fails or is still
// loading. Those metrics come from a separate feed and must not be hidden by a
// missing saturation snapshot.

const now = "2026-05-02T12:00:00.000Z";

test.beforeEach(async ({ page }) => {
  await mockAwfApi(page);
});

test("capacity panel still shows reliability facts when saturation fails to load", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  const capacityPanel = page.locator("#awf-capacity");
  await expect(capacityPanel).toBeVisible();

  // Saturation feed is down, so the capacity snapshot falls back to its loading
  // line instead of the saturation-driven content.
  await expect(capacityPanel.getByText("Unable to load capacity", { exact: false })).toBeVisible();

  // The reliability facts come from the (successful) summary feed and must
  // remain visible despite the failed saturation feed.
  await expect(capacityPanel.getByText("Stuck", { exact: true })).toBeVisible();
  await expect(capacityPanel.getByText("3 workspaces", { exact: true })).toBeVisible();
  await expect(capacityPanel.getByText("Reason Coverage", { exact: true })).toBeVisible();
  await expect(capacityPanel.getByText("80% (5 tracked)", { exact: true })).toBeVisible();
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
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, {
        schema_version: 1,
        backend_kind: "local",
        generated_at: "2026-09-06T17:00:00Z",
        identity: { backend_id: "awf-core-local", scope: "local" },
        widgets: [
          { id: "fleet_summary", availability: "available", route: "/v1/console/dashboard-summary", semantics: "fleet" },
          { id: "resource_capacity", availability: "available", route: "/v1/metrics/resources/saturation", semantics: "capacity" },
          { id: "cloud_runtime", availability: "unsupported", reason_code: "backend_kind_local", message: "hosted only", semantics: "cloud" },
        ],
        diagnostics: [
          { id: "reliability", availability: "available", route: "/v1/metrics/workspaces/summary", semantics: "reliability" },
          { id: "merge_queue", availability: "available", route: "/v1/merge-queue", semantics: "merge" },
          { id: "failures", availability: "available", route: "/v1/metrics/failures/summary", semantics: "failures" },
        ],
        controls: [
          { id: "remonitor", availability: "available", semantics: "remonitor" },
          { id: "refresh", availability: "available", semantics: "refresh" },
          { id: "revalidate", availability: "available", semantics: "revalidate" },
          { id: "cancel", availability: "available", semantics: "cancel" },
        ],
      });
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      // Saturation feed is unavailable while reliability summary stays healthy.
      await fulfillJson(route, { detail: { message: "saturation unavailable" } }, 503);
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

function workspaceReliability() {
  return {
    generated_at: now,
    window_start: now,
    since_hours: 24,
    status_counts: { running: 3 },
    failure_reason_counts: {},
    active_count: 3,
    destroying_count: 0,
    completed_count: 0,
    failed_count: 0,
    cancelled_count: 0,
    destroyed_count: 0,
    cleanup_failure_count: 0,
    stuck_count: 3,
    actionable_reason_count: 4,
    unactionable_reason_count: 1,
  };
}
