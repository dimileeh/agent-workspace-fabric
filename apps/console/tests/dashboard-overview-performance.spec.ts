import { expect, type Page, type Route, test } from "@playwright/test";

import {
  fulfillJson,
  localDashboardSummary,
  mockAwfConsoleApi,
} from "./fixtures/console-api";

const FLEET_SIZE = 1_301;
const PAGE_SIZE = 100;

type OverviewRouteOptions = {
  delayContinuation?: boolean;
  onRequest?: (cursor: string | null) => void;
};

function workspaceOverview(index: number) {
  const workspaceId = `ws_perf_${String(index).padStart(4, "0")}`;
  const occurredAt = new Date(Date.UTC(2026, 8, 10, 12, 0, 0) - index * 1_000).toISOString();
  return {
    workspace_id: workspaceId,
    task_id: `task-${workspaceId}`,
    title: `Performance workspace ${index}`,
    task_prompt: `Inspect performance workspace ${index}`,
    repo_url: index % 2 === 0 ? "https://example.com/even.git" : "https://example.com/odd.git",
    base_branch: "development",
    branch_name: `perf/${index}`,
    agent: index === FLEET_SIZE ? "grok" : "codex",
    agent_model: index === FLEET_SIZE ? "grok-code-fast-1" : "gpt-5.5",
    agent_effort: "medium",
    agent_model_source: "test",
    agent_effort_source: "test",
    status: index === FLEET_SIZE ? "completed" : "running",
    created_at: occurredAt,
    updated_at: occurredAt,
    last_activity_at: occurredAt,
    lifecycle: [],
    llm_usage: { status: "unavailable" },
    coordination_warnings: [],
    pr_url: null,
  };
}

const fleet = Array.from({ length: FLEET_SIZE }, (_, index) => workspaceOverview(index + 1));

async function installLargeFleetOverview(
  page: Page,
  options: OverviewRouteOptions = {},
): Promise<() => Promise<void>> {
  const delayedRoutes: Route[] = [];
  await page.route("**/api/awf/workspaces/overview*", async (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    options.onRequest?.(cursor);
    if (options.delayContinuation && cursor !== null) {
      delayedRoutes.push(route);
      return;
    }
    await fulfillOverviewPage(route, cursor);
  });
  return async () => {
    await Promise.all(
      delayedRoutes.splice(0).map((route) =>
        fulfillOverviewPage(
          route,
          new URL(route.request().url()).searchParams.get("cursor"),
        ),
      ),
    );
  };
}

async function fulfillOverviewPage(route: Route, cursor: string | null): Promise<void> {
  const url = new URL(route.request().url());
  const status = url.searchParams.get("status");
  const agent = url.searchParams.get("agent");
  const repoUrl = url.searchParams.get("repo_url");
  const matchingFleet = fleet.filter(
    (item) =>
      (!status || item.status === status) &&
      (!agent || item.agent === agent) &&
      (!repoUrl || item.repo_url === repoUrl),
  );
  const start = cursor === null ? 0 : Number.parseInt(cursor, 10);
  const items = matchingFleet.slice(start, start + PAGE_SIZE);
  const next = start + items.length;
  await fulfillJson(route, {
    items,
    has_more: next < matchingFleet.length,
    next_cursor: next < matchingFleet.length ? String(next) : null,
  });
}

