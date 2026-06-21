import type {
  ApiEnvelope,
  MergeQueueItem,
  Operation,
  ValidationTier,
  Workspace,
  WorkspaceControlResponse,
  WorkspaceControlWarning,
  WorkspaceOperatorAction,
  WorkspaceOverview,
} from "./types.ts";

export interface WorkspaceOperatorControl {
  action: WorkspaceOperatorAction;
  label: string;
  visible: boolean;
  enabled: boolean;
  reason: string | null;
  requestedTier?: ValidationTier;
}

export interface WorkspaceOperatorContext {
  overview: WorkspaceOverview;
  workspace?: Workspace | null;
  mergeQueueItem?: MergeQueueItem | null;
  operations?: Operation[];
}

export interface WorkspaceOperatorSuccessSummary {
  action: WorkspaceOperatorAction;
  operationId: string;
  status: string;
  message: string;
  warnings: WorkspaceControlWarning[];
}

export interface WorkspaceOperatorFailureSummary {
  errorCode: string | null;
  message: string;
}

const labels: Record<WorkspaceOperatorAction, string> = {
  remonitor: "Remonitor",
  refresh: "Refresh",
  revalidate: "Revalidate",
  cancel: "Cancel",
};

const cancellableStatuses = new Set([
  "requested",
  "provisioning",
  "ready",
  "running",
  "validating",
  "pushing",
  "monitoring_pr",
  // A paused (blocked) workspace holds its execution slot + warm stack while it
  // awaits an operator decision. Cancel stays the manual capacity-reclaim lever
  // (the state machine allows blocked → cancelled), so the operator can abandon a
  // workspace that should not be resumed instead of being forced to grant/revert.
  "blocked",
  // A recovering workspace is an auto-retry pause that likewise keeps its slot +
  // warm stack during the provider cooldown. The state machine allows
  // recovering → cancelled, so cancel must stay the manual capacity-reclaim lever
  // for operators who want to reclaim capacity instead of waiting out a long
  // auto-retry — otherwise the only escape is the CLI/API.
  "recovering",
]);

const terminalOperationStatuses = new Set(["succeeded", "failed", "cancelled", "canceled"]);
const cancellationOperationTypes = new Set(["cancel", "stop"]);

export function getWorkspaceOperatorControls(context: WorkspaceOperatorContext): WorkspaceOperatorControl[] {
  const active = hasActiveOperation(context);
  const cancelling = hasActiveCancellationOperation(context);
  const controls = [
    remonitorControl(context),
    refreshControl(context),
    revalidateControl(context),
    cancelControl(context),
  ];

  if (!active) {
    return controls;
  }

  return controls.map((control) => ({
    ...control,
    enabled: control.action === "cancel" ? (cancelling ? false : control.enabled) : false,
    visible: control.visible || eligibleEnoughForActiveReason(control.action, context),
    reason:
      control.action === "cancel" ? (cancelling ? "cancel/stop already active" : control.reason) : "active operation",
  }));
}

export function summarizeWorkspaceOperatorSuccess(
  action: WorkspaceOperatorAction,
  response: WorkspaceControlResponse | Operation,
): WorkspaceOperatorSuccessSummary {
  const operationId = "operation_id" in response ? response.operation_id : response.id;
  const status = "operation_status" in response ? response.operation_status : response.status;
  return {
    action,
    operationId,
    status,
    message: `${labels[action]} ${status}: ${operationId}`,
    warnings: controlWarnings(response),
  };
}

export function summarizeWorkspaceOperatorFailure(
  result: Extract<ApiEnvelope<unknown>, { ok: false }>,
): WorkspaceOperatorFailureSummary {
  const detail = structuredErrorDetail(result.detail);
  const errorCode = result.errorCode ?? detail.errorCode;
  const message = detail.message ?? result.message;
  return {
    errorCode: errorCode ?? null,
    message: errorCode ? `${errorCode}: ${message}` : message,
  };
}

