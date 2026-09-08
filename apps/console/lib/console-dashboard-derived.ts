/**
 * Pure helpers extracted from console-dashboard.tsx to stay under the first-party
 * 1500-line maintainability guard without changing feed/layout behavior.
 */
import {
  isDiagnosticAvailable,
  isWidgetAvailable,
  resolveWorkspaceLogStreamAccess,
} from "./console-capabilities.ts";
import type { ConsoleCapabilities, WorkspaceOverview } from "./types.ts";

export type WorkspaceSortKey = "created_at" | "updated_at";
export type SortDirection = "asc" | "desc";

type DisplayedTaskKeySource = {
  task_key?: string | null;
  task_tag?: string | null;
};

/** Key shown on cards and details. Prefer ``task_key``, else Core ``task_tag``. */
export function displayedTaskKey(item: DisplayedTaskKeySource | null | undefined): string | null {
  const key = item?.task_key || item?.task_tag;
  return key ? key : null;
}

/** Fields the overview card shows or operators already search, including task_key. */
export function overviewSearchText(item: WorkspaceOverview): string {
  return [
    item.workspace_id,
    item.task_id,
    displayedTaskKey(item) ?? "",
    item.title,
    item.repo_url,
    item.base_branch,
    item.agent,
    item.agent_model ?? "",
    item.agent_effort ?? "",
    item.status,
    item.recovery?.reason_code ?? "",
    item.recovery?.recovery_mode ?? "",
  ]
    .join(" ")
    .toLowerCase();
}

function compareWorkspaceDates(
  left: WorkspaceOverview,
  right: WorkspaceOverview,
  sortKey: WorkspaceSortKey,
  direction: SortDirection,
): number {
  const leftTime = Date.parse(left[sortKey]);
  const rightTime = Date.parse(right[sortKey]);
  const safeLeft = Number.isNaN(leftTime) ? 0 : leftTime;
  const safeRight = Number.isNaN(rightTime) ? 0 : rightTime;
  const delta = safeLeft - safeRight;
  if (delta === 0) {
    return left.workspace_id.localeCompare(right.workspace_id);
  }
  return direction === "asc" ? delta : -delta;
}

export type CapabilityFeedWithdrawal = {
  clearDashboardSummary: boolean;
  clearResourceCapacity: boolean;
  clearCloudRuntime: boolean;
  clearReliability: boolean;
  clearMergeQueue: boolean;
  clearFailures: boolean;
  clearRuntime: boolean;
  clearEvents: boolean;
  clearOperations: boolean;
  clearLogs: boolean;
};

export function planCapabilityFeedWithdrawal(
  previous: ConsoleCapabilities,
  next: ConsoleCapabilities,
): CapabilityFeedWithdrawal {
  return {
    clearDashboardSummary:
      isWidgetAvailable(previous, "fleet_summary") && !isWidgetAvailable(next, "fleet_summary"),
    clearResourceCapacity:
      isWidgetAvailable(previous, "resource_capacity") &&
      !isWidgetAvailable(next, "resource_capacity"),
    clearCloudRuntime:
      isWidgetAvailable(previous, "cloud_runtime") && !isWidgetAvailable(next, "cloud_runtime"),
    clearReliability:
      isDiagnosticAvailable(previous, "reliability") &&
      !isDiagnosticAvailable(next, "reliability"),
    clearMergeQueue:
      isDiagnosticAvailable(previous, "merge_queue") &&
      !isDiagnosticAvailable(next, "merge_queue"),
    clearFailures:
      isDiagnosticAvailable(previous, "failures") && !isDiagnosticAvailable(next, "failures"),
    clearRuntime:
      isDiagnosticAvailable(previous, "workspace_runtime") &&
      !isDiagnosticAvailable(next, "workspace_runtime"),
    clearEvents:
      isDiagnosticAvailable(previous, "workspace_events") &&
      !isDiagnosticAvailable(next, "workspace_events"),
    clearOperations:
      isDiagnosticAvailable(previous, "workspace_operations") &&
      !isDiagnosticAvailable(next, "workspace_operations"),
    clearLogs:
      isDiagnosticAvailable(previous, "workspace_logs") &&
      !isDiagnosticAvailable(next, "workspace_logs"),
  };
}

