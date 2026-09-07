import { expect, type Page, test } from "@playwright/test";

import {
  fulfillJson,
  hostedCapabilities,
  listEnvelope,
  loadConsoleFixture,
  localCapabilities,
  localDashboardSummary,
  mockAwfConsoleApi,
} from "./fixtures/console-api";

async function waitForConsoleReady(page: Page) {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

test("hosted mode does not request resource saturation", async ({ page }) => {
  const requested: string[] = [];
  await mockAwfConsoleApi(page, {
    mode: "hosted",
    onRequest: (path) => requested.push(path),
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByRole("heading", { name: "Cloud Runtime" })).toBeVisible();
  await page.waitForTimeout(1500);

  expect(requested.some((path) => path.includes("/metrics/resources/saturation"))).toBe(false);
  expect(requested.some((path) => path.includes("/console/cloud-runtime"))).toBe(true);
  expect(requested.some((path) => path.includes("/console/dashboard-summary"))).toBe(true);
});

test("capability unknown version keeps navigation and disables privileged polls", async ({ page }) => {
  const requested: string[] = [];
  await mockAwfConsoleApi(page, {
    capabilities: loadConsoleFixture("capabilities.unknown_version.json"),
    onRequest: (path) => requested.push(path),
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByText(/Unsupported console schema_version/i)).toBeVisible();
  await page.waitForTimeout(1000);

  expect(requested.some((path) => path.includes("/metrics/resources/saturation"))).toBe(false);
  expect(requested.some((path) => path.includes("/console/dashboard-summary"))).toBe(false);
  await expect(page.locator("#awf-workspaces")).toBeVisible();
});

test("capability 401 clears stale summary KPIs", async ({ page }) => {
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
          { detail: { error_code: "UNAUTHORIZED", message: "Invalid AWF API token." } },
          401,
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
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z", since_hours: 24, completed_count: 0, failed_count: 0, cancelled_count: 0, stuck_count: 0, actionable_reason_count: 0, unactionable_reason_count: 0, active_count: 0, destroying_count: 0, destroyed_count: 0, cleanup_failure_count: 0, status_counts: {}, failure_reason_counts: {}, window_start: "2026-09-05T17:00:00Z" });
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
  const active = page.getByText("Active", { exact: true }).locator("..").filter({ has: page.locator(".kpi-value") });
  await expect(active.locator(".kpi-value")).toHaveText("9");

  authDenied = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/Invalid AWF API token|authorization denied|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(active.locator(".kpi-value")).toHaveText("—");
});

test("same-identity capability refresh clears KPIs when fleet_summary becomes unsupported", async ({
  page,
}) => {
  let withdrawFleetSummary = false;
  let delaySummary = false;
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
      await fulfillJson(route, withdrawFleetSummary ? withdrawnCaps : baseCaps);
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      if (delaySummary) {
        await new Promise((resolve) => setTimeout(resolve, 750));
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
  const active = page
    .getByText("Active", { exact: true })
    .locator("..")
    .filter({ has: page.locator(".kpi-value") });
  await expect(active.locator(".kpi-value")).toHaveText("9");

  delaySummary = true;
  withdrawFleetSummary = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(active.locator(".kpi-value")).toHaveText("—", { timeout: 10_000 });
  // Delayed in-flight summary must not restore withdrawn fleet KPIs.
  await page.waitForTimeout(1000);
  await expect(active.locator(".kpi-value")).toHaveText("—");
});

test("same-identity capability refresh clears inspector when workspace_runtime becomes unsupported", async ({
  page,
}) => {
  let withdrawRuntime = false;
  let delayRuntime = false;
  const workspaceId = "ws_runtime_withdraw";
  const workspaceTitle = "Runtime withdraw workspace";
  const composeProject = "awf-ws-runtime-withdraw-unique";
  const baseCaps = localCapabilities() as {
    diagnostics: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const withdrawnCaps = {
    ...baseCaps,
    diagnostics: baseCaps.diagnostics.map((item) =>
      item.id === "workspace_runtime"
        ? {
            id: "workspace_runtime",
            availability: "unsupported",
            reason_code: "not_implemented",
            message: "Runtime detail withdrawn",
            semantics: "Optional workspace runtime detail feed.",
          }
        : item,
    ),
  };
  const overviewItem = {
    workspace_id: workspaceId,
    title: workspaceTitle,
    repo_url: "https://github.com/example/runtime-withdraw",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Prove runtime cache clears on same-identity withdrawal",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };
  const runtimePayload = {
    workspace_id: workspaceId,
    compose_project_name: composeProject,
    stack_state: "running",
    services: [],
    app_endpoints: [],
    logs_available: true,
    control_available: true,
    reason: null,
  };

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, withdrawRuntime ? withdrawnCaps : baseCaps);
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
      if (delayRuntime) {
        await new Promise((resolve) => setTimeout(resolve, 750));
      }
      await fulfillJson(route, runtimePayload);
      return;
    }
    if (
      path === `/api/awf/workspaces/${workspaceId}/events` ||
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

  delayRuntime = true;
  withdrawRuntime = true;
  // Inspector is fixed over the top-bar Refresh control; force the click so the
  // capability refresh still runs while retained runtime evidence stays mounted.
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect(page.getByText(composeProject, { exact: true })).toHaveCount(0, { timeout: 10_000 });
  await expect(page.getByText("Runtime snapshot unavailable.")).toBeVisible();
  // Delayed in-flight loadWorkspace must not restore withdrawn runtime.
  await page.waitForTimeout(1000);
  await expect(page.getByText(composeProject, { exact: true })).toHaveCount(0);
  await expect(page.getByText("Runtime snapshot unavailable.")).toBeVisible();
});

test("capability 401 clears retained agent and model filter options", async ({ page }) => {
  let authDenied = false;
  const priorWorkspace = {
    workspace_id: "ws_retained_filter",
    title: "Prior-tenant workspace",
    repo_url: "https://github.com/example/retained-filter",
    base_branch: "main",
    agent: "tenant-a-only-agent",
    agent_model: "tenant-a-only-model",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Retained filter metadata must clear on auth denial",
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
      if (authDenied) {
        await fulfillJson(
          route,
          { detail: { error_code: "UNAUTHORIZED", message: "Invalid AWF API token." } },
          401,
        );
        return;
      }
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(
        route,
        authDenied
          ? { items: [], next_cursor: null, has_more: false }
          : listEnvelope([priorWorkspace]),
      );
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
  await expect(page.getByTestId("workspace-card-ws_retained_filter")).toBeVisible();

  const expandButton = page.getByRole("button", { name: "Filters" });
  if ((await expandButton.getAttribute("aria-expanded")) === "false") {
    await expandButton.click();
  }
  const agentGroup = page.getByRole("group", { name: "Agent" });
  await agentGroup.getByRole("button", { name: /Agent/ }).click();
  await expect(agentGroup.getByLabel("tenant-a-only-agent")).toBeVisible();
  const modelGroup = page.getByRole("group", { name: "Model" });
  await modelGroup.getByRole("button", { name: /Model/ }).click();
  await expect(modelGroup.getByLabel("tenant-a-only-model")).toBeVisible();

  authDenied = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/Invalid AWF API token|authorization denied|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByTestId("workspace-card-ws_retained_filter")).toHaveCount(0);

  if ((await expandButton.getAttribute("aria-expanded")) === "false") {
    await expandButton.click();
  }
  await agentGroup.getByRole("button", { name: /Agent/ }).click();
  await expect(agentGroup.getByLabel("tenant-a-only-agent")).toHaveCount(0);
  await modelGroup.getByRole("button", { name: /Model/ }).click();
  await expect(modelGroup.getByLabel("tenant-a-only-model")).toHaveCount(0);
});

test("in-flight dashboard-summary after capability 401 does not restore cleared KPIs", async ({ page }) => {
  let authDenied = false;
  let delaySummary = false;
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
          { detail: { error_code: "UNAUTHORIZED", message: "Invalid AWF API token." } },
          401,
        );
        return;
      }
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      if (delaySummary) {
        await new Promise((resolve) => setTimeout(resolve, 750));
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
  const active = page
    .getByText("Active", { exact: true })
    .locator("..")
    .filter({ has: page.locator(".kpi-value") });
  await expect(active.locator(".kpi-value")).toHaveText("9");

  // Start an authenticated refresh whose summary response is intentionally slow,
  // then revoke auth so clearAuthorizedConsoleFeeds races the in-flight apply.
  delaySummary = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  authDenied = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/Invalid AWF API token|authorization denied|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(active.locator(".kpi-value")).toHaveText("—");
  // Wait past the delayed pre-clear summary; it must not restore Active=9.
  await page.waitForTimeout(1000);
  await expect(active.locator(".kpi-value")).toHaveText("—");
});

test("in-flight cloud-runtime after tenant switch does not restore prior snapshot", async ({ page }) => {
  let useNewTenant = false;
  let delayPriorRuntime = false;
  const priorRuntime = loadConsoleFixture<Record<string, unknown>>("cloud-runtime.hosted.json");
  const newTenantCaps = {
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
      await fulfillJson(route, useNewTenant ? newTenantCaps : hostedCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      if (useNewTenant) {
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "tenant summary unavailable" } },
          503,
        );
        return;
      }
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/console/cloud-runtime") {
      if (useNewTenant) {
        // Fail after identity switch so a restored prior snapshot is the only way
        // within_quota can reappear.
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "tenant runtime unavailable" } },
          503,
        );
        return;
      }
      if (delayPriorRuntime) {
        await new Promise((resolve) => setTimeout(resolve, 750));
      }
      await fulfillJson(route, priorRuntime);
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
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
  await expect(page.getByRole("heading", { name: "Cloud Runtime" })).toBeVisible();
  await expect(page.getByText("within_quota", { exact: true })).toBeVisible();

  // Start a slow prior-tenant runtime fetch, then switch identity so clear races it.
  delayPriorRuntime = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  useNewTenant = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText("within_quota", { exact: true })).toHaveCount(0, { timeout: 10_000 });
  await page.waitForTimeout(1000);
  await expect(page.getByText("within_quota", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Cloud Runtime" })).toBeVisible();
});

test("identity change after capabilities 404 clears retained overview", async ({ page }) => {
  // 404 gated clear keeps overview for legacy-safe nav but drops negotiation state.
  // Recovery must still compare the retained identity so a different backend/tenant
  // without URL-context change bumps the feed epoch (discarding in-flight prior rows).
  type CapPhase = "local" | "missing" | "tenant_b";
  let phase: CapPhase = "local";
  let holdPriorOverview = false;
  const priorOverviewGate = {
    waiters: [] as Array<() => void>,
    delivered: 0,
    releaseAll() {
      const pending = this.waiters.splice(0, this.waiters.length);
      for (const release of pending) {
        release();
      }
    },
  };
  const localWorkspace = {
    workspace_id: "ws_pre_404",
    title: "Pre-404 retained workspace",
    repo_url: "https://github.com/example/pre-404",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Must clear when identity changes after 404",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };
  const tenantBCaps = {
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
      if (phase === "missing") {
        await fulfillJson(
          route,
          { detail: { error_code: "NOT_FOUND", message: "capabilities negotiation unavailable" } },
          404,
        );
        return;
      }
      await fulfillJson(route, phase === "tenant_b" ? tenantBCaps : localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      if (phase === "tenant_b") {
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "tenant summary unavailable" } },
          503,
        );
        return;
      }
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/console/cloud-runtime") {
      await fulfillJson(route, loadConsoleFixture("cloud-runtime.hosted.json"));
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      // Capture request-time phase/payload before any await. A mutable-phase read
      // after the hold would turn prior-identity rows into tenant_b empty data and
      // fail to prove late tenant-A responses are discarded.
      const requestPhase = phase;
      const requestPayload =
        requestPhase === "tenant_b"
          ? { items: [], next_cursor: null, has_more: false }
          : { items: [localWorkspace], next_cursor: null, has_more: false };
      const heldPrior = holdPriorOverview && requestPhase !== "tenant_b";
      if (heldPrior) {
        await new Promise<void>((resolve) => {
          priorOverviewGate.waiters.push(resolve);
        });
      }
      await fulfillJson(route, requestPayload);
      if (heldPrior) {
        priorOverviewGate.delivered += 1;
      }
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
  await expect(page.getByTestId("workspace-card-ws_pre_404")).toBeVisible();

  phase = "missing";
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/capabilities negotiation unavailable/i).first()).toBeVisible({
    timeout: 10_000,
  });
  // Legacy-safe nav: overview retained across the 404 gap.
  await expect(page.getByTestId("workspace-card-ws_pre_404")).toBeVisible();

  // Hold a prior-identity overview response during the 404 gap, then recover under
  // a different identity. Release the deferred prior rows only after tenant_b
  // negotiation so epoch advancement must discard them — not merely empty B data.
  holdPriorOverview = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect.poll(() => priorOverviewGate.waiters.length).toBeGreaterThan(0);
  phase = "tenant_b";
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByTestId("workspace-card-ws_pre_404")).toHaveCount(0, { timeout: 10_000 });
  const pendingStaleDeliveries = priorOverviewGate.waiters.length;
  priorOverviewGate.releaseAll();
  // Wait until the deferred prior-identity fulfills actually complete; an immediate
  // empty-DOM check can pass before the stale response is delivered to the page.
  await expect.poll(() => priorOverviewGate.delivered).toBe(pendingStaleDeliveries);
  await expect(page.getByTestId("workspace-card-ws_pre_404")).toHaveCount(0);
});

