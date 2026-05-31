import assert from "node:assert/strict";
import test from "node:test";

import {
  getWorkspaceOperatorControls,
  summarizeWorkspaceOperatorFailure,
  summarizeWorkspaceOperatorSuccess,
} from "./workspace-operator-controls.ts";

test("control eligibility remonitor requires PR-monitorable workspace", () => {
  assertControl(
    { overview: overview({ status: "monitoring_pr", pr_url: "https://github.test/pr/1" }) },
    "remonitor",
    { enabled: true, reason: null },
  );
  assertControl(
    { overview: overview({ status: "failed", pr_url: "https://github.test/pr/1" }) },
    "remonitor",
    { enabled: true, reason: null },
  );
  assertControl(
    { overview: overview({ status: "monitoring_pr", pr_url: null }) },
    "remonitor",
    { enabled: false, reason: "no PR" },
  );
  assertControl(
    { overview: overview({ status: "completed", pr_url: "https://github.test/pr/1" }) },
    "remonitor",
    { enabled: false, reason: "not monitorable" },
  );
});

test("control eligibility refresh targets merge candidate or stale context", () => {
  assertControl(
    {
      overview: overview({ status: "monitoring_pr", pr_url: "https://github.test/pr/1" }),
      mergeQueueItem: mergeQueueItem(),
    },
    "refresh",
    { enabled: true, reason: null },
  );
  assertControl(
    {
      overview: overview({ status: "monitoring_pr", pr_url: "https://github.test/pr/1" }),
      mergeQueueItem: mergeQueueItem({ stale_reasons: [staleReason()] }),
    },
    "refresh",
    { enabled: true, reason: null },
  );
  assertControl(
    {
      overview: overview({ status: "monitoring_pr", pr_url: "https://github.test/pr/1" }),
      workspace: workspace({
        validation_provenance: {
          required_tier: 2,
          latest_satisfied_tier: 1,
          freshness_status: "stale",
          reason_code: "validation_target_stale",
          current_target_head_sha: "abc",
          latest_validation: null,
        },
      }),
    },
    "refresh",
    { enabled: true, reason: null },
  );
  assertControl(
    { overview: overview({ status: "ready", pr_url: null }) },
    "refresh",
    { enabled: false, reason: "no merge candidate" },
  );
});

test("control eligibility revalidate requires validation recovery", () => {
  assertControl(
    {
      overview: overview({ status: "monitoring_pr", pr_url: "https://github.test/pr/1" }),
      mergeQueueItem: mergeQueueItem({ required_next_action: "validate", required_validation_tier: 2 }),
    },
    "revalidate",
    { enabled: true, reason: null, requestedTier: 2 },
  );
  assertControl(
    {
      overview: overview({ status: "monitoring_pr", pr_url: "https://github.test/pr/1" }),
      mergeQueueItem: mergeQueueItem({
        readiness: { ...readiness(), stale: true, stale_reason: "validation_insufficient_tier" },
      }),
    },
    "revalidate",
    { enabled: true, reason: null },
  );
  assertControl(
    {
      overview: overview({ status: "monitoring_pr", pr_url: null }),
      mergeQueueItem: mergeQueueItem({ pr_url: "", required_next_action: "validate" }),
    },
    "revalidate",
    { enabled: false, reason: "no PR" },
  );
  assertControl(
    {
      overview: overview({ status: "ready", pr_url: "https://github.test/pr/1" }),
      mergeQueueItem: mergeQueueItem({ required_next_action: "validate" }),
    },
    "revalidate",
    { enabled: false, reason: "not PR-monitoring" },
  );
  assertControl(
    {
      overview: overview({ status: "monitoring_pr", pr_url: "https://github.test/pr/1" }),
      mergeQueueItem: mergeQueueItem({ validation_freshness_status: "fresh" }),
    },
    "revalidate",
    { enabled: false, reason: "validation fresh" },
  );
});

test("active operation disables all operator controls", () => {
  const eligible = {
    overview: overview({
      status: "monitoring_pr",
      pr_url: "https://github.test/pr/1",
      active_operation: "op_active",
    }),
    mergeQueueItem: mergeQueueItem({ required_next_action: "validate" }),
  };
  for (const control of getWorkspaceOperatorControls(eligible)) {
    assert.equal(control.enabled, false);
    assert.equal(control.reason, "active operation");
  }

  assertControl(
    {
      overview: overview({ status: "monitoring_pr", pr_url: "https://github.test/pr/1" }),
      mergeQueueItem: mergeQueueItem({ required_next_action: "validate" }),
      operations: [operation({ status: "running" })],
    },
    "refresh",
    { enabled: false, reason: "active operation" },
  );
  assertControl(
    {
      overview: overview({ status: "monitoring_pr", pr_url: "https://github.test/pr/1" }),
      workspace: workspace({
        recovery: {
          current_operation: {
            id: "op_recovery",
            type: "validate",
            status: "pending",
            created_at: "2026-04-29T12:00:00Z",
            started_at: null,
            payload: null,
          },
        },
      }),
      mergeQueueItem: mergeQueueItem({ required_next_action: "validate" }),
    },
    "revalidate",
    { enabled: false, reason: "active operation" },
  );
});

