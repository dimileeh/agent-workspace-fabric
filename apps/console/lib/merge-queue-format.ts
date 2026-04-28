import type { MergeBlockerReason, MergeQueueBlocker, MergeQueueItem, StaleReason } from "@/lib/types";
import { compactId } from "./format.ts";

export interface StaleReasonSummary {
  count: number;
  label: string;
  detail: string;
  overflowCount: number;
  activeReasons: StaleReason[];
}

export interface QueueBlockerSummary {
  count: number;
  label: string;
  detail: string;
  first: MergeQueueBlocker | null;
  overflowCount: number;
}

export interface ReadinessSummary {
  label: string;
  detail: string;
  canonicalLabel: "canonical" | "superseded";
  candidateLabel: string;
  attemptLabel: string;
}

export interface ValidationSummary {
  label: string;
  detail: string;
  freshLabel: string;
  headLabel: string;
  coverageLabel: string;
}

export type QueueTone = "neutral" | "info" | "good" | "warn" | "bad";

export function activeStaleReasons(item: Pick<MergeQueueItem, "stale_reasons">): StaleReason[] {
  return (item.stale_reasons ?? []).filter((reason) => reason.status === "active");
}

export function formatRequiredNextAction(
  action: string | null | undefined,
  blockerReason?: MergeBlockerReason | null,
): string {
  if (action) {
    return actionLabels[action] ?? humanizeCode(action);
  }

  switch (blockerReason) {
    case "manual_merge_required":
      return "manual merge";
    case "waiting_for_monitor":
      return "wait for monitor";
    case "waiting_for_older_candidate":
      return "wait for queue";
    case "workspace_not_terminal":
      return "wait for workspace";
    case "policy_blocked":
      return "resolve policy";
    case "stale":
      return "rebase";
    default:
      return "none";
  }
}

export function requiredNextActionTone(
  action: string | null | undefined,
  blockerReason: MergeBlockerReason,
): QueueTone {
  switch (action) {
    case "resolve_policy_findings":
    case "resolve_task_scope":
      return "bad";
    case "wait_for_queue":
    case "validate":
    case "rebase":
      return "warn";
    case null:
    case undefined:
      break;
    default:
      return "neutral";
  }

  switch (blockerReason) {
    case "policy_blocked":
      return "bad";
    case "manual_merge_required":
    case "waiting_for_monitor":
    case "waiting_for_older_candidate":
    case "stale":
      return "warn";
    case "workspace_not_terminal":
      return "neutral";
    default:
      return "good";
  }
}

export function formatMergeBlockerReason(reason: MergeBlockerReason): string {
  return blockerReasonLabels[reason] ?? humanizeCode(reason);
}

export function summarizeStaleReasons(item: Pick<MergeQueueItem, "stale_reasons">): StaleReasonSummary {
  const activeReasons = activeStaleReasons(item);
  if (activeReasons.length === 0) {
    return {
      count: 0,
      label: "none",
      detail: "no active stale reasons",
      overflowCount: 0,
      activeReasons,
    };
  }

  const visibleReasons = activeReasons.slice(0, 2);
  const overflowCount = Math.max(0, activeReasons.length - visibleReasons.length);
  const label = `${visibleReasons.map(staleReasonLabel).join(", ")}${overflowCount ? ` +${overflowCount}` : ""}`;

  return {
    count: activeReasons.length,
    label,
    detail: activeReasons.map(staleReasonDetail).join("; "),
    overflowCount,
    activeReasons,
  };
}

export function summarizeQueueBlockers(item: Pick<MergeQueueItem, "queue_blockers">): QueueBlockerSummary {
  const blockers = item.queue_blockers ?? [];
  const first = blockers[0] ?? null;
  if (!first) {
    return {
      count: 0,
      label: "none",
      detail: "no queue blockers",
      first: null,
      overflowCount: 0,
    };
  }

  const overflowCount = Math.max(0, blockers.length - 1);
  return {
    count: blockers.length,
    label: `${blockers.length} blocker${blockers.length === 1 ? "" : "s"}: ${first.title}${formatPrNumber(first.pr_number)}`,
    detail: `${first.blocker_state} / ${first.status} / ${first.reason_code}${overflowCount ? ` +${overflowCount} more` : ""}`,
    first,
    overflowCount,
  };
}

