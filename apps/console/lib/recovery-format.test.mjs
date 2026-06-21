import assert from "node:assert/strict";
import test from "node:test";

import {
  formatRecoveryAction,
  formatRecoveryBadge,
  formatRecoveryCallout,
  isReverseWorkspaceTransition,
} from "./recovery-format.ts";

const recovery = {
  from_state: "monitoring_pr",
  to_state: "ready",
  reason_code: "STALE_OVERLAP",
  action: "rebase",
  recovery_mode: "rebase_only",
  started_at: "2026-04-27T12:00:00Z",
  current_operation: {
    id: "op_recovery",
    type: "validate",
    status: "running",
    created_at: "2026-04-27T12:00:01Z",
    started_at: "2026-04-27T12:00:02Z",
    payload: null,
  },
  summary: "monitoring_pr -> ready for STALE_OVERLAP.",
  payload: null,
};

test("badge formatting prefers reason code and stays compact", () => {
  assert.equal(formatRecoveryBadge(recovery), "Recovery: STALE_OVERLAP");
  assert.equal(formatRecoveryBadge({ ...recovery, reason_code: null, recovery_mode: "validate_only" }), "Recovery: validate-only");
  assert.equal(formatRecoveryBadge(null), null);
});

test("badge formatting hides recovered history after successful completion", () => {
  assert.equal(formatRecoveryBadge(recovery, "validating"), "Recovery: STALE_OVERLAP");
  assert.equal(formatRecoveryBadge(recovery, "completed"), null);
});

test("callout formatting explains reverse transition, reason, and action", () => {
  const callout = formatRecoveryCallout(recovery, "validating");

  assert.equal(callout.title, "Reverted from monitoring_pr to ready");
  assert.equal(callout.reason, "STALE_OVERLAP");
  assert.equal(callout.action, "rebase + revalidate");
  assert.equal(callout.current, "validate running");
  assert.match(callout.body, /STALE_OVERLAP/);
  assert.match(callout.body, /rebase \+ revalidate/);
  assert.match(callout.body, /now validating/);
});

test("action labels map validate-only recovery and avoid undefined text", () => {
  assert.equal(
    formatRecoveryAction({
      ...recovery,
      action: "validate",
      recovery_mode: "validate_only",
      current_operation: null,
    }),
    "validate-only recovery",
  );

  const callout = formatRecoveryCallout(
    {
      ...recovery,
      from_state: null,
      to_state: null,
      reason_code: null,
      action: null,
      recovery_mode: null,
      current_operation: null,
      summary: "Reverted unknown -> unknown for recovery.",
    },
    undefined,
  );

  assert.equal(callout.title, "Reverted from unknown to unknown");
  assert.equal(callout.reason, "recovery");
  assert.equal(callout.action, "recovery");
  assert.equal(callout.current, "no active operation");
  assert.match(callout.body, /Reverted unknown -> unknown for recovery\./);
  assert.doesNotMatch(`${callout.title} ${callout.body}`, /undefined/);
});

test("reverse timeline detection identifies backward workspace transitions", () => {
  assert.equal(
    isReverseWorkspaceTransition({
      event_type: "workspace.state_changed",
      old_state: "monitoring_pr",
      new_state: "ready",
    }),
    true,
  );
  assert.equal(
    isReverseWorkspaceTransition({
      event_type: "workspace.state_changed",
      old_state: "ready",
      new_state: "running",
    }),
    false,
  );
  assert.equal(
    isReverseWorkspaceTransition({
      event_type: "workspace.created",
      old_state: null,
      new_state: "requested",
    }),
    false,
  );
});

test("blocked entries and resumes are not flagged as reverse transitions", () => {
  // Post-PR pause: a higher lifecycle index (monitoring_pr) moving into blocked.
  assert.equal(
    isReverseWorkspaceTransition({
      event_type: "workspace.state_changed",
      old_state: "monitoring_pr",
      new_state: "blocked",
    }),
    false,
  );
  // Pre-PR resume: blocked stepping back to an earlier execution stage.
  assert.equal(
    isReverseWorkspaceTransition({
      event_type: "workspace.state_changed",
      old_state: "blocked",
      new_state: "validating",
    }),
    false,
  );
  // A genuine regression that does not involve blocked is still detected.
  assert.equal(
    isReverseWorkspaceTransition({
      event_type: "workspace.state_changed",
      old_state: "pushing",
      new_state: "running",
    }),
    true,
  );
});

test("recovering entries and resumes are not flagged as reverse transitions", () => {
  // Mid-run pause: running stalls into the in-place provider retry.
  assert.equal(
    isReverseWorkspaceTransition({
      event_type: "workspace.state_changed",
      old_state: "running",
      new_state: "recovering",
    }),
    false,
  );
  // Auto-heal resume: recovering steps back to running (a lower lifecycle index).
  assert.equal(
    isReverseWorkspaceTransition({
      event_type: "workspace.state_changed",
      old_state: "recovering",
      new_state: "running",
    }),
    false,
  );
});
