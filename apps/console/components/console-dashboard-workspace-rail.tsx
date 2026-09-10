"use client";

import type { WorkspaceOverview } from "@/lib/types";
import type { SortDirection, WorkspaceSortKey } from "./console-dashboard-shared";
import {
  WorkspaceFilters,
  WorkspaceList,
  WorkspaceSelectionToolbar,
} from "./console-dashboard-overview";

type ConsoleDashboardWorkspaceRailProps = {
  statusFilters: string[];
  agentFilters: string[];
  modelFilters: string[];
  availableModels: string[];
  availableAgents: string[];
  repoFilter: string;
  searchText: string;
  sortKey: WorkspaceSortKey;
  sortDirection: SortDirection;
  filtersExpanded: boolean;
  onStatusFilters: (value: string[]) => void;
  onAgentFilters: (value: string[]) => void;
  onModelFilters: (value: string[]) => void;
  onRepoFilter: (value: string) => void;
  onSearchText: (value: string) => void;
  onSortKey: (value: WorkspaceSortKey) => void;
  onSortDirection: (value: SortDirection) => void;
  onToggleExpanded: () => void;
  overviewHasMore: boolean;
  overviewHistoryLoading: boolean;
  overviewHistoryComplete: boolean;
  overviewHistoryError: boolean;
  overviewLoadedCount: number;
  onLoadOverviewHistory: () => void;
  /** When false, omit log-selection toolbar, checkboxes, and Logs buttons. */
  showWorkspaceLogs: boolean;
  workspaceLogSelection: string[];
  onOpenSelectedLogs: () => void;
  onClearLogSelection: () => void;
  filteredOverview: WorkspaceOverview[];
  selectedId: string | null;
  onSelect: (workspaceId: string | null) => void;
  onToggleWorkspaceSelection: (workspaceId: string, checked: boolean) => void;
  onOpenDetails: (workspaceId: string) => void;
  onOpenLogs: (workspaceId: string) => void;
};

/** Left-rail filters + multi-select toolbar + workspace list. */
export function ConsoleDashboardWorkspaceRail(props: ConsoleDashboardWorkspaceRailProps) {
  return (
    <aside
      id="awf-workspaces"
      className="min-w-0 scroll-mt-14 border-b border-[var(--border)] bg-surface xl:border-r xl:border-b-0"
    >
      <WorkspaceFilters
        statusFilters={props.statusFilters}
        agentFilters={props.agentFilters}
        modelFilters={props.modelFilters}
        availableModels={props.availableModels}
        availableAgents={props.availableAgents}
        repoFilter={props.repoFilter}
        searchText={props.searchText}
        sortKey={props.sortKey}
        sortDirection={props.sortDirection}
        onStatusFilters={props.onStatusFilters}
        onAgentFilters={props.onAgentFilters}
        onModelFilters={props.onModelFilters}
        onRepoFilter={props.onRepoFilter}
        onSearchText={props.onSearchText}
        onSortKey={props.onSortKey}
        onSortDirection={props.onSortDirection}
        expanded={props.filtersExpanded}
        onToggleExpanded={props.onToggleExpanded}
      />
      {props.showWorkspaceLogs ? (
        <WorkspaceSelectionToolbar
          selectedCount={props.workspaceLogSelection.length}
          onOpen={props.onOpenSelectedLogs}
          onClear={props.onClearLogSelection}
        />
      ) : null}
      <WorkspaceList
        items={props.filteredOverview}
        selectedId={props.selectedId}
        showWorkspaceLogs={props.showWorkspaceLogs}
        selectedWorkspaceIds={props.workspaceLogSelection}
        onSelect={props.onSelect}
        onToggleWorkspaceSelection={props.onToggleWorkspaceSelection}
        onOpenDetails={props.onOpenDetails}
        onOpenLogs={props.onOpenLogs}
        hasMore={props.overviewHasMore}
        loadingMore={props.overviewHistoryLoading}
        historyComplete={props.overviewHistoryComplete}
        historyError={props.overviewHistoryError}
        loadedCount={props.overviewLoadedCount}
        onLoadMore={props.onLoadOverviewHistory}
      />
    </aside>
  );
}