test("action success summary handles control and operation responses", () => {
  assert.deepEqual(
    summarizeWorkspaceOperatorSuccess("remonitor", {
      workspace_id: "ws_123",
      operation_id: "op_remonitor",
      operation_status: "succeeded",
      status: "monitoring_pr",
      message: "monitor resumed",
    }),
    {
      action: "remonitor",
      operationId: "op_remonitor",
      status: "succeeded",
      message: "Remonitor succeeded: op_remonitor",
      warnings: [],
    },
  );
  assert.deepEqual(
    summarizeWorkspaceOperatorSuccess("revalidate", operation({ id: "op_validate", status: "pending" })),
    {
      action: "revalidate",
      operationId: "op_validate",
      status: "pending",
      message: "Revalidate pending: op_validate",
      warnings: [],
    },
  );
});

test("action success summary retains control warnings", () => {
  assert.deepEqual(
    summarizeWorkspaceOperatorSuccess("remonitor", {
      workspace_id: "ws_123",
      operation_id: "op_remonitor",
      operation_status: "succeeded",
      status: "monitoring_pr",
      message: "monitor resumed",
      warnings: [
        {
          warning_code: "REMONITOR_PAST_SETTLE",
          message: "Auto-merge is paused until a reviewer resumes it.",
        },
      ],
    }),
    {
      action: "remonitor",
      operationId: "op_remonitor",
      status: "succeeded",
      message: "Remonitor succeeded: op_remonitor",
      warnings: [
        {
          warning_code: "REMONITOR_PAST_SETTLE",
          message: "Auto-merge is paused until a reviewer resumes it.",
        },
      ],
    },
  );
});

test("action failure summary keeps error code and message", () => {
  assert.deepEqual(
    summarizeWorkspaceOperatorFailure({
      ok: false,
      status: 409,
      message: "Workspace cannot be refreshed from this state.",
      errorCode: "WORKSPACE_STATE_NOT_REFRESHABLE",
      detail: {
        detail: {
          error_code: "WORKSPACE_STATE_NOT_REFRESHABLE",
          message: "Workspace cannot be refreshed from this state.",
        },
      },
    }),
    {
      errorCode: "WORKSPACE_STATE_NOT_REFRESHABLE",
      message: "WORKSPACE_STATE_NOT_REFRESHABLE: Workspace cannot be refreshed from this state.",
    },
  );
});

function assertControl(context, action, expected) {
  const control = getWorkspaceOperatorControls(context).find((item) => item.action === action);
  assert.ok(control, `missing ${action} control`);
  if ("enabled" in expected) {
    assert.equal(control.enabled, expected.enabled);
  }
  if ("reason" in expected) {
    assert.equal(control.reason, expected.reason);
  }
  if ("requestedTier" in expected) {
    assert.equal(control.requestedTier, expected.requestedTier);
  }
}

function overview(overrides = {}) {
  return {
    workspace_id: "ws_123",
    task_id: "task_123",
    title: "Workspace",
    repo_url: "git@github.com:example/repo.git",
    base_branch: "main",
    branch_name: "codex/workspace",
    agent: "codex",
    agent_model: null,
    agent_effort: null,
    agent_model_source: "default",
    agent_effort_source: "default",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
    status: "monitoring_pr",
    current_phase: "monitoring_pr",
    active_operation: null,
    last_event: null,
    pr_url: "https://github.test/pr/1",
    failure_reason: null,
    failure_message: null,
    created_at: "2026-04-29T12:00:00Z",
    updated_at: "2026-04-29T12:00:00Z",
    ...overrides,
  };
}

function workspace(overrides = {}) {
  return {
    id: "ws_123",
    status: "monitoring_pr",
    version: 7,
    pr_url: "https://github.test/pr/1",
    recovery: null,
    validation_provenance: null,
    ...overrides,
  };
}

function mergeQueueItem(overrides = {}) {
  return {
    workspace_id: "ws_123",
    pr_url: "https://github.test/pr/1",
    status: "monitoring_pr",
    required_next_action: null,
    required_validation_tier: null,
    latest_satisfied_validation_tier: null,
    validation_freshness_status: "fresh",
    validation_reason_code: null,
    readiness: readiness(),
    latest_validation: null,
    stale_reasons: [],
    policy_findings: [],
    queue_blockers: [],
    ...overrides,
  };
}

function readiness(overrides = {}) {
  return {
    ready: true,
    manual_merge_required: false,
    waiting_for_monitor: false,
    failed_or_cancelled: false,
    completed: false,
    not_canonical: false,
    stale: false,
    stale_reason: null,
    ...overrides,
  };
}

function staleReason(overrides = {}) {
  return {
    id: "stale_1",
    workspace_id: "ws_123",
    candidate_id: "mc_123",
    attempt_id: "att_123",
    task_id: "task_123",
    trigger_type: "target_advanced",
    trigger_ref: "main@abc123",
    reason_code: "STALE_TARGET_ADVANCED",
    explanation: "Target advanced.",
    status: "active",
    severity: "blocking",
    blocks_merge: true,
    detected_at: "2026-04-29T12:00:00Z",
    resolved_at: null,
    ...overrides,
  };
}

function operation(overrides = {}) {
  return {
    id: "op_123",
    workspace_id: "ws_123",
    type: "refresh",
    status: "pending",
    error_code: null,
    error_message: null,
    payload: null,
    result: null,
    idempotency_key: null,
    created_at: "2026-04-29T12:00:00Z",
    started_at: null,
    finished_at: null,
    owner: "operator_api",
    source: "operator_api",
    action: null,
    pr_number: null,
    pr_url: null,
    source_head_sha: null,
    source_base_sha: null,
    reason: null,
    reason_code: null,
    failure_code: null,
    failure_message: null,
    log_stream_refs: {},
    log_stream_ids: [],
    ...overrides,
  };
}
