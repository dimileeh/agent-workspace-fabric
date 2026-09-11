import type { WorkspaceOverview } from "@/lib/types";

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
 * Workflow completion timestamp: prefer explicit workflow_finished_at, then the
 * documented workflow timing field finished_at. Native runtime finish stays
 * separate via native_runtime_finished_at.
 */
export function resolveWorkflowFinishedAt(
  workspace: WorkflowTimingFields,
): string | null {
  return workspace.workflow_finished_at ?? workspace.finished_at ?? null;
}

/**
 * True when two recorded timestamps are the same instant, including equivalent
 * ISO forms (`Z` vs `.000Z`) that would render as one clock time under both labels.
 */
function sameRecordedInstant(left: string, right: string): boolean {
  if (left === right) {
    return true;
  }
  const leftMs = Date.parse(left);
  const rightMs = Date.parse(right);
  return Number.isFinite(leftMs) && leftMs === rightMs;
}

/**
 * Separate "Finished" timestamp. `finished_at` is already the documented
 * fallback for Workflow finished, so omit it when that fact would repeat the
 * same instant (cloud rows that only send `finished_at`, or equivalent ISO forms).
 */
export function distinctFinishedAt(workspace: WorkflowTimingFields): string | null {
  const finishedAt = workspace.finished_at;
  if (!finishedAt) {
    return null;
  }
  const workflowFinishedAt = resolveWorkflowFinishedAt(workspace);
  if (workflowFinishedAt && sameRecordedInstant(finishedAt, workflowFinishedAt)) {
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

function recordedMilliseconds(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) ? milliseconds : null;
}

function recordedDurationSeconds(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
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
  for (const stage of item.lifecycle) {
    const startedMs = recordedMilliseconds(stage.started_at);
    const endedMs = recordedMilliseconds(stage.ended_at);
    if (
      (stage.started_at != null && startedMs == null) ||
      (stage.ended_at != null && endedMs == null)
    ) {
      return null;
    }
    if (
      stage.started_at == null &&
      (stage.ended_at != null ||
        stage.duration_seconds != null ||
        (stage.status !== "pending" && stage.status !== "terminal_skipped"))
    ) {
      return null;
    }
    if (stage.started_at != null && startedMs != null) {
      entered.push({ stage, startedAt: stage.started_at, startedMs, endedMs });
    }
  }
  const latestStartedMs = Math.max(...entered.map((entry) => entry.startedMs));
  const latestCandidates = entered.filter(
    (entry) => entry.startedMs === latestStartedMs,
  );
  if (latestCandidates.length !== 1) {
    return null;
  }
  const [latestEntered] = latestCandidates;

  let finishedAt = latestEntered.stage.ended_at;
  let finishedMs = latestEntered.endedMs;
  if (latestEntered.stage.stage === "completed") {
    const previousEndMs = entered.reduce<number | null>(
      (latest, entry) =>
        entry.stage.stage !== "completed" &&
        entry.endedMs != null &&
        (latest == null || entry.endedMs > latest)
          ? entry.endedMs
          : latest,
      null,
    );
    // The completed stage starts at workflow end but may itself end much later
    // during cleanup. Require the preceding lifecycle boundary to corroborate
    // that start so a collapsed retry history cannot invent a fresh finish.
    if (previousEndMs !== latestEntered.startedMs) {
      return null;
    }
    finishedAt = latestEntered.startedAt;
    finishedMs = latestEntered.startedMs;
  }
  if (finishedAt == null || finishedMs == null) {
    return null;
  }
  if (latestEntered.stage.stage !== "completed") {
    const terminalEvent = item.last_event;
    const terminalEventMs = recordedMilliseconds(terminalEvent?.occurred_at);
    // Pauses such as blocked/recovering are absent from lifecycle summaries.
    // For terminal paths without a completed stage, only trust that boundary
    // when the latest state-change corroborates the actual terminal transition.
    if (
      terminalEvent?.event_type !== "workspace.state_changed" ||
      terminalEvent.old_state !== latestEntered.stage.stage ||
      terminalEvent.new_state !== item.status ||
      terminalEventMs !== finishedMs
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
    .sort((left, right) => left.startedMs - right.startedMs);
  let durationSeconds = 0;
  let previousEndMs: number | null = null;
  for (const entry of durationStages) {
    const stageDuration = recordedDurationSeconds(entry.stage.duration_seconds);
    if (
      entry.endedMs == null ||
      entry.endedMs > finishedMs ||
      stageDuration == null ||
      (previousEndMs != null && previousEndMs !== entry.startedMs)
    ) {
      return { finishedAt, finishedMs, durationSeconds: null };
    }
    durationSeconds += stageDuration;
    previousEndMs = entry.endedMs;
  }
  if (previousEndMs !== finishedMs) {
    return { finishedAt, finishedMs, durationSeconds: null };
  }
  return { finishedAt, finishedMs, durationSeconds };
}

/**
 * Resolve the workflow timing displayed by console surfaces. Terminal local
 * overviews may derive timing from one complete lifecycle interval; active
 * workspaces retain their explicit timing fields and never infer a finish.
 */
export function resolveWorkflowTiming(item: WorkspaceOverview): ResolvedWorkflowTiming {
  const resolvedFinishedAt = resolveWorkflowFinishedAt(item);
  if (!TERMINAL_WORKFLOW_STATUSES.has(item.status)) {
    return {
      finishedAt: resolvedFinishedAt,
      durationSeconds: item.duration_seconds ?? null,
    };
  }

  const explicitFinishedMs = recordedMilliseconds(resolvedFinishedAt);
  if (resolvedFinishedAt != null && explicitFinishedMs == null) {
    return {
      finishedAt: null,
      durationSeconds: recordedDurationSeconds(item.duration_seconds),
    };
  }

  const lifecycleTiming = lifecycleWorkflowTiming(item);
  const finishedAt = resolvedFinishedAt ?? lifecycleTiming?.finishedAt ?? null;
  const finishedMs = explicitFinishedMs ?? lifecycleTiming?.finishedMs ?? null;
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
  } else if (lifecycleTiming?.finishedMs === finishedMs) {
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