export function capabilityFeedWithdrawalCleared(plan: CapabilityFeedWithdrawal): boolean {
  return (
    plan.clearDashboardSummary ||
    plan.clearResourceCapacity ||
    plan.clearCloudRuntime ||
    plan.clearReliability ||
    plan.clearMergeQueue ||
    plan.clearFailures ||
    plan.clearRuntime ||
    plan.clearEvents ||
    plan.clearOperations ||
    plan.clearLogs
  );
}

/** Optional inspector diagnostics invalidated by a gated-detail generation bump. */
export type GatedDetailDroppedFeeds = {
  runtime: boolean;
  events: boolean;
  operations: boolean;
  logs: boolean;
};

/**
 * Capabilities 404, auth revocation, and listing/tail denial drop every optional
 * inspector feed. The basic workspace GET still applies; optional envelopes must
 * not restore withdrawn data or supply the detail error.
 */
export const DROP_ALL_GATED_DETAIL_FEEDS: GatedDetailDroppedFeeds = {
  runtime: true,
  events: true,
  operations: true,
  logs: true,
};

/** Same-identity withdrawal of an inspector diagnostic, not an unrelated fleet feed. */
export function inspectorDetailFeedWithdrawn(plan: CapabilityFeedWithdrawal): boolean {
  return plan.clearRuntime || plan.clearEvents || plan.clearOperations || plan.clearLogs;
}

export function gatedDetailDropFromWithdrawal(
  plan: CapabilityFeedWithdrawal,
): GatedDetailDroppedFeeds {
  return {
    runtime: plan.clearRuntime,
    events: plan.clearEvents,
    operations: plan.clearOperations,
    logs: plan.clearLogs,
  };
}

export function allGatedDetailFeedsDropped(dropped: GatedDetailDroppedFeeds): boolean {
  return dropped.runtime && dropped.events && dropped.operations && dropped.logs;
}

const KEEP_ALL_GATED_DETAIL_FEEDS: GatedDetailDroppedFeeds = {
  runtime: false,
  events: false,
  operations: false,
  logs: false,
};

/** One gated-detail generation bump and the feeds it invalidated. */
export type GatedDetailDropStamp = {
  generation: number;
  dropped: GatedDetailDroppedFeeds;
};

const MAX_GATED_DETAIL_DROP_STAMPS = 32;

function unionGatedDetailDroppedFeeds(
  left: GatedDetailDroppedFeeds,
  right: GatedDetailDroppedFeeds,
): GatedDetailDroppedFeeds {
  return {
    runtime: left.runtime || right.runtime,
    events: left.events || right.events,
    operations: left.operations || right.operations,
    logs: left.logs || right.logs,
  };
}

function recordGatedDetailDrop(
  stamps: readonly GatedDetailDropStamp[],
  generation: number,
  dropped: GatedDetailDroppedFeeds,
): GatedDetailDropStamp[] {
  const next = [...stamps, { generation, dropped }];
  return next.length > MAX_GATED_DETAIL_DROP_STAMPS
    ? next.slice(next.length - MAX_GATED_DETAIL_DROP_STAMPS)
    : next;
}

/**
 * Append this bump's drop and advance the gated-detail generation together.
 * Replacing the latest mask loses earlier withdrawals that an in-flight
 * loadWorkspace still has to honor.
 */
export function noteGatedDetailDrop(
  stampsRef: { current: GatedDetailDropStamp[] },
  generationRef: { current: number },
  dropped: GatedDetailDroppedFeeds,
): void {
  const generation = generationRef.current + 1;
  stampsRef.current = recordGatedDetailDrop(stampsRef.current, generation, dropped);
  generationRef.current = generation;
}

