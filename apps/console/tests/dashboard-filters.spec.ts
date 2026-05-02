import { expect, type Page, test } from "@playwright/test";

const now = "2026-05-02T12:00:00.000Z";

test.beforeEach(async ({ page }) => {
  await mockAwfApi(page);
});

test("dashboard filters for agents and exact models", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  // Expand filters
  const expandButton = page.locator('button[aria-expanded="false"]').filter({ hasText: "Filters" });
  if (await expandButton.isVisible()) {
    await expandButton.click();
  }

  // Find agent dropdown and select opencode
  const agentSelect = page.getByLabel("Agent");
  await agentSelect.selectOption("opencode");

  // Validate agent filter
  await expect(page.getByText("agent opencode", { exact: false })).toBeVisible();

  // Switch back to all for models
  await agentSelect.selectOption("all");

  // Find model dropdown and select exact model
  const modelSelect = page.getByLabel("Model");
  await expect(modelSelect).toContainText("gemini-3.1-pro-preview");
  await expect(modelSelect).toContainText("ollama/glm-5.1:cloud");

  await modelSelect.selectOption("gemini-3.1-pro-preview");

  // Validate workspace list limits
  await expect(page.getByText("Gemini workspace").first()).toBeVisible();
  await expect(page.getByText("OpenCode workspace").first()).not.toBeVisible();

  // Validate filter summary
  await expect(page.getByText("model gemini-3.1-pro-preview", { exact: false })).toBeVisible();
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
          workspaceOverview("ws_1", "Gemini workspace", "gemini", "gemini-3.1-pro-preview"),
          workspaceOverview("ws_2", "OpenCode workspace", "opencode", "ollama/glm-5.1:cloud"),
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

function workspaceOverview(id: string, title: string, agent: string, model: string) {
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
    status: "running",
    created_at: now,
    updated_at: now,
    lifecycle: [],
    llm_usage: { status: "unavailable" },
    coordination_warnings: [],
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
