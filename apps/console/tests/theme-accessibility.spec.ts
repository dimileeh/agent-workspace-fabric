import { expect, type Locator, type Page, test } from "@playwright/test";

const now = "2026-05-02T12:00:00.000Z";
const workspaceId = "ws_theme_dark_0001";
const secondWorkspaceId = "ws_theme_dark_0002";

test.beforeEach(async ({ page }) => {
  await mockAwfApi(page);
});

test("theme controls are keyboard reachable, expose state, and persist after reload", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  const darkTheme = page.getByRole("button", { name: "Use dark theme" });
  await tabUntilFocused(page, darkTheme);
  await page.keyboard.press("Enter");
  await expect(darkTheme).toHaveAttribute("aria-pressed", "true");

  const highContrast = page.getByRole("button", { name: "Enable high contrast" });
  await tabUntilFocused(page, highContrast);
  await page.keyboard.press("Space");
  await expect(highContrast).toHaveAttribute("aria-pressed", "true");

  const largeFont = page.getByRole("button", { name: "Use larger font size" });
  await tabUntilFocused(page, largeFont);
  await page.keyboard.press("Space");
  await expect(largeFont).toHaveAttribute("aria-pressed", "true");

  await expect(page.locator("html")).toHaveAttribute("data-awf-theme", "dark");
  await expect(page.locator("html")).toHaveAttribute("data-awf-contrast", "high");
  await expect(page.locator("html")).toHaveAttribute("data-awf-font-size", "large");

  await page.reload();
  await waitForConsoleReady(page);

  await expect(page.getByRole("button", { name: "Use dark theme" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("button", { name: "Enable high contrast" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("button", { name: "Use larger font size" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("html")).toHaveAttribute("data-awf-theme", "dark");
  await expect(page.locator("html")).toHaveAttribute("data-awf-contrast", "high");
  await expect(page.locator("html")).toHaveAttribute("data-awf-font-size", "large");
  await expectNoViewportOverflow(page);
});

test("desktop theme screenshots cover dashboard, details, and fullscreen logs", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await seedDarkAccessiblePreferences(page);
  await page.goto("/");
  await waitForConsoleReady(page);

  await expect(page.getByRole("heading", { name: "Merge Queue" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-awf-theme", "dark");
  await expectNoViewportOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("desktop-dashboard.png"), fullPage: true });

  // The per-card "Details" button opens the Task details dialog. Use exact
  // matching: the full-card "Open workspace details for ..." buttons also
  // contain "details", so a substring match would resolve to the wrong button.
  await page.getByRole("button", { name: "Details", exact: true }).first().click();
  await expect(page.getByRole("dialog", { name: /Task details/i })).toBeVisible();
  await expectNoViewportOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("desktop-workspace-details.png"), fullPage: true });
  await page.getByRole("button", { name: "Close task details" }).click();

  await page.getByRole("button", { name: "Logs", exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "Logs" }).first()).toBeVisible();
  await expect(page.getByText("agent stdout ready").first()).toBeVisible();
  await expectNoViewportOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("desktop-fullscreen-logs.png"), fullPage: true });
});

test("task details modal scrolls long prompts without moving the dashboard", async ({ page }) => {
  await page.setViewportSize({ width: 947, height: 982 });
  await page.goto("/");
  await waitForConsoleReady(page);
  // Exact match targets the per-card "Details" button (opens the Task details
  // dialog), not the full-card "Open workspace details for ..." button.
  const detailsButton = page.getByRole("button", { name: "Details", exact: true }).first();
  await expect(detailsButton).toBeVisible();
  const scrollTarget = await detailsButton.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    return Math.max(1, window.scrollY + rect.top - window.innerHeight / 3);
  });
  await page.evaluate((top) => window.scrollTo(0, top), scrollTarget);
  await expect(detailsButton).toBeInViewport();
  const backgroundScrollBefore = await page.evaluate(() => window.scrollY);
  expect(backgroundScrollBefore).toBeGreaterThan(0);

  await detailsButton.click();
  await expect(page.getByRole("dialog", { name: /Task details/i })).toBeVisible();
  const detailsScroll = page.getByTestId("task-details-scroll");
  await expect(detailsScroll).toBeVisible();
  const modalScrollBefore = await detailsScroll.evaluate((node) => node.scrollTop);

  await detailsScroll.hover();
  await page.mouse.wheel(0, 900);

  await expect.poll(async () => detailsScroll.evaluate((node) => node.scrollTop)).toBeGreaterThan(modalScrollBefore + 20);
  await expect.poll(async () => page.evaluate(() => window.scrollY)).toBe(backgroundScrollBefore);
});

