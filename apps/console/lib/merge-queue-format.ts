import type {
  MergeBlockerReason,
  MergeQueueBlocker,
  MergeQueueItem,
  StaleReason,
  ValidationFreshnessStatus,
  ValidationFreshnessSummary,
  ValidationTier,
} from "@/lib/types";
import { compactId } from "./format.ts";

export interface StaleReasonSummary {
  count: number;
  blockingCount: number;
  advisoryCount: number;
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
  commandHashLabel: string;
  profileLabel: string;
  environmentLabel: string;
  identitySourceLabel: string;
  baseShaLabel: string;
  workspaceHeadShaLabel: string;
  validatedTargetShaLabel: string;
  currentTargetShaLabel: string;
  reasonLabel: string;
}

export interface RecoverySummary {
  recommendedActionLabel: string;
  requiredTierLabel: string;
  latestSatisfiedTierLabel: string;
  latestSatisfiedTierDetail: string;
  freshnessLabel: string;
  baseShaLabel: string;
  workspaceHeadShaLabel: string;
  validatedTargetShaLabel: string;
  currentTargetShaLabel: string;
  targetRangeLabel: string;
  validationReasonLabel: string;
  commandHashLabel: string;
  profileLabel: string;
  environmentLabel: string;
  blockerLabel: string;
  blockerDetail: string;
  candidateLabel: string;
  attemptLabel: string;
  staleReasonCount: number;
  staleReasonBlockingCount: number;
  staleReasonAdvisoryCount: number;
  staleReasonLabel: string;
  staleReasonDetail: string;
  queueBlockerCount: number;
  queueBlockerLabel: string;
  queueBlockerDetail: string;
  policyFindingCount: number;
  policyFindingLabel: string;
  policyFindingDetail: string;
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
    return requiredNextActions[action]?.label ?? humanizeCode(action);
  }
  return (blockerReason && requiredNextActionFallbacks[blockerReason]?.label) || "none";
}

export function requiredNextActionTone(
  action: string | null | undefined,
  blockerReason: MergeBlockerReason,
): QueueTone {
  if (action !== null && action !== undefined) {
    return requiredNextActions[action]?.tone ?? "neutral";
  }
  return requiredNextActionFallbacks[blockerReason]?.tone ?? "neutral";
}

export function formatMergeBlockerReason(reason: MergeBlockerReason): string {
  return blockerReasonLabels[reason] ?? humanizeCode(reason);
}