function remonitorControl(context: WorkspaceOperatorContext): WorkspaceOperatorControl {
  const prUrl = workspacePrUrl(context);
  const status = context.overview.status;
  const visible = Boolean(prUrl) || status === "monitoring_pr" || status === "failed";

  if (!prUrl && (status === "monitoring_pr" || status === "failed")) {
    return control("remonitor", { visible: true, enabled: false, reason: "no PR" });
  }
  if (prUrl && (status === "monitoring_pr" || status === "failed")) {
    return control("remonitor", { visible: true, enabled: true, reason: null });
  }
  return control("remonitor", { visible, enabled: false, reason: "not monitorable" });
}

function refreshControl(context: WorkspaceOperatorContext): WorkspaceOperatorControl {
  if (context.overview.status === "destroying" || context.overview.status === "destroyed") {
    return control("refresh", { visible: false, enabled: false, reason: "terminal" });
  }
  if (hasMergeCandidateOrStaleContext(context)) {
    return control("refresh", { visible: true, enabled: true, reason: null });
  }
  return control("refresh", { visible: false, enabled: false, reason: "no merge candidate" });
}

function revalidateControl(context: WorkspaceOperatorContext): WorkspaceOperatorControl {
  const requestedTier = requestedValidationTier(context);
  const prUrl = workspacePrUrl(context);
  if (!prUrl) {
    return control("revalidate", { visible: isPotentialValidationContext(context), enabled: false, reason: "no PR", requestedTier });
  }
  if (context.overview.status !== "monitoring_pr") {
    return control("revalidate", {
      visible: isPotentialValidationContext(context) || Boolean(context.mergeQueueItem),
      enabled: false,
      reason: "not PR-monitoring",
      requestedTier,
    });
  }
  if (needsValidationRecovery(context)) {
    return control("revalidate", { visible: true, enabled: true, reason: null, requestedTier });
  }
  return control("revalidate", { visible: Boolean(context.mergeQueueItem) || Boolean(context.workspace), enabled: false, reason: "validation fresh", requestedTier });
}

function cancelControl(context: WorkspaceOperatorContext): WorkspaceOperatorControl {
  if (isCancellable(context)) {
    return control("cancel", { visible: true, enabled: true, reason: null });
  }
  return control("cancel", { visible: false, enabled: false, reason: "not cancellable" });
}

function isCancellable(context: WorkspaceOperatorContext): boolean {
  return cancellableStatuses.has(context.overview.status);
}

function control(
  action: WorkspaceOperatorAction,
  state: Omit<WorkspaceOperatorControl, "action" | "label">,
): WorkspaceOperatorControl {
  return {
    action,
    label: labels[action],
    ...state,
  };
}

function hasActiveOperation(context: WorkspaceOperatorContext): boolean {
  if (context.overview.active_operation) {
    return true;
  }
  const recoveryOperation = context.workspace?.recovery?.current_operation ?? context.overview.recovery?.current_operation ?? null;
  if (recoveryOperation && !terminalOperationStatuses.has(recoveryOperation.status)) {
    return true;
  }
  return (context.operations ?? []).some((operation) => !terminalOperationStatuses.has(operation.status));
}

function hasActiveCancellationOperation(context: WorkspaceOperatorContext): boolean {
  const cancellationOperations = (context.operations ?? []).filter((operation) =>
    cancellationOperationTypes.has(operation.type),
  );

  const recoveryOperation = context.workspace?.recovery?.current_operation ?? context.overview.recovery?.current_operation ?? null;
  if (recoveryOperation) {
    if (cancellationOperationTypes.has(recoveryOperation.type) && !terminalOperationStatuses.has(recoveryOperation.status)) {
      return true;
    }
  }
  if (cancellationOperations.some((operation) => !terminalOperationStatuses.has(operation.status))) {
    return true;
  }
  if (cancellationOperations.length > 0) {
    return false;
  }
  if (context.overview.active_operation === "cancel" || context.overview.active_operation === "stop") {
    return true;
  }
  return false;
}

