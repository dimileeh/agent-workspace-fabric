import type { WorkspaceOverview } from "@/lib/types";
import { lifecycleStages } from "./format.ts";

type AgentLabelWorkspace = Pick<
  WorkspaceOverview,
  "agent" | "agent_model" | "agent_effort" | "cursor_auto_mode"
>;
type AgentTitleWorkspace = Pick<
  WorkspaceOverview,
  "agent" | "agent_model" | "agent_effort" | "cursor_auto_mode"
> &
  Partial<Pick<WorkspaceOverview, "agent_model_source" | "agent_effort_source">>;
type AgentEffortWorkspace = Pick<WorkspaceOverview, "agent_effort"> &
  Partial<Pick<WorkspaceOverview, "agent_effort_source">>;

type RequestedModelWorkspace = Partial<
  Pick<
    WorkspaceOverview,
    | "requested_model"
    | "requested_effort"
    | "requested_model_source"
    | "requested_effort_source"
    | "agent_model"
    | "agent_effort"
    | "agent_model_source"
    | "agent_effort_source"
  >
>;

type ConfirmedModelWorkspace = Partial<
  Pick<WorkspaceOverview, "confirmed_execution_model" | "confirmed_execution_model_source">
>;

/**
 * Provenance that must never be labeled as confirmed execution evidence.
 * Any other nonempty source is confirmation provenance: the shared type
 * permits arbitrary strings, and the contract only excludes non-confirming labels.
 */
const NON_CONFIRMING_MODEL_SOURCES = new Set([
  "task_policy",
  "default",
  "auto",
  "inferred",
  "configured",
  "unavailable",
]);

export function formatAgentLabel(workspace: AgentLabelWorkspace): string {
  const model = displayAgentModel(workspace);
  return [workspace.agent, model, workspace.agent_effort].filter(Boolean).join(" · ");
}

/**
 * Agent identity for panels that already render requested effort as its own fact.
 * Never embeds agent_effort: that field is policy/default/auto metadata and is
 * easy to mistake for confirmed execution evidence when repeated beside
 * Requested effort.
 */
export function formatAgentIdentityLabel(workspace: AgentLabelWorkspace): string {
  return formatAgentLabel({ ...workspace, agent_effort: null });
}

export function formatAgentTitle(workspace: AgentTitleWorkspace): string {
  const parts: string[] = [workspace.agent];
  const model = displayAgentModel(workspace);
  if (model) {
    parts.push(model);
  }
  if (workspace.agent_effort) {
    parts.push(`effort ${workspace.agent_effort}`);
  }
  if (workspace.agent_model_source && workspace.agent_model_source !== "default") {
    parts.push(`model ${workspace.agent_model_source}`);
  }
  if (workspace.agent_effort_source && workspace.agent_effort_source !== "default") {
    parts.push(`effort ${workspace.agent_effort_source}`);
  }
  return parts.join(" / ");
}

export function formatAgentEffort(workspace: AgentEffortWorkspace): string {
  if (!workspace.agent_effort) {
    return "—";
  }
  return workspace.agent_effort_source
    ? `${workspace.agent_effort} (${workspace.agent_effort_source})`
    : workspace.agent_effort;
}

export function compactAgentModel(model: string | null | undefined): string | null {
  if (!model) {
    return null;
  }
  return model.startsWith("ollama/") ? model.slice("ollama/".length) : model;
}

/** True unless source is empty or an explicitly non-confirming provenance. */
export function isConfirmedModelSource(source: string | null | undefined): boolean {
  if (!source) {
    return false;
  }
  const normalized = source.trim().toLowerCase();
  if (!normalized) {
    return false;
  }
  return !NON_CONFIRMING_MODEL_SOURCES.has(normalized);
}

