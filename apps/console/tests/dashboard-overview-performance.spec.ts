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
  failFirstContinuation?: boolean;
  onRequest?: (cursor: string | null) => void;
  onBatchRequest?: (workspaceIds: string[]) => void;
  shouldDelayBatch?: () => boolean;
  shouldFailBatch?: () => boolean;
  resolvePageItem?: (
    item: ReturnType<typeof workspaceOverview>,
  ) => ReturnType<typeof workspaceOverview>;
  resolvePageItems?: (
    items: ReturnType<typeof workspaceOverview>[],
    cursor: string | null,
  ) => ReturnType<typeof workspaceOverview>[];
  resolvePage?: (cursor: string | null, status: string | null) => {
    items: ReturnType<typeof workspaceOverview>[];
    has_more: boolean;
    next_cursor: string | null;
  } | null;
  resolveBatchItem?: (
    item: ReturnType<typeof workspaceOverview>,
  ) => ReturnType<typeof workspaceOverview> | null;
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
  const delayedBatchRoutes: Route[] = [];
  let continuationFailed = false;
  const fulfillOverviewBatch = async (route: Route, recordRequest = true) => {
    const body = route.request().postDataJSON() as { workspace_ids: string[] };
    if (recordRequest) {
      options.onBatchRequest?.(body.workspace_ids);
    }
    const requested = new Set(body.workspace_ids);
    const items = fleet
      .filter((item) => requested.has(item.workspace_id))
      .map((item) => options.resolveBatchItem ? options.resolveBatchItem(item) : item)
      .filter((item): item is ReturnType<typeof workspaceOverview> => item !== null);
    await fulfillJson(route, {
      items,
      missing_workspace_ids: body.workspace_ids.filter(
        (workspaceId) => !items.some((item) => item.workspace_id === workspaceId),
      ),
    });
  };
  const routeOverviewBatch = async (route: Route) => {
    const body = route.request().postDataJSON() as { workspace_ids: string[] };
    options.onBatchRequest?.(body.workspace_ids);
    if (options.shouldDelayBatch?.()) {
      delayedBatchRoutes.push(route);
      return;
    }
    if (options.shouldFailBatch?.()) {
      await fulfillJson(route, { detail: { message: "retained history unavailable" } }, 503);
      return;
    }
    await fulfillOverviewBatch(route, false);
  };
  const fulfillPage = async (route: Route, cursor: string | null) => {
    const status = new URL(route.request().url()).searchParams.get("status");
    const resolvedPage = options.resolvePage?.(cursor, status);
    if (resolvedPage) {
      await fulfillJson(route, resolvedPage);
      return;
    }
    await fulfillOverviewPage(
      route,
      cursor,
      options.resolvePageItem,
      options.resolvePageItems,
    );
  };
  await page.route("**/api/awf/workspaces/overview*", async (route) => {
    if (route.request().method() === "POST") {
      await routeOverviewBatch(route);
      return;
    }
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    options.onRequest?.(cursor);
    if (options.failFirstContinuation && cursor !== null && !continuationFailed) {
      continuationFailed = true;
      await fulfillJson(route, { detail: { message: "history temporarily unavailable" } }, 503);
      return;
    }
    if (options.delayContinuation && cursor !== null) {
      delayedRoutes.push(route);
      return;
    }
    await fulfillPage(route, cursor);
  });
  await page.route("**/api/awf/workspaces/overview/batch", routeOverviewBatch);
  return async () => {
    await Promise.allSettled([
      ...delayedRoutes.splice(0).map((route) =>
        fulfillPage(route, new URL(route.request().url()).searchParams.get("cursor")),
      ),
      ...delayedBatchRoutes.splice(0).map((route) => fulfillOverviewBatch(route, false)),
    ]);
  };
}