test("capability 401 clears workspace list inspector logs and events", async ({ page }) => {
  let authDenied = false;
  const workspaceId = "ws_auth_clear";
  const workspaceTitle = "Auth-clear workspace surface";
  const eventType = "auth_clear_unique_event";
  const overviewItem = {
    workspace_id: workspaceId,
    title: workspaceTitle,
    repo_url: "https://github.com/example/auth-clear",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Prove auth clear wipes workspace rows",
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
      if (authDenied) {
        await fulfillJson(
          route,
          { detail: { error_code: "UNAUTHORIZED", message: "Invalid AWF API token." } },
          401,
        );
        return;
      }
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      // Keep serving the prior workspace after auth denial so the assertion
      // proves clearAuthorizedConsoleFeeds wiped state (not overview failure).
      await fulfillJson(route, { items: [overviewItem], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, { ...overviewItem, id: workspaceId, version: 1 });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { status: "running" });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, {
        items: [
          {
            id: "evt_auth_clear",
            workspace_id: workspaceId,
            event_type: eventType,
            old_state: "ready",
            new_state: "running",
            reason_code: null,
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
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByTestId(`workspace-card-${workspaceId}`)).toBeVisible();
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  await expect(page.getByRole("heading", { name: workspaceTitle }).nth(1)).toBeVisible();
  await expect(page.getByText(eventType, { exact: true })).toBeVisible();

  authDenied = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/Invalid AWF API token|authorization denied|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByTestId(`workspace-card-${workspaceId}`)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: workspaceTitle })).toHaveCount(0);
  await expect(page.getByText(eventType, { exact: true })).toHaveCount(0);
});