test("workspace list cards wrap long text without clipping", async ({ page }) => {
  await page.setViewportSize({ width: 1358, height: 982 });
  await page.goto("/");
  await waitForConsoleReady(page);

  const title = page.getByTestId(`workspace-title-${workspaceId}`);
  await expect(title).toContainText("policy parity title should wrap");
  const metrics = await title.evaluate((node) => ({
    clientHeight: node.clientHeight,
    scrollHeight: node.scrollHeight,
    lineHeight: Number.parseFloat(window.getComputedStyle(node).lineHeight),
  }));

  expect(metrics.clientHeight).toBeGreaterThan(metrics.lineHeight * 2.4);
  expect(metrics.scrollHeight - metrics.clientHeight).toBeLessThanOrEqual(1);

  const repo = page.getByTestId(`workspace-repo-${workspaceId}`);
  const repoMetrics = await repo.evaluate((node) => ({
    clientWidth: node.clientWidth,
    scrollWidth: node.scrollWidth,
    clientHeight: node.clientHeight,
    scrollHeight: node.scrollHeight,
  }));
  expect(repoMetrics.scrollWidth - repoMetrics.clientWidth).toBeLessThanOrEqual(1);
  expect(repoMetrics.scrollHeight - repoMetrics.clientHeight).toBeLessThanOrEqual(1);
});

test("mobile theme screenshots cover dashboard, workspace, and logs views", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await seedDarkAccessiblePreferences(page);
  await page.goto("/");
  await waitForConsoleReady(page);

  await expect(page.getByRole("heading", { name: "AWF Console" })).toBeVisible();
  await expectNoViewportOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("mobile-dashboard.png"), fullPage: true });

  // Open the inspector by clicking the card. The title text sits in a
  // pointer-events-none layer over the full-card open-details button, and the
  // copy-id button overlaps that button's centre, so click the title box
  // directly (above the copy-id control) to land on the open-details button.
  const cardTitle = page.getByTestId(`workspace-title-${workspaceId}`);
  const titleBox = await cardTitle.boundingBox();
  if (!titleBox) {
    throw new Error(`Workspace title ${workspaceId} did not produce a clickable box`);
  }
  await page.mouse.click(titleBox.x + titleBox.width / 2, titleBox.y + titleBox.height / 2);
  await expect(page.getByRole("heading", { name: "Workspace", exact: true })).toBeVisible();
  await expectNoViewportOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("mobile-workspace.png"), fullPage: true });

  await page.getByRole("button", { name: "Close inspector" }).click();
  await expect(page.getByRole("heading", { name: "Workspace", exact: true })).not.toBeVisible();
  await page.getByRole("button", { name: "Logs", exact: true }).first().click();
  await expect(page.getByText("agent stdout ready").first()).toBeVisible();
  await expectNoViewportOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("mobile-logs.png"), fullPage: true });
});

test("fleet health strip surfaces the status-layer KPIs", async ({ page }) => {
  await page.goto("/");
  await waitForConsoleReady(page);

  const fleet = page.locator('[aria-label="Fleet health"]');
  await expect(fleet).toBeVisible();
  await expect(fleet.getByText("Monitoring PR")).toBeVisible();
  await expect(fleet.getByText("Capacity", { exact: true })).toBeVisible();
});

async function seedDarkAccessiblePreferences(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "awf.operator.preferences.v1",
      JSON.stringify({
        version: 1,
        theme: "dark",
        contrast: "high",
        fontSize: "large",
      }),
    );
  });
}