async function waitForConsoleReady(page: Page): Promise<void> {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

const kpi = (page: Page, label: string) =>
  page.getByText(label, { exact: true }).locator("..").filter({ has: page.locator(".kpi-value") });

test("1,301-row fleet paints its first page before history and keeps routine work bounded", async ({
  page,
}) => {
  let firstRequestAt = 0;
  const overviewRequests: Array<string | null> = [];
  await mockAwfConsoleApi(page, {
    dashboardSummary: localDashboardSummary({
      counts: { ...localDashboardSummary().counts, active: FLEET_SIZE },
    }),
  });
  const releaseHistory = await installLargeFleetOverview(page, {
    delayContinuation: true,
    onRequest: (cursor) => {
      overviewRequests.push(cursor);
      if (firstRequestAt === 0) {
        firstRequestAt = Date.now();
      }
    },
  });

  await page.goto("/");
  // Continuation requests are deliberately left unresolved. Visibility proves
  // that first-page publication is not coupled to history completion.
  await expect(page.getByTestId("workspace-card-ws_perf_0001")).toBeVisible({ timeout: 5_000 });
  const firstPaintMs = Date.now() - firstRequestAt;

  expect(firstPaintMs).toBeLessThan(5_000);
  expect(overviewRequests).toEqual([null]);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);
  await expect(kpi(page, "Active").locator(".kpi-value")).toHaveText("1301");

  const inspectorStartedAt = Date.now();
  await page.getByTestId("workspace-card-ws_perf_0001").click();
  await expect(page.getByRole("button", { name: "Close inspector" })).toBeVisible();
  await page.getByRole("button", { name: "Close inspector" }).click();
  await expect(page.locator(".fixed.inset-y-0.right-0").first()).toHaveClass(/translate-x-full/);
  const inspectorOpenCloseMs = Date.now() - inspectorStartedAt;
  expect(inspectorOpenCloseMs).toBeLessThan(1_000);
  console.info(
    `[overview-performance] first-page=${firstPaintMs}ms inspector-open-close=${inspectorOpenCloseMs}ms`,
  );
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);

  await page.waitForTimeout(5_500);
  expect(overviewRequests).toEqual([null, null]);
  await releaseHistory();
});

test("explicit history access keeps older search, filters, selection, and DOM windows usable", async ({
  page,
}) => {
  const overviewRequests: Array<string | null> = [];
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => overviewRequests.push(cursor),
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByTestId("workspace-card-ws_perf_0001")).toBeVisible();
  expect(overviewRequests).toEqual([null]);

  await page.getByRole("button", { name: "Load older workspaces" }).click();
  await expect(page.getByText(`1–${PAGE_SIZE} of ${FLEET_SIZE} loaded`, { exact: true })).toBeVisible();
  // One bootstrap request, then an explicit fresh first page plus its history.
  expect(overviewRequests).toHaveLength(1 + Math.ceil(FLEET_SIZE / PAGE_SIZE));
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);

  await page.getByRole("button", { name: "Next workspace results" }).click();
  await expect(page.getByTestId("workspace-card-ws_perf_0101")).toBeVisible();
  await expect(page.getByTestId("workspace-card-ws_perf_0001")).toHaveCount(0);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);

  const requestsAfterHistory = overviewRequests.length;
  await page.waitForTimeout(5_500);
  expect(overviewRequests).toHaveLength(requestsAfterHistory + 1);
  expect(overviewRequests.at(-1)).toBeNull();
  await expect(page.getByTestId("workspace-card-ws_perf_0101")).toBeVisible();

  const filters = page.getByRole("button", { name: "Filters" });
  await filters.click();
  await page.getByPlaceholder("Search workspaces").fill("Performance workspace 1301");
  await expect(page.getByTestId("workspace-card-ws_perf_1301")).toBeVisible();
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(1);

  const statusGroup = page.getByRole("group", { name: "Status" });
  await statusGroup.getByRole("button", { name: /Status all/ }).click();
  await statusGroup.getByLabel("completed").check();
  await expect(page.getByTestId("workspace-card-ws_perf_1301")).toBeVisible();

  await page.getByLabel("Select Performance workspace 1301 for fullscreen logs").check();
  await expect(page.getByText("1 selected for logs", { exact: true })).toBeVisible();
});

test("a deep link resolves an older workspace without mounting the intervening fleet", async ({
  page,
}) => {
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page);

  await page.goto("/?workspaceId=ws_perf_1301");
  await expect(page.getByTestId("workspace-card-ws_perf_1301")).toBeVisible();
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Close inspector" })).toBeVisible();
  await expect(page).toHaveURL(/workspaceId=ws_perf_1301/);
});
