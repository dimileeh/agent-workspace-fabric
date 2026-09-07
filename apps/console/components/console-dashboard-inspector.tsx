"use client";

import type {
  ConsoleCapabilities,
  MergeQueueItem,
  WorkspaceOperatorAction,
  WorkspaceOverview,
} from "@/lib/types";
import type { WorkspaceOperatorControl } from "@/lib/workspace-operator-controls";
import { WorkspaceInspector } from "./workspace-inspector";
import {
  EventsPanel,
  LifecycleRail,
  OperationsPanel,
  RuntimePanel,
  terminalLifecycleSourceStage,
} from "./console-dashboard-capacity";
import { LogsPanel } from "./console-dashboard-logs";
import { WorkspaceSummary } from "./console-dashboard-workspace-detail";
import { SecretsLeasesPanel, SecurityEgressPanel } from "./console-dashboard-security";
import {
  ErrorBanner,
  PanelContext,
  type DetailState,
  type LogEntry,
  type OperatorActionState,
  type RetryActionState,
  type SortDirection,
} from "./console-dashboard-shared";

type ConsoleDashboardInspectorProps = {
  selectedId: string | null;
  selectedOverview: WorkspaceOverview | null;
  selectedMergeQueueItem: MergeQueueItem | null;
  detail: DetailState;
  retryState: RetryActionState;
  operatorControls: WorkspaceOperatorControl[];
  operatorActionState: OperatorActionState;
  capabilities: ConsoleCapabilities | null;
  capabilitiesReady: boolean;
  showWorkspaceRuntime: boolean;
  showWorkspaceEvents: boolean;
  showWorkspaceOperations: boolean;
  showWorkspaceLogs: boolean;
  selectedStreams: string[];
  selectedStreamMetas: DetailState["streams"];
  selectedLogEntries: LogEntry[];
  streamOffsets: Record<string, number>;
  logSortDirection: SortDirection;
  logTailSignal: number;
  logTailRefreshError: string | null;
  workspaceDetailError: string | null;
  onClose: () => void;
  onRetry: () => void;
  onOperatorAction: (action: WorkspaceOperatorAction, requestedTier?: number) => void;
  onToggleStream: (streamId: string, checked: boolean) => void;
  onSelectAllStreams: () => void;
  onClearStreams: () => void;
  onReloadLogs: () => void;
  onOpenFullscreen: () => void;
  onToggleSortDirection: () => void;
};

/** Selected-workspace inspector body extracted for maintainability line budget. */
export function ConsoleDashboardInspector(props: ConsoleDashboardInspectorProps) {
  const {
    selectedId,
    selectedOverview,
    selectedMergeQueueItem,
    detail,
    retryState,
    operatorControls,
    operatorActionState,
    capabilities,
    capabilitiesReady,
    showWorkspaceRuntime,
    showWorkspaceEvents,
    showWorkspaceOperations,
    showWorkspaceLogs,
    selectedStreams,
    selectedStreamMetas,
    selectedLogEntries,
    streamOffsets,
    logSortDirection,
    logTailSignal,
    logTailRefreshError,
    workspaceDetailError,
  } = props;

  return (
    <WorkspaceInspector
      isOpen={!!(selectedId && selectedOverview)}
      onClose={props.onClose}
      title={selectedOverview ? selectedOverview.title : "Workspace Details"}
    >
      <PanelContext.Provider value="ghost">
        {selectedId && selectedOverview ? (
          <div className="grid min-w-0 gap-4 min-[1700px]:grid-cols-[minmax(0,1fr)_minmax(400px,0.8fr)]">
            {workspaceDetailError ? (
              <div className="min-[1700px]:col-span-2">
                <ErrorBanner message={workspaceDetailError} />
              </div>
            ) : null}
            <div className="grid min-w-0 content-start gap-4">
              <WorkspaceSummary
                overview={selectedOverview}
                workspace={detail.workspace}
                mergeQueueItem={selectedMergeQueueItem}
                retryState={retryState}
                operatorControls={operatorControls}
                operatorActionState={operatorActionState}
                capabilities={capabilities}
                capabilitiesReady={capabilitiesReady}
                onRetry={props.onRetry}
                onOperatorAction={props.onOperatorAction}
              />
              <LifecycleRail
                status={selectedOverview.status}
                lifecycle={detail.workspace?.lifecycle ?? selectedOverview.lifecycle ?? []}
                terminalSourceStage={terminalLifecycleSourceStage(
                  selectedOverview.status,
                  showWorkspaceEvents ? detail.events : [],
                  selectedOverview.last_event,
                  selectedOverview.current_phase,
                )}
              />
              {showWorkspaceRuntime ? <RuntimePanel runtime={detail.runtime} /> : null}
              <SecurityEgressPanel
                resolvedProfile={detail.workspace?.resolved_profile ?? null}
                policyFindings={detail.workspace?.policy_findings}
                egressAudit={detail.workspace?.egress_audit}
              />
              <SecretsLeasesPanel
                resolvedProfile={detail.workspace?.resolved_profile ?? null}
                secretLeases={detail.workspace?.secret_leases ?? null}
              />
              {showWorkspaceOperations ? (
                <OperationsPanel operations={detail.operations} />
              ) : null}
            </div>
            <div className="grid min-w-0 content-start gap-4">
              {showWorkspaceEvents ? <EventsPanel events={detail.events} /> : null}
              {showWorkspaceLogs ? (
                <LogsPanel
                  streams={detail.streams}
                  selectedStreams={selectedStreams}
                  selectedStreamMetas={selectedStreamMetas}
                  entries={selectedLogEntries}
                  offsets={streamOffsets}
                  sortDirection={logSortDirection}
                  tailSignal={logTailSignal}
                  refreshError={logTailRefreshError}
                  onToggleStream={props.onToggleStream}
                  onSelectAll={props.onSelectAllStreams}
                  onClear={props.onClearStreams}
                  onReload={props.onReloadLogs}
                  onOpenFullscreen={props.onOpenFullscreen}
                  onToggleSortDirection={props.onToggleSortDirection}
                />
              ) : null}
            </div>
          </div>
        ) : null}
      </PanelContext.Provider>
    </WorkspaceInspector>
  );
}