async function waitForConsoleReady(page: Page) {
  await expect(page.getByRole("heading", { name: "AWF Console" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Resource / Runtime Capacity" })).toBeVisible();
  await expect(page.getByText("Docker runtime").first()).toBeVisible();
  await expect(page.getByText(workspaceId, { exact: true }).first()).toBeVisible();
}

async function tabUntilFocused(page: Page, target: Locator) {
  const handle = await target.elementHandle();
  expect(handle).not.toBeNull();
  for (let index = 0; index < 18; index += 1) {
    await page.keyboard.press("Tab");
    const focused = await target.evaluate((node) => node === document.activeElement);
    if (focused) {
      return;
    }
  }
  throw new Error(`Expected ${await target.evaluate((node) => node.getAttribute("aria-label") ?? node.textContent)} to receive keyboard focus.`);
}

async function expectNoViewportOverflow(page: Page) {
  const overflow = await page.evaluate(() => {
    const root = document.documentElement;
    const body = document.body;
    const viewport = window.innerWidth;
    const amount = Math.max(root.scrollWidth, body.scrollWidth) - viewport;
    const offenders = [...document.querySelectorAll("body *")]
      .map((node) => {
        const element = node as HTMLElement;
        const rect = element.getBoundingClientRect();
        return {
          tag: element.tagName.toLowerCase(),
          className: element.className.toString(),
          text: (element.textContent ?? "").trim().slice(0, 80),
          left: Math.round(rect.left),
          right: Math.round(rect.right),
          width: Math.round(rect.width),
        };
      })
      .filter((item) => item.right > viewport + 1 || item.left < -1)
      .sort((left, right) => right.right - left.right)
      .slice(0, 8);
    return { amount, viewport, rootWidth: root.scrollWidth, bodyWidth: body.scrollWidth, offenders };
  });
  expect(overflow.amount, JSON.stringify(overflow, null, 2)).toBeLessThanOrEqual(1);
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
      await fulfillJson(route, listEnvelope([workspaceOverview(workspaceId), workspaceOverview(secondWorkspaceId, "completed")]));
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
      await fulfillJson(route, listEnvelope([mergeQueueItem()]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, {
        total_failures: 1,
        window_hours: 24,
        taxonomy: [{ reason: "VALIDATION_FAILED", count: 1 }],
        latest_examples: [
          {
            workspace_id: secondWorkspaceId,
            title: "Completed monitor verification",
            repo_url: "https://github.com/example/awf",
            agent: "codex",
            timestamp: now,
            failure_reason: "VALIDATION_FAILED",
            failure_message: "example retained for dark theme visual coverage",
            pr_url: "https://github.com/example/awf/pull/42",
          },
        ],
      });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}` || path === `/api/awf/workspaces/${secondWorkspaceId}`) {
      await fulfillJson(route, workspaceDetail(path.endsWith(secondWorkspaceId) ? secondWorkspaceId : workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime` || path === `/api/awf/workspaces/${secondWorkspaceId}/runtime`) {
      await fulfillJson(route, workspaceRuntime(path.includes(secondWorkspaceId) ? secondWorkspaceId : workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events` || path === `/api/awf/workspaces/${secondWorkspaceId}/events`) {
      await fulfillJson(route, listEnvelope(workspaceEvents(path.includes(secondWorkspaceId) ? secondWorkspaceId : workspaceId)));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations` || path === `/api/awf/workspaces/${secondWorkspaceId}/operations`) {
      await fulfillJson(route, listEnvelope(workspaceOperations(path.includes(secondWorkspaceId) ? secondWorkspaceId : workspaceId)));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs` || path === `/api/awf/workspaces/${secondWorkspaceId}/logs`) {
      await fulfillJson(route, listEnvelope(workspaceLogStreams(path.includes(secondWorkspaceId) ? secondWorkspaceId : workspaceId)));
      return;
    }
    if (path.includes("/logs/agent.stdout")) {
      await fulfillJson(route, {
        stream_id: "agent.stdout",
        offset: 0,
        next_offset: 94,
        eof: true,
        data: "agent stdout ready\nvalidation complete\nmerge monitor waiting for review grace\n",
      });
      return;
    }
    if (path.includes("/stream")) {
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

    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });
}

async function fulfillJson(route: Parameters<Parameters<Page["route"]>[1]>[0], body: unknown, status = 200) {
  await route.fulfill({
    status,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function listEnvelope<T>(items: T[]) {
  return {
    items,
    next_cursor: null,
    has_more: false,
  };
}

function workspaceOverview(id: string, status = "running") {
  return {
    workspace_id: id,
    task_id: `task-${id}`,
    title:
      id === workspaceId
        ? "P1: complete workspace create v2 CLI and MCP policy parity title should wrap fully inside the list row"
        : "Completed monitor verification",
    task_prompt: id === workspaceId ? longTaskPrompt() : "# Task\n- Verify dark mode\n- Exercise high contrast and large font",
    repo_url: "https://github.com/example/awf",
    base_branch: "codex/awf-post-merge-fixes",
    branch_name: `codex/${id}`,
    agent: "codex",
    agent_model: "gpt-5.5",
    agent_effort: "high",
    agent_model_source: "task_policy",
    agent_effort_source: "task_policy",
    lifecycle: lifecycle(status),
    llm_usage: llmUsage(),
    recovery: null,
    coordination_warnings: [],
    status,
    current_phase: status,
    active_operation: status === "running" ? "execute" : null,
    last_event: workspaceEvents(id)[0],
    pr_url: "https://github.com/example/awf/pull/42",
    failure_reason: null,
    failure_message: null,
    latest_queue_decision: null,
    active_resource_reservation: null,
    created_at: now,
    updated_at: now,
  };
}

function longTaskPrompt() {
  const steps = Array.from(
    { length: 48 },
    (_, index) =>
      `- Step ${index + 1}: verify that the task details dialog keeps prompt scrolling inside the modal viewport.`,
  );
  return ["# Task", "This prompt is intentionally long for modal scroll coverage.", ...steps].join("\n");
}

function workspaceDetail(id: string) {
  return {
    id,
    status: id === workspaceId ? "running" : "completed",
    version: 3,
    repo_url: "https://github.com/example/awf",
    branch_base: "codex/awf-post-merge-fixes",
    branch_name: `codex/${id}`,
    base_commit: "abc123",
    task_title: "Dark theme verification workspace",
    task_prompt: "# Task\n- Verify dark mode\n- Exercise high contrast and large font",
    task_external_id: null,
    task_class: "console",
    owned_paths: ["apps/console/**"],
    task_policy: {},
    auto_merge: true,
    initial_review_grace_period_seconds: 900,
    agent: "codex",
    agent_model: "gpt-5.5",
    agent_effort: "high",
    agent_model_source: "task_policy",
    agent_effort_source: "task_policy",
    lifecycle: lifecycle(id === workspaceId ? "running" : "completed"),
    llm_usage: llmUsage(),
    recovery: null,
    coordination_warnings: [],
    validation_provenance: null,
    app_endpoints: [],
    env_profile: "self",
    profile_ref: ".awf/workspace.yml",
    requested_profile: null,
    resolved_profile: {
      security: {
        egress: { mode: "open", allowlist: [] },
        host_home_auth_mounts: { mode: "block" },
      },
      secrets: [],
    },
    test_commands: ["npm --prefix apps/console run build"],
    requires_database: false,
    node_id: "local",
    compose_project_name: "awf-console",
    compose_file_path: null,
    pr_url: "https://github.com/example/awf/pull/42",
    failure_reason: null,
    failure_message: null,
    secret_leases: [],
    policy_findings: [],
    created_at: now,
    updated_at: now,
  };
}

function lifecycle(status: string) {
  return ["requested", "provisioning", "ready", "running"].map((stage) => ({
    stage,
    started_at: now,
    ended_at: stage === status ? null : now,
    duration_seconds: stage === status ? null : 5,
    status: stage === status ? "active" : "completed",
  }));
}

function llmUsage() {
  return {
    input_tokens: 1200,
    output_tokens: 400,
    total_tokens: 1600,
    cost_estimate: 0.42,
    currency: "USD",
    status: "available",
    source: "mock",
    reason: null,
  };
}

function workspaceRuntime(id: string) {
  return {
    workspace_id: id,
    compose_project_name: "awf-console",
    stack_state: "running",
    services: [
      {
        name: "console",
        container_id: "abcdef123456",
        image: "awf-console:test",
        state: "running",
        status: "Up 2 minutes",
        health: "healthy",
        ports: ["3100/tcp"],
        started_at: now,
      },
    ],
    app_endpoints: [
      {
        name: "console",
        service: "console",
        scheme: "http",
        port: 3100,
        path: "/",
        internal_url: "http://console:3100/",
        visibility: "console",
        health: {
          path: "/",
          method: "GET",
          expected_status: 200,
          internal_url: "http://console:3100/",
        },
      },
    ],
    logs_available: true,
    control_available: true,
    reason: null,
  };
}

function workspaceEvents(id: string) {
  return [
    {
      id: `evt-${id}`,
      workspace_id: id,
      event_type: "workspace.state_changed",
      old_state: "ready",
      new_state: "running",
      reason_code: "AGENT_STARTED",
      payload: null,
      occurred_at: now,
    },
  ];
}

function workspaceOperations(id: string) {
  return [
    {
      id: `op-${id}`,
      workspace_id: id,
      type: "execute",
      status: "running",
      error_code: null,
      error_message: null,
      payload: null,
      result: null,
      idempotency_key: null,
      created_at: now,
      started_at: now,
      finished_at: null,
      owner: "worker",
      source: "awf",
      action: "execute",
      pr_number: 42,
      pr_url: "https://github.com/example/awf/pull/42",
      source_head_sha: "def456",
      source_base_sha: "abc123",
      reason: null,
      reason_code: null,
      failure_code: null,
      failure_message: null,
      log_stream_refs: {},
      log_stream_ids: ["agent.stdout"],
    },
  ];
}

function workspaceLogStreams(id: string) {
  return [
    {
      stream_id: "agent.stdout",
      source: "agent",
      name: `${id} stdout`,
      kind: "stdout",
      path: `/tmp/${id}.log`,
      byte_count: 94,
      line_count: 3,
      opened_at: now,
      closed_at: null,
    },
  ];
}

function mergeQueueItem() {
  return {
    candidate_id: "cand-theme",
    candidate_status: "open",
    close_reason: null,
    attempt_id: "attempt-theme",
    task_id: `task-${workspaceId}`,
    workspace_id: workspaceId,
    title: "Dark theme verification workspace",
    repo_url: "https://github.com/example/awf",
    base_branch: "codex/awf-post-merge-fixes",
    branch_name: `codex/${workspaceId}`,
    pr_url: "https://github.com/example/awf/pull/42",
    status: "running",
    auto_merge: true,
    task_class: "console",
    owned_paths: ["apps/console/**"],
    created_at: now,
    updated_at: now,
    merged_at: null,
    last_event: workspaceEvents(workspaceId)[0],
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
  };
}

function resourceSaturation() {
  const dimension = {
    limit: 8,
    reserved: 2,
    available: 6,
    available_after_next_default: 5,
    reason_code: null,
  };
  return {
    generated_at: now,
    workspace_counts: {
      by_status: { running: 1, completed: 1 },
      active_total: 1,
      requested: 0,
      provisioning: 0,
      ready: 0,
      running: 1,
      validating: 0,
      pushing: 0,
      monitoring_pr: 0,
      destroying: 0,
      completed: 1,
      failed: 0,
      cancelled: 0,
      destroyed: 0,
    },
    worker: {
      max_concurrent_provisions: 2,
      max_concurrent_executions: 2,
    },
    local_capacity: {
      cpu_cores: 8,
      memory_gb: 24,
      source: "docker",
      reason_code: null,
      detail: null,
    },
    resource_defaults: {
      steady_cpu: 1,
      steady_memory_gb: 2,
      peak_cpu: 2,
      peak_memory_gb: 4,
    },
    reserved_resources: {
      active_workspace_count: 1,
      steady_cpu: 1,
      steady_memory_gb: 2,
      peak_cpu: 2,
      peak_memory_gb: 4,
      disk_mb: 2048,
      dind_slots: 0,
    },
    capacity: {
      steady_cpu: dimension,
      peak_cpu: dimension,
      steady_memory_gb: dimension,
      peak_memory_gb: dimension,
      disk_mb: { ...dimension, limit: 10240, reserved: 2048 },
      dind_slots: { ...dimension, limit: 2, reserved: 0 },
      pressure_reasons: [],
    },
    concurrency: {
      provision: { limit: 2, in_use: 0, queued: 0, available: 2 },
      execution: { limit: 2, in_use: 1, queued: 0, available: 1 },
    },
    disk: {
      path: "/workspace",
      checked_path: "/workspace",
      total_bytes: 100_000_000,
      used_bytes: 40_000_000,
      free_bytes: 60_000_000,
      percent_free: 60,
      threshold_bytes: 10_000_000,
      ok: true,
      status: "ok",
      reason: "DISK_OK",
      detail: null,
    },
    admission: {
      ok: true,
      status: "ok",
      reason: "ADMISSION_OK",
      detail: null,
    },
  };
}

function workspaceReliability() {
  return {
    generated_at: now,
    window_start: now,
    since_hours: 24,
    status_counts: { running: 1, completed: 1 },
    failure_reason_counts: {},
    active_count: 1,
    destroying_count: 0,
    completed_count: 1,
    failed_count: 0,
    cancelled_count: 0,
    destroyed_count: 0,
    cleanup_failure_count: 0,
    stuck_count: 0,
    actionable_reason_count: 1,
    unactionable_reason_count: 0,
  };
}
