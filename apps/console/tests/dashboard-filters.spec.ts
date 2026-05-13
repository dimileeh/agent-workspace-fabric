import { expect, type Page, test } from "@playwright/test";

const now = "2026-05-02T12:00:00.000Z";

test.beforeEach(async ({ page }) => {
  await mockAwfApi(page);
});

test("dashboard filters for agents and exact models", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  // Expand filters
  const expandButton = page.getByRole("button", { name: "Filters" });
  await expect(expandButton).toBeVisible();
  if (await expandButton.getAttribute("aria-expanded") === "false") {
    await expandButton.click();
  }
  // Find agent selector and select opencode
  const agentGroup = page.getByRole("group", { name: "Agent" });
  await agentGroup.getByRole("button", { name: /Agent all/ }).click();
  await agentGroup.getByLabel("opencode").check();

  // Validate agent filter
  await expect(page.getByText("agent opencode", { exact: false })).toBeVisible();

  // Multi-select keeps Gemini and OpenCode visible together.
  await agentGroup.getByLabel("gemini").check();
  await expect(page.getByText("Gemini workspace").first()).toBeVisible();
  await expect(page.getByText("OpenCode workspace").first()).toBeVisible();
  await agentGroup.getByLabel("all").check();

  // Find model selector and select exact models
  const modelGroup = page.getByRole("group", { name: "Model" });
  await modelGroup.getByRole("button", { name: /Model all/ }).click();
  await expect(modelGroup.getByLabel("gemini-3.1-pro-preview")).toBeVisible();
  await expect(modelGroup.getByLabel("ollama/glm-5.1:cloud")).toBeVisible();

  const statusBox = await page.getByRole("group", { name: "Status" }).boundingBox();
  const agentBox = await agentGroup.boundingBox();
  const modelBox = await modelGroup.boundingBox();
  expect(statusBox).not.toBeNull();
  expect(agentBox).not.toBeNull();
  expect(modelBox).not.toBeNull();
  expect(modelBox!.y).toBeGreaterThan(statusBox!.y + statusBox!.height - 1);
  expect(modelBox!.x).toBeCloseTo(statusBox!.x, 1);
  expect(modelBox!.x + modelBox!.width).toBeLessThanOrEqual(agentBox!.x + agentBox!.width + 1);

  await modelGroup.getByLabel("gemini-3.1-pro-preview").check();

  // Validate workspace list limits
  await expect(page.getByText("Gemini workspace").first()).toBeVisible();
  await expect(page.getByText("OpenCode workspace").first()).not.toBeVisible();

  await modelGroup.getByText("ollama/glm-5.1:cloud", { exact: true }).click();
  await expect(modelGroup.getByRole("checkbox", { name: "ollama/glm-5.1:cloud" })).toBeChecked();
  await expect(modelGroup.getByRole("menu", { name: "Model options" })).toBeVisible();
  await expect(page.getByText("Gemini workspace").first()).toBeVisible();
  await expect(page.getByText("OpenCode workspace").first()).toBeVisible();
  await expect(page.getByText("model gemini-3.1-pro-preview, ollama/glm-5.1:cloud", { exact: false })).toBeVisible();

  await page.getByPlaceholder("Search workspaces").click();
  await expect(modelGroup.getByRole("menu", { name: "Model options" })).not.toBeVisible();
  await modelGroup.getByRole("button", { name: /Model/ }).click();
  await modelGroup.getByLabel("all").check();

  const statusGroup = page.getByRole("group", { name: "Status" });
  await statusGroup.getByRole("button", { name: /Status all/ }).click();
  await statusGroup.getByLabel("completed").check();
  await expect(page.getByText("Completed workspace").first()).toBeVisible();
  await expect(page.getByText("Gemini workspace").first()).not.toBeVisible();
  await statusGroup.getByLabel("running").check();
  await expect(page.getByText("Gemini workspace").first()).toBeVisible();
  await expect(page.getByText("OpenCode workspace").first()).toBeVisible();
  await expect(page.getByText("Completed workspace").first()).toBeVisible();

  // Validate filter summary
  await expect(page.getByText("status completed, running", { exact: false })).toBeVisible();
});

