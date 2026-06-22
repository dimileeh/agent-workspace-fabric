import { expect, type Page, test } from "@playwright/test";

// PR-monitor HUMAN_WAIT escalation (awaiting human) console surfacing (#657):
//  - a flagged monitoring_pr workspace renders an "Awaiting human for …" badge;
//  - the "Awaiting human" KPI counts flagged workspaces, lands in Active, and is
//    EXCLUDED from Running (running+validating+pushing only — same non-Running rule
//    as blocked). NotifyHuman is NOT a pause, so the row stays monitoring_pr.

const now = "2026-06-20T12:00:00.000Z";
const awaitingSince = "2026-06-20T11:00:00.000Z";

test.beforeEach(async ({ page }) => {
  await mockAwfApi(page);
});

test("flagged monitoring_pr workspace renders the awaiting-human badge", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  const card = page.getByTestId("workspace-card-ws_awaiting1");
  await expect(card).toBeVisible();
  // The row stays monitoring_pr (NotifyHuman is not a pause / not a new status).
  await expect(card.getByText("monitoring_pr", { exact: true })).toBeVisible();
  // Awaiting-human attention badge.
  await expect(page.getByTestId("workspace-awaiting-human-ws_awaiting1")).toContainText(
    "Awaiting human for",
  );
});

test("Awaiting human KPI counts flagged; Active includes it but Running excludes it", async ({
  page,
}) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  // running:2 + validating:1 + pushing:1 => 4 (awaiting_human NOT folded in).
  await expect(kpiCard(page, "Running").locator(".kpi-value")).toHaveText("4");

  // awaiting_human:1 surfaced as its own KPI.
  await expect(kpiCard(page, "Awaiting human").locator(".kpi-value")).toHaveText("1");

  // Active is the server-side active_total (includes the monitoring_pr workspace).
  await expect(kpiCard(page, "Active").locator(".kpi-value")).toHaveText("5");
});

function kpiCard(page: Page, label: string) {
  return page
    .getByText(label, { exact: true })
    .locator("..")
    .filter({ has: page.locator(".kpi-value") });
}

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
      await fulfillJson(route, listEnvelope([awaitingOverview()]));
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

    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });
}

