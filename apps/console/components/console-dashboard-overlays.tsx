"use client";

import type { WorkspaceOverview } from "@/lib/types";
import { MultiWorkspaceLogsFullscreen } from "./console-dashboard-logs";
import { TaskDetailsModal } from "./console-dashboard-workspace-detail";
import type { LogWorkspaceTarget, SortDirection } from "./console-dashboard-shared";

type ConsoleDashboardOverlaysProps = {
  logsFullscreen: boolean;
  fullscreenWorkspaces: LogWorkspaceTarget[];
  logSortDirection: SortDirection;
  fullscreenTailSignal: number;
  allowFullscreenLogs: boolean;
  allowFullscreenStreamLogs: boolean;
  onTailAll: () => void;
  onToggleSortDirection: () => void;
  onRemoveWorkspace: (workspaceId: string) => void;
  onCloseFullscreen: () => void;
  taskDetailsWorkspace: WorkspaceOverview | null;
  onCloseTaskDetails: () => void;
};

/** Fullscreen log viewer + task details modal (kept out of the main dashboard file). */
export function ConsoleDashboardOverlays(props: ConsoleDashboardOverlaysProps) {
  return (
    <>
      {props.logsFullscreen && props.fullscreenWorkspaces.length > 0 ? (
        <MultiWorkspaceLogsFullscreen
          workspaces={props.fullscreenWorkspaces}
          sortDirection={props.logSortDirection}
          tailSignal={props.fullscreenTailSignal}
          allowLogs={props.allowFullscreenLogs}
          allowStreamLogs={props.allowFullscreenStreamLogs}
          onTailAll={props.onTailAll}
          onToggleSortDirection={props.onToggleSortDirection}
          onRemoveWorkspace={props.onRemoveWorkspace}
          onClose={props.onCloseFullscreen}
        />
      ) : null}
      {props.taskDetailsWorkspace ? (
        <TaskDetailsModal
          workspace={props.taskDetailsWorkspace}
          onClose={props.onCloseTaskDetails}
        />
      ) : null}
    </>
  );
}