export function formatRequestedModel(workspace: RequestedModelWorkspace): string {
  // Pair value with matching provenance: never label an explicit request with
  // leftover legacy agent_model_source when requested_model_source is omitted.
  const usedRequested = workspace.requested_model != null;
  const model = usedRequested ? workspace.requested_model : workspace.agent_model;
  if (!model) {
    return "not recorded";
  }
  const source = usedRequested
    ? workspace.requested_model_source
    : workspace.agent_model_source;
  const compact = compactAgentModel(model) ?? model;
  return source ? `${compact} (${source})` : compact;
}

export function formatRequestedEffort(workspace: RequestedModelWorkspace): string {
  // Pair value with matching provenance: never label an explicit request with
  // leftover legacy agent_effort_source when requested_effort_source is omitted.
  const usedRequested = workspace.requested_effort != null;
  const effort = usedRequested ? workspace.requested_effort : workspace.agent_effort;
  if (!effort) {
    return "not recorded";
  }
  const source = usedRequested
    ? workspace.requested_effort_source
    : workspace.agent_effort_source;
  return source ? `${effort} (${source})` : effort;
}

/**
 * Confirmed execution model when provenance is a nonempty confirming source.
 * Never labels task_policy / default / auto / inferred / configured / unavailable as confirmed.
 */
export function formatConfirmedExecutionModel(workspace: ConfirmedModelWorkspace): string {
  const source = workspace.confirmed_execution_model_source;
  if (!isConfirmedModelSource(source)) {
    return "not recorded";
  }
  const model = workspace.confirmed_execution_model;
  if (!model) {
    return "not recorded";
  }
  const compact = compactAgentModel(model) ?? model;
  return `${compact} (${source})`;
}

type WorkflowTimingFields = {
  workflow_finished_at?: string | null;
  finished_at?: string | null;
};

/**
 * Valid workflow completion timestamp: prefer explicit workflow_finished_at,
 * then the documented workflow timing field finished_at. Native runtime finish
 * stays separate via native_runtime_finished_at.
 */
export function resolveWorkflowFinishedAt(
  workspace: WorkflowTimingFields,
): string | null {
  for (const candidate of [workspace.workflow_finished_at, workspace.finished_at]) {
    if (candidate != null && recordedMilliseconds(candidate) != null) {
      return candidate;
    }
  }
  return null;
}

/**
 * True when two recorded timestamps are the same instant, including equivalent
 * ISO forms (`Z` vs `.000Z`) that would render as one clock time under both labels.
 */
function sameRecordedInstant(left: string, right: string): boolean {
  if (left === right) {
    return true;
  }
  return compareRecordedInstants(left, right) === 0;
}

/**
 * Separate "Finished" timestamp. `finished_at` is already the documented
 * fallback for Workflow finished, so omit it when that fact would repeat the
 * same instant (cloud rows that only send `finished_at`, or equivalent ISO forms).
 * Also omit malformed values when no valid workflow finish exists.
 */
export function distinctFinishedAt(workspace: WorkflowTimingFields): string | null {
  const finishedAt = workspace.finished_at;
  if (finishedAt == null || recordedMilliseconds(finishedAt) == null) {
    return null;
  }
  const workflowFinishedAt = resolveWorkflowFinishedAt(workspace);
  if (!workflowFinishedAt) {
    return null;
  }
  if (sameRecordedInstant(finishedAt, workflowFinishedAt)) {
    return null;
  }
  return finishedAt;
}

const TERMINAL_WORKFLOW_STATUSES = new Set<WorkspaceOverview["status"]>([
  "completed",
  "failed",
  "cancelled",
  "destroyed",
]);

type RetainedTerminalEvent = NonNullable<
  WorkspaceOverview["latest_workflow_terminal_state_change"]
>;