test("delayed overview after capability 401 does not restore revoked rows", async ({ page }) => {
  let authDenied = false;
  let delayOverview = false;
  const workspaceId = "ws_auth_delay";
  const workspaceTitle = "Auth-delay workspace surface";
  const eventType = "auth_delay_unique_event";
  const overviewItem = {
    workspace_id: workspaceId,
    title: workspaceTitle,
    repo_url: "https://github.com/example/auth-delay",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Prove delayed overview cannot refill after auth denial",
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
      if (authDenied) {
        await fulfillJson(
          route,
          { detail: { error_code: "UNAUTHORIZED", message: "Invalid AWF API token." } },
          401,
        );
        return;
      }
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      if (delayOverview) {
        await new Promise((resolve) => setTimeout(resolve, 750));
      }
      // Keep serving prior rows after auth denial so a stale loadOverview apply
      // would restore them — the sync auth latch must prevent that.
      await fulfillJson(route, { items: [overviewItem], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, { ...overviewItem, id: workspaceId, version: 1 });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { status: "running" });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, {
        items: [
          {
            id: "evt_auth_delay",
            workspace_id: workspaceId,
            event_type: eventType,
            old_state: "ready",
            new_state: "running",
            reason_code: null,
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
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByTestId(`workspace-card-${workspaceId}`)).toBeVisible();
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  await expect(page.getByRole("heading", { name: workspaceTitle }).nth(1)).toBeVisible();
  await expect(page.getByText(eventType, { exact: true })).toBeVisible();

  authDenied = true;
  delayOverview = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/Invalid AWF API token|authorization denied|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByTestId(`workspace-card-${workspaceId}`)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: workspaceTitle })).toHaveCount(0);
  await expect(page.getByText(eventType, { exact: true })).toHaveCount(0);
  // Wait past the delayed overview; it must not restore revoked rows/diagnostics.
  await page.waitForTimeout(1000);
  await expect(page.getByTestId(`workspace-card-${workspaceId}`)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: workspaceTitle })).toHaveCount(0);
  await expect(page.getByText(eventType, { exact: true })).toHaveCount(0);
});

