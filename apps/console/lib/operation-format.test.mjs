import assert from "node:assert/strict";
import test from "node:test";

import {
  formatOperationDetail,
  formatOperationFailure,
  formatOperationTitle,
} from "./operation-format.ts";

const baseOperation = {
  id: "op_1234567890abcdef",
  workspace_id: "ws_123",
  type: "monitor_state",
  status: "succeeded",
  error_code: null,
  error_message: null,
  payload: null,
  result: null,
  idempotency_key: null,
  created_at: "2026-04-29T10:00:00Z",
  started_at: "2026-04-29T10:00:00Z",
  finished_at: "2026-04-29T10:01:00Z",
  owner: null,
  source: null,
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
};

test("monitor grace wait gets a concise title and detail", () => {
  const operation = {
    ...baseOperation,
    action: "grace_wait",
    pr_number: 42,
    reason_code: "INITIAL_REVIEW_GRACE",
    payload: { wait_seconds: 60, stale_reason: "STALE_TARGET_ADVANCED" },
  };

  assert.equal(formatOperationTitle(operation), "Monitor: initial review grace");
  assert.equal(
    formatOperationDetail(operation),
    "PR #42 / INITIAL_REVIEW_GRACE / waits 60s / stale STALE_TARGET_ADVANCED",
  );
});

test("monitor check wait explains pending checks", () => {
  const operation = {
    ...baseOperation,
    action: "check_wait",
    pr_number: 43,
    reason_code: "CHECK_WAIT",
    payload: { wait_seconds: 30 },
  };

  assert.equal(formatOperationTitle(operation), "Monitor: waiting for checks");
  assert.equal(formatOperationDetail(operation), "PR #43 / CHECK_WAIT / waits 30s");
});

test("existing recovery operation types keep readable labels", () => {
  assert.equal(
    formatOperationTitle({
      ...baseOperation,
      type: "comment_repair",
      action: "comment_repair",
      reason_code: "COMMENT_REPAIR",
    }),
    "Comment repair",
  );
  assert.equal(
    formatOperationTitle({
      ...baseOperation,
      type: "validate",
      action: "validate_only",
      reason_code: "VALIDATION_INSUFFICIENT_TIER",
    }),
    "Validate-only recovery",
  );
  assert.equal(
    formatOperationTitle({
      ...baseOperation,
      type: "rebase",
      action: "rebase_only",
      reason_code: "TARGET_ADVANCED",
    }),
    "Rebase recovery",
  );
});

test("operator recovery operation types keep readable labels", () => {
  assert.equal(
    formatOperationTitle({
      ...baseOperation,
      type: "remonitor",
      payload: { requested_action: "remonitor", source: "operator_api" },
    }),
    "Remonitor",
  );
  assert.equal(
    formatOperationTitle({
      ...baseOperation,
      type: "validate",
      payload: { requested_action: "validate", source: "operator_api" },
    }),
    "Revalidate",
  );
});

test("legacy operations without payload or action render fallback text", () => {
  const operation = {
    ...baseOperation,
    type: "create",
    action: null,
    payload: null,
    result: null,
  };

  assert.equal(formatOperationTitle(operation), "create");
  assert.equal(formatOperationDetail(operation), "No details recorded.");
  assert.equal(formatOperationFailure(operation), null);
});

test("failure line prefers derived failure message over raw error message", () => {
  const operation = {
    ...baseOperation,
    status: "failed",
    failure_code: "GITHUB_MERGE_FAILED",
    failure_message: "merge rejected",
    error_message: "raw stderr",
  };

  assert.equal(formatOperationFailure(operation), "GITHUB_MERGE_FAILED: merge rejected");
});