function eligibleEnoughForActiveReason(action: WorkspaceOperatorAction, context: WorkspaceOperatorContext): boolean {
  if (action === "remonitor") {
    return Boolean(workspacePrUrl(context)) || context.overview.status === "monitoring_pr" || context.overview.status === "failed";
  }
  if (action === "refresh") {
    return hasMergeCandidateOrStaleContext(context);
  }
  if (action === "cancel") {
    return isCancellable(context);
  }
  return Boolean(workspacePrUrl(context)) || isPotentialValidationContext(context);
}

function hasMergeCandidateOrStaleContext(context: WorkspaceOperatorContext): boolean {
  return Boolean(context.mergeQueueItem) || hasActiveStaleReason(context) || hasStaleReadiness(context) || hasStaleValidationFreshness(context);
}

function hasActiveStaleReason(context: WorkspaceOperatorContext): boolean {
  return (context.mergeQueueItem?.stale_reasons ?? []).some((reason) => reason.status === "active");
}

function hasStaleReadiness(context: WorkspaceOperatorContext): boolean {
  return context.mergeQueueItem?.readiness?.stale === true;
}

function hasStaleValidationFreshness(context: WorkspaceOperatorContext): boolean {
  return (
    context.mergeQueueItem?.validation_freshness_status === "stale" ||
    context.workspace?.validation_provenance?.freshness_status === "stale"
  );
}

function needsValidationRecovery(context: WorkspaceOperatorContext): boolean {
  return (
    context.mergeQueueItem?.required_next_action === "validate" ||
    validationReasonMatches(context.mergeQueueItem?.readiness?.stale_reason, "validation_insufficient_tier") ||
    validationReasonMatches(context.mergeQueueItem?.validation_reason_code, "validation_insufficient_tier") ||
    hasStaleValidationFreshness(context)
  );
}

function isPotentialValidationContext(context: WorkspaceOperatorContext): boolean {
  return Boolean(context.mergeQueueItem) || context.overview.status === "monitoring_pr" || needsValidationRecovery(context);
}

function requestedValidationTier(context: WorkspaceOperatorContext): ValidationTier {
  return (
    normalizeTier(context.mergeQueueItem?.required_validation_tier) ??
    normalizeTier(context.workspace?.validation_provenance?.required_tier) ??
    normalizeTier(context.mergeQueueItem?.latest_satisfied_validation_tier ?? null) ??
    normalizeTier(context.workspace?.validation_provenance?.latest_satisfied_tier) ??
    1
  );
}

function normalizeTier(value: number | null | undefined): ValidationTier | null {
  if (value === 1 || value === 2 || value === 3) {
    return value;
  }
  return null;
}

function workspacePrUrl(context: WorkspaceOperatorContext): string | null {
  return context.workspace?.pr_url ?? context.overview.pr_url ?? context.mergeQueueItem?.pr_url ?? null;
}

function validationReasonMatches(value: string | null | undefined, expected: string): boolean {
  return value?.toLowerCase() === expected.toLowerCase();
}

function controlWarnings(response: WorkspaceControlResponse | Operation): WorkspaceControlWarning[] {
  if (!("warnings" in response) || !Array.isArray(response.warnings)) {
    return [];
  }
  return response.warnings.filter(isControlWarning);
}

function isControlWarning(value: unknown): value is WorkspaceControlWarning {
  if (!value || typeof value !== "object") {
    return false;
  }
  const record = value as Record<string, unknown>;
  return typeof record.warning_code === "string" && typeof record.message === "string";
}

function structuredErrorDetail(detail: unknown): { errorCode?: string; message?: string } {
  if (!detail || typeof detail !== "object") {
    return {};
  }
  const record = detail as Record<string, unknown>;
  const nested = record.detail;
  if (nested && typeof nested === "object") {
    const nestedRecord = nested as Record<string, unknown>;
    return {
      errorCode: stringValue(nestedRecord.error_code),
      message: stringValue(nestedRecord.message),
    };
  }
  return {
    errorCode: stringValue(record.error_code),
    message: stringValue(record.message),
  };
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}
