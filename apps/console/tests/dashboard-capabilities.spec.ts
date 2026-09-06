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