test("delayed capability 200 after newer 403 does not restore denial", async ({ page }) => {
  // Repro: start capability request A, then B; B returns 403 and clears auth;
  // delayed A=200 must not clear consoleAuthDeniedRef or restore capabilities.
  let authDenied = false;
  let holdNextSuccess: (() => void) | null = null;
  let releaseHeldSuccess: ((caps: unknown) => void) | null = null;
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
  const workspaceId = "ws_cap_race";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Capability race workspace",
    repo_url: "https://github.com/example/cap-race",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Stale capability success must not undo a newer 403",
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
      if (authDenied) {
        await fulfillJson(
          route,
          { detail: { error_code: "FORBIDDEN", message: "AWF API token lacks console access." } },
          403,
        );
        // After B=403, release the held A response as a delayed 200.
        if (releaseHeldSuccess) {
          const release = releaseHeldSuccess;
          releaseHeldSuccess = null;
          setTimeout(() => release(localCapabilities()), 100);
        }
        return;
      }
      if (holdNextSuccess) {
        await new Promise<void>((resolve) => {
          holdNextSuccess = null;
          releaseHeldSuccess = async (caps: unknown) => {
            await fulfillJson(route, caps);
            resolve();
          };
        });
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
      await fulfillJson(route, { items: [overviewItem], next_cursor: null, has_more: false });
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
  const active = page
    .getByText("Active", { exact: true })
    .locator("..")
    .filter({ has: page.locator(".kpi-value") });
  await expect(active.locator(".kpi-value")).toHaveText("9");
  await expect(page.getByTestId(`workspace-card-${workspaceId}`)).toBeVisible();

  // Start A (held), then B (403). Delayed A=200 must not restore auth.
  holdNextSuccess = () => undefined;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect.poll(() => releaseHeldSuccess !== null).toBe(true);
  authDenied = true;
  await page.getByRole("button", { name: /refresh/i }).click();
  await expect(page.getByText(/lacks console access|authorization denied|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(active.locator(".kpi-value")).toHaveText("—");
  await expect(page.getByTestId(`workspace-card-${workspaceId}`)).toHaveCount(0);

  await page.waitForTimeout(1000);
  await expect(page.getByText(/lacks console access|authorization denied|denied/i).first()).toBeVisible();
  await expect(active.locator(".kpi-value")).toHaveText("—");
  await expect(page.getByTestId(`workspace-card-${workspaceId}`)).toHaveCount(0);
});

test("malformed capabilities fail closed without saturation polls", async ({ page }) => {
  const requested: string[] = [];
  await mockAwfConsoleApi(page, {
    capabilities: loadConsoleFixture("capabilities.malformed.json"),
    onRequest: (path) => requested.push(path),
  });
  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByText(/malformed|capabilities/i).first()).toBeVisible();
  await page.waitForTimeout(800);
  expect(requested.some((path) => path.includes("/metrics/resources/saturation"))).toBe(false);
});

test("local mode still requests saturation when advertised", async ({ page }) => {
  const requested: string[] = [];
  await mockAwfConsoleApi(page, {
    mode: "local",
    onRequest: (path) => requested.push(path),
  });
  await page.goto("/");
  await waitForConsoleReady(page);
  await page.waitForTimeout(800);
  expect(requested.some((path) => path.includes("/metrics/resources/saturation"))).toBe(true);
  expect((hostedCapabilities() as { backend_kind: string }).backend_kind).toBe("hosted");
});

test("unsupported workspace_runtime diagnostic skips runtime poll", async ({ page }) => {
  const requested: string[] = [];
  const caps = localCapabilities() as {
    diagnostics: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const gated = {
    ...caps,
    diagnostics: caps.diagnostics.map((item) =>
      item.id === "workspace_runtime"
        ? {
            id: "workspace_runtime",
            availability: "unsupported",
            reason_code: "not_implemented",
            message: "runtime detail unavailable",
            semantics: "Optional workspace runtime detail feed.",
          }
        : item,
    ),
  };
  const overviewItem = {
    workspace_id: "ws_detail_gate",
    title: "Detail gate workspace",
    repo_url: "https://github.com/example/detail-gate",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Gate optional runtime",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };
  await mockAwfConsoleApi(page, {
    capabilities: gated,
    overviewItems: [overviewItem],
    onRequest: (path) => requested.push(path),
  });
  await page.route("**/api/awf/workspaces/ws_detail_gate**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    requested.push(path);
    if (path === "/api/awf/workspaces/ws_detail_gate") {
      await fulfillJson(route, { ...overviewItem, id: "ws_detail_gate", version: 1 });
      return;
    }
    if (path.endsWith("/events") || path.endsWith("/operations") || path.endsWith("/logs")) {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId("workspace-card-ws_detail_gate").click();
  await page.waitForTimeout(1000);
  expect(requested.some((path) => path.endsWith("/runtime"))).toBe(false);
  expect(requested.some((path) => path === "/api/awf/workspaces/ws_detail_gate")).toBe(true);
});

test("omitted workspace_* diagnostics stay disabled", async ({ page }) => {
  const requested: string[] = [];
  const caps = localCapabilities() as {
    diagnostics: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const omitted = {
    ...caps,
    diagnostics: caps.diagnostics.filter(
      (item) => typeof item.id !== "string" || !item.id.startsWith("workspace_"),
    ),
  };
  const overviewItem = {
    workspace_id: "ws_omit_detail",
    title: "Omitted detail feeds",
    repo_url: "https://github.com/example/omit-detail",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Do not enable omitted workspace diagnostics",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };
  await mockAwfConsoleApi(page, {
    capabilities: omitted,
    overviewItems: [overviewItem],
    onRequest: (path) => requested.push(path),
  });
  await page.route("**/api/awf/workspaces/ws_omit_detail**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    requested.push(path);
    if (path === "/api/awf/workspaces/ws_omit_detail") {
      await fulfillJson(route, { ...overviewItem, id: "ws_omit_detail", version: 1 });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId("workspace-card-ws_omit_detail").click();
  await page.waitForTimeout(1000);
  expect(requested.some((path) => path === "/api/awf/workspaces/ws_omit_detail")).toBe(true);
  expect(requested.some((path) => path.endsWith("/runtime"))).toBe(false);
  expect(requested.some((path) => path.endsWith("/events"))).toBe(false);
  expect(requested.some((path) => path.endsWith("/operations"))).toBe(false);
  expect(requested.some((path) => path.endsWith("/logs"))).toBe(false);
  expect(requested.some((path) => path.includes("/stream"))).toBe(false);
});

test("fullscreen logs skip unsupported workspace_logs and workspace_stream", async ({ page }) => {
  const requested: string[] = [];
  const caps = localCapabilities() as {
    diagnostics: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const gated = {
    ...caps,
    diagnostics: caps.diagnostics.map((item) =>
      item.id === "workspace_logs" || item.id === "workspace_stream"
        ? {
            id: item.id,
            availability: "unsupported",
            reason_code: "not_implemented",
            message: `${String(item.id)} unavailable`,
            semantics: "Optional workspace log diagnostic.",
          }
        : item,
    ),
  };
  const overviewItem = {
    workspace_id: "ws_fullscreen_gate",
    title: "Fullscreen log gate",
    repo_url: "https://github.com/example/fullscreen-gate",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Gate fullscreen log polls",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };
  await mockAwfConsoleApi(page, {
    capabilities: gated,
    overviewItems: [overviewItem],
    onRequest: (path) => requested.push(path),
  });
  await page.route("**/api/awf/workspaces/ws_fullscreen_gate**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    requested.push(path);
    if (path === "/api/awf/workspaces/ws_fullscreen_gate") {
      await fulfillJson(route, { ...overviewItem, id: "ws_fullscreen_gate", version: 1 });
      return;
    }
    if (path.endsWith("/events") || path.endsWith("/operations") || path.endsWith("/runtime")) {
      await fulfillJson(route, path.endsWith("/runtime") ? { status: "running" } : listEnvelope([]));
      return;
    }
    if (path.endsWith("/logs") || path.includes("/logs/") || path.includes("/stream")) {
      await fulfillJson(route, { detail: { message: `should not request ${path}` } }, 500);
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  // Reviewer path: workspace-selection toolbar → Open logs (not the card Logs button).
  await page
    .getByTestId("workspace-card-ws_fullscreen_gate")
    .getByRole("checkbox", { name: "Select Fullscreen log gate for fullscreen logs" })
    .check();
  await page.getByRole("button", { name: "Open logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  await expect(modal.getByRole("heading", { name: "Logs" })).toBeVisible();
  await expect(modal.getByText("Workspace log listing is unavailable.")).toBeVisible();
  await page.waitForTimeout(1200);

  expect(requested.some((path) => path.endsWith("/logs") || path.includes("/logs/"))).toBe(false);
  expect(requested.some((path) => path.includes("/stream"))).toBe(false);
});

test("refresh reloads overview when capabilities are malformed", async ({ page }) => {
  const overviewItem = {
    workspace_id: "ws_refresh_cap",
    title: "Refresh despite capability error",
    repo_url: "https://github.com/example/refresh-cap",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Overview refresh must not depend on capabilities",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };
  let overviewRequestsAfterReady = 0;
  let ready = false;
  await mockAwfConsoleApi(page, {
    capabilities: loadConsoleFixture("capabilities.malformed.json"),
    overviewItems: [overviewItem],
    onRequest: (path) => {
      if (!ready) {
        return;
      }
      if (path === "/api/awf/workspaces/overview") {
        overviewRequestsAfterReady += 1;
      }
    },
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByTestId("workspace-card-ws_refresh_cap")).toBeVisible();
  ready = true;
  const before = overviewRequestsAfterReady;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => overviewRequestsAfterReady).toBeGreaterThan(before);
});

test("desktop and mobile screenshots for capability error", async ({ page }) => {
  await mockAwfConsoleApi(page, {
    capabilities: loadConsoleFixture("capabilities.unknown_version.json"),
  });
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/");
  await waitForConsoleReady(page);
  await page.screenshot({ path: "test-results/capabilities-unknown-desktop.png", fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "test-results/capabilities-unknown-mobile.png", fullPage: true });
});

test.describe("hosted context query carry", () => {
  test.use({ baseURL: "http://127.0.0.1:3101" });

  test("overview request rebuilds context query after client-side tenant switch", async ({ page }) => {
    const apiPrefix = "/api/core-console";
    let useTenantB = false;
    const overviewUrls: string[] = [];
    const tenantACaps = {
      ...(hostedCapabilities() as Record<string, unknown>),
      identity: {
        backend_id: "awf-cloud-tenant-a",
        scope: "tenant",
        tenant_id: "tenant_a",
      },
    };
    const tenantBCaps = {
      ...(hostedCapabilities() as Record<string, unknown>),
      identity: {
        backend_id: "awf-cloud-tenant-b",
        scope: "tenant",
        tenant_id: "tenant_b",
      },
    };
    const workspaceFor = (tenant: "a" | "b") => ({
      workspace_id: `ws_tenant_${tenant}`,
      title: `Tenant ${tenant.toUpperCase()} workspace`,
      repo_url: `https://github.com/example/tenant-${tenant}`,
      base_branch: "main",
      agent: "codex",
      agent_model: "gpt-5.5",
      status: "running",
      created_at: "2026-09-06T17:00:00Z",
      updated_at: "2026-09-06T17:00:00Z",
      task_prompt: `Rows for tenant ${tenant}`,
      lifecycle: [],
      llm_usage: null,
      recovery: null,
    });

    await page.route("**/api/core-console/**", async (route) => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path === `${apiPrefix}/health`) {
        await fulfillJson(route, { status: "ok" });
        return;
      }
      if (path === `${apiPrefix}/console/capabilities`) {
        await fulfillJson(route, useTenantB ? tenantBCaps : tenantACaps);
        return;
      }
      if (path === `${apiPrefix}/console/dashboard-summary`) {
        await fulfillJson(route, loadConsoleFixture("dashboard-summary.hosted.json"));
        return;
      }
      if (path === `${apiPrefix}/console/cloud-runtime`) {
        await fulfillJson(route, loadConsoleFixture("cloud-runtime.hosted.json"));
        return;
      }
      if (path === `${apiPrefix}/workspaces/overview`) {
        overviewUrls.push(url.toString());
        await fulfillJson(
          route,
          listEnvelope([workspaceFor(useTenantB ? "b" : "a")]),
        );
        return;
      }
      if (path === `${apiPrefix}/metrics/workspaces/summary`) {
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
      if (path === `${apiPrefix}/merge-queue`) {
        await fulfillJson(route, listEnvelope([]));
        return;
      }
      if (path === `${apiPrefix}/metrics/failures/summary`) {
        await fulfillJson(route, {
          total_failures: 0,
          window_hours: 24,
          taxonomy: [],
          latest_examples: [],
        });
        return;
      }
      await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
    });

    await page.goto("/workspaces?org_id=org_a&project_id=proj_a");
    await waitForConsoleReady(page);
    await expect(page.getByTestId("workspace-card-ws_tenant_a")).toBeVisible();
    expect(overviewUrls.some((u) => u.includes("org_id=org_a") && u.includes("project_id=proj_a"))).toBe(
      true,
    );

    // Soft-switch page search (client-side) then bump capability identity so
    // authorized feeds clear and overview reloads under the new context.
    await page.evaluate(() => {
      const next = new URL(window.location.href);
      next.searchParams.set("org_id", "org_b");
      next.searchParams.set("project_id", "proj_b");
      window.history.replaceState(null, "", `${next.pathname}?${next.searchParams.toString()}`);
    });
    useTenantB = true;
    const before = overviewUrls.length;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect.poll(() => overviewUrls.length).toBeGreaterThan(before);
    const afterSwitch = overviewUrls.slice(before);
    expect(
      afterSwitch.some((u) => u.includes("org_id=org_b") && u.includes("project_id=proj_b")),
    ).toBe(true);
    expect(afterSwitch.every((u) => !u.includes("org_id=org_a"))).toBe(true);
    await expect(page.getByTestId("workspace-card-ws_tenant_b")).toBeVisible();
    await expect(page.getByTestId("workspace-card-ws_tenant_a")).toHaveCount(0);
  });

  test("soft context switch clears prior tenant rows before capabilities return", async ({ page }) => {
    const apiPrefix = "/api/core-console";
    let hangTenantBCapabilities = false;
    const tenantBCapabilityGate = {
      waiters: [] as Array<() => void>,
      releaseAll() {
        const pending = this.waiters.splice(0, this.waiters.length);
        for (const release of pending) {
          release();
        }
      },
    };
    const tenantACaps = {
      ...(hostedCapabilities() as Record<string, unknown>),
      identity: {
        backend_id: "awf-cloud-tenant-a",
        scope: "tenant",
        tenant_id: "tenant_a",
      },
    };
    const tenantBCaps = {
      ...(hostedCapabilities() as Record<string, unknown>),
      identity: {
        backend_id: "awf-cloud-tenant-b",
        scope: "tenant",
        tenant_id: "tenant_b",
      },
    };

    await page.route("**/api/core-console/**", async (route) => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path === `${apiPrefix}/health`) {
        await fulfillJson(route, { status: "ok" });
        return;
      }
      if (path === `${apiPrefix}/console/capabilities`) {
        if (hangTenantBCapabilities) {
          await new Promise<void>((resolve) => {
            tenantBCapabilityGate.waiters.push(resolve);
          });
          await fulfillJson(route, tenantBCaps);
          return;
        }
        await fulfillJson(route, tenantACaps);
        return;
      }
      if (path === `${apiPrefix}/console/dashboard-summary`) {
        await fulfillJson(route, loadConsoleFixture("dashboard-summary.hosted.json"));
        return;
      }
      if (path === `${apiPrefix}/console/cloud-runtime`) {
        await fulfillJson(route, loadConsoleFixture("cloud-runtime.hosted.json"));
        return;
      }
      if (path === `${apiPrefix}/workspaces/overview`) {
        await fulfillJson(
          route,
          listEnvelope([
            {
              workspace_id: hangTenantBCapabilities ? "ws_tenant_b" : "ws_tenant_a",
              title: hangTenantBCapabilities ? "Tenant B workspace" : "Tenant A workspace",
              repo_url: "https://github.com/example/tenant",
              base_branch: "main",
              agent: "codex",
              agent_model: "gpt-5.5",
              status: "running",
              created_at: "2026-09-06T17:00:00Z",
              updated_at: "2026-09-06T17:00:00Z",
              task_prompt: "tenant row",
              lifecycle: [],
              llm_usage: null,
              recovery: null,
            },
          ]),
        );
        return;
      }
      if (path === `${apiPrefix}/metrics/workspaces/summary`) {
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
      if (path === `${apiPrefix}/merge-queue`) {
        await fulfillJson(route, listEnvelope([]));
        return;
      }
      if (path === `${apiPrefix}/metrics/failures/summary`) {
        await fulfillJson(route, {
          total_failures: 0,
          window_hours: 24,
          taxonomy: [],
          latest_examples: [],
        });
        return;
      }
      await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
    });

    await page.goto("/workspaces?org_id=org_a&project_id=proj_a");
    await waitForConsoleReady(page);
    await expect(page.getByTestId("workspace-card-ws_tenant_a")).toBeVisible();

    hangTenantBCapabilities = true;
    await page.evaluate(() => {
      const next = new URL(window.location.href);
      next.searchParams.set("org_id", "org_b");
      next.searchParams.set("project_id", "proj_b");
      window.history.replaceState(null, "", `${next.pathname}?${next.searchParams.toString()}`);
    });

    // Prior-tenant rows must disappear immediately — before capabilities resolve.
    await expect(page.getByTestId("workspace-card-ws_tenant_a")).toHaveCount(0);
    await expect.poll(() => tenantBCapabilityGate.waiters.length).toBeGreaterThan(0);
    tenantBCapabilityGate.releaseAll();
  });

  test("soft context switch clears prior tenant rows before failed capabilities", async ({ page }) => {
    const apiPrefix = "/api/core-console";
    let failTenantBCapabilities = false;
    let tenantBCapabilityFailures = 0;
    const tenantACaps = {
      ...(hostedCapabilities() as Record<string, unknown>),
      identity: {
        backend_id: "awf-cloud-tenant-a",
        scope: "tenant",
        tenant_id: "tenant_a",
      },
    };

    await page.route("**/api/core-console/**", async (route) => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path === `${apiPrefix}/health`) {
        await fulfillJson(route, { status: "ok" });
        return;
      }
      if (path === `${apiPrefix}/console/capabilities`) {
        if (failTenantBCapabilities) {
          tenantBCapabilityFailures += 1;
          await fulfillJson(route, { detail: { message: "capabilities outage" } }, 503);
          return;
        }
        await fulfillJson(route, tenantACaps);
        return;
      }
      if (path === `${apiPrefix}/console/dashboard-summary`) {
        await fulfillJson(route, loadConsoleFixture("dashboard-summary.hosted.json"));
        return;
      }
      if (path === `${apiPrefix}/console/cloud-runtime`) {
        await fulfillJson(route, loadConsoleFixture("cloud-runtime.hosted.json"));
        return;
      }
      if (path === `${apiPrefix}/workspaces/overview`) {
        // After the soft switch, do not reintroduce tenant-A rows via the
        // legacy-safe overview fetch that still runs on capability outage.
        await fulfillJson(
          route,
          listEnvelope(
            failTenantBCapabilities
              ? []
              : [
                  {
                    workspace_id: "ws_tenant_a",
                    title: "Tenant A workspace",
                    repo_url: "https://github.com/example/tenant",
                    base_branch: "main",
                    agent: "codex",
                    agent_model: "gpt-5.5",
                    status: "running",
                    created_at: "2026-09-06T17:00:00Z",
                    updated_at: "2026-09-06T17:00:00Z",
                    task_prompt: "tenant row",
                    lifecycle: [],
                    llm_usage: null,
                    recovery: null,
                  },
                ],
          ),
        );
        return;
      }
      if (path === `${apiPrefix}/metrics/workspaces/summary`) {
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
      if (path === `${apiPrefix}/merge-queue`) {
        await fulfillJson(route, listEnvelope([]));
        return;
      }
      if (path === `${apiPrefix}/metrics/failures/summary`) {
        await fulfillJson(route, {
          total_failures: 0,
          window_hours: 24,
          taxonomy: [],
          latest_examples: [],
        });
        return;
      }
      await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
    });

    await page.goto("/workspaces?org_id=org_a&project_id=proj_a");
    await waitForConsoleReady(page);
    await expect(page.getByTestId("workspace-card-ws_tenant_a")).toBeVisible();

    failTenantBCapabilities = true;
    await page.evaluate(() => {
      const next = new URL(window.location.href);
      next.searchParams.set("org_id", "org_b");
      next.searchParams.set("project_id", "proj_b");
      window.history.replaceState(null, "", `${next.pathname}?${next.searchParams.toString()}`);
    });

    // Prior-tenant rows/controls must clear immediately even when the new
    // tenant's capability poll fails closed (not only when it hangs).
    await expect(page.getByTestId("workspace-card-ws_tenant_a")).toHaveCount(0);
    await expect.poll(() => tenantBCapabilityFailures).toBeGreaterThan(0);
    await expect(page.getByText(/capabilities outage/i).first()).toBeVisible();
  });
});