export function summarizeReadiness(
  item: Pick<MergeQueueItem, "attempt_id" | "candidate_id" | "canonical" | "readiness">,
): ReadinessSummary {
  const readiness = item.readiness;
  const canonicalLabel: ReadinessSummary["canonicalLabel"] = item.canonical ? "canonical" : "superseded";
  const base = {
    canonicalLabel,
    candidateLabel: item.candidate_id ? compactId(item.candidate_id, 10) : "legacy",
    attemptLabel: item.attempt_id ? compactId(item.attempt_id, 10) : "none",
  };

  if (!readiness) {
    return {
      ...base,
      label: "legacy",
      detail: "legacy workspace without candidate readiness",
    };
  }
  if (readiness.ready) {
    return { ...base, label: "ready", detail: "merge-ready" };
  }
  if (readiness.completed) {
    return { ...base, label: "completed", detail: "candidate completed" };
  }
  if (readiness.manual_merge_required) {
    return { ...base, label: "manual", detail: "manual merge required" };
  }
  if (readiness.waiting_for_monitor) {
    return { ...base, label: "waiting", detail: "waiting for monitor" };
  }
  if (readiness.failed_or_cancelled) {
    return { ...base, label: "failed/cancelled", detail: "workspace terminal" };
  }
  if (readiness.not_canonical) {
    return { ...base, label: "not canonical", detail: "superseded attempt" };
  }
  if (readiness.stale) {
    return { ...base, label: "stale", detail: readiness.stale_reason ?? "stale" };
  }
  return { ...base, label: "blocked", detail: "not merge-ready" };
}

export function summarizeValidation(item: Pick<MergeQueueItem, "latest_validation">): ValidationSummary {
  const validation = item.latest_validation;
  if (!validation) {
    return {
      label: "none",
      detail: "no validation run",
      freshLabel: "unknown",
      headLabel: "unknown -> unknown",
      coverageLabel: "coverage unknown",
    };
  }

  const freshLabel = validation.fresh_for_target === true
    ? "fresh"
    : validation.fresh_for_target === false
      ? "stale target"
      : "freshness unknown";
  const retryDetail = validation.retry_count > 0 ? ` / retries ${validation.retry_count}` : "";
  const detail = `${validation.reason_code ?? "no reason"}${retryDetail}`;

  return {
    label: `T${validation.tier} ${validation.status} / ${freshLabel}`,
    detail,
    freshLabel,
    headLabel: `${compactSha(validation.target_head_sha)} -> ${compactSha(validation.current_target_head_sha)}`,
    coverageLabel: formatCoverageLabel(validation),
  };
}

export function mergeQueueMergedAt(item: Pick<MergeQueueItem, "merged_at" | "status" | "updated_at">): string | null {
  return item.merged_at ?? (item.status === "completed" ? item.updated_at : null);
}

const actionLabels: Record<string, string> = {
  rebase: "rebase",
  validate: "validate",
  resolve_task_scope: "resolve task scope",
  resolve_policy_findings: "resolve policy",
  wait_for_queue: "wait for queue",
};

const blockerReasonLabels: Record<MergeBlockerReason, string> = {
  ready_to_merge_or_waiting_for_github: "ready / GitHub",
  manual_merge_required: "manual merge",
  waiting_for_monitor: "waiting for monitor",
  waiting_for_older_candidate: "older candidate",
  workspace_not_terminal: "workspace active",
  completed: "completed",
  failed_or_cancelled: "failed/cancelled",
  not_canonical: "not canonical",
  policy_blocked: "policy blocked",
  stale: "stale",
};

function staleReasonLabel(reason: StaleReason): string {
  return reason.trigger_ref ? `${reason.reason_code} @ ${reason.trigger_ref}` : reason.reason_code;
}

function staleReasonDetail(reason: StaleReason): string {
  const trigger = reason.trigger_ref ? `${reason.trigger_type} @ ${reason.trigger_ref}` : reason.trigger_type;
  return `${reason.reason_code} / ${trigger}`;
}

function formatPrNumber(prNumber: number | null): string {
  return prNumber === null ? "" : ` #${prNumber}`;
}

function compactSha(value: string | null | undefined): string {
  if (!value) {
    return "unknown";
  }
  return /^[0-9a-f]{12,}$/i.test(value) ? value.slice(0, 7) : value;
}

function formatCoverageLabel(validation: NonNullable<MergeQueueItem["latest_validation"]>): string {
  if (!validation.coverage_status) {
    return "coverage unknown";
  }
  const percent = formatPercent(validation.coverage_percent);
  const minimum = formatPercent(validation.coverage_minimum_percent);
  if (percent && minimum) {
    return `coverage ${validation.coverage_status} ${percent}/${minimum}%`;
  }
  if (percent) {
    return `coverage ${validation.coverage_status} ${percent}%`;
  }
  return `coverage ${validation.coverage_status}`;
}

function formatPercent(value: number | null): string | null {
  if (value === null || !Number.isFinite(value)) {
    return null;
  }
  return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(2)));
}

function humanizeCode(value: string): string {
  return value.replaceAll("_", " ");
}
