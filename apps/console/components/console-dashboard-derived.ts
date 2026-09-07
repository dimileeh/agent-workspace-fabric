/**
 * Pure helpers extracted from console-dashboard.tsx to stay under the first-party
 * 1500-line maintainability guard without changing feed/layout behavior.
 */
import {
  isDiagnosticAvailable,
  isWidgetAvailable,
  resolveWorkspaceLogStreamAccess,
} from "../lib/console-capabilities.ts";
import type { ConsoleCapabilities, WorkspaceOverview } from "../lib/types.ts";

export type WorkspaceSortKey = "created_at" | "updated_at";
export type SortDirection = "asc" | "desc";

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
    filtered = filtered.filter((item) =>
      [
        item.workspace_id,
        item.task_id,
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
        .toLowerCase()
        .includes(needle),
    );
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
