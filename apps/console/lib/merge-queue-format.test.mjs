import assert from "node:assert/strict";
import test from "node:test";

import {
  activeStaleReasons,
  formatRequiredNextAction,
  summarizeQueueBlockers,
  summarizeReadiness,
  summarizeStaleReasons,
  summarizeValidation,
} from "./merge-queue-format.ts";

function mergeQueueItem(overrides = {}) {
  return {
    candidate_id: "mc_111111111111111111111111",
    candidate_status: "open",
    close_reason: null,
    attempt_id: "att_111111111111111111111111",
    task_id: "task_1",
    workspace_id: "ws_1",
    title: "Queue item",
    repo_url: "git@github.com:example/awf.git",
    base_branch: "main",
    branch_name: "codex/queue-item",
    pr_url: "https://github.com/example/awf/pull/1",
    status: "monitoring_pr",
    auto_merge: true,
    task_class: "test_task",
    owned_paths: ["src/awf/**"],
    created_at: "2026-04-26T12:00:00Z",
    updated_at: "2026-04-26T12:05:00Z",
    merged_at: null,
    last_event: null,
    merge_blocker_reason: "ready_to_merge_or_waiting_for_github",
    required_next_action: null,
    readiness: {
      ready: true,
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
    ...overrides,
  };
}

function staleReason(overrides = {}) {
  return {
    id: "sr_1",
    workspace_id: "ws_1",
    candidate_id: "mc_111111111111111111111111",
    attempt_id: "att_111111111111111111111111",
    task_id: "task_1",
    trigger_type: "target_advanced",
    trigger_ref: "main@abc123",
    reason_code: "STALE_TARGET_ADVANCED",
    explanation: "Target branch advanced.",
    status: "active",
    detected_at: "2026-04-26T12:02:00Z",
    resolved_at: null,
    ...overrides,
  };
}

test("stale summary uses active reasons and maps rebase action", () => {
  const item = mergeQueueItem({
    merge_blocker_reason: "stale",
    required_next_action: "rebase",
    readiness: {
      ready: false,
      manual_merge_required: false,
      waiting_for_monitor: false,
      failed_or_cancelled: false,
      completed: false,
      not_canonical: false,
      stale: true,
      stale_reason: "STALE_TARGET_ADVANCED",
    },
    stale_reasons: [
      staleReason(),
      staleReason({
        id: "sr_2",
        trigger_type: "path_overlap",
        trigger_ref: "src/awf/api/**",
        reason_code: "STALE_OVERLAP",
      }),
      staleReason({
        id: "sr_3",
        status: "resolved",
        trigger_ref: "old-main",
        resolved_at: "2026-04-26T12:04:00Z",
      }),
    ],
  });

  assert.deepEqual(
    activeStaleReasons(item).map((reason) => reason.id),
    ["sr_1", "sr_2"],
  );
  assert.equal(formatRequiredNextAction(item.required_next_action, item.merge_blocker_reason), "rebase");
  assert.deepEqual(summarizeStaleReasons(item), {
    count: 2,
    label: "STALE_TARGET_ADVANCED @ main@abc123, STALE_OVERLAP @ src/awf/api/**",
    detail: "STALE_TARGET_ADVANCED / target_advanced @ main@abc123; STALE_OVERLAP / path_overlap @ src/awf/api/**",
    overflowCount: 0,
    activeReasons: activeStaleReasons(item),
  });
});

test("queue blocker summary includes older candidate context and wait action", () => {
  const item = mergeQueueItem({
    merge_blocker_reason: "waiting_for_older_candidate",
    required_next_action: "wait_for_queue",
    queue_blockers: [
      {
        candidate_id: "mc_old",
        workspace_id: "ws_old",
        attempt_id: "att_old",
        task_id: "task_old",
        title: "Older queue candidate",
        pr_url: "https://github.com/example/awf/pull/41",
        pr_number: 41,
        status: "monitoring_pr",
        blocker_state: "merge_eligible",
        reason_code: "MERGE_QUEUE_WAITING_FOR_OLDER_CANDIDATE",
      },
      {
        candidate_id: "mc_recovery",
        workspace_id: "ws_recovery",
        attempt_id: "att_recovery",
        task_id: "task_recovery",
        title: "Recovery in progress",
        pr_url: "https://github.com/example/awf/pull/42",
        pr_number: 42,
        status: "validating",
        blocker_state: "monitor_owned_recovery",
        reason_code: "MERGE_QUEUE_WAITING_FOR_OLDER_CANDIDATE",
      },
    ],
  });

  assert.equal(formatRequiredNextAction(item.required_next_action, item.merge_blocker_reason), "wait for queue");
  assert.deepEqual(summarizeQueueBlockers(item), {
    count: 2,
    label: "2 blockers: Older queue candidate #41",
    detail: "merge_eligible / monitoring_pr / MERGE_QUEUE_WAITING_FOR_OLDER_CANDIDATE +1 more",
    first: item.queue_blockers[0],
    overflowCount: 1,
  });
});

test("readiness summary distinguishes canonical, superseded, stale, and legacy rows", () => {
  assert.deepEqual(
    summarizeReadiness(
      mergeQueueItem({
        canonical: true,
        readiness: {
          ready: false,
          manual_merge_required: false,
          waiting_for_monitor: false,
          failed_or_cancelled: false,
          completed: false,
          not_canonical: false,
          stale: true,
          stale_reason: "validation_insufficient_tier",
        },
      }),
    ),
    {
      label: "stale",
      detail: "validation_insufficient_tier",
      canonicalLabel: "canonical",
      candidateLabel: "mc_1111111...1111",
      attemptLabel: "att_111111...1111",
    },
  );

  assert.deepEqual(
    summarizeReadiness(
      mergeQueueItem({
        canonical: false,
        readiness: {
          ready: false,
          manual_merge_required: false,
          waiting_for_monitor: false,
          failed_or_cancelled: false,
          completed: false,
          not_canonical: true,
          stale: false,
          stale_reason: null,
        },
      }),
    ),
    {
      label: "not canonical",
      detail: "superseded attempt",
      canonicalLabel: "superseded",
      candidateLabel: "mc_1111111...1111",
      attemptLabel: "att_111111...1111",
    },
  );

  assert.deepEqual(
    summarizeReadiness(
      mergeQueueItem({
        candidate_id: null,
        attempt_id: null,
        readiness: null,
        canonical: false,
      }),
    ),
    {
      label: "legacy",
      detail: "legacy workspace without candidate readiness",
      canonicalLabel: "superseded",
      candidateLabel: "legacy",
      attemptLabel: "none",
    },
  );
});

test("validation summary shows tier, status, freshness, heads, and coverage", () => {
  assert.deepEqual(
    summarizeValidation(
      mergeQueueItem({
        latest_validation: {
          validation_run_id: "vr_fresh",
          attempt_id: "att_111111111111111111111111",
          tier: 2,
          command_set_hash: "a".repeat(64),
          base_commit: "base123",
          target_branch: "main",
          target_head_sha: "head123",
          current_target_head_sha: "head123",
          status: "succeeded",
          reason_code: "VALIDATION_OK",
          started_at: "2026-04-26T12:00:00Z",
          finished_at: "2026-04-26T12:05:00Z",
          log_stream_refs: {},
          fresh_for_target: true,
          retry_count: 1,
          coverage_percent: 99.2,
          coverage_minimum_percent: 99,
          coverage_status: "passed",
          coverage_reason_code: "COVERAGE_OK",
          coverage_gaps: [],
        },
      }),
    ),
    {
      label: "T2 succeeded / fresh",
      detail: "VALIDATION_OK / retries 1",
      freshLabel: "fresh",
      headLabel: "head123 -> head123",
      coverageLabel: "coverage passed 99.2/99%",
    },
  );

  assert.deepEqual(
    summarizeValidation(
      mergeQueueItem({
        latest_validation: {
          validation_run_id: "vr_stale",
          attempt_id: "att_111111111111111111111111",
          tier: 1,
          command_set_hash: "b".repeat(64),
          base_commit: "base123",
          target_branch: "main",
          target_head_sha: "old-head",
          current_target_head_sha: "new-head",
          status: "failed",
          reason_code: "COVERAGE_BELOW_THRESHOLD",
          started_at: "2026-04-26T12:00:00Z",
          finished_at: "2026-04-26T12:05:00Z",
          log_stream_refs: {},
          fresh_for_target: false,
          retry_count: 0,
          coverage_percent: 98.4,
          coverage_minimum_percent: 99,
          coverage_status: "failed",
          coverage_reason_code: "COVERAGE_BELOW_THRESHOLD",
          coverage_gaps: [],
        },
      }),
    ),
    {
      label: "T1 failed / stale target",
      detail: "COVERAGE_BELOW_THRESHOLD",
      freshLabel: "stale target",
      headLabel: "old-head -> new-head",
      coverageLabel: "coverage failed 98.4/99%",
    },
  );
});
