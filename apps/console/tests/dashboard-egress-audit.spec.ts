import { expect, type Page, test } from "@playwright/test";

const now = "2026-05-02T12:00:00.000Z";
const workspaceId = "ws_egress_audit";

test("security panel renders workspace egress audit evidence", async ({ page }) => {
  await mockAwfApi(page);

  await page.goto(`/?workspaceId=${workspaceId}`);

  const securityPanel = page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "Security & Egress" }) })
    .first();
  await expect(securityPanel.getByText("Audit decision")).toBeVisible();
  await expect(securityPanel.getByText("allowed", { exact: true })).toBeVisible();
  await expect(securityPanel.getByText("EGRESS_ALLOWED_BY_PROFILE", { exact: true })).toBeVisible();
  await expect(securityPanel.getByText("package_registry", { exact: true })).toBeVisible();
});

async function mockAwfApi(page: Page) {
  await page.route("**/api/awf/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverview()]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: now });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, { active: 1, failed: 0 });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { taxonomy: [], latest_examples: [], total_failures: 0 });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceDetail());
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, {
        workspace_id: workspaceId,
        compose_project_name: "awf-console",
        stack_state: "running",
        services: [],
        app_endpoints: [],
        logs_available: false,
        control_available: true,
        reason: null,
      });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([]));
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

function workspaceOverview() {
  return {
    workspace_id: workspaceId,
    task_id: "task-egress-audit",
    title: "Egress audit verification",
    task_prompt: "Verify egress audit rendering",
    repo_url: "https://github.com/example/awf",
    base_branch: "main",
    branch_name: "codex/egress-audit",
    agent: "codex",
    agent_model: "gpt-5.5",
    agent_effort: "high",
    agent_model_source: "task_policy",
    agent_effort_source: "task_policy",
    network_posture: "restricted",
    lifecycle: [],
    llm_usage: null,
    pricing: null,
    recovery: null,
    coordination_warnings: [],
    provider_readiness_preflight: null,
    status: "running",
    subphase: null,
    last_activity_at: now,
    last_log_at: now,
    is_stale_running: false,
    current_phase: "running",
    active_operation: "execute",
    last_event: null,
    pr_url: null,
    failure_reason: null,
    failure_message: null,
    latest_queue_decision: null,
    active_resource_reservation: null,
    created_at: now,
    updated_at: now,
  };
}

function workspaceDetail() {
  return {
    id: workspaceId,
    status: "running",
    version: 3,
    repo_url: "https://github.com/example/awf",
    branch_base: "main",
    branch_name: "codex/egress-audit",
    base_commit: "abc123",
    task_title: "Egress audit verification",
    task_prompt: "Verify egress audit rendering",
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
    lifecycle: [],
    llm_usage: null,
    pricing: null,
    recovery: null,
    coordination_warnings: [],
    provider_readiness_preflight: null,
    validation_provenance: null,
    app_endpoints: [],
    env_profile: "self",
    profile_ref: ".awf/workspace.yml",
    requested_profile: null,
    resolved_profile: {
      security: {
        egress: { mode: "restricted" },
        host_home_auth_mounts: { mode: "block" },
      },
      secrets: [],
    },
    network_posture: "restricted",
    test_commands: ["npm --prefix apps/console run build"],
    requires_database: false,
    node_id: "local",
    compose_project_name: "awf-console",
    compose_file_path: null,
    pr_url: null,
    failure_reason: null,
    failure_message: null,
    secret_leases: [],
    policy_findings: [],
    egress_audit: {
      id: "egress-audit-ws_egress_audit",
      workspace_id: workspaceId,
      attempt_id: "attempt-egress-audit",
      policy_posture: "restricted",
      decision: "allowed",
      destination_category: "package_registry",
      reason_code: "EGRESS_ALLOWED_BY_PROFILE",
      details: { hostname: "registry.npmjs.org" },
      enforced_at: "2026-05-02T12:10:00.000Z",
      created_at: "2026-05-02T12:10:01.000Z",
    },
    created_at: now,
    updated_at: now,
  };
}
