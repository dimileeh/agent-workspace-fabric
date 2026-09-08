import { expect, type Page, type Route, test } from "@playwright/test";

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

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gGgMt: a network/5xx on
// one detail feed must warn as soon as that request settles, even if a sibling
// hangs. Promise.all never reaches firstFailure, and apiGet has no timeout, so
// the last-successful snapshot would otherwise stay up without the outage.
for (const failedFeed of ["runtime", "events", "operations", "logs", "workspace"] as const) {
  test(`detail ${failedFeed} outage warns without waiting for a hanging sibling`, async ({
    page,
  }) => {
    test.setTimeout(45_000);
    let detailMode: "ok" | "split" = "ok";
    const hangingSibling: Array<() => Promise<void>> = [];
    const workspaceId = `ws_settled_${failedFeed}_outage`;
    const retained = {
      branch: "settled-outage-branch-keep",
      runtime: "awf-ws-settled-outage-runtime",
      event: "settled-outage-event-keep",
      operation: "settled-outage-operation-keep",
      stream: "settled-outage-stream-keep",
    };
    const outageMessage = `${failedFeed} outage while sibling hangs`;
    const hangFeed = failedFeed === "runtime" ? "events" : "runtime";
    const overviewItem = {
      workspace_id: workspaceId,
      title: "Settled detail outage workspace",
      repo_url: "https://github.com/example/settled-detail-outage",
      base_branch: "main",
      branch_name: "overview-branch",
      agent: "codex",
      agent_model: "gpt-5.5",
      status: "running",
      created_at: "2026-09-06T17:00:00Z",
      updated_at: "2026-09-06T17:00:00Z",
      task_prompt: "Warn when a detail feed fails before siblings settle",
      lifecycle: [],
      llm_usage: null,
      recovery: null,
    };
    const workspaceBody = {
      ...overviewItem,
      id: workspaceId,
      version: 1,
      branch_name: retained.branch,
    };
    const runtimeBody = {
      workspace_id: workspaceId,
      compose_project_name: retained.runtime,
      stack_state: "running",
      services: [],
      app_endpoints: [],
      logs_available: true,
      control_available: true,
      reason: null,
    };
    const eventsBody = {
      items: [
        {
          id: "evt_settled_outage",
          workspace_id: workspaceId,
          event_type: retained.event,
          old_state: null,
          new_state: "running",
          reason_code: null,
          payload: null,
          occurred_at: "2026-09-06T17:00:00Z",
        },
      ],
      next_cursor: null,
      has_more: false,
    };
    const operationsBody = {
      items: [
        {
          id: "op_settled_outage",
          workspace_id: workspaceId,
          type: "refresh",
          status: "succeeded",
          error_code: null,
          error_message: null,
          payload: null,
          result: null,
          idempotency_key: null,
          created_at: "2026-09-06T17:00:00Z",
          started_at: "2026-09-06T17:00:00Z",
          finished_at: "2026-09-06T17:00:01Z",
          owner: null,
          source: null,
          action: null,
          pr_number: null,
          pr_url: null,
          source_head_sha: null,
          source_base_sha: null,
          reason: retained.operation,
          reason_code: null,
          failure_code: null,
          failure_message: null,
          log_stream_refs: {},
          log_stream_ids: [],
        },
      ],
      next_cursor: null,
      has_more: false,
    };
    const logsBody = {
      items: [
        {
          stream_id: retained.stream,
          source: "agent",
          name: retained.stream,
          kind: "stdout",
          path: "/tmp/settled-outage.log",
          byte_count: 4,
          line_count: 1,
          opened_at: "2026-09-06T17:00:00Z",
          closed_at: null,
        },
      ],
      next_cursor: null,
      has_more: false,
    };

    const feedPath = (feed: typeof failedFeed) => {
      if (feed === "workspace") {
        return `/api/awf/workspaces/${workspaceId}`;
      }
      return `/api/awf/workspaces/${workspaceId}/${feed}`;
    };
    const successBody = (feed: typeof failedFeed) => {
      if (feed === "workspace") {
        return workspaceBody;
      }
      if (feed === "runtime") {
        return runtimeBody;
      }
      if (feed === "events") {
        return eventsBody;
      }
      if (feed === "operations") {
        return operationsBody;
      }
      return logsBody;
    };
    const retainedText = {
      workspace: retained.branch,
      runtime: retained.runtime,
      events: retained.event,
      operations: retained.operation,
      logs: retained.stream,
    }[failedFeed];

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
      const detailFeeds = ["workspace", "runtime", "events", "operations", "logs"] as const;
      const matched = detailFeeds.find((feed) => path === feedPath(feed));
      if (matched) {
        if (detailMode === "split" && matched === failedFeed) {
          await fulfillJson(
            route,
            { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
            503,
          );
          return;
        }
        if (detailMode === "split" && matched === hangFeed) {
          await new Promise<void>((resolve) => {
            hangingSibling.push(async () => {
              await fulfillJson(route, successBody(matched));
              resolve();
            });
          });
          return;
        }
        await fulfillJson(route, successBody(matched));
        return;
      }
      await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
    });

    try {
      await page.goto("/");
      await waitForConsoleReady(page);
      await page.getByTestId(`workspace-card-${workspaceId}`).click();
      const inspector = page.locator(".fixed.inset-y-0.right-0").first();
      await expect(inspector.getByText(retainedText, { exact: true }).first()).toBeVisible({
        timeout: 10_000,
      });

      detailMode = "split";
      await page.getByRole("button", { name: /refresh/i }).click({ force: true });
      await expect.poll(() => hangingSibling.length, { timeout: 10_000 }).toBe(1);

      // The failed feed has settled; the sibling is still pending. Waiting for
      // Promise.all would leave the last-successful snapshot looking current.
      await expect(inspector.getByText(outageMessage)).toBeVisible({ timeout: 15_000 });
      await expect(inspector.getByText(retainedText, { exact: true }).first()).toBeVisible();
    } finally {
      await hangingSibling[0]?.();
    }
  });
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gG7PJ: a newer feed
// success must clear the outage banner even if a sibling never settles.
// Promise.all never reaches setError(null), and a late older 5xx must not
// restamp the warning that recovery already replaced.
test("recovered detail feed clears outage banner while a sibling hangs", async ({ page }) => {
  test.setTimeout(45_000);
  let detailPhase: "bootstrap" | "outage" | "recover" = "bootstrap";
  const hangingEvents: Route[] = [];
  const hangingOperations: Route[] = [];
  const workspaceId = "ws_recovered_feed_clears_outage";
  const initialRuntime = "initial-runtime-before-outage";
  const recoveredRuntime = "recovered-runtime-after-outage";
  const outageMessage = "runtime outage while newer refresh hangs";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Recovered feed must clear outage banner",
    repo_url: "https://github.com/example/recovered-feed-outage",
    base_branch: "main",
    branch_name: "recovered-outage-branch",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Clear a recovered detail outage while a sibling hangs",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };
  const workspaceBody = {
    ...overviewItem,
    id: workspaceId,
    version: 1,
  };
  const runtimeBody = (composeProject: string) => ({
    workspace_id: workspaceId,
    compose_project_name: composeProject,
    stack_state: "running",
    services: [],
    app_endpoints: [],
    logs_available: true,
    control_available: true,
    reason: null,
  });
  const emptyList = { items: [], next_cursor: null, has_more: false };

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
    if (
      path === `/api/awf/workspaces/${workspaceId}` ||
      path === `/api/awf/workspaces/${workspaceId}/runtime` ||
      path === `/api/awf/workspaces/${workspaceId}/events` ||
      path === `/api/awf/workspaces/${workspaceId}/operations` ||
      path === `/api/awf/workspaces/${workspaceId}/logs`
    ) {
      await route.fallback();
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

  await page.route(`**/api/awf/workspaces/${workspaceId}**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceBody);
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      if (detailPhase === "outage") {
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
          503,
        );
        return;
      }
      await fulfillJson(
        route,
        runtimeBody(detailPhase === "recover" ? recoveredRuntime : initialRuntime),
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      if (detailPhase === "outage") {
        hangingEvents.push(route);
        return;
      }
      await fulfillJson(route, emptyList);
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      if (detailPhase === "recover") {
        hangingOperations.push(route);
        return;
      }
      await fulfillJson(route, emptyList);
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, emptyList);
      return;
    }
    await route.fallback();
  });

  try {
    await page.goto("/");
    await waitForConsoleReady(page);
    await page.getByTestId(`workspace-card-${workspaceId}`).click();
    const inspector = page.locator(".fixed.inset-y-0.right-0").first();
    await expect(inspector.getByText(initialRuntime, { exact: true })).toBeVisible({
      timeout: 10_000,
    });

    detailPhase = "outage";
    await page.getByRole("button", { name: /refresh/i }).click({ force: true });
    await expect.poll(() => hangingEvents.length, { timeout: 10_000 }).toBe(1);
    await expect(inspector.getByText(outageMessage)).toBeVisible({ timeout: 15_000 });
    await expect(inspector.getByText(initialRuntime, { exact: true })).toBeVisible();

    detailPhase = "recover";
    // A Playwright click while the older GET is pending does not dispatch the
    // React refresh handler. A DOM click still starts the newer generation.
    await page.locator("header").getByRole("button", { name: "Refresh" }).evaluate((button) => {
      (button as HTMLButtonElement).click();
    });
    await expect(inspector.getByText(recoveredRuntime, { exact: true })).toBeVisible({
      timeout: 10_000,
    });
    await expect.poll(() => hangingOperations.length, { timeout: 10_000 }).toBe(1);
    await expect(inspector.getByText(outageMessage)).toHaveCount(0);

    // The older outage already stamped. Releasing the hung sibling of that
    // generation must not put the recovered feed back into outage.
    await fulfillJson(hangingEvents[0], emptyList);
    await page.waitForTimeout(500);
    await expect(inspector.getByText(recoveredRuntime, { exact: true })).toBeVisible();
    await expect(inspector.getByText(outageMessage)).toHaveCount(0);
  } finally {
    await fulfillJson(hangingEvents[0], emptyList).catch(() => undefined);
    await fulfillJson(hangingOperations[0], emptyList).catch(() => undefined);
  }
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

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gDKfP: /workspaces/{id}
// 401/403 clears detail.workspace but must also latch the denial and close the
// inspector EventSource. A later snapshot must not write frame.workspace back
// while workspace_stream stays advertised. A successful detail read recovers it.
for (const deniedStatus of [401, 403] as const) {
  test(`base workspace detail ${deniedStatus} closes live stream until a successful detail read`, async ({
    page,
  }) => {
    test.setTimeout(45_000);
    let detailMode: "ok" | "denied" = "ok";
    let streamConnections = 0;
    let releaseHeldStream: () => void = () => undefined;
    const heldStream = new Promise<void>((resolve) => {
      releaseHeldStream = resolve;
    });
    const workspaceId = "ws_base_detail_stream_denial";
    const overviewBranch = "overview-keep-branch";
    const authorizedBranch = "authorized-detail-branch";
    const revokedSnapshotBranch = "revoked-snapshot-branch-must-not-appear";
    const overviewItem = {
      workspace_id: workspaceId,
      title: "Base detail stream denial workspace",
      repo_url: "https://github.com/example/base-detail-stream-denial",
      base_branch: "main",
      branch_name: overviewBranch,
      agent: "codex",
      agent_model: "gpt-5.5",
      status: "running",
      created_at: "2026-09-06T17:00:00Z",
      updated_at: "2026-09-06T17:00:00Z",
      task_prompt: "Close the inspector stream when base workspace detail is denied",
      lifecycle: [],
      llm_usage: null,
      recovery: null,
    };
    const authorizedWorkspace = {
      ...overviewItem,
      id: workspaceId,
      version: 3,
      branch_name: authorizedBranch,
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
        if (detailMode === "denied") {
          await fulfillJson(
            route,
            {
              detail: {
                error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
                message: "workspace detail permission revoked",
              },
            },
            deniedStatus,
          );
          return;
        }
        await fulfillJson(route, authorizedWorkspace);
        return;
      }
      if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
        await fulfillJson(route, {
          workspace_id: workspaceId,
          compose_project_name: "awf-ws-base-detail-stream",
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
      if (
        path === `/api/awf/workspaces/${workspaceId}/operations` ||
        path === `/api/awf/workspaces/${workspaceId}/logs`
      ) {
        await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
        return;
      }
      if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
        streamConnections += 1;
        if (streamConnections === 1) {
          // Hold the already-open inspector EventSource until base-detail denial
          // is applied, then deliver a snapshot. workspace_stream stays advertised.
          await heldStream;
          const snapshot = {
            type: "snapshot",
            workspace: {
              ...authorizedWorkspace,
              branch_name: revokedSnapshotBranch,
              task_prompt: revokedSnapshotBranch,
            },
          };
          await route.fulfill({
            status: 200,
            headers: {
              "content-type": "text/event-stream; charset=utf-8",
              "cache-control": "no-cache",
            },
            body: `data: ${JSON.stringify(snapshot)}\n\n`,
          });
          return;
        }
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
    const inspector = page.locator(".fixed.inset-y-0.right-0").first();
    await expect(inspector.getByText(authorizedBranch, { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect.poll(() => streamConnections, { timeout: 10_000 }).toBeGreaterThan(0);

    detailMode = "denied";
    await page.getByRole("button", { name: /refresh/i }).click({ force: true });
    await expect(inspector.getByText(/workspace detail permission revoked/i)).toBeVisible({
      timeout: 15_000,
    });
    await expect(inspector.getByText(authorizedBranch, { exact: true })).toHaveCount(0);
    await expect(page.getByText("Stream: idle")).toBeVisible();

    const connectionsAtDenial = streamConnections;
    releaseHeldStream();
    await expect(page.getByText(revokedSnapshotBranch)).toHaveCount(0);
    await page.waitForTimeout(1_500);
    await expect(page.getByText(revokedSnapshotBranch)).toHaveCount(0);
    await expect(inspector.getByText(authorizedBranch, { exact: true })).toHaveCount(0);
    await expect(page.getByText("Stream: idle")).toBeVisible();
    expect(streamConnections).toBe(connectionsAtDenial);

    detailMode = "ok";
    await page.getByRole("button", { name: /refresh/i }).click({ force: true });
    await expect(inspector.getByText(authorizedBranch, { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(inspector.getByText(/workspace detail permission revoked/i)).toHaveCount(0);
    await expect.poll(() => streamConnections, { timeout: 10_000 }).toBeGreaterThan(connectionsAtDenial);
    await expect(page.getByText("Stream: idle")).toHaveCount(0);
    await expect(page.getByText(revokedSnapshotBranch)).toHaveCount(0);
  });
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gD09A: /workspaces/{id}/events
// 401/403 clears detail.events, but live event frames must not refill the Events
// panel while workspace_stream stays advertised. Latch the denial and ignore the
// event channel until a successful /events read recovers it.
for (const deniedStatus of [401, 403] as const) {
  test(`events feed ${deniedStatus} ignores live event frames until the events feed recovers`, async ({
    page,
  }) => {
    test.setTimeout(45_000);
    let eventsMode: "ok" | "denied" = "ok";
    let streamConnections = 0;
    let releaseDeniedStream: () => void = () => undefined;
    const heldDeniedStream = new Promise<void>((resolve) => {
      releaseDeniedStream = resolve;
    });
    let releaseRecoveredStream: () => void = () => undefined;
    const heldRecoveredStream = new Promise<void>((resolve) => {
      releaseRecoveredStream = resolve;
    });
    const workspaceId = `ws_events_feed_stream_denial_${deniedStatus}`;
    const authorizedEvent = "authorized-events-feed-marker";
    const revokedLiveEvent = "revoked-live-event-must-not-appear";
    const recoveredLiveEvent = "recovered-live-event-may-appear";
    const overviewItem = {
      workspace_id: workspaceId,
      title: "Events feed stream denial workspace",
      repo_url: "https://github.com/example/events-feed-stream-denial",
      base_branch: "main",
      branch_name: "events-feed-overview-branch",
      agent: "codex",
      agent_model: "gpt-5.5",
      status: "running",
      created_at: "2026-09-06T17:00:00Z",
      updated_at: "2026-09-06T17:00:00Z",
      task_prompt: "Ignore live events after the events feed is denied",
      lifecycle: [],
      llm_usage: null,
      recovery: null,
    };

    const eventItem = (eventType: string, id: string) => ({
      id,
      workspace_id: workspaceId,
      event_type: eventType,
      old_state: null,
      new_state: "running",
      reason_code: null,
      payload: null,
      occurred_at: "2026-09-06T17:00:00Z",
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
        await fulfillJson(route, { items: [overviewItem], next_cursor: null, has_more: false });
        return;
      }
      if (path === `/api/awf/workspaces/${workspaceId}`) {
        await fulfillJson(route, { ...overviewItem, id: workspaceId, version: 3 });
        return;
      }
      if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
        await fulfillJson(route, {
          workspace_id: workspaceId,
          compose_project_name: "awf-ws-events-feed-stream",
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
        if (eventsMode === "denied") {
          await fulfillJson(
            route,
            {
              detail: {
                error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
                message: "events feed permission revoked",
              },
            },
            deniedStatus,
          );
          return;
        }
        await fulfillJson(route, {
          items: [eventItem(authorizedEvent, "evt_events_feed_authorized")],
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
        streamConnections += 1;
        const connection = streamConnections;
        if (connection === 1) {
          // Hold the already-open inspector EventSource until event-feed denial
          // is applied, then deliver an event frame. workspace_stream stays advertised.
          await heldDeniedStream;
          await route.fulfill({
            status: 200,
            headers: {
              "content-type": "text/event-stream; charset=utf-8",
              "cache-control": "no-cache",
            },
            body: `data: ${JSON.stringify({
              type: "event",
              event: eventItem(revokedLiveEvent, "evt_events_feed_revoked_live"),
            })}\n\n`,
          });
          return;
        }
        await heldRecoveredStream;
        await route.fulfill({
          status: 200,
          headers: {
            "content-type": "text/event-stream; charset=utf-8",
            "cache-control": "no-cache",
          },
          body: `data: ${JSON.stringify({
            type: "event",
            event: eventItem(recoveredLiveEvent, "evt_events_feed_recovered_live"),
          })}\n\n`,
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
    const inspector = page.locator(".fixed.inset-y-0.right-0").first();
    await expect(inspector.getByText(authorizedEvent, { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect.poll(() => streamConnections, { timeout: 10_000 }).toBeGreaterThan(0);

    eventsMode = "denied";
    await page.getByRole("button", { name: /refresh/i }).click({ force: true });
    await expect(inspector.getByText(/events feed permission revoked/i)).toBeVisible({
      timeout: 15_000,
    });
    await expect(inspector.getByText(authorizedEvent, { exact: true })).toHaveCount(0);
    await expect(inspector.getByText("No events recorded.")).toBeVisible();
    // Event-feed denial must not close the inspector EventSource. Snapshots and
    // logs remain authorized while workspace_stream stays advertised.
    await expect(page.getByText("Stream: idle")).toHaveCount(0);

    const connectionsAtDenial = streamConnections;
    releaseDeniedStream();
    await expect(page.getByText(revokedLiveEvent)).toHaveCount(0);
    await page.waitForTimeout(1_500);
    await expect(page.getByText(revokedLiveEvent)).toHaveCount(0);
    await expect(inspector.getByText(authorizedEvent, { exact: true })).toHaveCount(0);
    await expect(inspector.getByText("No events recorded.")).toBeVisible();

    await expect.poll(() => streamConnections, { timeout: 10_000 }).toBeGreaterThan(connectionsAtDenial);

    eventsMode = "ok";
    await page.getByRole("button", { name: /refresh/i }).click({ force: true });
    await expect(inspector.getByText(authorizedEvent, { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(inspector.getByText(/events feed permission revoked/i)).toHaveCount(0);

    releaseRecoveredStream();
    await expect(inspector.getByText(recoveredLiveEvent, { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(revokedLiveEvent)).toHaveCount(0);
  });
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gD08-: a /workspaces/{id}
// 401/403 must latch denial and close the inspector EventSource without waiting
// for a sibling runtime/events/operations request. Promise.all never settled
// while that sibling hung, so cached workspace metadata stayed visible.
for (const deniedStatus of [401, 403] as const) {
  test(`base workspace detail ${deniedStatus} closes live stream without waiting for a hanging sibling`, async ({
    page,
  }) => {
    test.setTimeout(45_000);
    let detailMode: "ok" | "split" = "ok";
    let streamConnections = 0;
    let releaseHeldStream: () => void = () => undefined;
    const heldStream = new Promise<void>((resolve) => {
      releaseHeldStream = resolve;
    });
    const hangingSibling: Array<() => Promise<void>> = [];
    const workspaceId = "ws_base_detail_hanging_sibling";
    const overviewBranch = "overview-keep-branch";
    const authorizedBranch = "authorized-detail-branch";
    const revokedSnapshotBranch = "revoked-snapshot-branch-must-not-appear";
    const siblingAfterDenial = "hanging-runtime-must-not-restore-workspace";
    const overviewItem = {
      workspace_id: workspaceId,
      title: "Hanging sibling base detail denial workspace",
      repo_url: "https://github.com/example/base-detail-hanging-sibling",
      base_branch: "main",
      branch_name: overviewBranch,
      agent: "codex",
      agent_model: "gpt-5.5",
      status: "running",
      created_at: "2026-09-06T17:00:00Z",
      updated_at: "2026-09-06T17:00:00Z",
      task_prompt: "Close the inspector stream when base detail is denied before siblings settle",
      lifecycle: [],
      llm_usage: null,
      recovery: null,
    };
    const authorizedWorkspace = {
      ...overviewItem,
      id: workspaceId,
      version: 3,
      branch_name: authorizedBranch,
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
        if (detailMode === "split") {
          await fulfillJson(
            route,
            {
              detail: {
                error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
                message: "workspace detail permission revoked",
              },
            },
            deniedStatus,
          );
          return;
        }
        await fulfillJson(route, authorizedWorkspace);
        return;
      }
      if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
        if (detailMode === "split") {
          await new Promise<void>((resolve) => {
            hangingSibling.push(async () => {
              await fulfillJson(route, {
                workspace_id: workspaceId,
                compose_project_name: siblingAfterDenial,
                stack_state: "running",
                services: [],
                app_endpoints: [],
                logs_available: true,
                control_available: true,
                reason: null,
              });
              resolve();
            });
          });
          return;
        }
        await fulfillJson(route, {
          workspace_id: workspaceId,
          compose_project_name: "awf-ws-base-detail-hanging-sibling",
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
      if (
        path === `/api/awf/workspaces/${workspaceId}/operations` ||
        path === `/api/awf/workspaces/${workspaceId}/logs`
      ) {
        await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
        return;
      }
      if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
        streamConnections += 1;
        if (streamConnections === 1) {
          await heldStream;
          const snapshot = {
            type: "snapshot",
            workspace: {
              ...authorizedWorkspace,
              branch_name: revokedSnapshotBranch,
              task_prompt: revokedSnapshotBranch,
            },
          };
          await route.fulfill({
            status: 200,
            headers: {
              "content-type": "text/event-stream; charset=utf-8",
              "cache-control": "no-cache",
            },
            body: `data: ${JSON.stringify(snapshot)}\n\n`,
          });
          return;
        }
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
    const inspector = page.locator(".fixed.inset-y-0.right-0").first();
    await expect(inspector.getByText(authorizedBranch, { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect.poll(() => streamConnections, { timeout: 10_000 }).toBeGreaterThan(0);

    detailMode = "split";
    await page.getByRole("button", { name: /refresh/i }).click({ force: true });
    await expect.poll(() => hangingSibling.length, { timeout: 10_000 }).toBe(1);

    // Runtime is still pending. Denial must already have cleared workspace
    // metadata and closed /stream; waiting for Promise.all would leave both.
    await expect(inspector.getByText(/workspace detail permission revoked/i)).toBeVisible({
      timeout: 15_000,
    });
    await expect(inspector.getByText(authorizedBranch, { exact: true })).toHaveCount(0);
    await expect(page.getByText("Stream: idle")).toBeVisible();

    const connectionsAtDenial = streamConnections;
    releaseHeldStream();
    await expect(page.getByText(revokedSnapshotBranch)).toHaveCount(0);
    await page.waitForTimeout(1_500);
    await expect(page.getByText(revokedSnapshotBranch)).toHaveCount(0);
    await expect(inspector.getByText(authorizedBranch, { exact: true })).toHaveCount(0);
    await expect(page.getByText("Stream: idle")).toBeVisible();
    expect(streamConnections).toBe(connectionsAtDenial);

    await hangingSibling[0]();
    await expect(inspector.getByText(/workspace detail permission revoked/i)).toBeVisible();
    await expect(inspector.getByText(authorizedBranch, { exact: true })).toHaveCount(0);
    await expect(page.getByText("Stream: idle")).toBeVisible();
    expect(streamConnections).toBe(connectionsAtDenial);
  });
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gDcDc: a newer Refresh
// starting another detail load must not drop an older /workspaces/{id} 401/403.
// Capability loads already treat 401/403 as authoritative unless a newer
// success has applied. If the newer request hangs or fails transiently, the
// inspector EventSource must still close and a snapshot must not write
// revoked workspace metadata back.
test("superseded base workspace detail 403 closes live stream while a newer refresh hangs", async ({
  page,
}) => {
  test.setTimeout(45_000);
  let detailMode: "ok" | "hold-deny" | "hang" = "ok";
  let streamConnections = 0;
  let releaseHeldStream: () => void = () => undefined;
  const heldStream = new Promise<void>((resolve) => {
    releaseHeldStream = resolve;
  });
  const heldDeny: Array<() => Promise<void>> = [];
  const hanging: Array<() => Promise<void>> = [];
  const workspaceId = "ws_base_detail_superseded_denial";
  const overviewBranch = "overview-keep-branch";
  const authorizedBranch = "authorized-detail-branch";
  const revokedSnapshotBranch = "revoked-snapshot-branch-must-not-appear";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Superseded base detail denial workspace",
    repo_url: "https://github.com/example/base-detail-superseded-denial",
    base_branch: "main",
    branch_name: overviewBranch,
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Older base workspace detail 403 must close the inspector stream",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };
  const authorizedWorkspace = {
    ...overviewItem,
    id: workspaceId,
    version: 3,
    branch_name: authorizedBranch,
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
      if (detailMode === "hold-deny") {
        await new Promise<void>((resolve) => {
          heldDeny.push(async () => {
            await fulfillJson(
              route,
              {
                detail: {
                  error_code: "FORBIDDEN",
                  message: "workspace detail permission revoked",
                },
              },
              403,
            );
            resolve();
          });
        });
        return;
      }
      if (detailMode === "hang") {
        await new Promise<void>((resolve) => {
          hanging.push(async () => {
            await fulfillJson(route, { detail: { message: "workspace detail outage" } }, 503);
            resolve();
          });
        });
        return;
      }
      await fulfillJson(route, authorizedWorkspace);
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, {
        workspace_id: workspaceId,
        compose_project_name: "awf-ws-base-detail-superseded",
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
    if (
      path === `/api/awf/workspaces/${workspaceId}/operations` ||
      path === `/api/awf/workspaces/${workspaceId}/logs`
    ) {
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

  // Dedicated route so holding the inspector EventSource does not block the
  // overlapping /workspaces/{id} GET that this race depends on. After the
  // superseded 403 latches, a snapshot on the still-open source must not
  // write revoked workspace metadata back.
  await page.route(`**/api/awf/workspaces/${workspaceId}/stream*`, async (route) => {
    streamConnections += 1;
    const connection = streamConnections;
    const snapshot = {
      type: "snapshot",
      workspace: {
        ...authorizedWorkspace,
        branch_name: revokedSnapshotBranch,
        task_prompt: revokedSnapshotBranch,
      },
    };
    if (connection === 1) {
      await heldStream;
    }
    await route.fulfill({
      status: 200,
      headers: {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-cache",
      },
      body: `data: ${JSON.stringify(
        connection === 1
          ? snapshot
          : { type: "connected", workspace_id: workspaceId },
      )}\n\n`,
    });
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector.getByText(authorizedBranch, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect.poll(() => streamConnections, { timeout: 10_000 }).toBeGreaterThan(0);
  await expect(page.getByText("Stream: idle")).toHaveCount(0);

  detailMode = "hold-deny";
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect.poll(() => heldDeny.length, { timeout: 10_000 }).toBe(1);

  // A second matching GET is not delivered to this mock while the first
  // handler is awaiting. The refresh still advances detail generation before
  // that GET, so releasing the older 403 must latch denial before the newer
  // request is allowed to fail transiently.
  detailMode = "hang";
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });

  await heldDeny[0]();
  await expect(inspector.getByText(/workspace detail permission revoked/i)).toBeVisible({
    timeout: 15_000,
  });
  await expect(inspector.getByText(authorizedBranch, { exact: true })).toHaveCount(0);
  await expect(page.getByText("Stream: idle")).toBeVisible();

  const connectionsAtDenial = streamConnections;
  releaseHeldStream();
  await expect(page.getByText(revokedSnapshotBranch)).toHaveCount(0);
  await page.waitForTimeout(1_500);
  await expect(page.getByText(revokedSnapshotBranch)).toHaveCount(0);
  await expect(inspector.getByText(authorizedBranch, { exact: true })).toHaveCount(0);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  expect(streamConnections).toBe(connectionsAtDenial);

  await expect.poll(() => hanging.length, { timeout: 10_000 }).toBe(1);
  await hanging[0]();
  await page.waitForTimeout(500);
  await expect(inspector.getByText(/workspace detail permission revoked/i)).toBeVisible();
  await expect(page.getByText(/workspace detail outage/i)).toHaveCount(0);
  await expect(inspector.getByText(authorizedBranch, { exact: true })).toHaveCount(0);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  expect(streamConnections).toBe(connectionsAtDenial);

  detailMode = "ok";
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect(inspector.getByText(authorizedBranch, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(inspector.getByText(/workspace detail permission revoked/i)).toHaveCount(0);
  await expect.poll(() => streamConnections, { timeout: 10_000 }).toBeGreaterThan(connectionsAtDenial);
  await expect(page.getByText("Stream: idle")).toHaveCount(0);
  await expect(page.getByText(revokedSnapshotBranch)).toHaveCount(0);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gEc6I: an older
// /workspaces/{id} or /events 200 that settles after a newer success must not
// replace the inspector snapshot. The immediate handlers record applied* only
// when that generation still owns the feed; writing afterward sticks while a
// sibling hang keeps Promise.all from repairing the overwrite.
test("older settled workspace and events success does not overwrite a newer snapshot while a sibling hangs", async ({
  page,
}) => {
  test.setTimeout(45_000);
  let detailPhase: "bootstrap" | "hold-older" | "newer-hang" = "bootstrap";
  // Stash routes and return without awaiting. Awaiting inside the handler
  // blocks the next matching GET, so the newer success could never settle first.
  const olderWorkspace: Route[] = [];
  const olderEvents: Route[] = [];
  const hangingRuntime: Route[] = [];
  const workspaceId = "ws_older_success_must_not_clobber";
  const overviewBranch = "overview-older-success-branch";
  const initialBranch = "initial-detail-branch";
  const staleBranch = "stale-older-success-branch";
  const freshBranch = "fresh-newer-success-branch";
  const initialEvent = "initial-detail-event";
  const staleEvent = "stale-older-event";
  const freshEvent = "fresh-newer-event";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Older success must not clobber newer detail",
    repo_url: "https://github.com/example/older-success-clobber",
    base_branch: "main",
    branch_name: overviewBranch,
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Keep the newer inspector snapshot when an older success settles",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };

  const workspaceBody = (branchName: string) => ({
    ...overviewItem,
    id: workspaceId,
    version: 3,
    branch_name: branchName,
  });
  const eventItem = (eventType: string) => ({
    id: `evt-${eventType}`,
    workspace_id: workspaceId,
    event_type: eventType,
    old_state: null,
    new_state: "running",
    reason_code: null,
    payload: null,
    occurred_at: "2026-09-06T17:00:00Z",
    created_at: "2026-09-06T17:00:00Z",
  });
  const runtimeBody = {
    workspace_id: workspaceId,
    compose_project_name: "awf-ws-older-success-clobber",
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
    if (
      path === `/api/awf/workspaces/${workspaceId}` ||
      path === `/api/awf/workspaces/${workspaceId}/events` ||
      path === `/api/awf/workspaces/${workspaceId}/runtime`
    ) {
      await route.fallback();
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

  // Registered after the catch-all so these GETs are not stuck behind an
  // awaiting handler. Returning without fulfill keeps the older request
  // pending while the newer generation's 200 can still settle.
  await page.route(`**/api/awf/workspaces/${workspaceId}**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      if (detailPhase === "hold-older") {
        olderWorkspace.push(route);
        return;
      }
      await fulfillJson(
        route,
        workspaceBody(detailPhase === "newer-hang" ? freshBranch : initialBranch),
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      if (detailPhase === "hold-older") {
        olderEvents.push(route);
        return;
      }
      await fulfillJson(route, {
        items: [eventItem(detailPhase === "newer-hang" ? freshEvent : initialEvent)],
        next_cursor: null,
        has_more: false,
      });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      if (detailPhase === "newer-hang") {
        hangingRuntime.push(route);
        return;
      }
      await fulfillJson(route, runtimeBody);
      return;
    }
    await route.fallback();
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector.getByText(initialBranch, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(inspector.getByText(initialEvent, { exact: true })).toHaveCount(1);

  detailPhase = "hold-older";
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect.poll(() => olderWorkspace.length, { timeout: 10_000 }).toBe(1);
  await expect.poll(() => olderEvents.length, { timeout: 10_000 }).toBe(1);

  detailPhase = "newer-hang";
  // A Playwright click while the older GET is pending does not dispatch the
  // React refresh handler. A DOM click still starts the newer generation so
  // its 200 can settle before the held older success.
  await page.locator("header").getByRole("button", { name: "Refresh" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
  });
  await expect(inspector.getByText(freshBranch, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(inspector.getByText(freshEvent, { exact: true })).toHaveCount(1);
  await expect.poll(() => hangingRuntime.length, { timeout: 10_000 }).toBe(1);

  await fulfillJson(olderWorkspace[0], workspaceBody(staleBranch));
  await fulfillJson(olderEvents[0], {
    items: [eventItem(staleEvent)],
    next_cursor: null,
    has_more: false,
  });
  await page.waitForTimeout(1_500);
  await expect(inspector.getByText(freshBranch, { exact: true })).toBeVisible();
  await expect(inspector.getByText(staleBranch, { exact: true })).toHaveCount(0);
  await expect(inspector.getByText(freshEvent, { exact: true })).toHaveCount(1);
  await expect(inspector.getByText(staleEvent, { exact: true })).toHaveCount(0);

  // The newer refresh's hanging runtime later settles, so Promise.all merges
  // that generation. That merge must not treat a declined recovery as a
  // payload write, and the older success must still be absent.
  await fulfillJson(hangingRuntime[0], runtimeBody);
  await expect(inspector.getByText(freshBranch, { exact: true })).toBeVisible();
  await expect(inspector.getByText(staleBranch, { exact: true })).toHaveCount(0);
  await expect(inspector.getByText(freshEvent, { exact: true })).toHaveCount(1);
  await expect(inspector.getByText(staleEvent, { exact: true })).toHaveCount(0);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gEc6M: a late older
// runtime/operations 401/403 must not clear a newer recovered snapshot, and an
// in-flight denial after those feeds are withdrawn must not stamp a detail
// error that no later read of the withdrawn feed will clear.
test("older runtime and operations denial does not clear a newer recovered snapshot", async ({
  page,
}) => {
  test.setTimeout(45_000);
  let detailPhase: "bootstrap" | "hold-older" | "newer-hang" = "bootstrap";
  const olderRuntime: Route[] = [];
  const olderOperations: Route[] = [];
  const hangingEvents: Route[] = [];
  const workspaceId = "ws_optional_denial_recovered";
  const initialRuntime = "initial-optional-runtime-project";
  const recoveredRuntime = "recovered-optional-runtime-project";
  const initialOperation = "initial-optional-operation-reason";
  const recoveredOperation = "recovered-optional-operation-reason";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Optional denial must not wipe recovered feeds",
    repo_url: "https://github.com/example/optional-denial-recovered",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Keep recovered runtime and operations when an older denial settles",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };

  const runtimeBody = (composeProject: string) => ({
    workspace_id: workspaceId,
    compose_project_name: composeProject,
    stack_state: "running",
    services: [],
    app_endpoints: [],
    logs_available: true,
    control_available: true,
    reason: null,
  });
  const operationBody = (reason: string) => ({
    items: [
      {
        id: `op-${reason}`,
        workspace_id: workspaceId,
        type: "execute",
        status: "running",
        error_code: null,
        error_message: null,
        payload: null,
        result: null,
        idempotency_key: null,
        created_at: "2026-09-06T17:00:00Z",
        started_at: "2026-09-06T17:00:00Z",
        finished_at: null,
        owner: "worker",
        source: "awf",
        action: null,
        pr_number: null,
        pr_url: null,
        source_head_sha: null,
        source_base_sha: null,
        reason,
        reason_code: null,
        failure_code: null,
        failure_message: null,
        log_stream_refs: {},
        log_stream_ids: [],
      },
    ],
    next_cursor: null,
    has_more: false,
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
      await fulfillJson(route, { items: [overviewItem], next_cursor: null, has_more: false });
      return;
    }
    if (
      path === `/api/awf/workspaces/${workspaceId}` ||
      path === `/api/awf/workspaces/${workspaceId}/events` ||
      path === `/api/awf/workspaces/${workspaceId}/runtime` ||
      path === `/api/awf/workspaces/${workspaceId}/operations`
    ) {
      await route.fallback();
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
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

  await page.route(`**/api/awf/workspaces/${workspaceId}**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, { ...overviewItem, id: workspaceId, version: 2 });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      if (detailPhase === "newer-hang") {
        hangingEvents.push(route);
        return;
      }
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      if (detailPhase === "hold-older") {
        olderRuntime.push(route);
        return;
      }
      await fulfillJson(
        route,
        runtimeBody(detailPhase === "newer-hang" ? recoveredRuntime : initialRuntime),
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      if (detailPhase === "hold-older") {
        olderOperations.push(route);
        return;
      }
      await fulfillJson(
        route,
        operationBody(detailPhase === "newer-hang" ? recoveredOperation : initialOperation),
      );
      return;
    }
    await route.fallback();
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector.getByText(initialRuntime, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(inspector.getByText(initialOperation, { exact: true })).toBeVisible();

  detailPhase = "hold-older";
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect.poll(() => olderRuntime.length, { timeout: 10_000 }).toBe(1);
  await expect.poll(() => olderOperations.length, { timeout: 10_000 }).toBe(1);

  detailPhase = "newer-hang";
  await page.locator("header").getByRole("button", { name: "Refresh" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
  });
  await expect(inspector.getByText(recoveredRuntime, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(inspector.getByText(recoveredOperation, { exact: true })).toBeVisible();
  await expect.poll(() => hangingEvents.length, { timeout: 10_000 }).toBe(1);

  await fulfillJson(
    olderRuntime[0],
    { detail: { error_code: "FORBIDDEN", message: "runtime permission revoked" } },
    403,
  );
  await fulfillJson(
    olderOperations[0],
    { detail: { error_code: "FORBIDDEN", message: "operations permission revoked" } },
    403,
  );
  await page.waitForTimeout(1_500);
  await expect(inspector.getByText(recoveredRuntime, { exact: true })).toBeVisible();
  await expect(inspector.getByText(recoveredOperation, { exact: true })).toBeVisible();
  await expect(inspector.getByText(initialRuntime, { exact: true })).toHaveCount(0);
  await expect(inspector.getByText("runtime permission revoked")).toHaveCount(0);
  await expect(inspector.getByText("operations permission revoked")).toHaveCount(0);
  await expect(inspector.getByText("Runtime snapshot unavailable.")).toHaveCount(0);
  await expect(inspector.getByText("No operations recorded.")).toHaveCount(0);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gFmlJ: a later recovery
// 401/403 must not raise the runtime/operations watermark to the latest started
// generation, or an overlapping Refresh that began after the original denial
// stays cleared until another load starts.
test("later runtime and operations 401 does not block a newer in-flight refresh", async ({
  page,
}) => {
  test.setTimeout(45_000);
  let detailPhase: "bootstrap" | "first-deny" | "hold-recovery-deny" | "hold-newer-success" =
    "bootstrap";
  const recoveryRuntime: Route[] = [];
  const recoveryOperations: Route[] = [];
  const newerRuntime: Route[] = [];
  const newerOperations: Route[] = [];
  const workspaceId = "ws_optional_denial_overlap";
  const initialRuntime = "initial-overlap-runtime-project";
  const recoveredRuntime = "recovered-overlap-runtime-project";
  const initialOperation = "initial-overlap-operation-reason";
  const recoveredOperation = "recovered-overlap-operation-reason";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Later optional denial must not block a newer refresh",
    repo_url: "https://github.com/example/optional-denial-overlap",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "A newer refresh must apply after a recovery request itself 401s",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };

  const runtimeBody = (composeProject: string) => ({
    workspace_id: workspaceId,
    compose_project_name: composeProject,
    stack_state: "running",
    services: [],
    app_endpoints: [],
    logs_available: true,
    control_available: true,
    reason: null,
  });
  const operationBody = (reason: string) => ({
    items: [
      {
        id: `op-${reason}`,
        workspace_id: workspaceId,
        type: "execute",
        status: "running",
        error_code: null,
        error_message: null,
        payload: null,
        result: null,
        idempotency_key: null,
        created_at: "2026-09-06T17:00:00Z",
        started_at: "2026-09-06T17:00:00Z",
        finished_at: null,
        owner: "worker",
        source: "awf",
        action: null,
        pr_number: null,
        pr_url: null,
        source_head_sha: null,
        source_base_sha: null,
        reason,
        reason_code: null,
        failure_code: null,
        failure_message: null,
        log_stream_refs: {},
        log_stream_ids: [],
      },
    ],
    next_cursor: null,
    has_more: false,
  });
  const denied = (message: string) => ({
    detail: { error_code: "UNAUTHORIZED", message },
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
      await fulfillJson(route, { items: [overviewItem], next_cursor: null, has_more: false });
      return;
    }
    if (
      path === `/api/awf/workspaces/${workspaceId}` ||
      path === `/api/awf/workspaces/${workspaceId}/events` ||
      path === `/api/awf/workspaces/${workspaceId}/runtime` ||
      path === `/api/awf/workspaces/${workspaceId}/operations`
    ) {
      await route.fallback();
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
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

  await page.route(`**/api/awf/workspaces/${workspaceId}**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, { ...overviewItem, id: workspaceId, version: 2 });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      if (detailPhase === "first-deny") {
        await fulfillJson(route, denied("runtime permission revoked"), 401);
        return;
      }
      if (detailPhase === "hold-recovery-deny") {
        recoveryRuntime.push(route);
        return;
      }
      if (detailPhase === "hold-newer-success") {
        newerRuntime.push(route);
        return;
      }
      await fulfillJson(route, runtimeBody(initialRuntime));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      if (detailPhase === "first-deny") {
        await fulfillJson(route, denied("operations permission revoked"), 401);
        return;
      }
      if (detailPhase === "hold-recovery-deny") {
        recoveryOperations.push(route);
        return;
      }
      if (detailPhase === "hold-newer-success") {
        newerOperations.push(route);
        return;
      }
      await fulfillJson(route, operationBody(initialOperation));
      return;
    }
    await route.fallback();
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector.getByText(initialRuntime, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(inspector.getByText(initialOperation, { exact: true })).toBeVisible();

  detailPhase = "first-deny";
  await page.locator("header").getByRole("button", { name: "Refresh" }).click({ force: true });
  await expect(inspector.getByText("runtime permission revoked")).toBeVisible({ timeout: 10_000 });
  await expect(inspector.getByText("Runtime snapshot unavailable.")).toBeVisible();
  await expect(inspector.getByText("No operations recorded.")).toBeVisible();
  await expect(inspector.getByText(initialRuntime, { exact: true })).toHaveCount(0);

  detailPhase = "hold-recovery-deny";
  await page.locator("header").getByRole("button", { name: "Refresh" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
  });
  await expect.poll(() => recoveryRuntime.length, { timeout: 10_000 }).toBe(1);
  await expect.poll(() => recoveryOperations.length, { timeout: 10_000 }).toBe(1);

  detailPhase = "hold-newer-success";
  await page.locator("header").getByRole("button", { name: "Refresh" }).evaluate((button) => {
    (button as HTMLButtonElement).click();
  });
  await expect.poll(() => newerRuntime.length, { timeout: 10_000 }).toBe(1);
  await expect.poll(() => newerOperations.length, { timeout: 10_000 }).toBe(1);

  await fulfillJson(recoveryRuntime[0], denied("runtime permission still revoked"), 401);
  await fulfillJson(recoveryOperations[0], denied("operations permission still revoked"), 401);
  // Operations settles after runtime, so its denial owns the banner. Either
  // message proves this recovery 401 applied before the newer refresh is released.
  await expect(inspector.getByText("operations permission still revoked")).toBeVisible({
    timeout: 10_000,
  });
  await expect(inspector.getByText(recoveredRuntime, { exact: true })).toHaveCount(0);

  await fulfillJson(newerRuntime[0], runtimeBody(recoveredRuntime));
  await fulfillJson(newerOperations[0], operationBody(recoveredOperation));
  await expect(inspector.getByText(recoveredRuntime, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(inspector.getByText(recoveredOperation, { exact: true })).toBeVisible();
  await expect(inspector.getByText("runtime permission still revoked")).toHaveCount(0);
  await expect(inspector.getByText("runtime permission revoked")).toHaveCount(0);
  await expect(inspector.getByText("Runtime snapshot unavailable.")).toHaveCount(0);
  await expect(inspector.getByText("No operations recorded.")).toHaveCount(0);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gG7PC: a runtime or
// operations 401/403 that already applied at settlement owns the inspector
// banner. A sibling 5xx must not replace that authorization reason while
// another request still hangs, and the later Promise.all merge must not
// either — apiGet has no timeout, so the reason would otherwise never return.
for (const deniedFeed of ["runtime", "operations"] as const) {
  test(`settled ${deniedFeed} auth denial is not replaced by a sibling detail outage`, async ({
    page,
  }) => {
    test.setTimeout(45_000);
    let detailPhase: "bootstrap" | "split" = "bootstrap";
    const heldDenial: Route[] = [];
    const heldOutage: Route[] = [];
    const hangingLogs: Array<() => Promise<void>> = [];
    const workspaceId = `ws_${deniedFeed}_denial_sibling_outage`;
    const denialMessage = `${deniedFeed} permission revoked`;
    const outageMessage = "events outage while authorization is revoked";
    const runtimeProject = `${deniedFeed}-denial-runtime-project`;
    const operationReason = `${deniedFeed}-denial-operation-reason`;
    const overviewItem = {
      workspace_id: workspaceId,
      title: "Optional denial owns the banner over a sibling outage",
      repo_url: "https://github.com/example/optional-denial-sibling-outage",
      base_branch: "main",
      agent: "codex",
      agent_model: "gpt-5.5",
      status: "running",
      created_at: "2026-09-06T17:00:00Z",
      updated_at: "2026-09-06T17:00:00Z",
      task_prompt: "Keep a settled runtime or operations denial through a sibling 5xx",
      lifecycle: [],
      llm_usage: null,
      recovery: null,
    };
    const runtimeBody = {
      workspace_id: workspaceId,
      compose_project_name: runtimeProject,
      stack_state: "running",
      services: [],
      app_endpoints: [],
      logs_available: true,
      control_available: true,
      reason: null,
    };
    const operationsBody = {
      items: [
        {
          id: `op-${deniedFeed}-denial`,
          workspace_id: workspaceId,
          type: "execute",
          status: "running",
          error_code: null,
          error_message: null,
          payload: null,
          result: null,
          idempotency_key: null,
          created_at: "2026-09-06T17:00:00Z",
          started_at: "2026-09-06T17:00:00Z",
          finished_at: null,
          owner: "worker",
          source: "awf",
          action: null,
          pr_number: null,
          pr_url: null,
          source_head_sha: null,
          source_base_sha: null,
          reason: operationReason,
          reason_code: null,
          failure_code: null,
          failure_message: null,
          log_stream_refs: {},
          log_stream_ids: [],
        },
      ],
      next_cursor: null,
      has_more: false,
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
      if (path === `/api/awf/workspaces/${workspaceId}`) {
        await fulfillJson(route, { ...overviewItem, id: workspaceId, version: 2 });
        return;
      }
      if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
        if (detailPhase === "split" && deniedFeed === "runtime") {
          heldDenial.push(route);
          return;
        }
        await fulfillJson(route, runtimeBody);
        return;
      }
      if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
        if (detailPhase === "split" && deniedFeed === "operations") {
          heldDenial.push(route);
          return;
        }
        await fulfillJson(route, operationsBody);
        return;
      }
      if (path === `/api/awf/workspaces/${workspaceId}/events`) {
        if (detailPhase === "split") {
          heldOutage.push(route);
          return;
        }
        await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
        return;
      }
      if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
        if (detailPhase === "split") {
          await new Promise<void>((resolve) => {
            hangingLogs.push(async () => {
              await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
              resolve();
            });
          });
          return;
        }
        await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
        return;
      }
      await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
    });

    try {
      await page.goto("/");
      await waitForConsoleReady(page);
      await page.getByTestId(`workspace-card-${workspaceId}`).click();
      const inspector = page.locator(".fixed.inset-y-0.right-0").first();
      await expect(inspector.getByText(runtimeProject, { exact: true })).toBeVisible({
        timeout: 10_000,
      });
      await expect(inspector.getByText(operationReason, { exact: true })).toBeVisible();

      detailPhase = "split";
      await page.getByRole("button", { name: /refresh/i }).click({ force: true });
      await expect.poll(() => heldDenial.length, { timeout: 10_000 }).toBe(1);
      await expect.poll(() => heldOutage.length, { timeout: 10_000 }).toBe(1);
      await expect.poll(() => hangingLogs.length, { timeout: 10_000 }).toBe(1);

      await fulfillJson(
        heldDenial[0],
        { detail: { error_code: "FORBIDDEN", message: denialMessage } },
        403,
      );
      await expect(inspector.getByText(denialMessage)).toBeVisible({ timeout: 10_000 });
      if (deniedFeed === "runtime") {
        await expect(inspector.getByText("Runtime snapshot unavailable.")).toBeVisible();
      } else {
        await expect(inspector.getByText("No operations recorded.")).toBeVisible();
      }

      await fulfillJson(
        heldOutage[0],
        { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
        503,
      );
      await page.waitForTimeout(1_000);
      await expect(inspector.getByText(denialMessage)).toBeVisible();
      await expect(inspector.getByText(outageMessage)).toHaveCount(0);

      const releaseHang = hangingLogs[0];
      hangingLogs.length = 0;
      await releaseHang?.();
      await page.waitForTimeout(1_000);
      await expect(inspector.getByText(denialMessage)).toBeVisible();
      await expect(inspector.getByText(outageMessage)).toHaveCount(0);
    } finally {
      await hangingLogs[0]?.();
    }
  });
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gKGUJ: a latched
// authorization denial must not discard a concurrent sibling network/5xx
// before it reaches settledDetailOutagesRef. Record the outage and defer
// its banner until the denial clears. Otherwise an explicit refresh can
// recover the denied feed, releaseRecoveredDetailOutage clears the
// revocation reason, and the retained snapshot looks current while the
// failed feed's replacement hangs — serialized polling cannot resume.
test("latched authorization denial retains a sibling outage until recovery republishes it", async ({
  page,
}) => {
  test.setTimeout(45_000);
  let detailPhase: "bootstrap" | "split" | "recover" = "bootstrap";
  const heldDenial: Route[] = [];
  const heldOutage: Route[] = [];
  const hangingLogs: Array<() => Promise<void>> = [];
  const hangingRecoveryRuntime: Route[] = [];
  const workspaceId = "ws_denial_retains_sibling_outage";
  const denialMessage = "events permission revoked";
  const outageMessage = "runtime outage while authorization is revoked";
  const retainedRuntime = "retained-runtime-during-deferred-outage";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Denial must retain a sibling outage",
    repo_url: "https://github.com/example/denial-retains-sibling-outage",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "Retain a sibling detail outage while authorization denial is latched",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };
  const runtimeBody = {
    workspace_id: workspaceId,
    compose_project_name: retainedRuntime,
    stack_state: "running",
    services: [],
    app_endpoints: [],
    logs_available: true,
    control_available: true,
    reason: null,
  };
  const emptyList = { items: [], next_cursor: null, has_more: false };

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
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, { ...overviewItem, id: workspaceId, version: 2 });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      if (detailPhase === "split") {
        heldOutage.push(route);
        return;
      }
      if (detailPhase === "recover") {
        hangingRecoveryRuntime.push(route);
        return;
      }
      await fulfillJson(route, runtimeBody);
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, emptyList);
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      if (detailPhase === "split") {
        heldDenial.push(route);
        return;
      }
      await fulfillJson(route, emptyList);
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (detailPhase === "split") {
        await new Promise<void>((resolve) => {
          hangingLogs.push(async () => {
            await fulfillJson(route, emptyList);
            resolve();
          });
        });
        return;
      }
      await fulfillJson(route, emptyList);
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  try {
    await page.goto("/");
    await waitForConsoleReady(page);
    await page.getByTestId(`workspace-card-${workspaceId}`).click();
    const inspector = page.locator(".fixed.inset-y-0.right-0").first();
    await expect(inspector.getByText(retainedRuntime, { exact: true })).toBeVisible({
      timeout: 10_000,
    });

    detailPhase = "split";
    await page.getByRole("button", { name: /refresh/i }).click({ force: true });
    await expect.poll(() => heldDenial.length, { timeout: 10_000 }).toBe(1);
    await expect.poll(() => heldOutage.length, { timeout: 10_000 }).toBe(1);
    await expect.poll(() => hangingLogs.length, { timeout: 10_000 }).toBe(1);

    await fulfillJson(
      heldDenial[0],
      { detail: { error_code: "FORBIDDEN", message: denialMessage } },
      403,
    );
    await expect(inspector.getByText(denialMessage)).toBeVisible({ timeout: 10_000 });

    await fulfillJson(
      heldOutage[0],
      { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
      503,
    );
    await page.waitForTimeout(1_000);
    await expect(inspector.getByText(denialMessage)).toBeVisible();
    await expect(inspector.getByText(outageMessage)).toHaveCount(0);
    await expect(inspector.getByText(retainedRuntime, { exact: true })).toBeVisible();

    detailPhase = "recover";
    // A Playwright click while the older GET is pending does not dispatch the
    // React refresh handler. A DOM click still starts the newer generation.
    await page.locator("header").getByRole("button", { name: "Refresh" }).evaluate((button) => {
      (button as HTMLButtonElement).click();
    });
    await expect.poll(() => hangingRecoveryRuntime.length, { timeout: 10_000 }).toBe(1);
    await expect(inspector.getByText(denialMessage)).toHaveCount(0);
    await expect(inspector.getByText(outageMessage)).toBeVisible({ timeout: 10_000 });
    await expect(inspector.getByText(retainedRuntime, { exact: true })).toBeVisible();
  } finally {
    await hangingLogs[0]?.();
    await fulfillJson(hangingRecoveryRuntime[0], runtimeBody).catch(() => undefined);
  }
});

test("in-flight runtime and operations denial after withdrawal does not stamp a detail error", async ({
  page,
}) => {
  test.setTimeout(45_000);
  let capabilityPhase: "ok" | "hold" | "withdrawn" = "ok";
  let holdDenial = false;
  const heldCapabilities: Route[] = [];
  const heldRuntime: Route[] = [];
  const heldOperations: Route[] = [];
  const hangingEvents: Route[] = [];
  const workspaceId = "ws_optional_denial_withdrawn";
  const composeProject = "awf-ws-optional-denial-withdrawn";
  const operationReason = "withdrawn-optional-operation-reason";
  const baseCaps = localCapabilities() as {
    diagnostics: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const withdrawnCaps = {
    ...baseCaps,
    diagnostics: baseCaps.diagnostics.map((item) =>
      item.id === "workspace_runtime" || item.id === "workspace_operations"
        ? {
            ...item,
            availability: "unsupported",
            reason_code: "not_implemented",
            message: "Optional feed withdrawn",
          }
        : item,
    ),
  };
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Withdrawn optional denial workspace",
    repo_url: "https://github.com/example/optional-denial-withdrawn",
    base_branch: "main",
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "A withdrawn feed denial must not stamp an uncleared detail error",
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
      if (capabilityPhase === "hold") {
        heldCapabilities.push(route);
        return;
      }
      await fulfillJson(route, capabilityPhase === "withdrawn" ? withdrawnCaps : baseCaps);
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
    if (
      path === `/api/awf/workspaces/${workspaceId}` ||
      path === `/api/awf/workspaces/${workspaceId}/events` ||
      path === `/api/awf/workspaces/${workspaceId}/runtime` ||
      path === `/api/awf/workspaces/${workspaceId}/operations`
    ) {
      await route.fallback();
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
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

  await page.route(`**/api/awf/workspaces/${workspaceId}**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, { ...overviewItem, id: workspaceId, version: 2 });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      if (holdDenial) {
        hangingEvents.push(route);
        return;
      }
      await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      if (holdDenial) {
        heldRuntime.push(route);
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
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      if (holdDenial) {
        heldOperations.push(route);
        return;
      }
      await fulfillJson(route, {
        items: [
          {
            id: "op-withdrawn-optional",
            workspace_id: workspaceId,
            type: "execute",
            status: "running",
            error_code: null,
            error_message: null,
            payload: null,
            result: null,
            idempotency_key: null,
            created_at: "2026-09-06T17:00:00Z",
            started_at: "2026-09-06T17:00:00Z",
            finished_at: null,
            owner: "worker",
            source: "awf",
            action: null,
            pr_number: null,
            pr_url: null,
            source_head_sha: null,
            source_base_sha: null,
            reason: operationReason,
            reason_code: null,
            failure_code: null,
            failure_message: null,
            log_stream_refs: {},
            log_stream_ids: [],
          },
        ],
        next_cursor: null,
        has_more: false,
      });
      return;
    }
    await route.fallback();
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector.getByText(composeProject, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(inspector.getByText(operationReason, { exact: true })).toBeVisible();

  // Hold the refresh's capability response until runtime/operations/events are
  // pending. Flipping withdrawal first lets that load skip the feeds, so the
  // in-flight 403 never happens.
  holdDenial = true;
  capabilityPhase = "hold";
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect.poll(() => heldRuntime.length, { timeout: 10_000 }).toBe(1);
  await expect.poll(() => heldOperations.length, { timeout: 10_000 }).toBe(1);
  await expect.poll(() => hangingEvents.length, { timeout: 10_000 }).toBe(1);
  await expect.poll(() => heldCapabilities.length, { timeout: 10_000 }).toBeGreaterThan(0);
  capabilityPhase = "withdrawn";
  for (const route of heldCapabilities.splice(0, heldCapabilities.length)) {
    await fulfillJson(route, withdrawnCaps);
  }
  await expect(inspector.getByRole("heading", { name: "Runtime", exact: true })).toHaveCount(0, {
    timeout: 10_000,
  });
  await expect(inspector.getByRole("heading", { name: "Operations", exact: true })).toHaveCount(0);

  await fulfillJson(
    heldRuntime[0],
    { detail: { error_code: "FORBIDDEN", message: "runtime permission revoked after withdrawal" } },
    403,
  );
  await fulfillJson(
    heldOperations[0],
    {
      detail: { error_code: "FORBIDDEN", message: "operations permission revoked after withdrawal" },
    },
    403,
  );
  await page.waitForTimeout(1_500);
  await expect(inspector.getByText("runtime permission revoked after withdrawal")).toHaveCount(0);
  await expect(inspector.getByText("operations permission revoked after withdrawal")).toHaveCount(0);
  await expect(inspector.getByRole("heading", { name: "Runtime", exact: true })).toHaveCount(0);
  await expect(inspector.getByRole("heading", { name: "Operations", exact: true })).toHaveCount(0);
  await expect(inspector.getByText(composeProject, { exact: true })).toHaveCount(0);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gD0PE: leaving and
// re-opening the same workspace starts a new inspector visit. A late
// /workspaces/{id} 401/403 from the previous visit must not stamp the denial
// watermark onto the new visit's in-flight GET, or the inspector stays empty
// and /stream stays closed until a later refresh starts after that watermark.
test("stale base workspace detail 403 after reopening the same workspace does not block recovery", async ({
  page,
}) => {
  test.setTimeout(45_000);
  let detailMode: "ok" | "hold-deny" = "ok";
  let streamConnections = 0;
  const heldDeny: Array<() => Promise<void>> = [];
  const workspaceId = "ws_base_detail_denial_reselection";
  const overviewBranch = "overview-keep-branch";
  const authorizedBranch = "reopened-detail-branch";
  const overviewItem = {
    workspace_id: workspaceId,
    title: "Stale detail denial reselection workspace",
    repo_url: "https://github.com/example/base-detail-denial-reselection",
    base_branch: "main",
    branch_name: overviewBranch,
    agent: "codex",
    agent_model: "gpt-5.5",
    status: "running",
    created_at: "2026-09-06T17:00:00Z",
    updated_at: "2026-09-06T17:00:00Z",
    task_prompt: "A previous visit's base detail 403 must not block reopening this workspace",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };
  const authorizedWorkspace = {
    ...overviewItem,
    id: workspaceId,
    version: 4,
    branch_name: authorizedBranch,
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
      if (detailMode === "hold-deny") {
        await new Promise<void>((resolve) => {
          heldDeny.push(async () => {
            await fulfillJson(
              route,
              {
                detail: {
                  error_code: "FORBIDDEN",
                  message: "workspace detail permission revoked",
                },
              },
              403,
            );
            resolve();
          });
        });
        return;
      }
      await fulfillJson(route, authorizedWorkspace);
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, {
        workspace_id: workspaceId,
        compose_project_name: "awf-ws-base-detail-reselection",
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
    if (
      path === `/api/awf/workspaces/${workspaceId}/operations` ||
      path === `/api/awf/workspaces/${workspaceId}/logs`
    ) {
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

  await page.route(`**/api/awf/workspaces/${workspaceId}/stream*`, async (route) => {
    streamConnections += 1;
    await route.fulfill({
      status: 200,
      headers: {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-cache",
      },
      body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
    });
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector.getByText(authorizedBranch, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect.poll(() => streamConnections, { timeout: 10_000 }).toBeGreaterThan(0);
  const connectionsAtFirstVisit = streamConnections;

  detailMode = "hold-deny";
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect.poll(() => heldDeny.length, { timeout: 10_000 }).toBe(1);

  await page.getByRole("button", { name: "Close inspector" }).click();
  await expect(inspector).toHaveClass(/translate-x-full/);

  // The new visit's GET stays queued behind the held 403. Releasing that 403
  // after reselection used to stamp revoked to this visit's generation.
  detailMode = "ok";
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  await expect(inspector).toHaveClass(/translate-x-0/);

  await expect.poll(() => streamConnections, { timeout: 10_000 }).toBeGreaterThan(connectionsAtFirstVisit);
  await expect(page.getByText("Stream: idle")).toHaveCount(0);

  const connectionsAtReopen = streamConnections;
  await heldDeny[0]();
  // The queued reopen GET must apply. A later poll must not be what recovers
  // the inspector, so this window stays under pollMs.
  await expect(inspector.getByText(authorizedBranch, { exact: true })).toBeVisible({ timeout: 3_000 });
  await expect(inspector.getByText(/workspace detail permission revoked/i)).toHaveCount(0);
  await expect(page.getByText("Stream: idle")).toHaveCount(0);
  expect(streamConnections).toBeGreaterThanOrEqual(connectionsAtReopen);
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
      // Capture at entry so a later holdDetail flip cannot attach the unique
      // branch to the selection GET already in this handler.
      const overlapping = holdDetail;
      if (overlapping) {
        await new Promise<void>((resolve) => {
          detailGate.push(resolve);
        });
      }
      await fulfillJson(route, {
        ...overviewItem,
        id: workspaceId,
        version: 2,
        // Selection GET has no branch so the unique name can only come from
        // the held refresh load that overlaps the capabilities 404.
        ...(overlapping ? { branch_name: detailBranch } : {}),
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

  // Let the selection GET finish. Holding it inside the route handler blocks
  // the later refresh-triggered GET from entering this mock (Playwright does
  // not always deliver a second matching request while the first handler is
  // awaiting), so detailStarts never advances and the overlap is unobservable.
  await page.getByTestId(`workspace-card-${workspaceId}`).click();
  await expect.poll(() => detailStarts, { timeout: 10_000 }).toBeGreaterThan(0);
  await expect(page.getByText("no branch / main", { exact: true })).toBeVisible({ timeout: 10_000 });

  // Hold only the refresh load. The unique branch can appear only from that
  // GET, which overlaps the capabilities 404 below.
  holdDetail = true;
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
  await expect(page.getByText(`${detailBranch} / main`, { exact: true })).toHaveCount(0);
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

test("superseded dashboard-summary 403 applies while a newer refresh hangs", async ({ page }) => {
  // Regression for PR #933 review thread PRRT_kwDOSJAM6s6gEfkK: a periodic or
  // earlier dashboard-summary request can return 401/403 after Refresh has only
  // started a newer request. Discarding that denial because the newer request
  // exists leaves revoked tenant KPIs visible if the newer request hangs.
  let summaryMode: "ok" | "hold" | "hang" = "ok";
  const held: Array<() => Promise<void>> = [];
  const hanging: Array<() => Promise<void>> = [];
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
      if (summaryMode === "hold") {
        await new Promise<void>((resolve) => {
          held.push(async () => {
            await fulfillJson(
              route,
              { detail: { error_code: "FORBIDDEN", message: "tenant summary permission revoked" } },
              403,
            );
            resolve();
          });
        });
        return;
      }
      if (summaryMode === "hang") {
        await new Promise<void>((resolve) => {
          hanging.push(async () => {
            await fulfillJson(route, summary);
            resolve();
          });
        });
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

  summaryMode = "hold";
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => held.length).toBe(1);

  summaryMode = "hang";
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => hanging.length).toBe(1);

  await held[0]();
  await expect(page.getByText(/tenant summary permission revoked|forbidden|denied/i).first()).toBeVisible({
    timeout: 10_000,
  });
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("—");
  await expect(kpi(page, "Active")).not.toHaveAttribute("data-awf-stale", "true");
  await expect(page.getByText(/last snapshot|may be stale/i)).toHaveCount(0);

  // The in-flight refresh started before the denial; a later 200 is not recovery.
  await hanging[0]();
  await page.waitForTimeout(300);
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("—");
  await expect(page.getByText(/tenant summary permission revoked/i).first()).toBeVisible();
});

test("superseded dashboard-summary outage applies while a newer refresh hangs", async ({ page }) => {
  // Same thread: a completed network/5xx must still warn that retained KPIs are
  // stale when Refresh has only started a newer request that has not succeeded.
  let summaryMode: "ok" | "hold" | "hang" = "ok";
  const held: Array<() => Promise<void>> = [];
  const hanging: Array<() => Promise<void>> = [];
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
      if (summaryMode === "hold") {
        await new Promise<void>((resolve) => {
          held.push(async () => {
            await fulfillJson(
              route,
              { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "summary outage" } },
              503,
            );
            resolve();
          });
        });
        return;
      }
      if (summaryMode === "hang") {
        await new Promise<void>((resolve) => {
          hanging.push(async () => {
            await fulfillJson(route, summary);
            resolve();
          });
        });
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

  summaryMode = "hold";
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => held.length).toBe(1);

  summaryMode = "hang";
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect.poll(() => hanging.length).toBe(1);

  await held[0]();
  await expect(page.getByText(/last snapshot|may be stale/i)).toBeVisible({ timeout: 10_000 });
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("9");
  await expect(kpi(page, "Active")).toHaveAttribute("data-awf-stale", "true");

  await hanging[0]();
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
