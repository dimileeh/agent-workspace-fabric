import assert from "node:assert/strict";
import test from "node:test";

import {
  activeStaleReasons,
  formatRequiredNextAction,
  requiredNextActionTone,
  summarizeQueueBlockers,
  summarizeReadiness,
  summarizeRecovery,
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
    severity: "blocking",
    blocks_merge: true,
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
    blockingCount: 2,
    advisoryCount: 0,
    label: "STALE_TARGET_ADVANCED @ main@abc123, STALE_OVERLAP @ src/awf/api/**",
    detail: "STALE_TARGET_ADVANCED / target_advanced @ main@abc123; STALE_OVERLAP / path_overlap @ src/awf/api/**",
    overflowCount: 0,
    activeReasons: activeStaleReasons(item),
  });
});

test("stale summary shows advisory plan overlaps without a recovery-looking label", () => {
  const item = mergeQueueItem({
    stale_reasons: [
      staleReason({
        id: "sr_plan",
        trigger_type: "plan_artifact_overlap",
        trigger_ref: "docs/awf-plans/ws_other.md",
        reason_code: "ADVISORY_PLAN_ARTIFACT_OVERLAP",
        explanation: "AWF plan artifact changed.",
        severity: "advisory",
        blocks_merge: false,
      }),
    ],
  });

  const stale = summarizeStaleReasons(item);
  const recovery = summarizeRecovery(item);

  assert.equal(stale.count, 1);
  assert.equal(stale.blockingCount, 0);
  assert.equal(stale.advisoryCount, 1);
  assert.equal(stale.label, "1 advisory overlap");
  assert.equal(
    stale.detail,
    "advisory ADVISORY_PLAN_ARTIFACT_OVERLAP / plan_artifact_overlap @ docs/awf-plans/ws_other.md",
  );
  assert.equal(recovery.staleReasonBlockingCount, 0);
  assert.equal(recovery.staleReasonAdvisoryCount, 1);
  assert.equal(recovery.recommendedActionLabel, "none");
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

test("required next action tone maps raw action codes and blocker fallbacks", () => {
  assert.equal(requiredNextActionTone("resolve_policy_findings", "ready_to_merge_or_waiting_for_github"), "bad");
  assert.equal(requiredNextActionTone("resolve_task_scope", "stale"), "bad");
  assert.equal(requiredNextActionTone("wait_for_queue", "ready_to_merge_or_waiting_for_github"), "warn");
  assert.equal(requiredNextActionTone("validate", "stale"), "warn");
  assert.equal(requiredNextActionTone("rebase", "stale"), "warn");
  assert.equal(requiredNextActionTone("future_action", "policy_blocked"), "neutral");

  assert.equal(requiredNextActionTone(null, "policy_blocked"), "bad");
  assert.equal(requiredNextActionTone(null, "manual_merge_required"), "warn");
  assert.equal(requiredNextActionTone(null, "waiting_for_monitor"), "warn");
  assert.equal(requiredNextActionTone(null, "waiting_for_older_candidate"), "warn");
  assert.equal(requiredNextActionTone(null, "stale"), "warn");
  assert.equal(requiredNextActionTone(null, "workspace_not_terminal"), "neutral");
  assert.equal(formatRequiredNextAction(null, "failed_or_cancelled"), "inspect failure");
  assert.equal(requiredNextActionTone(null, "failed_or_cancelled"), "bad");
  assert.equal(formatRequiredNextAction(null, "not_canonical"), "superseded");
  assert.equal(requiredNextActionTone(null, "not_canonical"), "bad");
  assert.equal(requiredNextActionTone(null, "ready_to_merge_or_waiting_for_github"), "good");
  assert.equal(requiredNextActionTone(null, "completed"), "good");
  assert.equal(requiredNextActionTone(null, "future_blocker_reason"), "neutral");
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
      candidateLabel: "mc_1111111…1111",
      attemptLabel: "att_111111…1111",
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
      candidateLabel: "mc_1111111…1111",
      attemptLabel: "att_111111…1111",
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

test("recovery summary uses the readiness identity labels", () => {
  for (const item of [
    mergeQueueItem(),
    mergeQueueItem({
      candidate_id: null,
      attempt_id: null,
      readiness: null,
      canonical: false,
    }),
  ]) {
    const readiness = summarizeReadiness(item);
    const recovery = summarizeRecovery(item);

    assert.equal(recovery.candidateLabel, readiness.candidateLabel);
    assert.equal(recovery.attemptLabel, readiness.attemptLabel);
  }
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
          base_sha: "base123",
          workspace_head_sha: "workspace-head",
          target_head_sha: "1234567890abcdef1234567890abcdef12345678",
          current_target_head_sha: "fedcba9876543210fedcba9876543210fedcba98",
          profile_name: "python",
          profile_version: 4,
          profile_source: "repo:.awf/workspace.yml",
          resolved_profile_digest: "1".repeat(64),
          environment_identity_digest: "2".repeat(64),
          environment_identity_inputs: { schema_version: 1 },
          identity_source: "persisted",
          status: "succeeded",
          reason_code: "VALIDATION_OK",
          started_at: "2026-04-26T12:00:00Z",
          finished_at: "2026-04-26T12:05:00Z",
          log_stream_refs: {},
          fresh_for_target: true,
          freshness_status: "fresh",
          freshness_reason_code: "validation_fresh",
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
      headLabel: "1234567 -> fedcba9",
      coverageLabel: "coverage passed 99.2/99%",
      commandHashLabel: "aaaaaaa",
      profileLabel: "python@4",
      environmentLabel: "2222222",
      identitySourceLabel: "persisted",
      baseShaLabel: "base123",
      workspaceHeadShaLabel: "workspace-head",
      validatedTargetShaLabel: "1234567",
      currentTargetShaLabel: "fedcba9",
      reasonLabel: "VALIDATION_OK / validation fresh",
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
          base_sha: null,
          workspace_head_sha: null,
          target_branch: "main",
          target_head_sha: "old-head",
          current_target_head_sha: "new-head",
          profile_name: null,
          profile_version: null,
          profile_source: null,
          resolved_profile_digest: null,
          environment_identity_digest: null,
          environment_identity_inputs: {},
          identity_source: "legacy_fallback",
          status: "failed",
          reason_code: "COVERAGE_BELOW_THRESHOLD",
          started_at: "2026-04-26T12:00:00Z",
          finished_at: "2026-04-26T12:05:00Z",
          log_stream_refs: {},
          fresh_for_target: false,
          freshness_status: "stale",
          freshness_reason_code: "validation_target_stale",
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
      commandHashLabel: "bbbbbbb",
      profileLabel: "unavailable",
      environmentLabel: "unavailable",
      identitySourceLabel: "legacy fallback",
      baseShaLabel: "base123",
      workspaceHeadShaLabel: "unknown",
      validatedTargetShaLabel: "old-head",
      currentTargetShaLabel: "new-head",
      reasonLabel: "COVERAGE_BELOW_THRESHOLD / validation target stale",
    },
  );
});

test("validation formatter labels unknown and unavailable identity", () => {
  assert.deepEqual(
    summarizeValidation(
      mergeQueueItem({
        validation_freshness_status: "unavailable",
        validation_reason_code: "validation_unavailable",
        latest_validation: null,
      }),
    ),
    {
      label: "none",
      detail: "validation_unavailable",
      freshLabel: "unavailable",
      headLabel: "unknown -> unknown",
      coverageLabel: "coverage unknown",
      commandHashLabel: "unavailable",
      profileLabel: "unavailable",
      environmentLabel: "unavailable",
      identitySourceLabel: "unavailable",
      baseShaLabel: "unknown",
      workspaceHeadShaLabel: "unknown",
      validatedTargetShaLabel: "unknown",
      currentTargetShaLabel: "unknown",
      reasonLabel: "validation unavailable",
    },
  );

  const summary = summarizeValidation(
    mergeQueueItem({
      validation_freshness_status: "unknown",
      validation_reason_code: "validation_target_unknown",
      latest_validation: {
        validation_run_id: "vr_unknown",
        attempt_id: "att_111111111111111111111111",
        tier: 1,
        command_set_hash: "c".repeat(64),
        base_commit: "legacy-base",
        base_sha: null,
        workspace_head_sha: null,
        target_branch: null,
        target_head_sha: null,
        current_target_head_sha: "target-head",
        profile_name: null,
        profile_version: null,
        profile_source: null,
        resolved_profile_digest: null,
        environment_identity_digest: null,
        environment_identity_inputs: {},
        identity_source: "legacy_fallback",
        status: "succeeded",
        reason_code: "VALIDATION_OK",
        started_at: "2026-04-26T12:00:00Z",
        finished_at: "2026-04-26T12:05:00Z",
        log_stream_refs: {},
        fresh_for_target: null,
        freshness_status: "unknown",
        freshness_reason_code: "validation_target_unknown",
        retry_count: 0,
        coverage_percent: null,
        coverage_minimum_percent: null,
        coverage_status: null,
        coverage_reason_code: null,
        coverage_gaps: [],
      },
    }),
  );

  assert.equal(summary.freshLabel, "freshness unknown");
  assert.equal(summary.headLabel, "unknown -> target-head");
  assert.equal(summary.baseShaLabel, "legacy-base");
  assert.equal(summary.workspaceHeadShaLabel, "unknown");
  assert.equal(summary.reasonLabel, "VALIDATION_OK / validation target unknown");
});

test("validation formatter displays profile environment and reason labels", () => {
  const recovery = summarizeRecovery(
    mergeQueueItem({
      required_validation_tier: 3,
      latest_satisfied_validation_tier: 2,
      validation_freshness_status: "stale",
      validation_reason_code: "validation_target_stale",
      latest_validation: {
        validation_run_id: "vr_identity",
        attempt_id: "att_111111111111111111111111",
        tier: 2,
        command_set_hash: "d".repeat(64),
        base_commit: "legacy-base",
        base_sha: "base-identity",
        workspace_head_sha: "workspace-head",
        target_branch: "main",
        target_head_sha: "old-head",
        current_target_head_sha: "new-head",
        profile_name: "python",
        profile_version: 7,
        profile_source: "repo:.awf/workspace.yml",
        resolved_profile_digest: "1".repeat(64),
        environment_identity_digest: "e".repeat(64),
        environment_identity_inputs: { schema_version: 1 },
        identity_source: "persisted",
        status: "succeeded",
        reason_code: "VALIDATION_OK",
        started_at: "2026-04-26T12:00:00Z",
        finished_at: "2026-04-26T12:05:00Z",
        log_stream_refs: {},
        fresh_for_target: false,
        freshness_status: "stale",
        freshness_reason_code: "validation_target_stale",
        retry_count: 0,
        coverage_percent: null,
        coverage_minimum_percent: null,
        coverage_status: null,
        coverage_reason_code: null,
        coverage_gaps: [],
      },
    }),
  );

  assert.equal(recovery.validationReasonLabel, "VALIDATION_OK / validation target stale");
  assert.equal(recovery.commandHashLabel, "ddddddd");
  assert.equal(recovery.profileLabel, "python@7");
  assert.equal(recovery.environmentLabel, "eeeeeee");
  assert.equal(recovery.baseShaLabel, "base-identity");
  assert.equal(recovery.workspaceHeadShaLabel, "workspace-head");
  assert.equal(recovery.validatedTargetShaLabel, "old-head");
  assert.equal(recovery.currentTargetShaLabel, "new-head");
});

test("validation recovery summary shows required tier, satisfied tier, freshness, and heads", () => {
  const item = mergeQueueItem({
    required_validation_tier: 2,
    latest_satisfied_validation_tier: 2,
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
      stale_reason: "target_advanced",
    },
    latest_validation: {
      validation_run_id: "vr_recovery",
      attempt_id: "att_111111111111111111111111",
      tier: 2,
      command_set_hash: "c".repeat(64),
      base_commit: "abcdef1234567890abcdef1234567890abcdef12",
      target_branch: "main",
      target_head_sha: "1234567890abcdef1234567890abcdef12345678",
      current_target_head_sha: "fedcba9876543210fedcba9876543210fedcba98",
      status: "succeeded",
      reason_code: "VALIDATION_OK",
      started_at: "2026-04-26T12:00:00Z",
      finished_at: "2026-04-26T12:05:00Z",
      log_stream_refs: {},
      fresh_for_target: false,
      retry_count: 2,
      coverage_percent: null,
      coverage_minimum_percent: null,
      coverage_status: null,
      coverage_reason_code: null,
      coverage_gaps: [],
    },
  });

  const recovery = summarizeRecovery(item);

  assert.equal(recovery.recommendedActionLabel, "rebase");
  assert.equal(recovery.requiredTierLabel, "T2 required");
  assert.equal(recovery.latestSatisfiedTierLabel, "T2 satisfied");
  assert.equal(recovery.latestSatisfiedTierDetail, "VALIDATION_OK / retries 2");
  assert.equal(recovery.freshnessLabel, "stale target");
  assert.equal(recovery.baseShaLabel, "abcdef1");
  assert.equal(recovery.validatedTargetShaLabel, "1234567");
  assert.equal(recovery.currentTargetShaLabel, "fedcba9");
  assert.equal(recovery.targetRangeLabel, "1234567 -> fedcba9");
});

test("recovery summary maps stale reasons and queue blockers into compact details", () => {
  const item = mergeQueueItem({
    merge_blocker_reason: "waiting_for_older_candidate",
    required_next_action: "wait_for_queue",
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
        trigger_type: "dependency_changed",
        trigger_ref: "uv.lock",
        reason_code: "STALE_DEPENDENCY",
      }),
      staleReason({
        id: "sr_4",
        status: "resolved",
        trigger_ref: "old-main",
        resolved_at: "2026-04-26T12:04:00Z",
      }),
    ],
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
    policy_findings: [
      {
        id: "pf_1",
        workspace_id: "ws_1",
        candidate_id: "mc_111111111111111111111111",
        attempt_id: "att_111111111111111111111111",
        task_id: "task_1",
        reason_code: "OUT_OF_SCOPE_CHANGE",
        severity: "blocking",
        subject_path: "src/awf/api/routes/merge_queue.py",
        explanation: "Changed an unowned API route.",
        details: {},
        status: "active",
        detected_at: "2026-04-26T12:02:00Z",
        resolved_at: null,
      },
      {
        id: "pf_2",
        workspace_id: "ws_1",
        candidate_id: "mc_111111111111111111111111",
        attempt_id: "att_111111111111111111111111",
        task_id: "task_1",
        reason_code: "OUT_OF_SCOPE_CHANGE",
        severity: "warning",
        subject_path: "docs/old.md",
        explanation: "Resolved docs finding.",
        details: {},
        status: "resolved",
        detected_at: "2026-04-26T12:01:00Z",
        resolved_at: "2026-04-26T12:04:00Z",
      },
    ],
  });

  const recovery = summarizeRecovery(item);

  assert.equal(recovery.staleReasonCount, 3);
  assert.equal(recovery.staleReasonBlockingCount, 3);
  assert.equal(recovery.staleReasonAdvisoryCount, 0);
  assert.equal(recovery.staleReasonLabel, "STALE_TARGET_ADVANCED @ main@abc123, STALE_OVERLAP @ src/awf/api/** +1");
  assert.equal(recovery.queueBlockerCount, 2);
  assert.equal(
    recovery.queueBlockerDetail,
    "ws_old / mc_old / merge_eligible / monitoring_pr / MERGE_QUEUE_WAITING_FOR_OLDER_CANDIDATE +1 more",
  );
  assert.equal(recovery.policyFindingCount, 1);
  assert.equal(recovery.policyFindingLabel, "1 blocking policy");
  assert.equal(recovery.recommendedActionLabel, "wait for queue");
});

test("recovery summary falls back safely for legacy or missing validation data", () => {
  const item = mergeQueueItem({
    candidate_id: null,
    attempt_id: null,
    task_class: "test_task",
    merge_blocker_reason: "manual_merge_required",
    required_next_action: null,
    readiness: null,
    canonical: false,
    latest_validation: null,
  });

  const recovery = summarizeRecovery(item);

  assert.equal(recovery.recommendedActionLabel, "manual merge");
  assert.equal(recovery.requiredTierLabel, "T1 required");
  assert.equal(recovery.latestSatisfiedTierLabel, "none satisfied");
  assert.equal(recovery.freshnessLabel, "unavailable");
  assert.equal(recovery.baseShaLabel, "unknown");
  assert.equal(recovery.validatedTargetShaLabel, "unknown");
  assert.equal(recovery.currentTargetShaLabel, "unknown");
  assert.equal(recovery.targetRangeLabel, "unknown -> unknown");
  assert.equal(recovery.candidateLabel, "legacy");
  assert.equal(recovery.blockerLabel, "manual merge");
});
