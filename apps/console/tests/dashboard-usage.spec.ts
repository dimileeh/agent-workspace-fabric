import { expect, type Page, test } from "@playwright/test";

const now = "2026-05-02T12:00:00.000Z";

type UsageKind = "available" | "unavailable" | "ccusage" | "ccusage_timeout";

const WORKSPACE_DEFS: { id: string; title: string; usage: UsageKind }[] = [
  { id: "ws_usage", title: "Usage Workspace", usage: "available" },
  { id: "ws_no_usage", title: "No Usage Workspace", usage: "unavailable" },
  { id: "ws_ccusage", title: "Ccusage Workspace", usage: "ccusage" },
  { id: "ws_ccusage_timeout", title: "Ccusage Timeout Workspace", usage: "ccusage_timeout" },
];

function llmUsageFor(usage: UsageKind) {
  switch (usage) {
    case "available":
      return { status: "available", input_tokens: 10, output_tokens: 20, total_tokens: 30, cost_estimate: 0.05, currency: "USD", source: "mock", reason: null };
    case "ccusage":
      return {
        status: "available",
        input_tokens: 111,
        cached_input_tokens: 900,
        output_tokens: 222,
        reasoning_output_tokens: 77,
        total_tokens: 1233,
        cost_estimate: 0.0123,
        currency: "USD",
        source: "ccusage",
        reason: null,
      };
    case "ccusage_timeout":
      return { status: "unavailable", input_tokens: null, output_tokens: null, total_tokens: null, cost_estimate: null, currency: null, source: "ccusage", reason: "ccusage_timeout" };
    default:
      return { status: "unavailable" };
  }
}

test.beforeEach(async ({ page }) => {
  await mockAwfApi(page);
});

test("dashboard displays llm usage correctly when available", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  // Open the workspace that has usage via its full-card open-details button.
  // The title text now sits in a pointer-events-none layer over this button, so
  // a click on the title text never lands. Use exact matching because the
  // aria-label is a substring of "...No Usage Workspace".
  await page
    .getByRole("button", { name: "Open workspace details for Usage Workspace", exact: true })
    .click();

  // Find the LLM usage block
  const usageBlock = page.locator("section").filter({ hasText: "LLM usage" }).first();
  await expect(usageBlock).toBeVisible();

  // Scope metric assertions to the usage block: a relative timestamp elsewhere
  // on the page (e.g. "generated 10 days ago") trips strict-mode on a bare getByText("10").
  await expect(usageBlock.getByText("Input", { exact: true })).toBeVisible();
  await expect(usageBlock.getByText("10", { exact: true })).toBeVisible();
  await expect(usageBlock.getByText("Output", { exact: true })).toBeVisible();
  await expect(usageBlock.getByText("20", { exact: true })).toBeVisible();
  await expect(usageBlock.getByText("Total", { exact: true })).toBeVisible();
  await expect(usageBlock.getByText("30", { exact: true })).toBeVisible();
  await expect(usageBlock.getByText("Cost", { exact: true })).toBeVisible();
  await expect(usageBlock.getByText("$0.05", { exact: false })).toBeVisible();
});

test("dashboard hides llm usage metrics when unavailable", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  // Open the workspace that has no usage via its full-card open-details button.
  await page
    .getByRole("button", { name: "Open workspace details for No Usage Workspace", exact: true })
    .click();

  // Actually wait, our UsageSummaryBlock renders a div, and it is inside some Panel in details.
  // Wait, let's verify what the text is.
  await expect(page.getByText("LLM usage").first()).toBeVisible();
  await expect(page.getByText("unavailable").first()).toBeVisible();

  // Input/Output/Total should NOT be visible.
  await expect(page.getByText("Input")).not.toBeVisible();
  await expect(page.getByText("Output")).not.toBeVisible();
  await expect(page.getByText("Total")).not.toBeVisible();
});

test("dashboard renders ccusage-backed totals and source label", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  await page
    .getByRole("button", { name: "Open workspace details for Ccusage Workspace", exact: true })
    .click();

  // Scope to the usage block itself: the surrounding details section also
  // carries the workspace title/id/branch ("Ccusage Workspace", "ws_ccusage"),
  // which would otherwise trip strict-mode on a substring match for "ccusage".
  const usageBlock = page.getByTestId("llm-usage");
  await expect(usageBlock).toBeVisible();
  await expect(usageBlock.getByText("900", { exact: true })).toBeVisible();
  await expect(usageBlock.getByText("77", { exact: true })).toBeVisible();
  await expect(usageBlock.getByText("1,233", { exact: true })).toBeVisible();
  await expect(usageBlock.getByText("$0.0123", { exact: true })).toBeVisible();
  await expect(usageBlock.getByText("pricing not configured", { exact: false })).not.toBeVisible();
  // The ccusage source is surfaced as a friendly provenance label.
  await expect(usageBlock.getByText("ccusage", { exact: false })).toBeVisible();
});

test("dashboard renders ccusage unavailable reason", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  await page
    .getByRole("button", { name: "Open workspace details for Ccusage Timeout Workspace", exact: true })
    .click();

  await expect(page.getByText("LLM usage").first()).toBeVisible();
  await expect(page.getByText("unavailable").first()).toBeVisible();
  // Reason code is rendered through its friendly label.
  await expect(page.getByText("ccusage timed out", { exact: false }).first()).toBeVisible();
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
        listEnvelope(WORKSPACE_DEFS.map((def) => workspaceOverview(def.id, def.title, def.usage)))
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
      else {
        const def = WORKSPACE_DEFS.find((d) => d.id === id) ?? WORKSPACE_DEFS[1];
        await fulfillJson(route, workspaceOverview(def.id, def.title, def.usage));
      }
      return;
    }
    if (path.endsWith("/stream")) {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache" },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: "ws_usage" })}\n\n`,
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

function workspaceOverview(id: string, title: string, usage: UsageKind) {
  return {
    id,
    workspace_id: id,
    task_id: `task-${id}`,
    title,
    task_prompt: "Test prompt",
    repo_url: "https://github.com/example/awf",
    base_branch: "main",
    branch_name: `branch/${id}`,
    agent: "codex",
    agent_model: "gpt-5.5",
    agent_effort: "low",
    agent_model_source: "mock",
    agent_effort_source: "mock",
    status: "running",
    created_at: now,
    updated_at: now,
    lifecycle: [],
    llm_usage: llmUsageFor(usage),
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
