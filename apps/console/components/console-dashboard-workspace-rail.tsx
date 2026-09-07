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
      <WorkspaceSelectionToolbar
        selectedCount={props.workspaceLogSelection.length}
        onOpen={props.onOpenSelectedLogs}
        onClear={props.onClearLogSelection}
      />
      <WorkspaceList
        items={props.filteredOverview}
        selectedId={props.selectedId}
        selectedWorkspaceIds={props.workspaceLogSelection}
        onSelect={props.onSelect}
        onToggleWorkspaceSelection={props.onToggleWorkspaceSelection}
        onOpenDetails={props.onOpenDetails}
        onOpenLogs={props.onOpenLogs}
      />
    </aside>
  );
}
