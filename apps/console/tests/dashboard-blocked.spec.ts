import { expect, type Page, test } from "@playwright/test";

// Protected-file pause (blocked) console surfacing:
//  - the blocked workspace renders the pause glyph + a "Blocked for …" badge;
//  - the "Awaiting operator" KPI counts blocked, lands in Active, and is EXCLUDED
//    from Running (running+validating+pushing only — the PR #598 contract);
//  - the inspector shows the protected violation(s) + the two guide commands.

const now = "2026-06-18T12:00:00.000Z";
const blockedAt = "2026-06-18T11:00:00.000Z";

test.beforeEach(async ({ page }) => {
  await mockAwfApi(page);
});

test("blocked workspace renders the pause badge and a blocked-for indicator", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  const card = page.getByTestId("workspace-card-ws_blocked1");
  await expect(card).toBeVisible();
  // Badge glyph + label (glyph proves status is not conveyed by color alone).
  await expect(card.getByText("blocked", { exact: true })).toBeVisible();
  await expect(card.getByText("⏸")).toBeVisible();
  // Blocked-age indicator.
  await expect(page.getByTestId("workspace-blocked-age-ws_blocked1")).toContainText("Blocked for");
});

test("Awaiting operator KPI counts blocked; Active includes it but Running excludes it", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  // running:2 + validating:1 + pushing:1 => 4 (blocked NOT folded in — PR #598).
  const runningKpi = kpiCard(page, "Running");
  await expect(runningKpi.locator(".kpi-value")).toHaveText("4");

  // blocked:2 surfaced as its own KPI.
  const awaitingKpi = kpiCard(page, "Awaiting operator");
  await expect(awaitingKpi.locator(".kpi-value")).toHaveText("2");

  // Active is the server-side active_total (includes blocked).
  const activeKpi = kpiCard(page, "Active");
  await expect(activeKpi.locator(".kpi-value")).toHaveText("6");
});

test("inspector shows the protected violation and both guide resolution commands", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  await openInspector(page, "ws_blocked1");

  const block = page.getByTestId("blocked-violation-block");
  await expect(block).toBeVisible();
  await expect(block).toContainText("Awaiting operator");
  await expect(block).toContainText("PROTECTED_QUALITY_GATE_VIOLATION");

  // Violation path + classification.
  const violation = page.getByTestId("blocked-violation-row").first();
  await expect(violation).toContainText(".github/workflows/ci.yml");
  await expect(violation).toContainText(".github/**");

  // Both ready-to-run guide commands, pre-filled with the workspace id + path.
  await expect(block).toContainText(
    "awf workspace guide ws_blocked1 --grant '.github/workflows/ci.yml' --reason '<why>'",
  );
  await expect(block).toContainText(
    "awf workspace guide ws_blocked1 --directive 'revert .github/workflows/ci.yml; <alternative>'",
  );
});

function kpiCard(page: Page, label: string) {
  return page
    .getByText(label, { exact: true })
    .locator("..")
    .filter({ has: page.locator(".kpi-value") });
}

async function openInspector(page: Page, workspaceId: string) {
  const title = page.getByTestId(`workspace-title-${workspaceId}`);
  await title.waitFor({ state: "visible" });
  const box = await title.boundingBox();
  if (!box) {
    throw new Error(`Workspace title ${workspaceId} did not produce a clickable box`);
  }
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.locator("h2", { hasText: "Protected CI tweak" }).first()).toBeVisible();
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
      await fulfillJson(route, listEnvelope([blockedOverview()]));
      return;
    }
    if (path === "/api/awf/workspaces/ws_blocked1") {
      await fulfillJson(route, blockedWorkspace());
      return;
    }
    if (path === "/api/awf/workspaces/ws_blocked1/runtime") {
      await fulfillJson(route, { status: "blocked" });
      return;
    }
    if (
      path === "/api/awf/workspaces/ws_blocked1/events" ||
      path === "/api/awf/workspaces/ws_blocked1/operations" ||
      path === "/api/awf/workspaces/ws_blocked1/logs"
    ) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path.startsWith("/api/awf/workspaces/ws_blocked1/stream")) {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache" },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: "ws_blocked1" })}\n\n`,
      });
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

function blockedOverview() {
  return {
    workspace_id: "ws_blocked1",
    task_id: "task_blocked1",
    title: "Protected CI tweak",
    task_prompt: "edit ci",
    repo_url: "https://github.com/test/repo",
    base_branch: "main",
    branch_name: "awf/ws_blocked1",
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
    status: "blocked",
    subphase: null,
    last_activity_at: blockedAt,
    last_log_at: blockedAt,
    is_stale_running: false,
    current_phase: "validating",
    active_operation: null,
    last_event: {
      id: "evt_block",
      workspace_id: "ws_blocked1",
      event_type: "workspace.state_changed",
      old_state: "validating",
      new_state: "blocked",
      reason_code: "PROTECTED_QUALITY_GATE_VIOLATION",
      payload: null,
      occurred_at: blockedAt,
    },
    pr_url: null,
    pr_number: null,
    failure_reason: null,
    failure_message: null,
    created_at: "2026-06-18T10:00:00.000Z",
    updated_at: blockedAt,
  };
}

function blockedWorkspace() {
  return {
    ...blockedOverview(),
    id: "ws_blocked1",
    version: 4,
    block_state: {
      block_type: "pre_pr_protected_quality_gate",
      block_reason_code: "PROTECTED_QUALITY_GATE_VIOLATION",
      block_resume_phase: "validating",
      block_epoch: 1,
      blocked_at: blockedAt,
      violations: [
        {
          path: ".github/workflows/ci.yml",
          protected_pattern: ".github/**",
          section: "ci",
          line: 12,
          reason: "protected quality gate file",
        },
      ],
    },
  };
}

function resourceSaturation() {
  return {
    generated_at: now,
    workspace_counts: {
      by_status: { running: 2, validating: 1, pushing: 1, monitoring_pr: 0, blocked: 2 },
      active_total: 6,
      requested: 0,
      provisioning: 0,
      ready: 0,
      running: 2,
      validating: 1,
      pushing: 1,
      monitoring_pr: 0,
      blocked: 2,
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
      active_workspace_count: 6,
      steady_cpu: 6,
      steady_memory_gb: 12,
      peak_cpu: 12,
      peak_memory_gb: 24,
      disk_mb: 10240,
      dind_slots: 0,
    },
    capacity: {
      steady_cpu: { limit: null, reserved: 6, available: 2, available_after_next_default: 1, reason_code: null },
      peak_cpu: { limit: null, reserved: 12, available: 4, available_after_next_default: 2, reason_code: null },
      steady_memory_gb: { limit: null, reserved: 12, available: 20, available_after_next_default: 18, reason_code: null },
      peak_memory_gb: { limit: null, reserved: 24, available: 8, available_after_next_default: 4, reason_code: null },
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
    status_counts: { running: 2, validating: 1, pushing: 1, blocked: 2 },
    failure_reason_counts: {},
    active_count: 6,
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