/**
 * Union every drop recorded after `capturedGeneration`. A truncated log that
 * no longer contains the first bump after capture fails closed to DROP_ALL so
 * a missing earlier withdrawal cannot be written back.
 */
export function gatedDetailDropsSince(
  stamps: readonly GatedDetailDropStamp[],
  capturedGeneration: number,
): GatedDetailDroppedFeeds {
  const relevant = stamps.filter((stamp) => stamp.generation > capturedGeneration);
  const earliest = relevant[0]?.generation;
  if (earliest === undefined || earliest > capturedGeneration + 1) {
    return DROP_ALL_GATED_DETAIL_FEEDS;
  }
  return relevant.reduce(
    (acc, stamp) => unionGatedDetailDroppedFeeds(acc, stamp.dropped),
    KEEP_ALL_GATED_DETAIL_FEEDS,
  );
}

export function filterAndSortOverview(
  overview: WorkspaceOverview[],
  options: {
    searchText: string;
    statusFilters: string[];
    agentFilters: string[];
    modelFilters: string[];
    sortKey: WorkspaceSortKey;
    sortDirection: SortDirection;
  },
): WorkspaceOverview[] {
  const needle = options.searchText.trim().toLowerCase();
  let filtered = overview;
  if (options.statusFilters.length > 0) {
    filtered = filtered.filter((item) => options.statusFilters.includes(item.status));
  }
  if (options.agentFilters.length > 0) {
    filtered = filtered.filter((item) => options.agentFilters.includes(item.agent));
  }
  if (options.modelFilters.length > 0) {
    filtered = filtered.filter(
      (item) => item.agent_model !== null && options.modelFilters.includes(item.agent_model),
    );
  }
  if (needle) {
    filtered = filtered.filter((item) => overviewSearchText(item).includes(needle));
  }
  return [...filtered].sort((left, right) =>
    compareWorkspaceDates(left, right, options.sortKey, options.sortDirection),
  );
}

export type DashboardPanelVisibility = {
  showResourceCapacity: boolean;
  showCloudRuntime: boolean;
  showReliability: boolean;
  showMergeQueue: boolean;
  showFailures: boolean;
  showCapacitySection: boolean;
  showWorkspaceRuntime: boolean;
  showWorkspaceEvents: boolean;
  showWorkspaceOperations: boolean;
  showWorkspaceLogs: boolean;
  allowFullscreenLogs: boolean;
  allowFullscreenStreamLogs: boolean;
  fleetSummaryAvailable: boolean;
};

export function resolveDashboardPanelVisibility(
  capabilities: ConsoleCapabilities | null | undefined,
): DashboardPanelVisibility {
  const { allowLogs, allowStreamLogs } = resolveWorkspaceLogStreamAccess(capabilities);
  const showResourceCapacity = isWidgetAvailable(capabilities, "resource_capacity");
  const showCloudRuntime = isWidgetAvailable(capabilities, "cloud_runtime");
  const showReliability = isDiagnosticAvailable(capabilities, "reliability");
  return {
    showResourceCapacity,
    showCloudRuntime,
    showReliability,
    showMergeQueue: isDiagnosticAvailable(capabilities, "merge_queue"),
    showFailures: isDiagnosticAvailable(capabilities, "failures"),
    // Single source for #awf-capacity mount + SectionNav Capacity link so they cannot drift.
    showCapacitySection: showResourceCapacity || showCloudRuntime || showReliability,
    showWorkspaceRuntime: isDiagnosticAvailable(capabilities, "workspace_runtime"),
    showWorkspaceEvents: isDiagnosticAvailable(capabilities, "workspace_events"),
    showWorkspaceOperations: isDiagnosticAvailable(capabilities, "workspace_operations"),
    showWorkspaceLogs: allowLogs,
    allowFullscreenLogs: allowLogs,
    // Fullscreen columns only surface selectable log streams — gate live frames on
    // listing + stream together (allowStreamLogs), not bare workspace_stream.
    allowFullscreenStreamLogs: allowStreamLogs,
    fleetSummaryAvailable: isWidgetAvailable(capabilities, "fleet_summary"),
  };
}

