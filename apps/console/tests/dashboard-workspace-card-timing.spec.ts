import { expect, type Page, test } from "@playwright/test";

import type {
  WorkspaceLifecycleStage,
  WorkspaceOverview,
  WorkspaceStatus,
} from "@/lib/types";
import { formatDateTime } from "@/lib/format";

import { mockAwfConsoleApi } from "./fixtures/console-api";

async function waitForConsoleReady(page: Page) {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

function overview(
  workspaceId: string,
  status: WorkspaceStatus,
  overrides: Partial<WorkspaceOverview> = {},
): WorkspaceOverview {
  return {
    workspace_id: workspaceId,
    task_id: `task_${workspaceId}`,
    title: `${status} timing workspace`,
    task_prompt: "Verify workspace card timing",
    repo_url: "https://github.com/example/awf.git",
    base_branch: "development",
    branch_name: `awf/${workspaceId}`,
    agent: "codex",
    agent_model: "gpt-5.5",
    agent_effort: "high",
    agent_model_source: "task_policy",
    agent_effort_source: "default",
    network_posture: "restricted",
    lifecycle: [],
    llm_usage: {
      input_tokens: null,
      output_tokens: null,
      total_tokens: null,
      cost_estimate: null,
      currency: null,
      status: "unavailable",
      source: "none",
      reason: "usage_not_reported",
    },
    recovery: null,
    coordination_warnings: [],
    provider_readiness_preflight: null,
    status,
    subphase: null,
    last_activity_at: "2026-09-06T12:15:00Z",
    last_log_at: "2026-09-06T12:14:00Z",
    is_stale_running: false,
    current_phase: status,
    active_operation: null,
    last_event: null,
    pr_url: null,
    pr_number: null,
    failure_reason: null,
    failure_message: null,
    created_at: "2026-09-06T12:00:00Z",
    updated_at: "2026-09-06T12:45:00Z",
    ...overrides,
  };
}

function stage(
  name: string,
  startedAt: string | null,
  endedAt: string | null,
  durationSeconds: number | null,
): WorkspaceLifecycleStage {
  return {
    stage: name,
    started_at: startedAt,
    ended_at: endedAt,
    duration_seconds: durationSeconds,
    status: startedAt ? "completed" : "pending",
  };
}

const surfaces = [
  { name: "local desktop", mode: "local" as const, width: 1280, height: 900 },
  { name: "hosted mobile", mode: "hosted" as const, width: 390, height: 844 },
];

for (const surface of surfaces) {
  test(`active cards show activity without Updated on ${surface.name}`, async ({ page }) => {
    await page.setViewportSize({ width: surface.width, height: surface.height });
    const running = overview("ws_running_timing", "running");
    const monitoring = overview("ws_monitoring_timing", "monitoring_pr", {
      last_activity_at: "2026-09-06T12:20:00Z",
      pr_url: "https://github.com/example/awf/pull/12",
      pr_number: 12,
    });
    const requestedWithoutActivity = overview("ws_requested_timing", "requested", {
      last_activity_at: null,
    });
    await mockAwfConsoleApi(page, {
      mode: surface.mode,
      overviewItems: [running, monitoring, requestedWithoutActivity],
    });

    await page.goto("/");
    await waitForConsoleReady(page);

    for (const item of [running, monitoring]) {
      const card = page.getByTestId(`workspace-card-${item.workspace_id}`);
      const timing = card.getByTestId(`workspace-card-timing-${item.workspace_id}`);
      await expect(timing).toContainText("Created");
      await expect(timing).toContainText("Last activity");
      await expect(timing).not.toContainText("Updated");
      await expect(timing).not.toContainText("Finished");
      await expect(timing).not.toContainText("Duration");
    }
    await expect(
      page.getByTestId("workspace-last-activity-ws_requested_timing"),
    ).toContainText("not recorded");

    const runningCard = page.getByTestId("workspace-card-ws_running_timing");
    await runningCard.getByRole("button", { name: "Details", exact: true }).click();
    const details = page.getByRole("dialog", { name: /Task details/i });
    await expect(details).toBeVisible();
    await expect(details.getByText("Updated", { exact: true })).toBeVisible();
  });
}

test("hosted terminal cards use workflow finish and recorded duration for every terminal status", async ({
  page,
}) => {
  const terminals = (["completed", "failed", "cancelled", "destroyed"] as const).map(
    (status, index) =>
      overview(`ws_hosted_${status}`, status, {
        started_at: "2026-09-06T12:00:00Z",
        workflow_finished_at: `2026-09-06T12:${20 + index}:00Z`,
        finished_at: null,
        duration_seconds: 1200 + index * 60,
        native_runtime_finished_at: "2026-09-06T12:05:00Z",
        last_activity_at: "2026-09-06T12:40:00Z",
        updated_at: "2026-09-06T12:45:00Z",
      }),
  );
  await mockAwfConsoleApi(page, { mode: "hosted", overviewItems: terminals });

  await page.goto("/");
  await waitForConsoleReady(page);

  for (const [index, item] of terminals.entries()) {
    const card = page.getByTestId(`workspace-card-${item.workspace_id}`);
    const timing = card.getByTestId(`workspace-card-timing-${item.workspace_id}`);
    await expect(timing).toContainText("Created");
    await expect(timing).toContainText("Finished");
    await expect(timing).toContainText("Duration");
    await expect(timing).not.toContainText("Last activity");
    await expect(timing).not.toContainText("Updated");
    await expect(card.getByTestId(`workspace-card-finished-${item.workspace_id}`)).toContainText(
      formatDateTime(item.workflow_finished_at),
    );
    await expect(card.getByTestId(`workspace-card-duration-${item.workspace_id}`)).toContainText(
      `${20 + index}m 0s`,
    );
  }
});

test("local terminal cards use one complete lifecycle interval", async ({ page }) => {
  const completed = overview("ws_local_completed", "completed", {
    lifecycle: [
      stage("requested", "2026-09-06T12:00:00Z", "2026-09-06T12:02:00Z", 120),
      stage("running", "2026-09-06T12:02:00Z", "2026-09-06T12:10:00Z", 480),
      stage("completed", "2026-09-06T12:10:00Z", "2026-09-06T12:10:00Z", 0),
    ],
  });
  const failed = overview("ws_local_failed", "failed", {
    lifecycle: [
      stage("requested", "2026-09-06T12:00:00Z", "2026-09-06T12:01:00Z", 60),
      stage("running", "2026-09-06T12:01:00Z", "2026-09-06T12:08:00Z", 420),
      stage("validating", null, null, null),
    ],
  });
  const destroyed = overview("ws_local_destroyed", "destroyed", {
    lifecycle: [
      stage("requested", "2026-09-06T12:00:00Z", "2026-09-06T12:01:00Z", 60),
      stage("running", "2026-09-06T12:01:00Z", "2026-09-06T12:09:00Z", 480),
      // Cleanup closes the completed stage later; workflow finish remains its start.
      stage("completed", "2026-09-06T12:09:00Z", "2026-09-06T12:30:00Z", 1260),
    ],
  });
  await mockAwfConsoleApi(page, {
    overviewItems: [completed, failed, destroyed],
  });

  await page.goto("/");
  await waitForConsoleReady(page);

  for (const [item, finished, duration] of [
    [completed, "2026-09-06T12:10:00Z", "10m 0s"],
    [failed, "2026-09-06T12:08:00Z", "8m 0s"],
    [destroyed, "2026-09-06T12:09:00Z", "9m 0s"],
  ] as const) {
    const card = page.getByTestId(`workspace-card-${item.workspace_id}`);
    await expect(card.getByTestId(`workspace-card-finished-${item.workspace_id}`)).toContainText(
      formatDateTime(finished),
    );
    await expect(card.getByTestId(`workspace-card-duration-${item.workspace_id}`)).toContainText(
      duration,
    );
  }
});

test("terminal cards call missing or ambiguous timing not recorded while preserving zero", async ({
  page,
}) => {
  const missing = overview("ws_timing_missing", "completed", {
    native_runtime_finished_at: "2026-09-06T12:05:00Z",
    workflow_finished_at: null,
    finished_at: null,
    duration_seconds: null,
    lifecycle: [],
  });
  const invalid = overview("ws_timing_invalid", "failed", {
    workflow_finished_at: "not-a-timestamp",
    duration_seconds: -1,
    lifecycle: [stage("running", "2026-09-06T12:00:00Z", "2026-09-06T12:11:00Z", 660)],
  });
  const missingDuration = overview("ws_duration_missing", "completed", {
    workflow_finished_at: "2026-09-06T12:12:00Z",
    duration_seconds: null,
  });
  const mismatchedDuration = overview("ws_duration_mismatch", "completed", {
    workflow_finished_at: "2026-09-06T12:12:00Z",
    finished_at: "2026-09-06T12:10:00Z",
    duration_seconds: 600,
  });
  const lifecycleGap = overview("ws_duration_gap", "failed", {
    lifecycle: [
      stage("requested", "2026-09-06T12:00:00Z", "2026-09-06T12:01:00Z", 60),
      stage("running", "2026-09-06T12:02:00Z", "2026-09-06T12:05:00Z", 180),
    ],
  });
  const completedStageWithoutStart = overview("ws_timing_missing_stage_start", "failed", {
    lifecycle: [
      stage("requested", "2026-09-06T12:00:00Z", "2026-09-06T12:01:00Z", 60),
      {
        stage: "running",
        started_at: null,
        ended_at: null,
        duration_seconds: null,
        status: "completed",
      },
    ],
  });
  const retryAmbiguous = overview("ws_timing_retry", "destroyed", {
    recovery: {
      from_state: "failed",
      to_state: "requested",
      reason_code: "retry",
      action: "retry",
      recovery_mode: null,
      started_at: "2026-09-06T12:10:00Z",
      current_operation: null,
      summary: "Workflow retried.",
      payload: null,
    },
    lifecycle: [
      stage("requested", "2026-09-06T12:00:00Z", "2026-09-06T12:01:00Z", 60),
      stage("running", "2026-09-06T12:01:00Z", "2026-09-06T12:08:00Z", 420),
      // A later completion no longer matches the collapsed earlier-stage lifecycle.
      stage("completed", "2026-09-06T12:20:00Z", "2026-09-06T12:30:00Z", 600),
    ],
  });
  const zero = overview("ws_timing_zero", "cancelled", {
    workflow_finished_at: null,
    finished_at: "2026-09-06T12:00:00Z",
    duration_seconds: 0,
  });
  await mockAwfConsoleApi(page, {
    overviewItems: [
      missing,
      invalid,
      missingDuration,
      mismatchedDuration,
      lifecycleGap,
      completedStageWithoutStart,
      retryAmbiguous,
      zero,
    ],
  });

  await page.goto("/");
  await waitForConsoleReady(page);

  for (const item of [missing, invalid, completedStageWithoutStart, retryAmbiguous]) {
    const card = page.getByTestId(`workspace-card-${item.workspace_id}`);
    await expect(card.getByTestId(`workspace-card-finished-${item.workspace_id}`)).toContainText(
      "not recorded",
    );
    await expect(card.getByTestId(`workspace-card-duration-${item.workspace_id}`)).toContainText(
      "not recorded",
    );
  }

  for (const item of [missingDuration, mismatchedDuration, lifecycleGap]) {
    const card = page.getByTestId(`workspace-card-${item.workspace_id}`);
    await expect(card.getByTestId(`workspace-card-finished-${item.workspace_id}`)).not.toContainText(
      "not recorded",
    );
    await expect(card.getByTestId(`workspace-card-duration-${item.workspace_id}`)).toContainText(
      "not recorded",
    );
  }

  const zeroCard = page.getByTestId("workspace-card-ws_timing_zero");
  await expect(zeroCard.getByTestId("workspace-card-finished-ws_timing_zero")).not.toContainText(
    "not recorded",
  );
  await expect(zeroCard.getByTestId("workspace-card-duration-ws_timing_zero")).toContainText(
    "0s",
  );
});