function terminalEventMatchesWorkflowStatus(
  item: WorkspaceOverview,
  terminalEvent: RetainedTerminalEvent,
): boolean {
  const cleanupFailureConfirmsEarlierTerminalBoundary =
    item.status === "failed" &&
    (terminalEvent.new_state === "completed" ||
      terminalEvent.new_state === "cancelled") &&
    item.latest_state_change?.event_type === "workspace.state_changed" &&
    item.latest_state_change.old_state === "destroying" &&
    item.latest_state_change.new_state === "failed";
  return (
    terminalEvent.new_state === item.status ||
    ((item.status === "destroying" || item.status === "destroyed") &&
      (terminalEvent.new_state === "completed" ||
        terminalEvent.new_state === "failed" ||
        terminalEvent.new_state === "cancelled")) ||
    cleanupFailureConfirmsEarlierTerminalBoundary
  );
}

/**
 * True when workflow timing should be presented as terminal. Destroy cleanup
 * is post-terminal only when its latest state transition entered cleanup from
 * the retained workflow terminal transition. Retry history can retain an older
 * terminal event, so its presence alone does not prove the current attempt
 * finished before a direct destroy.
 */
export function hasTerminalWorkflowTiming(
  item: Pick<
    WorkspaceOverview,
    | "status"
    | "latest_state_change"
    | "latest_workflow_terminal_state_change"
  >,
): boolean {
  const terminalTransition = item.latest_workflow_terminal_state_change;
  const cleanupTransition = item.latest_state_change;
  return (
    TERMINAL_WORKFLOW_STATUSES.has(item.status) ||
    (item.status === "destroying" &&
      terminalTransition?.event_type === "workspace.state_changed" &&
      cleanupTransition?.event_type === "workspace.state_changed" &&
      cleanupTransition.new_state === "destroying" &&
      cleanupTransition.old_state === terminalTransition.new_state &&
      terminalTransition.new_state !== "destroyed" &&
      TERMINAL_WORKFLOW_STATUSES.has(
        terminalTransition.new_state as WorkspaceOverview["status"],
      ))
  );
}

export type ResolvedWorkflowTiming = {
  finishedAt: string | null;
  durationSeconds: number | null;
};

type TimedLifecycleStage = {
  stage: WorkspaceOverview["lifecycle"][number];
  startedAt: string;
  startedMs: number;
  endedMs: number | null;
};

const LIFECYCLE_STAGE_STATUSES = new Set([
  "pending",
  "active",
  "completed",
  "terminal_skipped",
]);

const RESUMED_TERMINAL_SOURCE_STAGES = new Set([
  "running",
  "validating",
  "pushing",
  "monitoring_pr",
]);

function lifecycleStageOrder(stage: string): number {
  return lifecycleStages.findIndex((candidate) => candidate === stage);
}

const RFC3339_DATE_TIME =
  /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-](\d{2}):(\d{2}))$/;

function submillisecondFraction(value: string): string {
  const fractionalSeconds = RFC3339_DATE_TIME.exec(value)?.[7] ?? "";
  return fractionalSeconds.slice(4).replace(/0+$/, "");
}

function compareSubmillisecondFractions(left: string, right: string): number {
  const leftFraction = submillisecondFraction(left);
  const rightFraction = submillisecondFraction(right);
  const precision = Math.max(leftFraction.length, rightFraction.length);
  const normalizedLeft = leftFraction.padEnd(precision, "0");
  const normalizedRight = rightFraction.padEnd(precision, "0");
  return normalizedLeft === normalizedRight
    ? 0
    : normalizedLeft < normalizedRight
      ? -1
      : 1;
}

function compareRecordedInstants(left: string, right: string): number | null {
  const leftMs = recordedMilliseconds(left);
  const rightMs = recordedMilliseconds(right);
  if (leftMs == null || rightMs == null) {
    return null;
  }
  if (leftMs !== rightMs) {
    return leftMs < rightMs ? -1 : 1;
  }
  return compareSubmillisecondFractions(left, right);
}