async function fulfillOverviewPage(
  route: Route,
  cursor: string | null,
  resolvePageItem?: (
    item: ReturnType<typeof workspaceOverview>,
  ) => ReturnType<typeof workspaceOverview>,
  resolvePageItems?: (
    items: ReturnType<typeof workspaceOverview>[],
    cursor: string | null,
  ) => ReturnType<typeof workspaceOverview>[],
): Promise<void> {
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
  const pageItems = matchingFleet
    .slice(start, start + PAGE_SIZE)
    .map((item) => resolvePageItem?.(item) ?? item);
  const items = resolvePageItems?.(pageItems, cursor) ?? pageItems;
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

test("scroll loads one history page and refresh preserves the bounded loaded window", async ({
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
  await expect(page.getByRole("button", { name: "Load more workspaces" })).toBeVisible();
  await expect(page.getByText(/Search and client-side filters cover loaded workspaces only/)).toBeVisible();

  const list = page.getByTestId("workspace-list-scroll");
  await list.evaluate((element) => element.scrollTo({ top: element.scrollHeight }));
  await expect.poll(() => overviewRequests).toEqual([null, String(PAGE_SIZE)]);
  await expect(page.getByText(`1–${PAGE_SIZE} of ${PAGE_SIZE * 2} loaded`, { exact: true })).toBeVisible();
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);
  const scrollTopAfterAppend = await list.evaluate((element) => element.scrollTop);
  expect(scrollTopAfterAppend).toBeGreaterThan(0);

  await page.getByRole("button", { name: "Next workspace results" }).click();
  await expect(page.getByTestId("workspace-card-ws_perf_0101")).toBeVisible();
  await expect(page.getByTestId("workspace-card-ws_perf_0001")).toHaveCount(0);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);

  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect.poll(() => overviewRequests).toEqual([null, "100", "200"]);
  await expect(page.getByText(`101–200 of ${PAGE_SIZE * 3} loaded`, { exact: true })).toBeVisible();
  await page.getByTestId("workspace-card-ws_perf_0101").click();
  await expect(page.getByRole("button", { name: "Close inspector" })).toBeVisible();
  await page.getByRole("button", { name: "Next workspace results" }).click();
  await expect(page.getByTestId("workspace-card-ws_perf_0201")).toBeVisible();
  const scrollTopBeforeRefresh = await list.evaluate((element) => element.scrollTop);

  const requestsAfterHistory = overviewRequests.length;
  await page.waitForTimeout(5_500);
  expect(overviewRequests).toHaveLength(requestsAfterHistory + 1);
  expect(overviewRequests.at(-1)).toBeNull();
  await expect(page.getByTestId("workspace-card-ws_perf_0201")).toBeVisible();
  await expect(page.getByTestId("workspace-card-ws_perf_0101")).toHaveCount(0);
  expect(await list.evaluate((element) => element.scrollTop)).toBe(scrollTopBeforeRefresh);
  await page.getByRole("button", { name: "Close inspector" }).click();

  const filters = page.getByRole("button", { name: "Filters" });
  await filters.click();
  await page.getByPlaceholder("Search workspaces").fill("Performance workspace 1301");
  await expect(page.getByText("No loaded workspaces match the current filters.")).toBeVisible();
  await expect(page.getByText(/Search and client-side filters cover loaded workspaces only/)).toBeVisible();
  expect(overviewRequests).toHaveLength(requestsAfterHistory + 1);

  await page.getByPlaceholder("Search workspaces").fill("");

  const statusGroup = page.getByRole("group", { name: "Status" });
  await statusGroup.getByRole("button", { name: /Status all/ }).click();
  await statusGroup.getByLabel("completed").check();
  await expect(page.getByTestId("workspace-card-ws_perf_1301")).toBeVisible();

  await page.getByLabel("Select Performance workspace 1301 for fullscreen logs").check();
  await expect(page.getByText("1 selected for logs", { exact: true })).toBeVisible();
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hMh1_: when a full
// page of newer workspaces makes page one disjoint, the old keyset cursor skips
// the pages inserted ahead of its boundary.
test("disjoint first-page refresh replaces a stale overview cursor", async ({ page }) => {
  const overviewRequests: Array<string | null> = [];
  let refreshed = false;
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => overviewRequests.push(cursor),
    resolvePage: (cursor) => {
      if (cursor === null) {
        return refreshed
          ? { items: [workspaceOverview(201)], has_more: true, next_cursor: "new-page-2" }
          : { items: [workspaceOverview(1)], has_more: true, next_cursor: "old-page-2" };
      }
      if (cursor === "old-page-2") {
        return { items: [workspaceOverview(101)], has_more: true, next_cursor: "old-page-3" };
      }
      if (cursor === "new-page-2") {
        return { items: [workspaceOverview(202)], has_more: false, next_cursor: null };
      }
      return { items: [workspaceOverview(102)], has_more: false, next_cursor: null };
    },
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect.poll(() => overviewRequests).toContain("old-page-2");

  refreshed = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByTestId("workspace-card-ws_perf_0201")).toBeVisible();
  await page.getByRole("button", { name: "Load more workspaces" }).click();

  await expect.poll(() => overviewRequests).toContain("new-page-2");
});

test("disjoint first-page refresh reopens completed overview history", async ({ page }) => {
  let refreshed = false;
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    resolvePage: (cursor) => {
      if (cursor === "new-page-2") {
        return { items: [workspaceOverview(202)], has_more: false, next_cursor: null };
      }
      return refreshed
        ? { items: [workspaceOverview(201)], has_more: true, next_cursor: "new-page-2" }
        : { items: [workspaceOverview(1)], has_more: false, next_cursor: null };
    },
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByText("All 1 matching workspaces loaded.")).toBeVisible();

  refreshed = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();

  await expect(page.getByTestId("workspace-card-ws_perf_0201")).toBeVisible();
  await expect(page.getByRole("button", { name: "Load more workspaces" })).toBeVisible();
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hTOC5: a completed
// filtered query can gain an older matching workspace without making its
// refreshed first page disjoint from the retained rows.
test("overlapping first-page refresh reopens completed filtered history", async ({ page }) => {
  let membershipGrew = false;
  let continuationRequests = 0;
  const completedPage = Array.from({ length: PAGE_SIZE }, (_, index) => ({
    ...workspaceOverview(index + 1),
    status: "completed",
  }));
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    resolvePage: (cursor, status) => {
      if (status !== "completed") {
        return null;
      }
      if (cursor === "completed-page-2") {
        continuationRequests += 1;
        return {
          items: [{ ...workspaceOverview(PAGE_SIZE + 1), status: "completed" }],
          has_more: false,
          next_cursor: null,
        };
      }
      return {
        items: completedPage,
        has_more: membershipGrew,
        next_cursor: membershipGrew ? "completed-page-2" : null,
      };
    },
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Filters" }).click();
  const statusGroup = page.getByRole("group", { name: "Status" });
  await statusGroup.getByRole("button", { name: /Status all/ }).click();
  await statusGroup.getByLabel("completed").check();
  await expect(page.getByText(`All ${PAGE_SIZE} matching workspaces loaded.`)).toBeVisible();

  membershipGrew = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();

  await expect(page.getByRole("button", { name: "Load more workspaces" })).toBeVisible();
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect.poll(() => continuationRequests).toBe(1);
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hUDfA: a filtered
// query can gain a matching row between the refreshed first page and the
// retained continuation boundary while partially loaded history stays open.
test("filtered refresh reopens partial history from the first-page cursor", async ({ page }) => {
  const completedRequests: Array<string | null> = [];
  let membershipGrew = false;
  const completedItems = (start: number, end: number) =>
    Array.from({ length: end - start + 1 }, (_, index) => ({
      ...workspaceOverview(start + index),
      status: "completed",
    }));
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => completedRequests.push(cursor),
    resolveBatchItem: (item) => ({ ...item, status: "completed" }),
    resolvePage: (cursor, status) => {
      if (status !== "completed") {
        return null;
      }
      if (cursor === "old-completed-page-2") {
        return {
          items: completedItems(PAGE_SIZE + 2, PAGE_SIZE * 2 + 1),
          has_more: true,
          next_cursor: "stale-completed-page-3",
        };
      }
      if (cursor === "fresh-completed-page-2") {
        return {
          items: completedItems(PAGE_SIZE + 1, PAGE_SIZE * 2),
          has_more: true,
          next_cursor: "fresh-completed-page-3",
        };
      }
      if (cursor === "stale-completed-page-3") {
        return {
          items: completedItems(PAGE_SIZE * 2 + 2, PAGE_SIZE * 2 + 2),
          has_more: false,
          next_cursor: null,
        };
      }
      return {
        items: completedItems(1, PAGE_SIZE).map((item, index) =>
          membershipGrew && index === 0
            ? { ...item, title: `${item.title} refreshed` }
            : item
        ),
        has_more: true,
        next_cursor: membershipGrew
          ? "fresh-completed-page-2"
          : "old-completed-page-2",
      };
    },
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Filters" }).click();
  const statusGroup = page.getByRole("group", { name: "Status" });
  await statusGroup.getByRole("button", { name: /Status all/ }).click();
  await statusGroup.getByLabel("completed").check();
  const loadMore = page.getByRole("button", { name: "Load more workspaces" });
  // This regression covers cursor replacement, while near-bottom loading has
  // dedicated coverage. Avoid Playwright scrolling the footer into view and
  // racing that scroll loader with the button action.
  const requestHistoryWithoutScrolling = async (expectedCursor: string) => {
    await expect(async () => {
      if (!completedRequests.includes(expectedCursor)) {
        await loadMore.evaluate((button: HTMLButtonElement) => button.click());
      }
      expect(completedRequests).toContain(expectedCursor);
    }).toPass({ intervals: [100, 250, 500], timeout: 5_000 });
  };
  await requestHistoryWithoutScrolling("old-completed-page-2");

  membershipGrew = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByTestId("workspace-title-ws_perf_0001")).toContainText("refreshed");
  await requestHistoryWithoutScrolling("fresh-completed-page-2");
  expect(completedRequests).not.toContain("stale-completed-page-3");
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hVJ_b: an off-page
// workspace can enter a single-status result without changing page one or its
// immutable keyset cursor. The next continuation must backfill the loaded range
// before it advances beyond the newly matching row.
test("filtered continuation backfills unchanged first-page membership", async ({ page }) => {
  const completedRequests: Array<string | null> = [];
  let membershipGrew = false;
  const completedItems = (start: number, end: number) =>
    Array.from({ length: end - start + 1 }, (_, index) => ({
      ...workspaceOverview(start + index),
      status: "completed",
    }));
  const newlyMatching = {
    ...workspaceOverview(777),
    title: "Newly matching off-page workspace",
    status: "completed",
  };
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => completedRequests.push(cursor),
    resolveBatchItem: (item) => ({ ...item, status: "completed" }),
    resolvePage: (cursor, status) => {
      if (status !== "completed") {
        return null;
      }
      if (cursor === "completed-page-2") {
        return membershipGrew
          ? {
              items: [
                ...completedItems(PAGE_SIZE + 1, PAGE_SIZE + 49),
                newlyMatching,
                ...completedItems(PAGE_SIZE + 50, PAGE_SIZE * 2 - 1),
              ],
              has_more: true,
              next_cursor: "shifted-completed-page-3",
            }
          : {
              items: completedItems(PAGE_SIZE + 1, PAGE_SIZE * 2),
              has_more: true,
              next_cursor: "old-completed-page-3",
            };
      }
      if (cursor === "shifted-completed-page-3") {
        return {
          items: completedItems(PAGE_SIZE * 2, PAGE_SIZE * 3 - 1),
          has_more: true,
          next_cursor: "shifted-completed-page-4",
        };
      }
      if (cursor === "old-completed-page-3") {
        return {
          items: completedItems(PAGE_SIZE * 2 + 1, PAGE_SIZE * 3),
          has_more: true,
          next_cursor: "old-completed-page-4",
        };
      }
      return {
        items: completedItems(1, PAGE_SIZE),
        has_more: true,
        // Membership changed below page one, so this boundary is unchanged.
        next_cursor: "completed-page-2",
      };
    },
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Filters" }).click();
  const statusGroup = page.getByRole("group", { name: "Status" });
  await statusGroup.getByRole("button", { name: /Status all/ }).click();
  await statusGroup.getByLabel("completed").check();
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect.poll(() => completedRequests).toContain("completed-page-2");

  // Membership can change after the latest page-one poll. The continuation
  // itself must be armed to replay the loaded range; waiting for another poll
  // would leave a window where the stale page-three cursor skips this row.
  membershipGrew = true;
  const firstPageRequests = completedRequests.filter((cursor) => cursor === null).length;
  await page.getByRole("button", { name: "Load more workspaces" }).click();

  await expect.poll(() => completedRequests).toContain("shifted-completed-page-3");
  expect(completedRequests.filter((cursor) => cursor === null)).toHaveLength(firstPageRequests);
  await expect(page.getByText(new RegExp(`of ${PAGE_SIZE * 3} loaded$`))).toBeVisible();
  await page.getByPlaceholder("Search workspaces").fill("Newly matching off-page workspace");
  await expect(page.getByTestId("workspace-card-ws_perf_0777")).toBeVisible();
  expect(completedRequests).not.toContain("old-completed-page-3");
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hVeoS: the prior
// boundary row can leave the filtered result. Replaying must stop once it has
// crossed that row's stable sort position instead of exhausting the page cap.
test("filtered continuation survives a missing backfill boundary row", async ({ page }) => {
  const completedRequests: Array<string | null> = [];
  let boundaryRemoved = false;
  const completedItems = (start: number, end: number) =>
    Array.from({ length: end - start + 1 }, (_, index) => ({
      ...workspaceOverview(start + index),
      status: "completed",
    }));
  const laterMatching = {
    ...workspaceOverview(202),
    title: "Matching workspace after removed boundary",
    status: "completed",
  };
  const crossedBoundary = {
    ...workspaceOverview(201),
    workspace_id: "ws_perf_0199a",
    title: "Workspace after removed tied boundary",
    created_at: workspaceOverview(200).created_at,
    status: "completed",
  };
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => completedRequests.push(cursor),
    resolveBatchItem: (item) => ({ ...item, status: "completed" }),
    resolvePage: (cursor, status) => {
      if (status !== "completed") {
        return null;
      }
      if (cursor === "completed-page-2") {
        return boundaryRemoved
          ? {
              items: completedItems(PAGE_SIZE + 1, PAGE_SIZE * 2 - 1).concat(
                crossedBoundary,
              ),
              has_more: true,
              next_cursor: "shifted-completed-page-3",
            }
          : {
              items: completedItems(PAGE_SIZE + 1, PAGE_SIZE * 2),
              has_more: true,
              next_cursor: "old-completed-page-3",
            };
      }
      if (cursor === "shifted-completed-page-3") {
        return {
          items: [laterMatching],
          has_more: true,
          next_cursor: "shifted-completed-page-4",
        };
      }
      if (cursor?.startsWith("shifted-completed-page-")) {
        const pageNumber = Number.parseInt(cursor.slice("shifted-completed-page-".length), 10);
        return {
          items: completedItems(PAGE_SIZE * 2 + pageNumber - 1, PAGE_SIZE * 2 + pageNumber - 1),
          has_more: true,
          next_cursor: `shifted-completed-page-${pageNumber + 1}`,
        };
      }
      return {
        items: completedItems(1, PAGE_SIZE),
        has_more: true,
        next_cursor: "completed-page-2",
      };
    },
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Filters" }).click();
  const statusGroup = page.getByRole("group", { name: "Status" });
  await statusGroup.getByRole("button", { name: /Status all/ }).click();
  await statusGroup.getByLabel("completed").check();
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect.poll(() => completedRequests).toContain("completed-page-2");

  boundaryRemoved = true;
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect(page.getByText(new RegExp(`of ${PAGE_SIZE * 2 + 1} loaded$`))).toBeVisible();
  expect(completedRequests).not.toContain("shifted-completed-page-3");

  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect.poll(() => completedRequests).toContain("shifted-completed-page-3");
  await page.getByPlaceholder("Search workspaces").fill(laterMatching.title);
  await expect(page.getByTestId("workspace-card-ws_perf_0202")).toBeVisible();
  await expect(page.getByText(/page safety limit/)).toHaveCount(0);
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hU_13: routine
// first-page polls must not move a filtered continuation back to page 2.
test("filtered routine poll preserves pagination progress", async ({ page }) => {
  const completedRequests: Array<string | null> = [];
  const completedItems = (start: number, end: number) =>
    Array.from({ length: end - start + 1 }, (_, index) => ({
      ...workspaceOverview(start + index),
      status: "completed",
    }));
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    resolveBatchItem: (item) => ({ ...item, status: "completed" }),
    resolvePage: (cursor, status) => {
      if (status !== "completed") {
        return null;
      }
      completedRequests.push(cursor);
      if (cursor === "completed-page-2") {
        return {
          items: completedItems(PAGE_SIZE + 1, PAGE_SIZE * 2),
          has_more: true,
          next_cursor: "completed-page-3",
        };
      }
      if (cursor === "completed-page-3") {
        return {
          items: completedItems(PAGE_SIZE * 2 + 1, PAGE_SIZE * 2 + 1),
          has_more: false,
          next_cursor: null,
        };
      }
      return {
        items: completedItems(1, PAGE_SIZE),
        has_more: true,
        next_cursor: "completed-page-2",
      };
    },
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Filters" }).click();
  const statusGroup = page.getByRole("group", { name: "Status" });
  await statusGroup.getByRole("button", { name: /Status all/ }).click();
  await statusGroup.getByLabel("completed").check();
  await expect.poll(() => completedRequests).toContain(null);
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect.poll(() => completedRequests).toContain("completed-page-2");

  const pageTwoRequest = completedRequests.indexOf("completed-page-2");
  await expect.poll(
    () => completedRequests.slice(pageTwoRequest + 1),
    { timeout: 7_000 },
  ).toContain(null);
  await page.getByRole("button", { name: "Load more workspaces" }).click();

  await expect.poll(() => completedRequests).toContain("completed-page-3");
});

// Regression for PR #958 operator acceptance: appended history must extend the
// virtual scroll range instead of requiring the explicit paging controls.
test("scroll traverses bounded history windows in both directions", async ({ page }) => {
  const overviewRequests: Array<string | null> = [];
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => overviewRequests.push(cursor),
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  const list = page.getByTestId("workspace-list-scroll");
  const scrollTo = async (position: "top" | "middle" | "later" | "bottom") => {
    await list.evaluate((element, target) => {
      const top = target === "top"
        ? 0
        : target === "middle"
          ? element.scrollHeight / 2
          : target === "later"
            ? element.scrollHeight * 0.8
            : element.scrollHeight;
      element.scrollTo({ top });
    }, position);
  };

  await expect(page.getByTestId("workspace-card-ws_perf_0001")).toBeVisible();
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);

  await scrollTo("bottom");
  await expect.poll(() => overviewRequests).toEqual([null, String(PAGE_SIZE)]);
  await scrollTo("bottom");
  await expect(page.getByTestId("workspace-card-ws_perf_0101")).toBeVisible();
  await expect(page.getByTestId("workspace-card-ws_perf_0001")).toHaveCount(0);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);
  await expect.poll(() => overviewRequests).toEqual([null, "100", "200"]);

  await scrollTo("later");
  await expect(page.getByTestId("workspace-card-ws_perf_0201")).toBeVisible();
  await expect(page.getByTestId("workspace-card-ws_perf_0101")).toHaveCount(0);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);

  await scrollTo("middle");
  await expect(page.getByTestId("workspace-card-ws_perf_0101")).toBeVisible();
  await expect(page.getByTestId("workspace-card-ws_perf_0201")).toHaveCount(0);
  await scrollTo("top");
  await expect(page.getByTestId("workspace-card-ws_perf_0001")).toBeVisible();
  await expect(page.getByTestId("workspace-card-ws_perf_0101")).toHaveCount(0);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);
  expect(overviewRequests).toEqual([null, "100", "200"]);
});

test("virtualization keeps the viewport covered while crossing a row-window boundary", async ({
  page,
}) => {
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    resolvePageItem: (item) => ({ ...item, status: "completed" }),
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect(page.getByText(`1–${PAGE_SIZE} of ${PAGE_SIZE * 2} loaded`, { exact: true })).toBeVisible();

  const list = page.getByTestId("workspace-list-scroll");
  await list.evaluate((element) => element.scrollTo({ top: 0 }));
  await expect(page.getByTestId("workspace-card-ws_perf_0001")).toBeVisible();
  const firstWindowHeight = await list.locator('[data-testid^="workspace-card-"]').evaluateAll(
    (cards) => cards.reduce((height, card) => height + card.getBoundingClientRect().height, 0),
  );
  await list.evaluate((element, top) => element.scrollTo({ top }), firstWindowHeight - 200);
  await expect.poll(async () =>
    list.locator('[data-testid^="workspace-card-"]').first().getAttribute("data-testid"),
  ).not.toBe("workspace-card-ws_perf_0001");

  const geometry = await list.evaluate((element) => {
    const rows = Array.from(
      element.querySelectorAll<HTMLElement>('[data-testid^="workspace-card-"]'),
    );
    const header = element.querySelector<HTMLElement>(":scope > .sticky");
    const viewport = element.getBoundingClientRect();
    return {
      firstRowTop: rows.at(0)?.getBoundingClientRect().top ?? Number.POSITIVE_INFINITY,
      lastRowBottom: rows.at(-1)?.getBoundingClientRect().bottom ?? Number.NEGATIVE_INFINITY,
      viewportTop: header?.getBoundingClientRect().bottom ?? viewport.top,
      viewportBottom: viewport.bottom,
    };
  });
  expect(geometry.firstRowTop).toBeLessThanOrEqual(geometry.viewportTop + 1);
  expect(geometry.lastRowBottom).toBeGreaterThanOrEqual(geometry.viewportBottom - 1);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hYPze: selecting a
// visible row must not jump the rail to the start of its 100-row window.
test("selecting a visible workspace keeps its rail position", async ({ page }) => {
  await page.setViewportSize({ width: 1_000, height: 720 });
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page);

  await page.goto("/");
  await waitForConsoleReady(page);
  const list = page.getByTestId("workspace-list-scroll");
  const selected = page.getByTestId("workspace-card-ws_perf_0075");
  await selected.evaluate((element) => {
    const listElement = element.closest<HTMLElement>('[data-testid="workspace-list-scroll"]');
    if (!listElement) throw new Error("workspace list is missing");
    const controlsHeight = listElement.querySelector<HTMLElement>(":scope > .sticky")
      ?.offsetHeight ?? 0;
    const viewportTop = listElement.getBoundingClientRect().top + controlsHeight;
    listElement.scrollTo({
      top: listElement.scrollTop + element.getBoundingClientRect().top - viewportTop,
    });
  });
  await expect(selected).toBeVisible();
  const scrollTopBeforeSelection = await list.evaluate((element) => element.scrollTop);
  expect(scrollTopBeforeSelection).toBeGreaterThan(0);

  await selected.click();

  await expect(page.getByRole("button", { name: "Close inspector" })).toBeVisible();
  await expect(selected).toBeVisible();
  await expect.poll(() => list.evaluate((element) => element.scrollTop))
    .toBe(scrollTopBeforeSelection);
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hYbpe: a visible row
// from the next page must not replace preceding viewport rows with a spacer.
test("selecting a visible workspace keeps a boundary viewport covered", async ({ page }) => {
  await page.setViewportSize({ width: 1_000, height: 720 });
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page);

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect(page.getByText(`1–${PAGE_SIZE} of ${PAGE_SIZE * 2} loaded`, { exact: true }))
    .toBeVisible();

  const list = page.getByTestId("workspace-list-scroll");
  const firstWindowHeight = await list.locator('[data-testid^="workspace-card-"]').evaluateAll(
    (cards) => cards.reduce((height, card) => height + card.getBoundingClientRect().height, 0),
  );
  await list.evaluate((element, top) => element.scrollTo({ top }), firstWindowHeight - 200);

  const preceding = page.getByTestId("workspace-card-ws_perf_0100");
  const selected = page.getByTestId("workspace-card-ws_perf_0101");
  await expect(preceding).toBeVisible();
  await expect(selected).toBeVisible();

  await selected.click();

  await expect(page.getByRole("button", { name: "Close inspector" })).toBeVisible();
  const geometry = await list.evaluate((element) => {
    const rows = Array.from(
      element.querySelectorAll<HTMLElement>('[data-testid^="workspace-card-"]'),
    );
    const header = element.querySelector<HTMLElement>(":scope > .sticky");
    const viewport = element.getBoundingClientRect();
    return {
      firstRowTop: rows.at(0)?.getBoundingClientRect().top ?? Number.POSITIVE_INFINITY,
      lastRowBottom: rows.at(-1)?.getBoundingClientRect().bottom ?? Number.NEGATIVE_INFINITY,
      viewportTop: header?.getBoundingClientRect().bottom ?? viewport.top,
      viewportBottom: viewport.bottom,
    };
  });
  expect(geometry.firstRowTop).toBeLessThanOrEqual(geometry.viewportTop + 1);
  expect(geometry.lastRowBottom).toBeGreaterThanOrEqual(geometry.viewportBottom - 1);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);
});

test("virtualization remeasures variable rows after filtering and viewport resize", async ({
  page,
}) => {
  const overviewRequests: Array<string | null> = [];
  const filteredSize = PAGE_SIZE * 2;
  const variableTitle =
    "Variable height workspace with a deliberately verbose title that wraps as the workspace rail narrows ".repeat(2);
  await page.setViewportSize({ width: 1_000, height: 720 });
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => overviewRequests.push(cursor),
    resolvePageItem: (item) => {
      const index = Number.parseInt(item.workspace_id.slice(-4), 10);
      const includedByFilter = index <= PAGE_SIZE ||
        (index > PAGE_SIZE * 2 && index <= PAGE_SIZE * 3);
      const hasLongTitle = (index > 50 && index <= PAGE_SIZE) || index > 250;
      return {
        ...item,
        status: "completed",
        title: includedByFilter
          ? hasLongTitle
            ? `${variableTitle}${item.workspace_id}`
            : `Variable height compact ${item.workspace_id}`
          : item.title,
      };
    },
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  const loadMore = page.getByRole("button", { name: "Load more workspaces" });
  for (const [index, loaded] of [PAGE_SIZE * 2, PAGE_SIZE * 3].entries()) {
    // This test measures row geometry rather than pointer behavior. Dispatch
    // directly so Playwright does not scroll the moving footer and trigger the
    // separately covered near-bottom loader before activating the button.
    await loadMore.evaluate((button: HTMLButtonElement) => button.click());
    await expect(page.getByTestId("workspace-history-scope").locator("span")).toHaveText(
      `${loaded} loaded. More matching workspaces are available. Search and client-side filters cover loaded workspaces only.`,
    );
    await expect.poll(() => overviewRequests).toEqual(
      [null, ...Array.from({ length: index + 1 }, (_, offset) => String((offset + 1) * PAGE_SIZE))],
    );
  }

  await page.getByRole("button", { name: "Filters" }).click();
  await page.getByPlaceholder("Search workspaces").fill("Variable height");
  await expect(page.getByText(new RegExp(`of ${filteredSize} loaded$`))).toBeVisible();
  const list = page.getByTestId("workspace-list-scroll");
  await list.evaluate((element) => element.scrollTo({ top: 0 }));
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);

  const rowMetrics = async () => list.evaluate((element, totalRows) => {
    const rows = Array.from(
      element.querySelectorAll<HTMLElement>('[data-testid^="workspace-card-"]'),
    );
    const headerHeight = element.querySelector<HTMLElement>(":scope > .sticky")?.offsetHeight ?? 0;
    const footerHeight = element.querySelector<HTMLElement>(
      '[data-testid="workspace-history-scope"]',
    )?.offsetHeight ?? 0;
    const renderedHeight = rows.reduce((height, row) => height + row.getBoundingClientRect().height, 0);
    return {
      renderedAverage: renderedHeight / rows.length,
      unrenderedAverage: (element.scrollHeight - headerHeight - footerHeight - renderedHeight) /
        (totalRows - rows.length),
    };
  }, filteredSize);
  const visibleAnchor = async () => list.evaluate((element) => {
    const viewportTop = element.querySelector<HTMLElement>(":scope > .sticky")
      ?.getBoundingClientRect().bottom ?? element.getBoundingClientRect().top;
    const row = Array.from(
      element.querySelectorAll<HTMLElement>('[data-testid^="workspace-card-"]'),
    ).find((candidate) => candidate.getBoundingClientRect().bottom > viewportTop);
    const bounds = row?.getBoundingClientRect();
    return {
      id: row?.dataset.testid ?? null,
      offsetRatio: bounds ? (bounds.top - viewportTop) / bounds.height : null,
    };
  });
  const wideMetrics = await rowMetrics();
  const anchorScrollTop = await list.locator('[data-testid^="workspace-card-"]').evaluateAll(
    (cards) => cards.slice(0, 40).reduce(
      (height, card) => height + card.getBoundingClientRect().height,
      cards[40].getBoundingClientRect().height * 0.25,
    ),
  );
  await list.evaluate(
    (element, top) => element.scrollTo({ top }),
    anchorScrollTop,
  );
  const wideAnchor = await visibleAnchor();
  await page.setViewportSize({ width: 1_280, height: 720 });
  await expect.poll(async () => (await rowMetrics()).renderedAverage).toBeGreaterThan(
    wideMetrics.renderedAverage,
  );
  await expect.poll(async () => {
    const metrics = await rowMetrics();
    return Math.abs(metrics.renderedAverage - metrics.unrenderedAverage);
  }).toBeLessThan(2);
  await expect.poll(async () => (await visibleAnchor()).id).toBe(wideAnchor.id);
  const resizedAnchor = await visibleAnchor();
  expect(resizedAnchor.offsetRatio).toBeCloseTo(wideAnchor.offsetRatio ?? 0, 1);

  const standardFontMetrics = await rowMetrics();
  const standardFontAnchor = await visibleAnchor();
  await page.getByRole("button", { name: "Use larger font size" }).click();
  await expect.poll(async () => (await rowMetrics()).renderedAverage).toBeGreaterThan(
    standardFontMetrics.renderedAverage,
  );
  await expect.poll(async () => {
    const metrics = await rowMetrics();
    return Math.abs(metrics.renderedAverage - metrics.unrenderedAverage);
  }).toBeLessThan(2);
  await expect.poll(async () => (await visibleAnchor()).id).toBe(standardFontAnchor.id);

  const resizedHeight = await list.locator('[data-testid^="workspace-card-"]').evaluateAll(
    (cards) => cards.reduce((height, card) => height + card.getBoundingClientRect().height, 0),
  );
  await list.evaluate((element, top) => element.scrollTo({ top }), resizedHeight - 100);
  await expect.poll(async () =>
    list.locator('[data-testid^="workspace-card-"]').first().getAttribute("data-testid"),
  ).not.toBe("workspace-card-ws_perf_0001");
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);
  const boundaryGeometry = await list.evaluate((element) => {
    const rows = Array.from(
      element.querySelectorAll<HTMLElement>('[data-testid^="workspace-card-"]'),
    );
    const header = element.querySelector<HTMLElement>(":scope > .sticky");
    const viewport = element.getBoundingClientRect();
    return {
      firstRowTop: rows.at(0)?.getBoundingClientRect().top ?? Number.POSITIVE_INFINITY,
      lastRowBottom: rows.at(-1)?.getBoundingClientRect().bottom ?? Number.NEGATIVE_INFINITY,
      viewportTop: header?.getBoundingClientRect().bottom ?? viewport.top,
      viewportBottom: viewport.bottom,
    };
  });
  expect(boundaryGeometry.firstRowTop).toBeLessThanOrEqual(boundaryGeometry.viewportTop + 1);
  expect(boundaryGeometry.lastRowBottom).toBeGreaterThanOrEqual(
    boundaryGeometry.viewportBottom - 1,
  );
});

test("keeps the visible row anchored when a refresh changes row heights", async ({ page }) => {
  const expandedTitle =
    "Expanded after refresh with enough detail to wrap across several lines in the workspace rail ".repeat(4);
  let firstPageRequests = 0;
  await page.setViewportSize({ width: 1_000, height: 720 });
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => {
      if (cursor === null) firstPageRequests += 1;
    },
    resolvePageItem: (item) => {
      const index = Number.parseInt(item.workspace_id.slice(-4), 10);
      return firstPageRequests > 1 && index <= 50
        ? { ...item, title: `${expandedTitle}${item.workspace_id}` }
        : item;
    },
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  const list = page.getByTestId("workspace-list-scroll");
  const anchorScrollTop = await list.locator('[data-testid^="workspace-card-"]').evaluateAll(
    (cards) => cards.slice(0, 60).reduce(
      (height, card) => height + card.getBoundingClientRect().height,
      cards[60].getBoundingClientRect().height * 0.25,
    ),
  );
  await list.evaluate((element, top) => element.scrollTo({ top }), anchorScrollTop);

  const visibleAnchor = async () => list.evaluate((element) => {
    const viewportTop = element.querySelector<HTMLElement>(":scope > .sticky")
      ?.getBoundingClientRect().bottom ?? element.getBoundingClientRect().top;
    const row = Array.from(
      element.querySelectorAll<HTMLElement>('[data-testid^="workspace-card-"]'),
    ).find((candidate) => candidate.getBoundingClientRect().bottom > viewportTop);
    const bounds = row?.getBoundingClientRect();
    return {
      id: row?.dataset.testid ?? null,
      offsetRatio: bounds ? (bounds.top - viewportTop) / bounds.height : null,
    };
  });
  const beforeRefresh = await visibleAnchor();

  await expect.poll(() => firstPageRequests, { timeout: 10_000 }).toBeGreaterThan(1);
  await expect(page.getByTestId("workspace-title-ws_perf_0001")).toContainText(
    "Expanded after refresh",
  );
  await expect.poll(async () => (await visibleAnchor()).id).toBe(beforeRefresh.id);
  const afterRefresh = await visibleAnchor();
  expect(afterRefresh.offsetRatio).toBeCloseTo(beforeRefresh.offsetRatio ?? 0, 1);
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hJmGY: refreshing
// membership ahead of the viewport must preserve the visible workspace, not
// merely the same numeric scrollTop.
test("refresh preserves the visible workspace when membership shifts", async ({ page }) => {
  let firstPageRequests = 0;
  let prependWorkspace = false;
  let removeVisibleAnchor = false;
  const withTallAnchor = (item: ReturnType<typeof workspaceOverview>) =>
    item.workspace_id === "ws_perf_0150"
      ? {
          ...item,
          title: `Tall visible anchor ${"with wrapped content ".repeat(20)}`,
        }
      : item;
  await page.setViewportSize({ width: 1_000, height: 720 });
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => {
      if (cursor === null) firstPageRequests += 1;
    },
    resolvePageItems: (items, cursor) =>
      cursor === null && prependWorkspace
        ? [workspaceOverview(0), ...items.slice(0, -1)]
        : items,
    resolvePageItem: withTallAnchor,
    resolveBatchItem: (item) =>
      removeVisibleAnchor && item.workspace_id === "ws_perf_0150"
        ? null
        : withTallAnchor(item),
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect(page.getByText(`1–${PAGE_SIZE} of ${PAGE_SIZE * 2} loaded`, { exact: true }))
    .toBeVisible();
  await page.getByRole("button", { name: "Next workspace results" }).click();

  const list = page.getByTestId("workspace-list-scroll");
  const anchor = page.getByTestId("workspace-card-ws_perf_0150");
  await expect(anchor).toBeAttached();
  const anchorScrollTop = await anchor.evaluate((element) => {
    const listElement = element.closest<HTMLElement>('[data-testid="workspace-list-scroll"]');
    if (!listElement) throw new Error("workspace list is missing");
    const controlsHeight = listElement.querySelector<HTMLElement>(":scope > .sticky")
      ?.offsetHeight ?? 0;
    const listTop = listElement.getBoundingClientRect().top + controlsHeight;
    return listElement.scrollTop + element.getBoundingClientRect().top - listTop +
      element.getBoundingClientRect().height * 0.75;
  });
  await list.evaluate((element, top) => element.scrollTo({ top }), anchorScrollTop);
  await expect(anchor).toBeVisible();

  const visibleAnchor = () => list.evaluate((element) => {
    const viewportTop = element.querySelector<HTMLElement>(":scope > .sticky")
      ?.getBoundingClientRect().bottom ?? element.getBoundingClientRect().top;
    const row = Array.from(
      element.querySelectorAll<HTMLElement>('[data-testid^="workspace-card-"]'),
    ).find((candidate) => candidate.getBoundingClientRect().bottom > viewportTop);
    const bounds = row?.getBoundingClientRect();
    return {
      id: row?.dataset.testid ?? null,
      offsetRatio: bounds ? (bounds.top - viewportTop) / bounds.height : null,
    };
  });
  const beforeRefresh = await visibleAnchor();
  expect(beforeRefresh.id).toBe("workspace-card-ws_perf_0150");

  prependWorkspace = true;
  const requestsBeforePrepend = firstPageRequests;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => firstPageRequests).toBeGreaterThan(requestsBeforePrepend);
  await expect.poll(async () => (await visibleAnchor()).id).toBe(beforeRefresh.id);
  const afterPrepend = await visibleAnchor();
  expect(afterPrepend.offsetRatio).toBeCloseTo(beforeRefresh.offsetRatio ?? 0, 1);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);

  prependWorkspace = false;
  const requestsBeforeRemoval = firstPageRequests;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => firstPageRequests).toBeGreaterThan(requestsBeforeRemoval);
  await expect.poll(async () => (await visibleAnchor()).id).toBe(beforeRefresh.id);
  const afterRemoval = await visibleAnchor();
  expect(afterRemoval.offsetRatio).toBeCloseTo(beforeRefresh.offsetRatio ?? 0, 1);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);

  // Regression for PR #958 review thread PRRT_kwDOSJAM6s6hWaKU: if the
  // visible row disappears, keep the fallback where it was on screen.
  const fallback = page.getByTestId("workspace-card-ws_perf_0151");
  const fallbackViewportDelta = () => fallback.evaluate((element) => {
    const listElement = element.closest<HTMLElement>('[data-testid="workspace-list-scroll"]');
    if (!listElement) throw new Error("workspace list is missing");
    const viewportTop = listElement.querySelector<HTMLElement>(":scope > .sticky")
      ?.getBoundingClientRect().bottom ?? listElement.getBoundingClientRect().top;
    return element.getBoundingClientRect().top - viewportTop;
  });
  const fallbackDeltaBeforeAnchorRemoval = await fallbackViewportDelta();
  removeVisibleAnchor = true;
  const requestsBeforeAnchorRemoval = firstPageRequests;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => firstPageRequests).toBeGreaterThan(requestsBeforeAnchorRemoval);
  await expect(page.getByTestId("workspace-card-ws_perf_0150")).toHaveCount(0);
  await expect.poll(fallbackViewportDelta).toBeCloseTo(fallbackDeltaBeforeAnchorRemoval, 1);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE - 1);
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hIfD1: a
// programmatic anchor restore must still move the virtual window when shorter
// rows expose content beyond the previously mounted 100-card range.
test("height restore keeps the viewport covered across a virtual-window boundary", async ({
  page,
}) => {
  const wrappedTitle =
    "A deliberately long workspace title that wraps throughout the narrow workspace rail ".repeat(5);
  let useCompactTitles = false;
  const resizeItem = (item: ReturnType<typeof workspaceOverview>) => ({
    ...item,
    title: useCompactTitles
      ? `Compact ${item.workspace_id}`
      : `${wrappedTitle}${item.workspace_id}`,
  });
  await page.setViewportSize({ width: 1_280, height: 1_160 });
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    resolvePageItem: resizeItem,
    resolveBatchItem: resizeItem,
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  for (const loaded of [PAGE_SIZE * 2, PAGE_SIZE * 3]) {
    await page.getByRole("button", { name: "Load more workspaces" }).click();
    await expect(page.getByTestId("workspace-history-scope")).toContainText(`${loaded} loaded`);
  }

  const list = page.getByTestId("workspace-list-scroll");
  await list.evaluate((element) => element.scrollTo({ top: 0 }));
  await expect(page.getByTestId("workspace-card-ws_perf_0001")).toBeVisible();
  await page.getByRole("button", { name: "Next workspace results" }).click();
  const anchor = page.getByTestId("workspace-card-ws_perf_0198");
  await expect(anchor).toBeAttached();
  const anchorScrollTop = await anchor.evaluate((element) => {
    const listElement = element.closest<HTMLElement>('[data-testid="workspace-list-scroll"]');
    if (!listElement) throw new Error("workspace list is missing");
    const controlsHeight = listElement.querySelector<HTMLElement>(":scope > .sticky")
      ?.offsetHeight ?? 0;
    return listElement.scrollTop + element.getBoundingClientRect().top -
      listElement.getBoundingClientRect().top - controlsHeight;
  });
  await list.evaluate((element, top) => element.scrollTo({ top }), anchorScrollTop);
  await expect(anchor).toBeVisible();

  const viewportCoverage = () => list.evaluate((element) => {
    const rows = Array.from(
      element.querySelectorAll<HTMLElement>('[data-testid^="workspace-card-"]'),
    );
    const viewport = element.getBoundingClientRect();
    return {
      firstRowTop: rows.at(0)?.getBoundingClientRect().top ?? Number.POSITIVE_INFINITY,
      lastRowBottom: rows.at(-1)?.getBoundingClientRect().bottom ?? Number.NEGATIVE_INFINITY,
      viewportTop: element.querySelector<HTMLElement>(":scope > .sticky")
        ?.getBoundingClientRect().bottom ?? viewport.top,
      viewportBottom: viewport.bottom,
    };
  });
  const expandedAnchorHeight = await anchor.evaluate(
    (element) => element.getBoundingClientRect().height,
  );
  const beforeResize = await viewportCoverage();
  expect(beforeResize.firstRowTop).toBeLessThanOrEqual(beforeResize.viewportTop + 1);
  expect(beforeResize.lastRowBottom).toBeGreaterThanOrEqual(beforeResize.viewportBottom - 1);

  useCompactTitles = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByTestId("workspace-title-ws_perf_0198")).toHaveText(
    "Compact ws_perf_0198",
  );
  await expect.poll(
    () => anchor.evaluate((element) => element.getBoundingClientRect().height),
  ).toBeLessThan(expandedAnchorHeight);
  await expect.poll(async () => (await viewportCoverage()).lastRowBottom).toBeGreaterThanOrEqual(
    (await viewportCoverage()).viewportBottom - 1,
  );
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hDQDq: selections
// retained across overview render windows must not mount one live column per ID.
test("fullscreen logs cap retained selections across overview windows", async ({ page }) => {
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page);

  await page.goto("/");
  await waitForConsoleReady(page);
  for (const index of [1, 2, 3]) {
    await page.getByLabel(`Select Performance workspace ${index} for fullscreen logs`).check();
  }

  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await page.getByRole("button", { name: "Next workspace results" }).click();
  for (const index of [101, 102, 103]) {
    await page.getByLabel(`Select Performance workspace ${index} for fullscreen logs`).check();
  }

  await expect(page.getByText("6 selected for logs; first 5 will open", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Open logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  await expect(modal.getByText("5 workspace columns", { exact: true })).toBeVisible();
  for (const workspaceId of ["ws_perf_0001", "ws_perf_0002", "ws_perf_0003", "ws_perf_0101", "ws_perf_0102"]) {
    await expect(modal.getByText(workspaceId, { exact: true })).toBeVisible();
  }
  await expect(modal.getByText("ws_perf_0103", { exact: true })).toHaveCount(0);
});

test("routine refresh updates and removes retained workspaces outside page one", async ({ page }) => {
  const batchRequests: string[][] = [];
  let refreshRetained = false;
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onBatchRequest: (workspaceIds) => batchRequests.push(workspaceIds),
    resolveBatchItem: (item) => {
      if (!refreshRetained) {
        return item;
      }
      if (item.workspace_id === "ws_perf_0101") {
        return {
          ...item,
          status: "completed",
          updated_at: "2026-09-10T13:00:00.000Z",
        };
      }
      return item.workspace_id === "ws_perf_0102" ? null : item;
    },
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect(
    page.getByText(`1–${PAGE_SIZE} of ${PAGE_SIZE * 2} loaded`, { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Next workspace results" }).click();
  const retainedCard = page.getByTestId("workspace-card-ws_perf_0101");
  await expect(retainedCard.getByText("running", { exact: true })).toBeVisible();
  await expect(page.getByTestId("workspace-card-ws_perf_0102")).toBeVisible();
  await expect(page.getByText(`101–200 of ${PAGE_SIZE * 2} loaded`, { exact: true })).toBeVisible();

  refreshRetained = true;
  batchRequests.length = 0;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();

  await expect.poll(() => batchRequests.length).toBeGreaterThan(0);
  expect(batchRequests[0]).toContain("ws_perf_0101");
  expect(batchRequests[0]).toContain("ws_perf_0102");
  expect(batchRequests[0]).not.toContain("ws_perf_0001");
  expect(batchRequests[0].length).toBeLessThanOrEqual(200);
  await expect(retainedCard.getByText("completed", { exact: true })).toBeVisible();
  await expect(page.getByTestId("workspace-card-ws_perf_0102")).toHaveCount(0);
  await expect(
    page.getByText(`1–${PAGE_SIZE} of ${PAGE_SIZE * 2 - 1} loaded`, { exact: true }),
  ).toBeVisible();
  await expect(page.locator('[data-testid^="workspace-card-"]').first()).toHaveAttribute(
    "data-testid",
    "workspace-card-ws_perf_0101",
  );
  await expect(page.getByRole("button", { name: "Previous workspace results" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Next workspace results" })).toBeEnabled();
});

test("bounds retained-history refresh work per poll and rotates across loaded rows", async ({
  page,
}) => {
  const overviewRequests: Array<string | null> = [];
  const batchRequests: string[][] = [];
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => overviewRequests.push(cursor),
    onBatchRequest: (workspaceIds) => batchRequests.push(workspaceIds),
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  const loadedSummary = page.getByText(/^\d+–\d+ of \d+ loaded$/);
  const loadedCount = async () => {
    if (await loadedSummary.count() === 0) {
      return PAGE_SIZE;
    }
    const match = (await loadedSummary.first().textContent())?.match(/of (\d+) loaded/);
    return Number(match?.[1] ?? 0);
  };
  for (let attempt = 0; attempt < 5 && await loadedCount() < 600; attempt += 1) {
    const before = await loadedCount();
    await page.getByRole("button", { name: "Load more workspaces" }).click();
    await expect.poll(loadedCount).toBeGreaterThan(before);
  }
  expect(await loadedCount()).toBeGreaterThanOrEqual(600);

  overviewRequests.length = 0;
  batchRequests.length = 0;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => batchRequests.length).toBeGreaterThan(0);

  const batchesAfterFirstRefresh = batchRequests.length;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => batchRequests.length).toBeGreaterThan(batchesAfterFirstRefresh);

  const firstPageRequests = overviewRequests.filter((cursor) => cursor === null).length;
  expect(batchRequests.length).toBeLessThanOrEqual(firstPageRequests);

  expect(batchRequests[0].length).toBeLessThanOrEqual(200);
  expect(batchRequests[1].length).toBeLessThanOrEqual(200);
  expect(batchRequests[0]).not.toContain("ws_perf_0001");
  expect(batchRequests[1]).not.toContain("ws_perf_0001");
  expect(batchRequests[1].some((workspaceId) => batchRequests[0].includes(workspaceId))).toBe(false);
});

test("stalled retained history does not block first-page publication or the next poll", async ({
  page,
}) => {
  let delayRetainedBatch = false;
  let refreshedFirstPage = false;
  const firstPageRequests: number[] = [];
  const batchRequests: string[][] = [];
  await mockAwfConsoleApi(page);
  const releaseHistory = await installLargeFleetOverview(page, {
    onRequest: (cursor) => {
      if (cursor === null) {
        firstPageRequests.push(Date.now());
      }
    },
    onBatchRequest: (workspaceIds) => batchRequests.push(workspaceIds),
    shouldDelayBatch: () => delayRetainedBatch,
    resolvePageItem: (item) =>
      refreshedFirstPage && item.workspace_id === "ws_perf_0001"
        ? { ...item, title: "Fresh first-page workspace" }
        : item,
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect(
    page.getByText(`1–${PAGE_SIZE} of ${PAGE_SIZE * 2} loaded`, { exact: true }),
  ).toBeVisible();

  delayRetainedBatch = true;
  refreshedFirstPage = true;
  const firstPagesBeforeRefresh = firstPageRequests.length;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();

  await expect.poll(() => batchRequests.length).toBeGreaterThan(0);
  await expect(page.getByText("Fresh first-page workspace", { exact: true })).toBeVisible({
    timeout: 2_000,
  });
  await expect
    .poll(() => firstPageRequests.length, { timeout: 7_000 })
    .toBeGreaterThan(firstPagesBeforeRefresh + 1);

  await releaseHistory();
});

test("stalled selected-workspace lookup releases the next overview poll", async ({ page }) => {
  const firstPageRequests: number[] = [];
  const batchRequests: string[][] = [];
  await mockAwfConsoleApi(page);
  const releaseSelectedLookups = await installLargeFleetOverview(page, {
    onRequest: (cursor) => {
      if (cursor === null) {
        firstPageRequests.push(Date.now());
      }
    },
    onBatchRequest: (workspaceIds) => batchRequests.push(workspaceIds),
    shouldDelayBatch: () => true,
  });

  await page.goto("/?workspaceId=ws_perf_1301");
  await expect.poll(() => batchRequests).toEqual([["ws_perf_1301"]]);
  await expect
    .poll(() => firstPageRequests.length, { timeout: 18_000 })
    .toBeGreaterThan(1);

  await releaseSelectedLookups();
});

test("failed retained history does not discard a successful first-page refresh", async ({
  page,
}) => {
  let failRetainedBatch = false;
  let refreshedFirstPage = false;
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    shouldFailBatch: () => failRetainedBatch,
    resolvePageItem: (item) =>
      refreshedFirstPage && item.workspace_id === "ws_perf_0001"
        ? { ...item, title: "First page survived history failure" }
        : item,
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect(
    page.getByText(`1–${PAGE_SIZE} of ${PAGE_SIZE * 2} loaded`, { exact: true }),
  ).toBeVisible();

  failRetainedBatch = true;
  refreshedFirstPage = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();

  await expect(
    page.getByText("First page survived history failure", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("retained history unavailable", { exact: true })).toBeVisible();
});

test("routine refresh drops a selected retained workspace excluded by its repository query", async ({
  page,
}) => {
  let excludeSelectedRetained = false;
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    resolveBatchItem: (item) =>
      excludeSelectedRetained && item.workspace_id === "ws_perf_0202"
        ? { ...item, repo_url: "https://example.com/odd.git" }
        : item,
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByRole("button", { name: "Filters" }).click();
  await page.getByPlaceholder("exact repo filter").fill("https://example.com/even.git");
  await expect(page.getByTestId("workspace-card-ws_perf_0002")).toBeVisible();
  await expect(page.getByTestId("workspace-card-ws_perf_0001")).toHaveCount(0);
  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect(page.getByText(`1–${PAGE_SIZE} of ${PAGE_SIZE * 2} loaded`, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Next workspace results" }).click();
  await page.getByTestId("workspace-card-ws_perf_0202").click();
  await expect(page).toHaveURL(/workspaceId=ws_perf_0202/);

  excludeSelectedRetained = true;

  await expect(page.getByTestId("workspace-card-ws_perf_0202")).toHaveCount(0, {
    timeout: 10_000,
  });
  await expect(page).not.toHaveURL(/workspaceId=ws_perf_0202/);
});

test("a deep link resolves an older workspace without mounting the intervening fleet", async ({
  page,
}) => {
  const overviewRequests: Array<string | null> = [];
  const batchRequests: string[][] = [];
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => overviewRequests.push(cursor),
    onBatchRequest: (workspaceIds) => batchRequests.push(workspaceIds),
  });

  await page.goto("/?workspaceId=ws_perf_1301");
  await expect.poll(() => batchRequests).toEqual([["ws_perf_1301"]]);
  await expect(page.getByTestId("workspace-card-ws_perf_1301")).toBeVisible();
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Close inspector" })).toBeVisible();
  await expect(page).toHaveURL(/workspaceId=ws_perf_1301/);
  expect(overviewRequests).toEqual([null]);
  expect(batchRequests).toEqual([["ws_perf_1301"]]);
});

test("deep link remains isolated after a transient selected-row lookup failure", async ({
  page,
}) => {
  const batchRequests: string[][] = [];
  let failNextBatch = true;
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onBatchRequest: (workspaceIds) => batchRequests.push(workspaceIds),
    shouldFailBatch: () => {
      if (!failNextBatch) {
        return false;
      }
      failNextBatch = false;
      return true;
    },
  });

  await page.goto("/?workspaceId=ws_perf_1301");
  await expect
    .poll(() => batchRequests.length, { timeout: 18_000 })
    .toBeGreaterThan(1);
  await expect(page.getByTestId("workspace-card-ws_perf_1301")).toBeVisible();
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Close inspector" })).toBeVisible();
  await expect(page).toHaveURL(/workspaceId=ws_perf_1301/);
});

test("deep link preserves overview pagination after refresh", async ({ page }) => {
  const overviewRequests: Array<string | null> = [];
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => overviewRequests.push(cursor),
  });

  await page.goto("/?workspaceId=ws_perf_1301");
  await expect(page.getByTestId("workspace-card-ws_perf_1301")).toBeVisible();

  await page.locator("header").getByRole("button", { name: "Refresh" }).evaluate(
    (button: HTMLButtonElement) => button.click(),
  );

  await expect(page.getByText("1–100 of 101 loaded", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Load more workspaces" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Close inspector" })).toBeVisible();
  await expect(page).toHaveURL(/workspaceId=ws_perf_1301/);

  await page.getByRole("button", { name: "Load more workspaces" }).click();
  await expect.poll(() => overviewRequests).toContain("100");
  await expect(page.getByText(/of 201 loaded$/)).toBeVisible();
});

test("repository filtering drops a selected off-page workspace excluded by the new query", async ({
  page,
}) => {
  const batchRequests: string[][] = [];
  const overviewRequests: Array<string | null> = [];
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onBatchRequest: (workspaceIds) => batchRequests.push(workspaceIds),
    onRequest: (cursor) => overviewRequests.push(cursor),
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  // This scenario covers query-driven selection removal, while scroll-to-footer
  // autoload coordination has dedicated coverage below. Dispatch directly so
  // Playwright does not scroll the footer and race that separate load path.
  await page.getByRole("button", { name: "Load more workspaces" }).evaluate(
    (button: HTMLButtonElement) => button.click(),
  );
  await expect
    .poll(() => overviewRequests, { timeout: 10_000 })
    .toContain(String(PAGE_SIZE));
  await expect(
    page.getByText(`1–${PAGE_SIZE} of ${PAGE_SIZE * 2} loaded`, { exact: true }),
  ).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: "Next workspace results" }).click();
  await page.getByTestId("workspace-card-ws_perf_0101").click();
  await expect(page).toHaveURL(/workspaceId=ws_perf_0101/);

  await page.getByRole("button", { name: "Filters" }).click();
  await page.getByPlaceholder("exact repo filter").fill("https://example.com/even.git");

  await expect.poll(() =>
    batchRequests.some(
      (workspaceIds) =>
        workspaceIds.length === 1 && workspaceIds[0] === "ws_perf_0101",
    ),
  ).toBe(true);
  await expect(page.getByTestId("workspace-card-ws_perf_0101")).toHaveCount(0);
  await expect(page).not.toHaveURL(/workspaceId=ws_perf_0101/);
});

test("failed scroll loading keeps an accessible one-page retry", async ({ page }) => {
  const overviewRequests: Array<string | null> = [];
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    failFirstContinuation: true,
    onRequest: (cursor) => overviewRequests.push(cursor),
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  const list = page.getByTestId("workspace-list-scroll");
  await list.evaluate((element) => element.scrollTo({ top: element.scrollHeight }));
  await expect(page.getByRole("button", { name: "Retry loading older workspaces" })).toBeVisible();
  expect(overviewRequests).toEqual([null, "100"]);

  await page.getByRole("button", { name: "Retry loading older workspaces" }).click();
  await expect(page.getByText(`1–${PAGE_SIZE} of ${PAGE_SIZE * 2} loaded`, { exact: true })).toBeVisible();
  expect(overviewRequests).toEqual([null, "100", "100"]);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(PAGE_SIZE);
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hPe-d: a settled
// near-bottom autoload must not consume a later explicit fallback click.
test("settled scroll loading leaves the Load more fallback actionable", async ({ page }) => {
  const overviewRequests: Array<string | null> = [];
  await mockAwfConsoleApi(page);
  await installLargeFleetOverview(page, {
    onRequest: (cursor) => overviewRequests.push(cursor),
    resolvePageItems: (items, cursor) => cursor === "100" ? items.slice(0, 1) : items,
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  const list = page.getByTestId("workspace-list-scroll");
  await list.evaluate((element) => element.scrollTo({ top: element.scrollHeight }));
  await expect(page.getByText("1–100 of 101 loaded", { exact: true })).toBeVisible();
  expect(overviewRequests).toEqual([null, "100"]);

  await page.getByRole("button", { name: "Next workspace results" }).click();
  await expect(page.getByTestId("workspace-card-ws_perf_0101")).toBeVisible();
  const loadMore = page.getByRole("button", { name: "Load more workspaces" });
  await loadMore.focus();
  await loadMore.press("Enter");

  await expect.poll(() => overviewRequests).toEqual([null, "100", "101"]);
});