test("workspace list keeps long titles clear of status badges", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  const card = page.getByTestId("workspace-card-ws_long_title");
  await expect(card).toBeVisible();

  const titleBox = await card
    .getByText("test(parity): guard MCP parity matrix status against surface drift", { exact: true })
    .boundingBox();
  const statusBox = await card.getByText("monitoring_pr", { exact: true }).boundingBox();

  expect(titleBox).not.toBeNull();
  expect(statusBox).not.toBeNull();
  expect(titleBox!.x + titleBox!.width).toBeLessThanOrEqual(statusBox!.x - 4);
});

test("workspace PR links open externally without navigating the console", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "open", {
      configurable: true,
      value: (url: string, target?: string, features?: string) => {
        (window as typeof window & { __openedUrls?: unknown[] }).__openedUrls = [
          ...((window as typeof window & { __openedUrls?: unknown[] }).__openedUrls ?? []),
          { url, target, features },
        ];
        return null;
      },
    });
  });
  await page.goto("/");
  await waitForConsoleReady(page);

  const beforeUrl = page.url();
  await page
    .getByTestId("workspace-card-ws_long_title")
    .getByRole("link", { name: "PR #123" })
    .click();

  await expect(page).toHaveURL(beforeUrl);
  await expect
    .poll(async () =>
      page.evaluate(() => (window as typeof window & { __openedUrls?: unknown[] }).__openedUrls ?? []),
    )
    .toEqual([
      {
        url: "https://github.com/example/awf/pull/123",
        target: "_blank",
        features: "noopener,noreferrer",
      },
    ]);
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
      await fulfillJson(
        route,
        listEnvelope([
          workspaceOverview("ws_1", "Gemini workspace", "gemini", "gemini-3.1-pro-preview", "running"),
          workspaceOverview(
            "ws_long_title",
            "test(parity): guard MCP parity matrix status against surface drift",
            "gemini",
            "gemini-3.1-pro-preview",
            "monitoring_pr"
          ),
          workspaceOverview("ws_2", "OpenCode workspace", "opencode", "ollama/glm-5.1:cloud", "running"),
          workspaceOverview("ws_3", "Completed workspace", "codex", "gpt-5.5", "completed"),
        ])
      );
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
    if (path.startsWith("/api/awf/workspaces/") && !path.includes("stream")) {
      const parts = path.split("/");
      const id = parts[4];
      if (path.endsWith("/runtime")) await fulfillJson(route, { stack_state: "stopped", services: [], app_endpoints: [] });
      else if (path.endsWith("/events")) await fulfillJson(route, listEnvelope([]));
      else if (path.endsWith("/operations")) await fulfillJson(route, listEnvelope([]));
      else if (path.endsWith("/logs")) await fulfillJson(route, listEnvelope([]));
      else await fulfillJson(route, workspaceOverview(id, "detail", "codex", "gpt-5.5"));
      return;
    }
    if (path.endsWith("/stream")) {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache" },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: "ws_1" })}\n\n`,
      });
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

function workspaceOverview(id: string, title: string, agent: string, model: string, status = "running") {
  return {
    workspace_id: id,
    task_id: `task-${id}`,
    title,
    task_prompt: "Test prompt",
    repo_url: "https://github.com/example/awf",
    base_branch: "main",
    branch_name: `branch/${id}`,
    agent,
    agent_model: model,
    agent_effort: "low",
    agent_model_source: "mock",
    agent_effort_source: "mock",
    status,
    created_at: now,
    updated_at: now,
    lifecycle: [],
    llm_usage: { status: "unavailable" },
    coordination_warnings: [],
    pr_url: id === "ws_long_title" ? "https://github.com/example/awf/pull/123" : null,
  };
}

function resourceSaturation() {
  return {
    generated_at: now,
    workspace_counts: { by_status: {}, active_total: 0 },
    worker: { max_concurrent_provisions: 10, max_concurrent_executions: 10 },
    resource_defaults: { steady_cpu: 1, steady_memory_gb: 2, peak_cpu: 2, peak_memory_gb: 4 },
    reserved_resources: { active_workspace_count: 0, steady_cpu: 0, steady_memory_gb: 0, peak_cpu: 0, peak_memory_gb: 0, disk_mb: 0, dind_slots: 0 },
    capacity: {},
    concurrency: {},
    disk: { ok: true },
    admission: { ok: true },
  };
}

function workspaceReliability() {
  return {
    window_hours: 24,
    total_completed: 0,
    total_failed: 0,
    total_cancelled: 0,
    reliability_percentage: 100,
  };
}