export function summarizeStaleReasons(item: Pick<MergeQueueItem, "stale_reasons">): StaleReasonSummary {
  const activeReasons = activeStaleReasons(item);
  const blockingReasons = activeReasons.filter(staleReasonBlocksMerge);
  const advisoryReasons = activeReasons.filter((reason) => !staleReasonBlocksMerge(reason));
  if (activeReasons.length === 0) {
    return {
      count: 0,
      blockingCount: 0,
      advisoryCount: 0,
      label: "none",
      detail: "no active stale reasons",
      overflowCount: 0,
      activeReasons,
    };
  }

  const primaryReasons = blockingReasons.length > 0 ? blockingReasons : advisoryReasons;
  const visibleReasons = primaryReasons.slice(0, 2);
  const overflowCount = Math.max(0, primaryReasons.length - visibleReasons.length);
  const label = blockingReasons.length > 0
    ? `${visibleReasons.map(staleReasonLabel).join(", ")}${overflowCount ? ` +${overflowCount}` : ""}`
    : `${advisoryReasons.length} advisory overlap${advisoryReasons.length === 1 ? "" : "s"}`;

  return {
    count: activeReasons.length,
    blockingCount: blockingReasons.length,
    advisoryCount: advisoryReasons.length,
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
  const base = summarizeQueueIdentity(item);

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

export function summarizeValidation(
  item: Pick<MergeQueueItem, "latest_validation" | "validation_freshness_status" | "validation_reason_code">,
): ValidationSummary {
  const validation = item.latest_validation;
  if (!validation) {
    const reasonCode = item.validation_reason_code ?? "validation_unavailable";
    const reasonLabel = formatValidationReasonLabel(reasonCode);
    return {
      label: "none",
      detail: reasonCode,
      freshLabel: formatFreshnessLabel(item.validation_freshness_status ?? "unavailable"),
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
      reasonLabel,
    };
  }

  const freshnessStatus = validation.freshness_status ?? freshnessStatusFromBoolean(validation.fresh_for_target);
  const freshnessReason = validation.freshness_reason_code ?? item.validation_reason_code ?? freshnessReasonFromStatus(freshnessStatus);
  const freshnessReasonLabel = formatValidationReasonLabel(freshnessReason);
  const freshLabel = formatFreshnessLabel(freshnessStatus);
  const retryDetail = validation.retry_count > 0 ? ` / retries ${validation.retry_count}` : "";
  const detail = `${validation.reason_code ?? "no reason"}${retryDetail}`;
  const baseShaLabel = compactSha(validation.base_sha ?? validation.base_commit);
  const validatedTargetShaLabel = compactSha(validation.target_head_sha);
  const currentTargetShaLabel = compactSha(validation.current_target_head_sha);

  return {
    label: `T${validation.tier} ${validation.status} / ${freshLabel}`,
    detail,
    freshLabel,
    headLabel: `${validatedTargetShaLabel} -> ${currentTargetShaLabel}`,
    coverageLabel: formatCoverageLabel(validation),
    commandHashLabel: compactShaOrUnavailable(validation.command_set_hash),
    profileLabel: formatProfileLabel(validation),
    environmentLabel: compactShaOrUnavailable(validation.environment_identity_digest),
    identitySourceLabel: validation.identity_source === "legacy_fallback" ? "legacy fallback" : validation.identity_source,
    baseShaLabel,
    workspaceHeadShaLabel: compactSha(validation.workspace_head_sha),
    validatedTargetShaLabel,
    currentTargetShaLabel,
    reasonLabel: `${validation.reason_code ?? "no reason"} / ${freshnessReasonLabel}`,
  };
}

export function summarizeValidationProvenance(
  provenance: ValidationFreshnessSummary | null | undefined,
): Pick<
  RecoverySummary,
  | "requiredTierLabel"
  | "latestSatisfiedTierLabel"
  | "latestSatisfiedTierDetail"
  | "freshnessLabel"
  | "baseShaLabel"
  | "workspaceHeadShaLabel"
  | "validatedTargetShaLabel"
  | "currentTargetShaLabel"
  | "targetRangeLabel"
  | "validationReasonLabel"
  | "commandHashLabel"
  | "profileLabel"
  | "environmentLabel"
> {
  const validation = summarizeValidation({
    latest_validation: provenance?.latest_validation ?? null,
    validation_freshness_status: provenance?.freshness_status,
    validation_reason_code: provenance?.reason_code,
  });
  return {
    requiredTierLabel: formatRequiredTierLabel(normalizeTier(provenance?.required_tier) ?? 1),
    latestSatisfiedTierLabel: formatLatestSatisfiedTierLabel(
      normalizeTier(provenance?.latest_satisfied_tier),
      provenance?.latest_validation ?? null,
    ),
    latestSatisfiedTierDetail: validation.detail,
    freshnessLabel: validation.freshLabel,
    baseShaLabel: validation.baseShaLabel,
    workspaceHeadShaLabel: validation.workspaceHeadShaLabel,
    validatedTargetShaLabel: validation.validatedTargetShaLabel,
    currentTargetShaLabel: validation.currentTargetShaLabel,
    targetRangeLabel: validation.headLabel,
    validationReasonLabel: validation.reasonLabel,
    commandHashLabel: validation.commandHashLabel,
    profileLabel: validation.profileLabel,
    environmentLabel: validation.environmentLabel,
  };
}

export function summarizeRecovery(item: MergeQueueItem): RecoverySummary {
  const identity = summarizeQueueIdentity(item);
  const validation = summarizeValidation(item);
  const stale = summarizeStaleReasons(item);
  const queueBlockers = summarizeQueueBlockers(item);
  const policyFindings = summarizePolicyFindings(item);
  const blockerLabel = formatMergeBlockerReason(item.merge_blocker_reason);

  return {
    recommendedActionLabel: formatRequiredNextAction(item.required_next_action, item.merge_blocker_reason),
    requiredTierLabel: formatRequiredTierLabel(requiredValidationTier(item)),
    latestSatisfiedTierLabel: formatLatestSatisfiedTierLabel(latestSatisfiedValidationTier(item), item.latest_validation),
    latestSatisfiedTierDetail: validation.detail,
    freshnessLabel: validation.freshLabel,
    baseShaLabel: validation.baseShaLabel,
    workspaceHeadShaLabel: validation.workspaceHeadShaLabel,
    validatedTargetShaLabel: validation.validatedTargetShaLabel,
    currentTargetShaLabel: validation.currentTargetShaLabel,
    targetRangeLabel: validation.headLabel,
    validationReasonLabel: validation.reasonLabel,
    commandHashLabel: validation.commandHashLabel,
    profileLabel: validation.profileLabel,
    environmentLabel: validation.environmentLabel,
    blockerLabel,
    blockerDetail: item.merge_blocker_reason,
    candidateLabel: identity.candidateLabel,
    attemptLabel: identity.attemptLabel,
    staleReasonCount: stale.count,
    staleReasonBlockingCount: stale.blockingCount,
    staleReasonAdvisoryCount: stale.advisoryCount,
    staleReasonLabel: stale.label,
    staleReasonDetail: stale.detail,
    queueBlockerCount: queueBlockers.count,
    queueBlockerLabel: queueBlockers.label,
    queueBlockerDetail: queueBlockerDetail(queueBlockers),
    policyFindingCount: policyFindings.count,
    policyFindingLabel: policyFindings.label,
    policyFindingDetail: policyFindings.detail,
  };
}

function summarizeQueueIdentity(
  item: Pick<MergeQueueItem, "attempt_id" | "candidate_id" | "canonical">,
): Pick<ReadinessSummary, "attemptLabel" | "candidateLabel" | "canonicalLabel"> {
  return {
    canonicalLabel: item.canonical ? "canonical" : "superseded",
    candidateLabel: item.candidate_id ? compactId(item.candidate_id, 10) : "legacy",
    attemptLabel: item.attempt_id ? compactId(item.attempt_id, 10) : "none",
  };
}

export function mergeQueueMergedAt(item: Pick<MergeQueueItem, "merged_at" | "status" | "updated_at">): string | null {
  return item.merged_at ?? (item.status === "completed" ? item.updated_at : null);
}

interface RequiredNextActionDefinition {
  label: string;
  tone: QueueTone;
}

const requiredNextActions: Partial<Record<string, RequiredNextActionDefinition>> = {
  rebase: { label: "rebase", tone: "warn" },
  validate: { label: "validate", tone: "warn" },
  resolve_task_scope: { label: "resolve task scope", tone: "bad" },
  resolve_policy_findings: { label: "resolve policy", tone: "bad" },
  wait_for_queue: { label: "wait for queue", tone: "warn" },
};

const requiredNextActionFallbacks: Partial<Record<MergeBlockerReason, RequiredNextActionDefinition>> = {
  ready_to_merge_or_waiting_for_github: { label: "none", tone: "good" },
  manual_merge_required: { label: "manual merge", tone: "warn" },
  waiting_for_monitor: { label: "wait for monitor", tone: "warn" },
  waiting_for_older_candidate: { label: "wait for queue", tone: "warn" },
  workspace_not_terminal: { label: "wait for workspace", tone: "neutral" },
  completed: { label: "none", tone: "good" },
  failed_or_cancelled: { label: "inspect failure", tone: "bad" },
  not_canonical: { label: "superseded", tone: "bad" },
  policy_blocked: { label: "resolve policy", tone: "bad" },
  stale: { label: "rebase", tone: "warn" },
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

const staleReasonLabels: Record<string, string> = {
  STALE_TARGET_ADVANCED: "STALE_TARGET_ADVANCED",
  STALE_OVERLAP: "STALE_OVERLAP",
  STALE_DEPENDENCY: "STALE_DEPENDENCY",
  STALE_BUILD_CONFIG: "STALE_BUILD_CONFIG",
  STALE_SCHEMA: "STALE_SCHEMA",
  ADVISORY_PLAN_ARTIFACT_OVERLAP: "ADVISORY_PLAN_ARTIFACT_OVERLAP",
  validation_insufficient_tier: "validation tier not yet satisfied",
  validation_missing_for_current_head: "AWF validation missing for current PR head",
};

const validationReasonLabels: Record<string, string> = {
  VALIDATION_INSUFFICIENT_TIER: "validation tier not yet satisfied",
  VALIDATION_MISSING_FOR_CURRENT_HEAD: "AWF validation missing for current PR head",
  VALIDATION_SUCCEEDED: "validation succeeded",
  validation_fresh: "validation fresh",
  validation_insufficient_tier: "validation tier not yet satisfied",
  validation_missing_for_current_head: "AWF validation missing for current PR head",
  validation_target_stale: "validation target stale",
  validation_target_unknown: "validation target unknown",
  validation_unavailable: "validation unavailable",
};

function staleReasonLabel(reason: StaleReason): string {
  const reasonLabel = staleReasonLabels[reason.reason_code] ?? reason.reason_code;
  return reason.trigger_ref ? `${reasonLabel} @ ${reason.trigger_ref}` : reasonLabel;
}

function staleReasonDetail(reason: StaleReason): string {
  const reasonLabel = staleReasonLabels[reason.reason_code] ?? reason.reason_code;
  const trigger = reason.trigger_ref ? `${reason.trigger_type} @ ${reason.trigger_ref}` : reason.trigger_type;
  const prefix = staleReasonBlocksMerge(reason) ? "" : "advisory ";
  return `${prefix}${reasonLabel} / ${trigger}`;
}

function staleReasonBlocksMerge(reason: StaleReason): boolean {
  return reason.blocks_merge !== false && reason.severity !== "advisory";
}

function queueBlockerDetail(summary: QueueBlockerSummary): string {
  if (!summary.first) {
    return summary.detail;
  }
  return `${summary.first.workspace_id} / ${summary.first.candidate_id} / ${summary.detail}`;
}

interface PolicyFindingSummary {
  count: number;
  label: string;
  detail: string;
}

function summarizePolicyFindings(item: Pick<MergeQueueItem, "policy_findings">): PolicyFindingSummary {
  const activeFindings = (item.policy_findings ?? []).filter((finding) => finding.status === "active");
  if (activeFindings.length === 0) {
    return {
      count: 0,
      label: "none",
      detail: "no active policy findings",
    };
  }

  const blockingCount = activeFindings.filter((finding) => finding.severity === "blocking").length;
  const first = activeFindings[0];
  const overflowCount = Math.max(0, activeFindings.length - 1);
  return {
    count: activeFindings.length,
    label:
      blockingCount > 0
        ? `${blockingCount} blocking polic${blockingCount === 1 ? "y" : "ies"}`
        : `${activeFindings.length} policy finding${activeFindings.length === 1 ? "" : "s"}`,
    detail: `${first.reason_code}${first.subject_path ? ` / ${first.subject_path}` : ""}${overflowCount ? ` +${overflowCount} more` : ""}`,
  };
}

function requiredValidationTier(
  item: Pick<
    MergeQueueItem,
    "required_validation_tier" | "task_class" | "required_next_action" | "readiness" | "latest_validation" | "latest_satisfied_validation_tier"
  >,
): ValidationTier {
  const explicitTier = normalizeTier(item.required_validation_tier);
  if (explicitTier !== null) {
    return explicitTier;
  }

  const taskTier = taskClassRequiredTier(item.task_class);
  const staleReason = item.readiness?.stale_reason;
  const shouldInferHigherValidationTier =
    staleReason === "validation_insufficient_tier" ||
    (item.required_next_action === "validate" &&
      staleReason !== "validation_missing_for_current_head");
  if (shouldInferHigherValidationTier) {
    const satisfiedTier = latestSatisfiedValidationTier(item);
    if (satisfiedTier !== null) {
      return normalizeTier(Math.max(taskTier, Math.min(3, satisfiedTier + 1))) ?? taskTier;
    }
  }

  return taskTier;
}

function latestSatisfiedValidationTier(
  item: Pick<MergeQueueItem, "latest_satisfied_validation_tier" | "latest_validation">,
): ValidationTier | null {
  const explicitTier = normalizeTier(item.latest_satisfied_validation_tier);
  if (explicitTier !== null) {
    return explicitTier;
  }
  const validation = item.latest_validation;
  if (!validation || validation.status !== "succeeded") {
    return null;
  }
  return normalizeTier(validation.tier);
}

function taskClassRequiredTier(taskClass: string | null): ValidationTier {
  if (taskClass === "migration_task") {
    return 3;
  }
  if (taskClass === "refactor_task" || taskClass === "dependency_task" || taskClass === "build_config_task") {
    return 2;
  }
  return 1;
}

function normalizeTier(value: number | null | undefined): ValidationTier | null {
  if (value === 1 || value === 2 || value === 3) {
    return value;
  }
  return null;
}

function formatRequiredTierLabel(tier: ValidationTier): string {
  return `T${tier} required`;
}

function formatLatestSatisfiedTierLabel(
  tier: ValidationTier | null,
  latestValidation: MergeQueueItem["latest_validation"],
): string {
  if (tier !== null) {
    return `T${tier} satisfied`;
  }
  if (latestValidation && latestValidation.status !== "succeeded") {
    return "unknown satisfied";
  }
  return "none satisfied";
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

function compactShaOrUnavailable(value: string | null | undefined): string {
  const compact = compactSha(value);
  return compact === "unknown" ? "unavailable" : compact;
}

function formatFreshnessLabel(status: ValidationFreshnessStatus): string {
  if (status === "fresh") {
    return "fresh";
  }
  if (status === "stale") {
    return "stale target";
  }
  if (status === "unavailable") {
    return "unavailable";
  }
  return "freshness unknown";
}

function formatValidationReasonLabel(reason: string): string {
  return validationReasonLabels[reason] ?? reason;
}

function freshnessStatusFromBoolean(value: boolean | null | undefined): ValidationFreshnessStatus {
  if (value === true) {
    return "fresh";
  }
  if (value === false) {
    return "stale";
  }
  return "unknown";
}

function freshnessReasonFromStatus(status: ValidationFreshnessStatus): string {
  if (status === "fresh") {
    return "validation_fresh";
  }
  if (status === "stale") {
    return "validation_target_stale";
  }
  if (status === "unavailable") {
    return "validation_unavailable";
  }
  return "validation_target_unknown";
}

function formatProfileLabel(
  validation: NonNullable<MergeQueueItem["latest_validation"]>,
): string {
  if (!validation.profile_name) {
    return "unavailable";
  }
  return validation.profile_version === null || validation.profile_version === undefined
    ? validation.profile_name
    : `${validation.profile_name}@${validation.profile_version}`;
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