function recordedMilliseconds(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }
  const match = RFC3339_DATE_TIME.exec(value);
  if (!match) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const dateTime = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  if (
    dateTime.getUTCFullYear() !== year ||
    dateTime.getUTCMonth() !== month - 1 ||
    dateTime.getUTCDate() !== day ||
    dateTime.getUTCHours() !== hour ||
    dateTime.getUTCMinutes() !== minute ||
    dateTime.getUTCSeconds() !== second
  ) {
    return null;
  }
  const timezone = match[8];
  if (
    timezone !== "Z" &&
    timezone !== "z" &&
    (Number(match[9]) > 23 || Number(match[10]) > 59)
  ) {
    return null;
  }
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) ? milliseconds : null;
}

function retainedTerminalEventTiming(item: WorkspaceOverview): {
  finishedAt: string;
  finishedMs: number;
} | null {
  const recoveryStartedAt = item.recovery?.started_at;
  const terminalEvent = item.latest_workflow_terminal_state_change;
  const isDirectPauseExit =
    recoveryStartedAt == null &&
    (terminalEvent?.old_state === "blocked" ||
      terminalEvent?.old_state === "recovering") &&
    (terminalEvent.new_state === "failed" ||
      terminalEvent.new_state === "cancelled");
  const retainedSourceStages = item.lifecycle.filter(
    (stage) => stage.stage === terminalEvent?.old_state,
  );
  const retainedSourceStage =
    retainedSourceStages.length === 1 ? retainedSourceStages[0] : null;
  const retainedSourceIntervalOrder =
    retainedSourceStage?.started_at != null && retainedSourceStage.ended_at != null
      ? compareRecordedInstants(
          retainedSourceStage.ended_at,
          retainedSourceStage.started_at,
        )
      : null;
  const latestStateChange = item.latest_state_change;
  const cleanupOrder =
    latestStateChange != null && terminalEvent != null
      ? compareRecordedInstants(
          latestStateChange.occurred_at,
          terminalEvent.occurred_at,
        )
      : null;
  const cleanupEventOrder = latestStateChange?.event_order;
  const terminalEventOrder = terminalEvent?.event_order;
  const hasCleanupEventOrderProof =
    typeof cleanupEventOrder === "number" &&
    Number.isSafeInteger(cleanupEventOrder) &&
    typeof terminalEventOrder === "number" &&
    Number.isSafeInteger(terminalEventOrder);
  const cleanupFollowsTerminalEvent =
    cleanupOrder != null &&
    cleanupOrder !== -1 &&
    (hasCleanupEventOrderProof
      ? cleanupEventOrder > terminalEventOrder
      : cleanupOrder === 1);
  const subsequentCleanupCorroboratesTerminal =
    terminalEvent != null &&
    latestStateChange?.event_type === "workspace.state_changed" &&
    latestStateChange.new_state === item.status &&
    ((latestStateChange.old_state === terminalEvent.new_state &&
      latestStateChange.new_state === "destroying") ||
      (latestStateChange.old_state === "destroying" &&
        latestStateChange.new_state === "destroyed")) &&
    cleanupFollowsTerminalEvent;
  const isRetainedResumeExit =
    recoveryStartedAt == null &&
    terminalEvent != null &&
    typeof terminalEvent.id === "string" &&
    terminalEvent.id.length > 0 &&
    (latestStateChange?.id === terminalEvent.id ||
      subsequentCleanupCorroboratesTerminal) &&
    terminalEvent.old_state != null &&
    RESUMED_TERMINAL_SOURCE_STAGES.has(terminalEvent.old_state) &&
    retainedSourceStage?.status === "completed" &&
    retainedSourceStage.ended_at != null &&
    retainedSourceIntervalOrder != null &&
    retainedSourceIntervalOrder !== -1 &&
    compareRecordedInstants(
      terminalEvent.occurred_at,
      retainedSourceStage.ended_at,
    ) === 1;
  // Recovery makes the collapsed lifecycle ambiguous, so its dedicated
  // terminal event must belong to the latest recovery. Blocked and recovering
  // are themselves omitted from that lifecycle and recovery summary; a direct
  // terminal exit from either pause is authoritative without that boundary.
  // After a pause resumes, the lifecycle retains the first interval for the
  // re-entered stage. The matching current state transition, or a subsequent
  // cleanup transition that follows it, can prove the later finish, but not a
  // duration that spans the omitted pause.
  if (
    (recoveryStartedAt == null &&
      !isDirectPauseExit &&
      !isRetainedResumeExit) ||
    terminalEvent?.event_type !== "workspace.state_changed" ||
    (terminalEvent.new_state !== "completed" &&
      terminalEvent.new_state !== "failed" &&
      terminalEvent.new_state !== "cancelled") ||
    !terminalEventMatchesWorkflowStatus(item, terminalEvent)
  ) {
    return null;
  }

  const finishedMs = recordedMilliseconds(terminalEvent.occurred_at);
  const recoveryComparison =
    recoveryStartedAt == null
      ? null
      : compareRecordedInstants(terminalEvent.occurred_at, recoveryStartedAt);
  const recoveryStartedEventOrder = item.recovery?.started_event_order;
  const explicitEventOrderProof =
    typeof terminalEventOrder === "number" &&
    Number.isSafeInteger(terminalEventOrder) &&
    typeof recoveryStartedEventOrder === "number" &&
    Number.isSafeInteger(recoveryStartedEventOrder) &&
    terminalEventOrder > recoveryStartedEventOrder;
  // Event order remains authoritative after cleanup replaces latest_state_change.
  // Retain the matching-ID proof for legacy payloads without order metadata.
  const orderedAfterSameInstantRecovery =
    recoveryComparison === 0 &&
    (explicitEventOrderProof ||
      (typeof terminalEvent.id === "string" &&
        terminalEvent.id.length > 0 &&
        item.latest_state_change?.id === terminalEvent.id));
  if (
    finishedMs == null ||
    (!isDirectPauseExit &&
      !isRetainedResumeExit &&
      recoveryComparison !== 1 &&
      !orderedAfterSameInstantRecovery)
  ) {
    return null;
  }
  return { finishedAt: terminalEvent.occurred_at, finishedMs };
}