async function fulfillJson(route: Parameters<Parameters<Page["route"]>[1]>[0], body: unknown, status = 200) {
  await route.fulfill({ status, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
}

function listEnvelope<T>(items: T[]) {
  return { items, next_cursor: null, has_more: false };
}

function awaitingOverview() {
  return {
    workspace_id: "ws_awaiting1",
    task_id: "task_awaiting1",
    title: "PR awaiting a human",
    task_prompt: "ship it",
    repo_url: "https://github.com/test/repo",
    base_branch: "main",
    branch_name: "awf/ws_awaiting1",
    agent: "codex",
    agent_model: "gpt-5-codex",
    agent_effort: "high",
    agent_model_source: "default",
    agent_effort_source: "default",
    network_posture: "restricted",
    lifecycle: [],
    llm_usage: null,
    pricing: null,
    recovery: null,
    coordination_warnings: [],
    provider_readiness_preflight: null,
    status: "monitoring_pr",
    subphase: null,
    last_activity_at: awaitingSince,
    last_log_at: awaitingSince,
    is_stale_running: false,
    current_phase: "monitoring_pr",
    active_operation: null,
    last_event: {
      id: "evt_human_wait",
      workspace_id: "ws_awaiting1",
      event_type: "workspace.note",
      old_state: "monitoring_pr",
      new_state: "monitoring_pr",
      reason_code: "HUMAN_WAIT",
      payload: null,
      occurred_at: awaitingSince,
    },
    pr_url: "https://github.com/test/repo/pull/7",
    pr_number: 7,
    failure_reason: null,
    failure_message: null,
    attention_required: true,
    awaiting_human_since: awaitingSince,
    awaiting_human_reason: "blocking review requires a human",
    created_at: "2026-06-20T10:00:00.000Z",
    updated_at: awaitingSince,
  };
}

function resourceSaturation() {
  return {
    generated_at: now,
    workspace_counts: {
      by_status: { running: 2, validating: 1, pushing: 1, monitoring_pr: 1 },
      active_total: 5,
      requested: 0,
      provisioning: 0,
      ready: 0,
      running: 2,
      validating: 1,
      pushing: 1,
      monitoring_pr: 1,
      blocked: 0,
      recovering: 0,
      awaiting_human: 1,
      destroying: 0,
      completed: 0,
      failed: 0,
      cancelled: 0,
      destroyed: 0,
    },
    worker: { max_concurrent_provisions: 2, max_concurrent_executions: 4 },
    local_capacity: { cpu_cores: 8, memory_gb: 32, source: "docker", reason_code: null, detail: null },
    resource_defaults: { steady_cpu: 1, steady_memory_gb: 2, peak_cpu: 2, peak_memory_gb: 4 },
    reserved_resources: {
      active_workspace_count: 5,
      steady_cpu: 5,
      steady_memory_gb: 10,
      peak_cpu: 10,
      peak_memory_gb: 20,
      disk_mb: 10240,
      dind_slots: 0,
    },
    capacity: {
      steady_cpu: { limit: null, reserved: 5, available: 3, available_after_next_default: 2, reason_code: null },
      peak_cpu: { limit: null, reserved: 10, available: 6, available_after_next_default: 4, reason_code: null },
      steady_memory_gb: { limit: null, reserved: 10, available: 22, available_after_next_default: 20, reason_code: null },
      peak_memory_gb: { limit: null, reserved: 20, available: 12, available_after_next_default: 8, reason_code: null },
      disk_mb: { limit: null, reserved: 10240, available: 1048576, available_after_next_default: 1038336, reason_code: null },
      dind_slots: { limit: null, reserved: 0, available: null, available_after_next_default: null, reason_code: null },
      pressure_reasons: [],
    },
    allocated_resources: {
      active_workspace_count: 0,
      steady_cpu: 0,
      steady_memory_gb: 0,
      peak_cpu: 0,
      peak_memory_gb: 0,
      disk_mb: 0,
      dind_slots: 0,
    },
    allocated_capacity: {
      steady_cpu: { limit: null, reserved: 0, available: 8, available_after_next_default: 6, reason_code: null },
      peak_cpu: { limit: null, reserved: 0, available: 16, available_after_next_default: 12, reason_code: null },
      steady_memory_gb: { limit: null, reserved: 0, available: 32, available_after_next_default: 28, reason_code: null },
      peak_memory_gb: { limit: null, reserved: 0, available: 32, available_after_next_default: 28, reason_code: null },
      disk_mb: { limit: null, reserved: 0, available: 1060864, available_after_next_default: 1049600, reason_code: null },
      dind_slots: { limit: null, reserved: 0, available: null, available_after_next_default: null, reason_code: null },
      pressure_reasons: [],
    },
    capacity_queue: {
      queued_workspace_count: 0,
      oldest_workspace_id: null,
      oldest_wait_seconds: null,
      planned_resources: { steady_cpu: 0, steady_memory_gb: 0, peak_cpu: 0, peak_memory_gb: 0, disk_mb: 0, dind_slots: 0 },
      blocked_reason_counts: {},
    },
    concurrency: {
      provision: { limit: 2, in_use: 0, queued: 0, available: 2 },
      execution: { limit: 4, in_use: 4, queued: 0, available: 0 },
    },
    disk: {
      path: "/workspace",
      checked_path: "/workspace",
      total_bytes: 107374182400,
      used_bytes: 21474836480,
      free_bytes: 85899345920,
      percent_free: 80,
      threshold_bytes: 10737418240,
      ok: true,
      status: "ok",
      reason: "healthy",
      detail: null,
    },
    admission: { ok: true, status: "ok", reason: "healthy", detail: null },
  };
}

function workspaceReliability() {
  return {
    generated_at: now,
    window_start: now,
    since_hours: 24,
    status_counts: { running: 2, validating: 1, pushing: 1, monitoring_pr: 1 },
    failure_reason_counts: {},
    active_count: 5,
    destroying_count: 0,
    completed_count: 0,
    failed_count: 0,
    cancelled_count: 0,
    destroyed_count: 0,
    cleanup_failure_count: 0,
    stuck_count: 0,
    actionable_reason_count: 0,
    unactionable_reason_count: 0,
  };
}
