import type { WorkspaceEvent, WorkspaceRecoverySummary, WorkspaceStatus } from "@/lib/types";
import { lifecycleStages } from "./format.ts";

export interface RecoveryCallout {
  title: string;
  body: string;
  reason: string;
  action: string;
  current: string;
}

export function formatRecoveryBadge(
  recovery: WorkspaceRecoverySummary | null | undefined,
  currentStatus?: WorkspaceStatus | string | null,
): string | null {
  if (!recovery) {
    return null;
  }
  if (currentStatus === "completed") {
    return null;
  }
  return `Recovery: ${recovery.reason_code ?? recoveryModeLabel(recovery.recovery_mode) ?? "active"}`;
}

export function formatRecoveryCallout(
  recovery: WorkspaceRecoverySummary,
  currentStatus: WorkspaceStatus | string | null | undefined,
): RecoveryCallout {
  const fromState = recovery.from_state ?? "unknown";
  const toState = recovery.to_state ?? "unknown";
  const reason = recovery.reason_code ?? "recovery";
  const action = formatRecoveryAction(recovery);
  const current = recovery.current_operation
    ? `${recovery.current_operation.type} ${recovery.current_operation.status}`
    : "no active operation";
  const statusText = currentStatus ? `now ${currentStatus}` : "now status unknown";
  return {
    title: `Reverted from ${fromState} to ${toState}`,
    reason,
    action,
    current,
    body: `${reason} / ${action} / ${current} / ${statusText}. ${recovery.summary}`,
  };
}

export function formatRecoveryAction(recovery: WorkspaceRecoverySummary): string {
  if (recovery.recovery_mode === "rebase_only") {
    return "rebase + revalidate";
  }
  if (recovery.recovery_mode === "validate_only") {
    return "validate-only recovery";
  }
  if (recovery.action) {
    return humanizeCode(recovery.action);
  }
  return recoveryModeLabel(recovery.recovery_mode) ?? "recovery";
}

export function isReverseWorkspaceTransition(
  event: Pick<WorkspaceEvent, "event_type" | "old_state" | "new_state">,
): boolean {
  if (event.event_type !== "workspace.state_changed") {
    return false;
  }
  // `blocked` is a non-linear operator pause: it is entered from any execution
  // stage (incl. monitoring_pr, a higher lifecycle index) and resumes back to an
  // earlier one. Those entries/exits are expected, not regressions, so a
  // lifecycle-index comparison would mislabel them as backward "step-back" moves.
  // Never flag a transition into or out of `blocked` as reverse. `recovering` is
  // the same shape — a mid-run pause that resumes back to `running` (a lower
  // lifecycle index) — so its entries/exits must not be flagged reverse either.
  if (
    event.old_state === "blocked" ||
    event.new_state === "blocked" ||
    event.old_state === "recovering" ||
    event.new_state === "recovering"
  ) {
    return false;
  }
  const oldIndex = lifecycleStageIndex(event.old_state);
  const newIndex = lifecycleStageIndex(event.new_state);
  return oldIndex >= 0 && newIndex >= 0 && oldIndex > newIndex;
}

function lifecycleStageIndex(value: string | null): number {
  return lifecycleStages.indexOf(value as WorkspaceStatus);
}

function recoveryModeLabel(value: string | null | undefined): string | null {
  if (value === "rebase_only") {
    return "rebase recovery";
  }
  if (value === "validate_only") {
    return "validate-only";
  }
  return value ? humanizeCode(value) : null;
}

function humanizeCode(value: string): string {
  return value.replaceAll("_", " ");
}