export function orderFullscreenWorkspaceIds(
  selection: string[],
  filteredOverview: WorkspaceOverview[],
): string[] {
  const selected = new Set(selection);
  const orderedVisible = filteredOverview
    .filter((workspace) => selected.has(workspace.workspace_id))
    .map((workspace) => workspace.workspace_id);
  const remaining = selection.filter((workspaceId) => !orderedVisible.includes(workspaceId));
  return [...orderedVisible, ...remaining];
}

export type AutomaticLogTailRefreshCandidate = {
  streamId: string;
  part: string;
};

export type AutomaticLogTailRefreshPlan = {
  start: AutomaticLogTailRefreshCandidate[];
  pending: AutomaticLogTailRefreshCandidate[];
  nextParts: Map<string, string>;
};

function automaticLogTailGenerationKey(workspaceId: string, streamId: string): string {
  return `${workspaceId}:${streamId}`;
}

/**
 * Decide which selected tails a listing refresh should read.
 *
 * Unchanged byte/line/open/close metadata skips a new read so array identity
 * cannot supersede an in-flight success. A stream still latched for 401/403
 * is the exception: each authorized listing refresh starts one retry even
 * when metadata is static. An in-flight read is queued, never restarted.
 */
export function planAutomaticLogTailRefresh(input: {
  workspaceId: string;
  selectedStreamIds: readonly string[];
  streams: readonly AutomaticLogTailRefreshCandidate[];
  previousParts: ReadonlyMap<string, string>;
  inFlightStreamKeys: ReadonlySet<string>;
  deniedStreamKeys: ReadonlySet<string>;
}): AutomaticLogTailRefreshPlan {
  const selected = new Set(input.selectedStreamIds);
  const nextParts = new Map<string, string>();
  const start: AutomaticLogTailRefreshCandidate[] = [];
  const pending: AutomaticLogTailRefreshCandidate[] = [];
  for (const stream of input.streams) {
    if (!selected.has(stream.streamId)) {
      continue;
    }
    nextParts.set(stream.streamId, stream.part);
    const unchanged = input.previousParts.get(stream.streamId) === stream.part;
    const generationKey = automaticLogTailGenerationKey(input.workspaceId, stream.streamId);
    const denied = input.deniedStreamKeys.has(generationKey);
    if (unchanged && !denied) {
      continue;
    }
    if (input.inFlightStreamKeys.has(generationKey)) {
      pending.push(stream);
      continue;
    }
    start.push(stream);
  }
  return { start, pending, nextParts };
}

/**
 * A queued automatic tail may start only after its own read settles, and
 * never while that stream is still authorization-denied. Drain is not a
 * listing refresh: starting here would retry a 401/403 in the same turn.
 */
export function shouldStartPendingAutomaticLogTail(input: {
  workspaceId: string;
  streamId: string;
  part: string;
  selectedWorkspaceId: string | null;
  listingAuthDenied: boolean;
  deniedStreamKeys: ReadonlySet<string>;
  selectedStreamIds: readonly string[];
  recordedParts: ReadonlyMap<string, string>;
}): boolean {
  const generationKey = automaticLogTailGenerationKey(input.workspaceId, input.streamId);
  return (
    input.selectedWorkspaceId === input.workspaceId &&
    !input.listingAuthDenied &&
    !input.deniedStreamKeys.has(generationKey) &&
    input.selectedStreamIds.includes(input.streamId) &&
    input.recordedParts.get(input.streamId) === input.part
  );
}