function recordedDurationSeconds(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function recordedIntervalDurationSeconds(
  startedAt: string,
  startedMs: number,
  endedAt: string,
  endedMs: number,
): number {
  const elapsedMilliseconds = endedMs - startedMs;
  const elapsedWholeSeconds = Math.floor(elapsedMilliseconds / 1000);
  const elapsedMillisecondRemainder =
    elapsedMilliseconds - elapsedWholeSeconds * 1000;
  // Date.parse retains milliseconds. The remaining fractional digits can
  // lower the floored duration only at an exact millisecond-second boundary.
  return elapsedMillisecondRemainder === 0 &&
    compareSubmillisecondFractions(endedAt, startedAt) === -1
    ? elapsedWholeSeconds - 1
    : elapsedWholeSeconds;
}

function lifecycleWorkflowTiming(item: WorkspaceOverview): {
  finishedAt: string;
  finishedMs: number;
  durationSeconds: number | null;
} | null {
  // Lifecycle summaries collapse repeated visits to a stage. Once a retry or
  // reverse transition exists, they cannot identify the current workflow end.
  if (item.recovery != null) {
    return null;
  }
  const entered: TimedLifecycleStage[] = [];
  const seenStages = new Set<string>();
  for (const stage of item.lifecycle) {
    if (!LIFECYCLE_STAGE_STATUSES.has(stage.status)) {
      return null;
    }
    if (seenStages.has(stage.stage)) {
      return null;
    }
    seenStages.add(stage.stage);
    const startedMs = recordedMilliseconds(stage.started_at);
    const endedMs = recordedMilliseconds(stage.ended_at);
    if (
      (stage.started_at != null && startedMs == null) ||
      (stage.ended_at != null && endedMs == null) ||
      (stage.started_at != null &&
        stage.ended_at != null &&
        compareRecordedInstants(stage.ended_at, stage.started_at) === -1)
    ) {
      return null;
    }
    if (
      ((stage.status === "pending" || stage.status === "terminal_skipped") &&
        stage.started_at != null) ||
      stage.status === "active" ||
      (stage.started_at == null &&
        (stage.ended_at != null ||
          stage.duration_seconds != null ||
          (stage.status !== "pending" && stage.status !== "terminal_skipped")))
    ) {
      return null;
    }
    if (stage.started_at != null && startedMs != null) {
      entered.push({ stage, startedAt: stage.started_at, startedMs, endedMs });
    }
  }
  const chronologicallyEntered = [...entered].sort((left, right) => {
    const instantOrder = compareRecordedInstants(left.startedAt, right.startedAt);
    return instantOrder === 0
      ? lifecycleStageOrder(left.stage.stage) - lifecycleStageOrder(right.stage.stage)
      : (instantOrder ?? 0);
  });
  let previousStageOrder = -1;
  for (const entry of chronologicallyEntered) {
    const stageOrder = lifecycleStageOrder(entry.stage.stage);
    if (stageOrder < 0 || stageOrder <= previousStageOrder) {
      return null;
    }
    previousStageOrder = stageOrder;
  }
  let latestEntered = entered.reduce<TimedLifecycleStage | null>(
    (latest, entry) =>
      latest == null || compareRecordedInstants(entry.startedAt, latest.startedAt) === 1
        ? entry
        : latest,
    null,
  );
  if (latestEntered == null) {
    return null;
  }
  const latestStartedAt = latestEntered.startedAt;
  const latestCandidates = entered.filter((entry) =>
    sameRecordedInstant(entry.startedAt, latestStartedAt),
  );
  if (latestCandidates.length > 1) {
    const stageOrders = latestCandidates.map((entry) =>
      lifecycleStageOrder(entry.stage.stage),
    );
    if (
      stageOrders.some((stageOrder) => stageOrder < 0) ||
      new Set(stageOrders).size !== stageOrders.length
    ) {
      return null;
    }
    latestEntered = latestCandidates.reduce((latest, entry) =>
      lifecycleStageOrder(entry.stage.stage) > lifecycleStageOrder(latest.stage.stage)
        ? entry
        : latest,
    );
  }

  const terminalEvent =
    item.latest_workflow_terminal_state_change ??
    item.latest_state_change ??
    item.last_event;
  let finishedAt = latestEntered.stage.ended_at;
  let finishedMs = latestEntered.endedMs;
  if (latestEntered.stage.stage === "completed") {
    const previousEntered = chronologicallyEntered[chronologicallyEntered.length - 2];
    // The completed stage starts at workflow end but may itself end much later
    // during cleanup. Require the immediately preceding entered stage to
    // corroborate that start so an overlapping or collapsed retry history
    // cannot invent a fresh finish.
    if (
      previousEntered?.stage.ended_at == null ||
      compareRecordedInstants(
        previousEntered.stage.ended_at,
        latestEntered.startedAt,
      ) !== 0
    ) {
      return null;
    }
    finishedAt = latestEntered.startedAt;
    finishedMs = latestEntered.startedMs;
    // A completed or destroyed status can corroborate legacy payloads that
    // predate retained state-change fields. Other current statuses must retain
    // the transition into completed, including the supported completed ->
    // cleanup failure path. Any retained state transition must still agree.
    const requiresCompletedTransition =
      (item.status !== "completed" && item.status !== "destroyed") ||
      terminalEvent?.event_type === "workspace.state_changed";
    if (
      requiresCompletedTransition &&
      (terminalEvent?.event_type !== "workspace.state_changed" ||
        terminalEvent.old_state !== previousEntered.stage.stage ||
        terminalEvent.new_state !== "completed" ||
        !terminalEventMatchesWorkflowStatus(item, terminalEvent) ||
        compareRecordedInstants(terminalEvent.occurred_at, finishedAt) !== 0)
    ) {
      return null;
    }
  }
  if (finishedAt == null || finishedMs == null) {
    return null;
  }
  if (latestEntered.stage.stage !== "completed") {
    // Pauses such as blocked/recovering are absent from lifecycle summaries.
    // For terminal paths without a completed stage, only trust that boundary
    // when a retained workflow state-change corroborates the actual terminal
    // transition. Cleanup may retain an earlier failed/cancelled boundary
    // after successful destruction, or after cancellation cleanup itself
    // fails. `last_event` remains a compatibility fallback for older overview
    // payloads.
    if (
      terminalEvent?.event_type !== "workspace.state_changed" ||
      terminalEvent.old_state !== latestEntered.stage.stage ||
      !terminalEventMatchesWorkflowStatus(item, terminalEvent) ||
      compareRecordedInstants(terminalEvent.occurred_at, finishedAt) !== 0
    ) {
      return null;
    }
  }

  const durationStages = entered
    .filter(
      (entry) =>
        entry.stage.stage !== "completed" &&
        entry.startedMs <= finishedMs,
    )
    .sort((left, right) => {
      const instantOrder = compareRecordedInstants(left.startedAt, right.startedAt);
      return instantOrder === 0
        ? lifecycleStageOrder(left.stage.stage) - lifecycleStageOrder(right.stage.stage)
        : (instantOrder ?? 0);
    });
  // A contiguous tail can corroborate the finish without representing the
  // whole workflow. Only `requested` supplies the authoritative start needed
  // to calculate workflow duration.
  const [workflowStart] = durationStages;
  if (workflowStart?.stage.stage !== "requested") {
    return { finishedAt, finishedMs, durationSeconds: null };
  }
  let previousEndAt: string | null = null;
  for (const entry of durationStages) {
    const stageDuration = recordedDurationSeconds(entry.stage.duration_seconds);
    const intervalDurationSeconds =
      entry.stage.ended_at == null || entry.endedMs == null
        ? null
        : recordedIntervalDurationSeconds(
            entry.startedAt,
            entry.startedMs,
            entry.stage.ended_at,
            entry.endedMs,
          );
    if (
      entry.endedMs == null ||
      entry.endedMs > finishedMs ||
      stageDuration == null ||
      stageDuration !== intervalDurationSeconds ||
      (previousEndAt != null &&
        compareRecordedInstants(previousEndAt, entry.startedAt) !== 0)
    ) {
      return { finishedAt, finishedMs, durationSeconds: null };
    }
    previousEndAt = entry.stage.ended_at;
  }
  if (
    previousEndAt == null ||
    compareRecordedInstants(previousEndAt, finishedAt) !== 0
  ) {
    return { finishedAt, finishedMs, durationSeconds: null };
  }
  const durationSeconds = recordedIntervalDurationSeconds(
    workflowStart.startedAt,
    workflowStart.startedMs,
    finishedAt,
    finishedMs,
  );
  return { finishedAt, finishedMs, durationSeconds };
}

/**
 * Resolve the workflow timing displayed by console surfaces. Terminal local
 * overviews and evidenced post-terminal cleanup may derive timing from one
 * complete lifecycle interval. Recovered terminal overviews may retain only
 * their terminal event, which supplies a finish but not a duration. Active
 * workspaces retain their explicit timing fields and never infer a finish.
 */
export function resolveWorkflowTiming(item: WorkspaceOverview): ResolvedWorkflowTiming {
  const resolvedFinishedAt = resolveWorkflowFinishedAt(item);
  if (!hasTerminalWorkflowTiming(item)) {
    return {
      finishedAt: resolvedFinishedAt,
      durationSeconds: item.duration_seconds ?? null,
    };
  }

  const explicitFinishedMs = recordedMilliseconds(resolvedFinishedAt);

  const lifecycleTiming = lifecycleWorkflowTiming(item);
  const retainedTerminalTiming = retainedTerminalEventTiming(item);
  const finishedAt =
    resolvedFinishedAt ??
    lifecycleTiming?.finishedAt ??
    retainedTerminalTiming?.finishedAt ??
    null;
  const finishedMs =
    explicitFinishedMs ??
    lifecycleTiming?.finishedMs ??
    retainedTerminalTiming?.finishedMs ??
    null;
  if (finishedAt == null || finishedMs == null) {
    return {
      finishedAt: null,
      durationSeconds: recordedDurationSeconds(item.duration_seconds),
    };
  }

  const explicitDurationPresent = item.duration_seconds != null;
  const explicitDuration = recordedDurationSeconds(item.duration_seconds);
  let durationSeconds: number | null = null;
  if (explicitDurationPresent) {
    durationSeconds = explicitDuration;
  } else if (
    lifecycleTiming != null &&
    sameRecordedInstant(lifecycleTiming.finishedAt, finishedAt)
  ) {
    durationSeconds = lifecycleTiming.durationSeconds;
  }

  return { finishedAt, durationSeconds };
}

type PresentationModelFields = RequestedModelWorkspace & ConfirmedModelWorkspace;

/**
 * Prefer detail fields when present; fall back to overview for optional
 * requested/confirmed metadata so a sparse detail payload cannot blank
 * authoritative overview values already on screen.
 * Requested model/effort and their sources are selected as pairs: when detail
 * supplies a value, its matching source comes from the same payload (even if
 * omitted); otherwise the complete overview pair is kept. Confirmed execution
 * model and source likewise stay atomic: detail wins only when both are
 * present.
 */
export function mergeWorkspacePresentationFields(
  overview: PresentationModelFields,
  workspace: PresentationModelFields | null | undefined,
): PresentationModelFields {
  if (!workspace) {
    return overview;
  }
  // Requested value + provenance must stay a single pair per field: a sparse
  // detail that supplies only one half must not cross-wire with overview.
  const detailHasRequestedModel = workspace.requested_model != null;
  const detailHasRequestedEffort = workspace.requested_effort != null;
  // Confirmed model + provenance must stay a single pair: a sparse detail that
  // supplies only one field must not cross-wire with overview's other half.
  const detailConfirmedModel = workspace.confirmed_execution_model;
  const detailConfirmedSource = workspace.confirmed_execution_model_source;
  const detailHasConfirmedPair =
    detailConfirmedModel != null && detailConfirmedSource != null;

  return {
    requested_model: detailHasRequestedModel
      ? workspace.requested_model
      : overview.requested_model,
    requested_model_source: detailHasRequestedModel
      ? workspace.requested_model_source
      : overview.requested_model_source,
    requested_effort: detailHasRequestedEffort
      ? workspace.requested_effort
      : overview.requested_effort,
    requested_effort_source: detailHasRequestedEffort
      ? workspace.requested_effort_source
      : overview.requested_effort_source,
    agent_model: workspace.agent_model ?? overview.agent_model,
    agent_effort: workspace.agent_effort ?? overview.agent_effort,
    agent_model_source: workspace.agent_model_source ?? overview.agent_model_source,
    agent_effort_source: workspace.agent_effort_source ?? overview.agent_effort_source,
    confirmed_execution_model: detailHasConfirmedPair
      ? detailConfirmedModel
      : overview.confirmed_execution_model,
    confirmed_execution_model_source: detailHasConfirmedPair
      ? detailConfirmedSource
      : overview.confirmed_execution_model_source,
  };
}

function displayAgentModel(workspace: AgentLabelWorkspace): string | null {
  if (workspace.agent === "cursor" && workspace.cursor_auto_mode) {
    const mode = workspace.cursor_auto_mode;
    return `Auto ${mode.charAt(0).toUpperCase()}${mode.slice(1)}`;
  }
  return compactAgentModel(workspace.agent_model);
}
