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

function capabilitiesWithUnsupportedRetry() {
  const caps = localCapabilities() as {
    controls: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  return {
    ...caps,
    controls: caps.controls.map((control) =>
      control.id === "retry"
        ? {
            ...control,
            availability: "unsupported",
            reason_code: "policy_disabled",
            message: "Retry is not available on this backend.",
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

test("unsupported retry is disabled with capability reason", async ({ page }) => {
  const overview = {
    ...presentationOverview(),
    status: "failed",
    current_phase: "failed",
    pr_url: null,
    pr_number: null,
    native_runtime_finished_at: null,
    failure_reason: "VALIDATION_FAILED",
    failure_message: "tests failed",
  } as Record<string, unknown>;
  let retryPosted = false;
  await mockAwfConsoleApi(page, {
    capabilities: capabilitiesWithUnsupportedRetry(),
    overviewItems: [overview],
  });
  await page.route("**/api/awf/workspaces/ws_presentation_sample**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/workspaces/ws_presentation_sample/retry") {
      retryPosted = true;
      await fulfillJson(route, { detail: { message: "retry should not be posted" } }, 500);
      return;
    }
    if (path === "/api/awf/workspaces/ws_presentation_sample") {
      await fulfillJson(route, {
        ...overview,
        id: overview.workspace_id,
        version: 1,
      });
      return;
    }
    if (path.endsWith("/runtime")) {
      await fulfillJson(route, { status: "failed" });
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
  const retry = page.getByRole("button", { name: "Retry" });
  await expect(retry).toBeVisible();
  await expect(retry).toBeDisabled();
  await retry.locator("..").hover();
  await expect(page.getByRole("tooltip", { name: /Retry is not available on this backend/ })).toBeVisible();
  expect(retryPosted).toBe(false);
});

test("malformed capabilities disable retry without posting", async ({ page }) => {
  const overview = {
    ...presentationOverview(),
    status: "cancelled",
    current_phase: "cancelled",
    pr_url: null,
    pr_number: null,
    native_runtime_finished_at: null,
  } as Record<string, unknown>;
  let retryPosted = false;
  await mockAwfConsoleApi(page, {
    capabilities: loadConsoleFixture("capabilities.malformed.json"),
    overviewItems: [overview],
  });
  await page.route("**/api/awf/workspaces/ws_presentation_sample**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/workspaces/ws_presentation_sample/retry") {
      retryPosted = true;
      await fulfillJson(route, { detail: { message: "retry should not be posted" } }, 500);
      return;
    }
    if (path === "/api/awf/workspaces/ws_presentation_sample") {
      await fulfillJson(route, {
        ...overview,
        id: overview.workspace_id,
        version: 1,
      });
      return;
    }
    if (path.endsWith("/runtime")) {
      await fulfillJson(route, { status: "cancelled" });
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
  const retry = page.getByRole("button", { name: "Retry" });
  await expect(retry).toBeVisible();
  await expect(retry).toBeDisabled();
  expect(retryPosted).toBe(false);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6f9g8g: inspector
// diagnostic feed outages must retain last-successful snapshots while showing
// the error (gated-off feeds still clear; do not blank on every 5xx blip).
test("workspace detail feed outage keeps last-successful runtime and events", async ({ page }) => {
  let detailOutage = false;
  const workspaceId = "ws_detail_outage";
  const composeProject = "awf-ws-detail-outage-unique";
  const eventMarker = "detail-outage-event-marker";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Detail outage workspace",
    repo_url: "https://github.com/example/detail-outage",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Retain inspector diagnostics across transient feed outages",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };

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
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, { items: [overviewItem], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, { ...overviewItem, id: workspaceId, version: 1 });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      if (detailOutage) {
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "runtime outage" } },
          503,
        );
        return;
      }
      await fulfillJson(route, {
        workspace_id: workspaceId,
        compose_project_name: composeProject,
        stack_state: "running",
        services: [],
        app_endpoints: [],
        logs_available: true,
        control_available: true,
        reason: null,
      });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      if (detailOutage) {
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "events outage" } },
          503,
        );
        return;
      }
      await fulfillJson(route, {
        items: [
          {
            id: "evt_detail_outage",
            workspace_id: workspaceId,
            event_type: eventMarker,
            old_state: null,
            new_state: "running",
            reason_code: null,
            payload: null,
            occurred_at: "2026-09-06T17:00:00Z",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
      return;
    }
    if (
      path === `/api/awf/workspaces/${workspaceId}/operations` ||
      path === `/api/awf/workspaces/${workspaceId}/logs`
    ) {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
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
      await fulfillJson(route, {
        total_failures: 0,
        since_hours: 24,
        taxonomy: [],
        latest_examples: [],
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  await expect(page.getByText(composeProject, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(eventMarker, { exact: true })).toBeVisible();

  detailOutage = true;
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect(page.getByText(/runtime outage|events outage/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByText(composeProject, { exact: true })).toBeVisible();
  await expect(page.getByText(eventMarker, { exact: true })).toBeVisible();
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gAjDt: a later overview
// success must not clear a retained workspace-detail warning, or the last-good
// inspector snapshot looks current while the diagnostic feed is still down.
test("overview success does not clear a retained workspace-detail error", async ({ page }) => {
  let detailOutage = false;
  let holdRuntime = false;
  let releaseHeldRuntime = () => {};
  let heldRuntime = Promise.resolve();
  const workspaceId = "ws_detail_error_isolated";
  const composeProject = "awf-ws-detail-error-isolated-unique";
  const eventMarker = "detail-error-isolated-event";
  let overviewTitle = "Detail error isolated workspace";
  const overviewItem = () => ({
    workspace_id: workspaceId,
    title: overviewTitle,
    repo_url: "https://github.com/example/detail-error-isolated",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Keep inspector diagnostic errors independent of overview polls",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
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
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, { items: [overviewItem()], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, { ...overviewItem(), id: workspaceId, version: 1 });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      if (detailOutage) {
        if (holdRuntime) {
          await heldRuntime;
        }
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "runtime outage" } },
          503,
        );
        return;
      }
      await fulfillJson(route, {
        workspace_id: workspaceId,
        compose_project_name: composeProject,
        stack_state: "running",
        services: [],
        app_endpoints: [],
        logs_available: true,
        control_available: true,
        reason: null,
      });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, {
        items: [
          {
            id: "evt_detail_error_isolated",
            workspace_id: workspaceId,
            event_type: eventMarker,
            old_state: null,
            new_state: "running",
            reason_code: null,
            payload: null,
            occurred_at: "2026-09-06T17:00:00Z",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
      return;
    }
    if (
      path === `/api/awf/workspaces/${workspaceId}/operations` ||
      path === `/api/awf/workspaces/${workspaceId}/logs`
    ) {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
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
      await fulfillJson(route, {
        total_failures: 0,
        since_hours: 24,
        taxonomy: [],
        latest_examples: [],
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  try {
    await page.goto("/");
    await waitForConsoleReady(page);
    await page.getByTestId(`workspace-card-${workspaceId}`).click();
    await expect(page.getByText(composeProject, { exact: true })).toBeVisible({ timeout: 10_000 });

    detailOutage = true;
    await page.getByRole("button", { name: /refresh/i }).click({ force: true });
    const inspector = page
      .getByRole("button", { name: "Close inspector" })
      .locator("xpath=ancestor::div[contains(@class,'translate-x-0')]");
    await expect(page.getByText("runtime outage").first()).toBeVisible({ timeout: 10_000 });
    await expect(inspector.getByText("runtime outage")).toBeVisible();
    await expect(page.getByText(composeProject, { exact: true })).toBeVisible();

    // Hold the still-failing runtime feed so a detail poll cannot re-set the
    // banner, then let an independent overview success apply.
    heldRuntime = new Promise((resolve) => {
      releaseHeldRuntime = resolve;
    });
    holdRuntime = true;
    overviewTitle = "Overview succeeded during detail outage";
    await page.getByRole("button", { name: /refresh/i }).click({ force: true });
    await expect(page.getByTestId(`workspace-title-${workspaceId}`)).toHaveText(
      "Overview succeeded during detail outage",
      { timeout: 10_000 },
    );
    await expect(page.getByText("runtime outage").first()).toBeVisible();
    await expect(inspector.getByText("runtime outage")).toBeVisible();
    await expect(page.getByText(composeProject, { exact: true })).toBeVisible();
  } finally {
    releaseHeldRuntime();
  }
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6f9zxG: overlapping
// selected-workspace detail polls must stamp a request generation so an older
// in-flight 200 cannot restore runtime/events after a newer feed-level 403 clear
// (epoch/gated-detail refs alone do not advance on that path).
test("in-flight workspace detail success after feed-level 403 does not restore cleared inspector", async ({
  page,
}) => {
  let detailMode: "ok" | "delay_ok" | "denied" = "ok";
  let delayedDetailStarts = 0;
  const workspaceId = "ws_detail_auth_clear_race";
  const composeProject = "awf-ws-detail-auth-clear-race-unique";
  const eventMarker = "detail-auth-clear-race-event";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Detail auth clear race workspace",
    repo_url: "https://github.com/example/detail-auth-clear-race",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Discard superseded workspace detail responses after feed-level 403",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };

  const fulfillDetailFeed = async (
    route: Parameters<Parameters<Page["route"]>[1]>[0],
    okBody: unknown,
  ) => {
    if (detailMode === "denied") {
      await fulfillJson(
        route,
        { detail: { error_code: "FORBIDDEN", message: "workspace detail permission revoked" } },
        403,
      );
      return;
    }
    if (detailMode === "delay_ok") {
      delayedDetailStarts += 1;
      // Longer than the console poll interval so the next poll overlaps this one.
      await new Promise((resolve) => setTimeout(resolve, 6000));
      await fulfillJson(route, okBody);
      return;
    }
    await fulfillJson(route, okBody);
  };

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
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, { items: [overviewItem], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillDetailFeed(route, { ...overviewItem, id: workspaceId, version: 1 });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillDetailFeed(route, {
        workspace_id: workspaceId,
        compose_project_name: composeProject,
        stack_state: "running",
        services: [],
        app_endpoints: [],
        logs_available: true,
        control_available: true,
        reason: null,
      });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillDetailFeed(route, {
        items: [
          {
            id: "evt_detail_auth_clear_race",
            workspace_id: workspaceId,
            event_type: eventMarker,
            old_state: null,
            new_state: "running",
            reason_code: null,
            payload: null,
            occurred_at: "2026-09-06T17:00:00Z",
          },
        ],
        next_cursor: null,
        has_more: false,
      });
      return;
    }
    if (
      path === `/api/awf/workspaces/${workspaceId}/operations` ||
      path === `/api/awf/workspaces/${workspaceId}/logs`
    ) {
      await fulfillDetailFeed(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
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
      await fulfillJson(route, {
        total_failures: 0,
        since_hours: 24,
        taxonomy: [],
        latest_examples: [],
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  await expect(page.getByText(composeProject, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(eventMarker, { exact: true })).toBeVisible();

  // Start a slow authorized detail poll, then revoke via explicit Refresh so a
  // newer loadWorkspace supersedes it. Periodic ticks no longer overlap a slow
  // detail request; this must not weaken the old-200-after-new-403 discard.
  detailMode = "delay_ok";
  await expect
    .poll(() => delayedDetailStarts, { timeout: 10_000 })
    .toBeGreaterThan(0);
  detailMode = "denied";
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect(page.getByText(/workspace detail permission revoked|forbidden|denied/i).first()).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByText(composeProject, { exact: true })).toHaveCount(0);
  await expect(page.getByText(eventMarker, { exact: true })).toHaveCount(0);
  // Wait past the delayed pre-clear success; it must not restore revoked inspector rows.
  await page.waitForTimeout(7000);
  await expect(page.getByText(composeProject, { exact: true })).toHaveCount(0);
  await expect(page.getByText(eventMarker, { exact: true })).toHaveCount(0);
  await expect(page.getByText("Runtime snapshot unavailable.")).toBeVisible();
});

// Requests slower than pollMs must still apply. A wall-clock interval that
// calls loadWorkspace every pollMs advances the detail generation, so four
// overlapping successes produce zero setDetail writes until overlap ends.
test("slow workspace detail poll applies when the request exceeds the poll interval", async ({
  page,
}) => {
  let detailMode: "ok" | "slow" = "ok";
  let slowGetInFlight = 0;
  let maxSlowGetInFlight = 0;
  const workspaceId = "ws_detail_slow_poll";
  const initialProject = "awf-ws-detail-slow-poll-initial";
  const updatedProject = "awf-ws-detail-slow-poll-updated";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Slow detail poll workspace",
    repo_url: "https://github.com/example/detail-slow-poll",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Apply selected workspace detail when the request exceeds pollMs",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };

  const fulfillSlowAware = async (
    route: Parameters<Parameters<Page["route"]>[1]>[0],
    okBody: unknown,
    trackOverlap: boolean,
  ) => {
    if (detailMode === "slow") {
      if (trackOverlap) {
        slowGetInFlight += 1;
        maxSlowGetInFlight = Math.max(maxSlowGetInFlight, slowGetInFlight);
      }
      // Longer than the console poll interval. Without serialization the next
      // tick supersedes this request and its success never reaches setDetail.
      await new Promise((resolve) => setTimeout(resolve, 6000));
      if (trackOverlap) {
        slowGetInFlight -= 1;
      }
    }
    await fulfillJson(route, okBody);
  };

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
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, { items: [overviewItem], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillSlowAware(
        route,
        {
          ...overviewItem,
          id: workspaceId,
          version: detailMode === "slow" ? 2 : 1,
        },
        true,
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillSlowAware(
        route,
        {
          workspace_id: workspaceId,
          compose_project_name: detailMode === "slow" ? updatedProject : initialProject,
          stack_state: "running",
          services: [],
          app_endpoints: [],
          logs_available: true,
          control_available: true,
          reason: null,
        },
        false,
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillSlowAware(route, { items: [], next_cursor: null, has_more: false }, false);
      return;
    }
    if (
      path === `/api/awf/workspaces/${workspaceId}/operations` ||
      path === `/api/awf/workspaces/${workspaceId}/logs`
    ) {
      await fulfillSlowAware(route, { items: [], next_cursor: null, has_more: false }, false);
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
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
      await fulfillJson(route, {
        total_failures: 0,
        since_hours: 24,
        taxonomy: [],
        latest_examples: [],
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  await expect(page.getByText(initialProject, { exact: true })).toBeVisible({ timeout: 10_000 });

  detailMode = "slow";
  await expect(page.getByText(updatedProject, { exact: true })).toBeVisible({ timeout: 20_000 });
  expect(maxSlowGetInFlight).toBe(1);
  await expect(page.getByText(initialProject, { exact: true })).toHaveCount(0);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gAqk6: a persistent
// capabilities 404 bumps gated-detail generation, but that must not discard an
// overlapping basic /workspaces/{id} load. Optional diagnostic feeds stay gated.
test("capabilities 404 does not discard an overlapping basic workspace detail load", async ({
  page,
}) => {
  let holdDetail = false;
  let detailStarts = 0;
  let capability404sAfterHold = 0;
  const detailGate: Array<() => void> = [];
  const workspaceId = "ws_cap_404_detail";
  const detailBranch = "awf/cap-404-detail-kept";
  const staleRuntime = "awf-ws-cap-404-stale-runtime";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Capabilities 404 detail workspace",
    repo_url: "https://github.com/example/cap-404-detail",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Keep basic workspace detail when capabilities stay 404",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };

  const releaseHeldDetail = () => {
    const pending = detailGate.splice(0, detailGate.length);
    for (const release of pending) {
      release();
    }
  };

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      const generation = holdDetail ? ++capability404sAfterHold : 0;
      await fulfillJson(
        route,
        {
          detail: {
            error_code: "NOT_FOUND",
            message:
              generation > 0
                ? `capabilities negotiation unavailable overlap-${generation}`
                : "capabilities negotiation unavailable",
          },
        },
        404,
      );
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, { items: [overviewItem], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      detailStarts += 1;
      if (holdDetail) {
        await new Promise<void>((resolve) => {
          detailGate.push(resolve);
        });
      }
      await fulfillJson(route, {
        ...overviewItem,
        id: workspaceId,
        version: 2,
        branch_name: detailBranch,
        task_title: overviewItem.title,
      });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      if (holdDetail) {
        await new Promise<void>((resolve) => {
          detailGate.push(resolve);
        });
      }
      await fulfillJson(route, {
        workspace_id: workspaceId,
        compose_project_name: staleRuntime,
        stack_state: "running",
        services: [],
        app_endpoints: [],
        logs_available: true,
        control_available: true,
        reason: null,
      });
      return;
    }
    if (
      path === `/api/awf/workspaces/${workspaceId}/events` ||
      path === `/api/awf/workspaces/${workspaceId}/operations` ||
      path === `/api/awf/workspaces/${workspaceId}/logs`
    ) {
      if (holdDetail) {
        await new Promise<void>((resolve) => {
          detailGate.push(resolve);
        });
      }
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
      await fulfillJson(route, {
        total_failures: 0,
        since_hours: 24,
        taxonomy: [],
        latest_examples: [],
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByText(/capabilities negotiation unavailable/i).first()).toBeVisible({
    timeout: 10_000,
  });

  // Hold every basic GET so the unique branch can only appear from a load that
  // overlaps a capabilities 404, not from a fast selection fetch.
  holdDetail = true;
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  await expect.poll(() => detailStarts, { timeout: 10_000 }).toBeGreaterThan(0);
  await expect(page.getByText("no branch / main", { exact: true })).toBeVisible({ timeout: 10_000 });

  const startsBeforeRefresh = detailStarts;
  const capsBeforeRefresh = capability404sAfterHold;
  // Refresh starts loadWorkspace, then a capabilities 404 that bumps gated-detail
  // generation. Release the basic GET only after that 404 is on screen so the
  // response cannot win the race against the generation bump.
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect.poll(() => detailStarts, { timeout: 10_000 }).toBeGreaterThan(startsBeforeRefresh);
  await expect
    .poll(async () => {
      const text = await page
        .getByText(/capabilities negotiation unavailable overlap-\d+/)
        .textContent();
      const match = text?.match(/overlap-(\d+)/);
      return match ? Number(match[1]) : 0;
    })
    .toBeGreaterThan(capsBeforeRefresh);
  releaseHeldDetail();

  await expect(page.getByText(`${detailBranch} / main`, { exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByText(staleRuntime, { exact: true })).toHaveCount(0);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gBVzf: withdrawing an
// unrelated fleet feed must not bump gated-detail generation. An in-flight
// detail poll whose runtime/events request then fails must keep that failure
// and the last-good diagnostic snapshot, not clear the detail error because
// the basic workspace GET succeeded.
test("unrelated fleet_summary withdrawal preserves in-flight detail feed errors", async ({
  page,
}) => {
  let holdDetail = false;
  let withdrawFleetSummary = false;
  let detailStarts = 0;
  const detailGate: Array<() => void> = [];
  const workspaceId = "ws_fleet_withdraw_detail_error";
  const composeProject = "awf-ws-fleet-withdraw-detail-error";
  const runtimeOutage = "runtime outage after unrelated fleet withdrawal";
  const baseCaps = localCapabilities() as {
    widgets: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const withdrawnCaps = {
    ...baseCaps,
    widgets: baseCaps.widgets.map((item) =>
      item.id === "fleet_summary"
        ? {
            id: "fleet_summary",
            availability: "unsupported",
            reason_code: "backend_kind_local",
            message: "Fleet summary withdrawn",
            semantics: "Authoritative fleet counters independent of capacity probes.",
          }
        : item,
    ),
  };
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Fleet withdrawal detail error workspace",
    repo_url: "https://github.com/example/fleet-withdraw-detail-error",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Preserve detail errors when only fleet_summary is withdrawn",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };

  const releaseHeldDetail = () => {
    const pending = detailGate.splice(0, detailGate.length);
    for (const release of pending) {
      release();
    }
  };

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, withdrawFleetSummary ? withdrawnCaps : baseCaps);
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, { items: [overviewItem], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      detailStarts += 1;
      if (holdDetail) {
        await new Promise<void>((resolve) => {
          detailGate.push(resolve);
        });
      }
      await fulfillJson(route, {
        ...overviewItem,
        id: workspaceId,
        version: 2,
        branch_name: "awf/fleet-withdraw-detail-kept",
      });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      if (holdDetail) {
        await new Promise<void>((resolve) => {
          detailGate.push(resolve);
        });
      }
      if (withdrawFleetSummary) {
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: runtimeOutage } },
          503,
        );
        return;
      }
      await fulfillJson(route, {
        workspace_id: workspaceId,
        compose_project_name: composeProject,
        stack_state: "running",
        services: [],
        app_endpoints: [],
        logs_available: true,
        control_available: true,
        reason: null,
      });
      return;
    }
    if (
      path === `/api/awf/workspaces/${workspaceId}/events` ||
      path === `/api/awf/workspaces/${workspaceId}/operations` ||
      path === `/api/awf/workspaces/${workspaceId}/logs`
    ) {
      if (holdDetail) {
        await new Promise<void>((resolve) => {
          detailGate.push(resolve);
        });
      }
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
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
      await fulfillJson(route, {
        total_failures: 0,
        since_hours: 24,
        taxonomy: [],
        latest_examples: [],
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("8", { timeout: 10_000 });
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  await expect(page.getByText(composeProject, { exact: true })).toBeVisible({ timeout: 10_000 });

  holdDetail = true;
  withdrawFleetSummary = true;
  const startsBeforeRefresh = detailStarts;
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect.poll(() => detailStarts, { timeout: 10_000 }).toBeGreaterThan(startsBeforeRefresh);
  await expect(kpi(page, "Active")).toHaveCount(0, { timeout: 10_000 });
  releaseHeldDetail();

  await expect(page.getByText(runtimeOutage, { exact: true }).first()).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(composeProject, { exact: true })).toBeVisible();
  await expect(kpi(page, "Active")).toHaveCount(0);
});

// Requests slower than pollMs must still negotiate. A wall-clock interval that
// calls loadCapabilities every pollMs advances capabilityRequestGenerationRef,
// so every slower success is discarded and optional feeds stay absent.
test("slow capability poll negotiates when the request exceeds the poll interval", async ({
  page,
}) => {
  let capabilityMode: "withdrawn" | "slow_available" = "withdrawn";
  let slowCapabilityInFlight = 0;
  let maxSlowCapabilityInFlight = 0;
  const baseCaps = localCapabilities() as {
    widgets: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const withdrawnCaps = {
    ...baseCaps,
    widgets: baseCaps.widgets.map((item) =>
      item.id === "fleet_summary"
        ? {
            id: "fleet_summary",
            availability: "unsupported",
            reason_code: "backend_kind_local",
            message: "Fleet summary withheld until the slow negotiation applies",
            semantics: "Authoritative fleet counters independent of capacity probes.",
          }
        : item,
    ),
  };
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
      if (capabilityMode === "slow_available") {
        slowCapabilityInFlight += 1;
        maxSlowCapabilityInFlight = Math.max(maxSlowCapabilityInFlight, slowCapabilityInFlight);
        // Longer than the console poll interval. Without serialization the next
        // tick supersedes this request and fleet_summary never applies.
        await new Promise((resolve) => setTimeout(resolve, 6000));
        slowCapabilityInFlight -= 1;
        await fulfillJson(route, baseCaps);
        return;
      }
      await fulfillJson(route, withdrawnCaps);
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
      await fulfillJson(route, {
        total_failures: 0,
        since_hours: 24,
        taxonomy: [],
        latest_examples: [],
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(kpi(page, "Active")).toHaveCount(0);

  capabilityMode = "slow_available";
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("9", { timeout: 25_000 });
  expect(maxSlowCapabilityInFlight).toBe(1);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6f-kK9: overlapping
// selected-stream log tails must stamp a per-stream request generation so an
// older in-flight 200 cannot restore revoked contents after a newer feed-level
// 403 (epoch/gated-detail refs alone do not advance on that path).
test("in-flight log tail success after feed-level 403 does not restore revoked stream contents", async ({
  page,
}) => {
  let tailMode: "delay_ok" | "denied" = "delay_ok";
  let delayedTailStarts = 0;
  const workspaceId = "ws_log_tail_auth_clear_race";
  const revokedMarker = "revoked-log-tail-payload-must-not-return";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Log tail auth clear race workspace",
    repo_url: "https://github.com/example/log-tail-auth-clear-race",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Discard superseded log-tail responses after feed-level 403",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };
  const stream = {
    stream_id: "agent.stdout",
    source: "agent",
    name: "agent.stdout",
    kind: "stdout",
    path: "/tmp/agent.stdout",
    byte_count: 48,
    line_count: 1,
    opened_at: "2026-09-06T17:00:00Z",
    closed_at: null,
  };

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
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, { items: [overviewItem], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, { ...overviewItem, id: workspaceId, version: 1 });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, {
        workspace_id: workspaceId,
        compose_project_name: "awf-ws-log-tail-auth-clear-race",
        stack_state: "running",
        services: [],
        app_endpoints: [],
        logs_available: true,
        control_available: true,
        reason: null,
      });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, { items: [stream], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/agent.stdout`) {
      if (tailMode === "denied") {
        await fulfillJson(
          route,
          { detail: { error_code: "FORBIDDEN", message: "log tail permission revoked" } },
          403,
        );
        return;
      }
      delayedTailStarts += 1;
      // Longer than a Tail click so the newer denial overlaps this success.
      await new Promise((resolve) => setTimeout(resolve, 6000));
      await fulfillJson(route, {
        stream_id: "agent.stdout",
        offset: 0,
        next_offset: revokedMarker.length,
        eof: true,
        data: revokedMarker,
      });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
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
      await fulfillJson(route, {
        total_failures: 0,
        since_hours: 24,
        taxonomy: [],
        latest_examples: [],
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  const tailButton = page.getByRole("button", { name: "Tail", exact: true });
  await expect(tailButton).toBeEnabled({ timeout: 10_000 });
  await expect.poll(() => delayedTailStarts, { timeout: 10_000 }).toBeGreaterThan(0);

  tailMode = "denied";
  await tailButton.click();
  await expect(page.getByText(/log tail permission revoked/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByText(revokedMarker, { exact: false })).toHaveCount(0);
  // Wait past the delayed pre-denial success; it must not restore revoked contents.
  await page.waitForTimeout(7000);
  await expect(page.getByText(revokedMarker, { exact: false })).toHaveCount(0);
  await expect(page.getByText(/log tail permission revoked/i).first()).toBeVisible();
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

test("dashboard-summary first-load failure surfaces error without cached KPIs", async ({ page }) => {
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
      await fulfillJson(
        route,
        { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "summary feed unavailable" } },
        503,
      );
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
  const summaryError = page.getByTestId("dashboard-summary-error");
  await expect(summaryError).toBeVisible();
  await expect(summaryError).toContainText(/summary feed unavailable|UPSTREAM_UNAVAILABLE|503/i);
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("—");
  await expect(page.getByText(/last snapshot|may be stale/i)).toHaveCount(0);
});

test("dashboard-summary feed-level 403 clears last-good KPIs while capabilities stay reachable", async ({
  page,
}) => {
  let summaryDenied = false;
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
      if (summaryDenied) {
        await fulfillJson(
          route,
          { detail: { error_code: "FORBIDDEN", message: "tenant summary permission revoked" } },
          403,
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

  summaryDenied = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/tenant summary permission revoked|forbidden|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  // Capabilities still advertise fleet_summary → KPI shells remain, but counters
  // must not keep the revoked snapshot (— / not 9, and not stale-cached).
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("—");
  await expect(kpi(page, "Active")).not.toHaveAttribute("data-awf-stale", "true");
  await expect(page.getByText(/last snapshot|may be stale/i)).toHaveCount(0);
});

async function mockMergeQueueAuthClearRoutes(
  page: Page,
  options: {
    denied: () => boolean;
    status: 401 | 403;
    errorCode: string;
    message: string;
    queueItem: Record<string, unknown>;
  },
) {
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
      await fulfillJson(route, localDashboardSummary());
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
      if (options.denied()) {
        await fulfillJson(
          route,
          { detail: { error_code: options.errorCode, message: options.message } },
          options.status,
        );
        return;
      }
      await fulfillJson(route, { items: [options.queueItem], next_cursor: null, has_more: false });
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });
}

function mergeQueueAuthClearItem(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    candidate_id: "cand-auth-clear",
    candidate_status: "open",
    close_reason: null,
    attempt_id: "attempt-auth-clear",
    task_id: "task-auth-clear",
    workspace_id: "ws_merge_auth_clear",
    title: "Revoked merge queue candidate",
    repo_url: "https://github.com/example/awf",
    base_branch: "main",
    branch_name: "codex/ws_merge_auth_clear",
    pr_url: "https://github.com/example/awf/pull/933",
    status: "monitoring_pr",
    auto_merge: true,
    task_class: "console",
    owned_paths: ["apps/console/**"],
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:05:00Z",
    merged_at: null,
    last_event: null,
    merge_blocker_reason: "workspace_not_terminal",
    required_next_action: "wait_for_workspace_terminal_state",
    required_validation_tier: 2,
    latest_satisfied_validation_tier: 1,
    validation_freshness_status: "fresh",
    validation_reason_code: "VALIDATION_SUCCEEDED",
    readiness: {
      ready: false,
      manual_merge_required: false,
      waiting_for_monitor: false,
      failed_or_cancelled: false,
      completed: false,
      not_canonical: false,
      stale: false,
      stale_reason: null,
    },
    canonical: true,
    queue_blockers: [],
    latest_validation: null,
    stale_reasons: [],
    policy_findings: [],
    ...overrides,
  };
}

test("merge-queue feed-level 403 clears last-good rows while capabilities stay reachable", async ({
  page,
}) => {
  let mergeDenied = false;
  const queueItem = mergeQueueAuthClearItem({});
  await mockMergeQueueAuthClearRoutes(page, {
    denied: () => mergeDenied,
    status: 403,
    errorCode: "FORBIDDEN",
    message: "merge queue permission revoked",
    queueItem,
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  const mergePanel = page.locator("#awf-merge-queue");
  await expect(mergePanel.getByText("Revoked merge queue candidate")).toBeVisible();
  await expect(mergePanel.getByText("ws_merge_auth_clear")).toBeVisible();

  mergeDenied = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/merge queue permission revoked|forbidden|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  // Capabilities still advertise merge_queue → panel remains, but rows must not
  // keep the revoked snapshot (no stale "Showing last merge queue snapshot").
  await expect(mergePanel.getByText("Revoked merge queue candidate")).toHaveCount(0);
  await expect(mergePanel.getByText("ws_merge_auth_clear")).toHaveCount(0);
  await expect(mergePanel.getByText(/Showing last merge queue snapshot/i)).toHaveCount(0);
  await expect(mergePanel.getByText(/Unable to load merge queue/i)).toBeVisible();
});

test("merge-queue feed-level 401 clears last-good rows while capabilities stay reachable", async ({
  page,
}) => {
  let mergeDenied = false;
  const queueItem = mergeQueueAuthClearItem({
    candidate_id: "cand-auth-clear-401",
    attempt_id: "attempt-auth-clear-401",
    task_id: "task-auth-clear-401",
    workspace_id: "ws_merge_auth_clear_401",
    title: "Unauthorized merge queue candidate",
    branch_name: "codex/ws_merge_auth_clear_401",
  });
  await mockMergeQueueAuthClearRoutes(page, {
    denied: () => mergeDenied,
    status: 401,
    errorCode: "UNAUTHORIZED",
    message: "merge queue token rejected",
    queueItem,
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  const mergePanel = page.locator("#awf-merge-queue");
  await expect(mergePanel.getByText("Unauthorized merge queue candidate")).toBeVisible();
  await expect(mergePanel.getByText("ws_merge_auth_clear_401")).toBeVisible();

  mergeDenied = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/merge queue token rejected|unauthorized|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(mergePanel.getByText("Unauthorized merge queue candidate")).toHaveCount(0);
  await expect(mergePanel.getByText("ws_merge_auth_clear_401")).toHaveCount(0);
  await expect(mergePanel.getByText(/Showing last merge queue snapshot/i)).toHaveCount(0);
  await expect(mergePanel.getByText(/Unable to load merge queue/i)).toBeVisible();
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6f78oZ: an in-flight
// merge-queue 200 started before feed-level 401/403 must not restore rows after
// the clear (request generation, same contract as dashboard-summary / cloud-runtime).
test("in-flight merge-queue success after feed-level 403 does not restore cleared rows", async ({
  page,
}) => {
  let mergeDenied = false;
  let delayAuthorizedQueue = false;
  const queueItem = mergeQueueAuthClearItem({
    candidate_id: "cand-auth-clear-race",
    attempt_id: "attempt-auth-clear-race",
    task_id: "task-auth-clear-race",
    workspace_id: "ws_merge_auth_clear_race",
    title: "Race merge queue candidate",
    branch_name: "codex/ws_merge_auth_clear_race",
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
      await fulfillJson(route, localDashboardSummary());
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
      if (mergeDenied) {
        await fulfillJson(
          route,
          { detail: { error_code: "FORBIDDEN", message: "merge queue permission revoked" } },
          403,
        );
        return;
      }
      if (delayAuthorizedQueue) {
        await new Promise((resolve) => setTimeout(resolve, 750));
      }
      await fulfillJson(route, { items: [queueItem], next_cursor: null, has_more: false });
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
  const mergePanel = page.locator("#awf-merge-queue");
  await expect(mergePanel.getByText("Race merge queue candidate")).toBeVisible();
  await expect(mergePanel.getByText("ws_merge_auth_clear_race")).toBeVisible();

  // Start an authorized refresh whose merge-queue response is intentionally slow,
  // then revoke the feed so the clear races the in-flight success apply.
  delayAuthorizedQueue = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  mergeDenied = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/merge queue permission revoked|forbidden|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(mergePanel.getByText("Race merge queue candidate")).toHaveCount(0);
  await expect(mergePanel.getByText("ws_merge_auth_clear_race")).toHaveCount(0);
  // Wait past the delayed pre-clear success; it must not restore revoked rows.
  await page.waitForTimeout(1000);
  await expect(mergePanel.getByText("Race merge queue candidate")).toHaveCount(0);
  await expect(mergePanel.getByText("ws_merge_auth_clear_race")).toHaveCount(0);
  await expect(mergePanel.getByText(/Showing last merge queue snapshot/i)).toHaveCount(0);
  await expect(mergePanel.getByText(/Unable to load merge queue/i)).toBeVisible();
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6f8F2B: feed-level
// 401/403 on failures / resource saturation / reliability must drop last-good
// snapshots (same contract as dashboard-summary / merge-queue), with request
// generation so an in-flight 200 cannot restore revoked data.
test("failures feed-level 403 clears last-good examples while capabilities stay reachable", async ({
  page,
}) => {
  let failuresDenied = false;
  let delayAuthorizedFailures = false;
  const failureExample = {
    workspace_id: "ws_fail_auth_clear",
    title: "Revoked failure example",
    repo_url: "https://github.com/example/revoked-fail",
    agent: "codex",
    failure_reason: "VALIDATION_FAILED",
    message: "revoked failure message",
    timestamp: "2026-09-06T17:00:00Z",
  };

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
      await fulfillJson(route, localDashboardSummary());
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
      if (failuresDenied) {
        await fulfillJson(
          route,
          { detail: { error_code: "FORBIDDEN", message: "failures permission revoked" } },
          403,
        );
        return;
      }
      if (delayAuthorizedFailures) {
        await new Promise((resolve) => setTimeout(resolve, 750));
      }
      await fulfillJson(route, {
        total_failures: 1,
        window_hours: 24,
        taxonomy: [{ reason: "VALIDATION_FAILED", count: 1 }],
        latest_examples: [failureExample],
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  const failuresPanel = page.locator("#awf-failures");
  await expect(failuresPanel.getByText("Revoked failure example")).toBeVisible();
  await expect(failuresPanel.getByText("https://github.com/example/revoked-fail")).toBeVisible();

  delayAuthorizedFailures = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  failuresDenied = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/failures permission revoked|forbidden|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(failuresPanel.getByText("Revoked failure example")).toHaveCount(0);
  await expect(failuresPanel.getByText(/Showing last snapshot/i)).toHaveCount(0);
  await expect(failuresPanel.getByText(/Unable to load failure analysis/i)).toBeVisible();
  await page.waitForTimeout(1000);
  await expect(failuresPanel.getByText("Revoked failure example")).toHaveCount(0);
  await expect(failuresPanel.getByText(/Unable to load failure analysis/i)).toBeVisible();
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6f_3Jn: advertised
// failures 404/503 are refresh outages, not capability withdrawal. Keep the
// last snapshot and show the error; do not swap in the unavailable placeholder.
test("failures advertised-feed 404 and 503 retain last-successful snapshot", async ({ page }) => {
  let failuresOutage: 404 | 503 | null = null;
  const failureExample = {
    workspace_id: "ws_fail_outage_keep",
    title: "Retained failure example",
    repo_url: "https://github.com/example/fail-outage",
    agent: "codex",
    failure_reason: "VALIDATION_FAILED",
    failure_message: "retained failure message",
    timestamp: "2026-09-06T17:00:00Z",
    pr_url: null,
  };

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
      await fulfillJson(route, localDashboardSummary());
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
      if (failuresOutage !== null) {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: failuresOutage === 404 ? "NOT_FOUND" : "UPSTREAM_UNAVAILABLE",
              message: failuresOutage === 404 ? "failures feed missing" : "failures feed outage",
            },
          },
          failuresOutage,
        );
        return;
      }
      await fulfillJson(route, {
        total_failures: 1,
        window_hours: 24,
        taxonomy: [{ reason: "VALIDATION_FAILED", count: 1 }],
        latest_examples: [failureExample],
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  const failuresPanel = page.locator("#awf-failures");
  await expect(failuresPanel.getByText("Retained failure example")).toBeVisible();
  await expect(failuresPanel.getByText("Failure analysis is currently unavailable.")).toHaveCount(0);

  failuresOutage = 503;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(failuresPanel.getByText(/Showing last snapshot\. Refresh failed: failures feed outage/i)).toBeVisible({
    timeout: 10_000,
  });
  await expect(failuresPanel.getByText("Retained failure example")).toBeVisible();
  await expect(failuresPanel.getByText("Failure analysis is currently unavailable.")).toHaveCount(0);
  await expect(failuresPanel.locator("[data-awf-stale='true']")).toBeVisible();

  failuresOutage = 404;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(failuresPanel.getByText(/Showing last snapshot\. Refresh failed: failures feed missing/i)).toBeVisible({
    timeout: 10_000,
  });
  await expect(failuresPanel.getByText("Retained failure example")).toBeVisible();
  await expect(failuresPanel.getByText("Failure analysis is currently unavailable.")).toHaveCount(0);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6f7qt6: feed-level
// 401/403 on cloud-runtime must drop last-good tenant snapshot (same contract
// as dashboard-summary / merge-queue), not keep stale queue/quota facts.
test("cloud-runtime feed-level 403 clears last-good snapshot while capabilities stay reachable", async ({
  page,
}) => {
  let cloudRuntimeDenied = false;
  const runtime = loadConsoleFixture<Record<string, unknown>>("cloud-runtime.hosted.json");

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, hostedCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, loadConsoleFixture("dashboard-summary.hosted.json"));
      return;
    }
    if (path === "/api/awf/console/cloud-runtime") {
      if (cloudRuntimeDenied) {
        await fulfillJson(
          route,
          { detail: { error_code: "FORBIDDEN", message: "cloud runtime permission revoked" } },
          403,
        );
        return;
      }
      await fulfillJson(route, runtime);
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
  const cloudPanel = page.locator("#awf-capacity");
  await expect(page.getByRole("heading", { name: "Cloud Runtime" })).toBeVisible();
  await expect(cloudPanel.getByText("within_quota")).toBeVisible();
  await expect(cloudPanel.getByText("12 in use / 50 limit / 38 available")).toBeVisible();

  cloudRuntimeDenied = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/cloud runtime permission revoked|forbidden|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  // Capabilities still advertise cloud_runtime → panel remains, but facts must
  // not keep the revoked snapshot (no stale "Showing last cloud runtime snapshot").
  await expect(cloudPanel.getByText("within_quota")).toHaveCount(0);
  await expect(cloudPanel.getByText("12 in use / 50 limit / 38 available")).toHaveCount(0);
  await expect(cloudPanel.getByText(/Showing last cloud runtime snapshot/i)).toHaveCount(0);
  await expect(cloudPanel.getByText(/Unable to load cloud runtime/i)).toBeVisible();
});

test("dashboard-summary cached outage shows error and last_success_at", async ({ page }) => {
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
  const lastSuccessAt = String(summary.last_success_at);

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
  const summaryError = page.getByTestId("dashboard-summary-error");
  await expect(summaryError).toBeVisible({ timeout: 10_000 });
  await expect(summaryError).toContainText(/summary outage/i);
  await expect(summaryError).toContainText(lastSuccessAt);
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
  await expect(page.getByText("Workflow finished", { exact: true })).toBeVisible();
  await expect(page.getByText("not recorded", { exact: true })).toBeVisible();
  await expect(page.getByText("Task key", { exact: true })).toBeVisible();
  await expect(page.getByText("Last activity", { exact: true })).toBeVisible();
});

test("native runtime finished shows not recorded when history is absent", async ({ page }) => {
  const overview = {
    ...presentationOverview(),
    native_runtime_finished_at: null,
    workflow_finished_at: "2026-09-06T17:10:00Z",
  };
  await mockAwfConsoleApi(page, { overviewItems: [overview] });
  await page.route("**/api/awf/workspaces/ws_presentation_sample**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/workspaces/ws_presentation_sample") {
      await fulfillJson(route, {
        ...overview,
        id: "ws_presentation_sample",
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
  await page.getByTestId("workspace-card-ws_presentation_sample").click();
  const nativeFact = page.getByText("Native runtime finished", { exact: true }).locator("..");
  await expect(nativeFact).toContainText("not recorded");
  const workflowFact = page.getByText("Workflow finished", { exact: true }).locator("..");
  await expect(workflowFact).not.toContainText("not recorded");
});

test("task details modal shows duration when overview duration_seconds is recorded", async ({
  page,
}) => {
  const overview = {
    ...presentationOverview(),
    duration_seconds: 125,
  };
  await mockAwfConsoleApi(page, { overviewItems: [overview] });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page
    .getByTestId("workspace-card-ws_presentation_sample")
    .getByRole("button", { name: "Details", exact: true })
    .click();

  const dialog = page.getByRole("dialog", { name: /Task details/i });
  await expect(dialog).toBeVisible();
  const durationFact = dialog.getByText("Duration", { exact: true }).locator("..");
  await expect(durationFact).toContainText("2m 5s");
});

test("workflow finished falls back to finished_at when workflow_finished_at omitted", async ({
  page,
}) => {
  const overview = {
    ...presentationOverview(),
    workflow_finished_at: null,
    finished_at: "2026-09-06T17:10:00Z",
  };
  await mockAwfConsoleApi(page, { overviewItems: [overview] });
  await page.route("**/api/awf/workspaces/ws_presentation_sample**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/workspaces/ws_presentation_sample") {
      await fulfillJson(route, {
        ...overview,
        id: "ws_presentation_sample",
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
  await page.getByTestId("workspace-card-ws_presentation_sample").click();
  const workflowFact = page.getByText("Workflow finished", { exact: true }).locator("..");
  await expect(workflowFact).not.toContainText("not recorded");
  // formatDateTime omits the year (e.g. "Sep 06, 05:10:00 PM"); assert that shape.
  await expect(workflowFact).toContainText(/[A-Za-z]{3}\s+\d{2},/);
  // finished_at is the Workflow finished fallback; do not repeat it as Finished.
  await expect(page.getByText("Finished", { exact: true })).toHaveCount(0);

  await page
    .getByTestId("workspace-card-ws_presentation_sample")
    .getByRole("button", { name: "Details", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: /Task details/i });
  await expect(dialog).toBeVisible();
  const modalWorkflowFact = dialog.getByText("Workflow finished", { exact: true }).locator("..");
  await expect(modalWorkflowFact).toContainText(/[A-Za-z]{3}\s+\d{2},/);
  await expect(dialog.getByText("Finished", { exact: true })).toHaveCount(0);
});

test("Finished stays visible when it differs from workflow_finished_at", async ({ page }) => {
  const overview = {
    ...presentationOverview(),
    workflow_finished_at: "2026-09-06T17:10:00Z",
    finished_at: "2026-09-06T16:00:00Z",
  };
  await mockAwfConsoleApi(page, { overviewItems: [overview] });
  await page.route("**/api/awf/workspaces/ws_presentation_sample**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/workspaces/ws_presentation_sample") {
      await fulfillJson(route, {
        ...overview,
        id: "ws_presentation_sample",
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
  await page.getByTestId("workspace-card-ws_presentation_sample").click();
  await expect(page.getByText("Finished", { exact: true })).toBeVisible();
  await expect(page.getByText("Workflow finished", { exact: true })).toBeVisible();

  await page
    .getByTestId("workspace-card-ws_presentation_sample")
    .getByRole("button", { name: "Details", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: /Task details/i });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("Finished", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Workflow finished", { exact: true })).toBeVisible();
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
  // Auth clear drops capabilities → omit fleet_summary KPIs (not dash shells).
  await expect(kpi(page, "Active")).toHaveCount(0);
});
