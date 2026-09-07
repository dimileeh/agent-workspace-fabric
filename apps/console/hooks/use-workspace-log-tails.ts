"use client";

import {
  useCallback,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
} from "react";
import { orderFullscreenWorkspaceIds } from "@/lib/console-dashboard-derived";
import { awfPath } from "@/lib/console-urls";
import type { WorkspaceLogRead, WorkspaceLogStream, WorkspaceOverview } from "@/lib/types";
import {
  type DetailState,
  type LogEntry,
  type LogStreamActivityMap,
  apiGet,
  emptyDetail,
  logStreamActivityFor,
  trimLogEntries,
} from "@/components/console-dashboard-shared";

type UseWorkspaceLogTailsArgs = {
  selectedId: string | null;
  selectedIdRef: MutableRefObject<string | null>;
  setSelectedId: (workspaceId: string | null) => void;
  detailStreams: WorkspaceLogStream[];
  selectedStreams: string[];
  workspaceLogSelection: string[];
  filteredOverview: WorkspaceOverview[];
  fullscreenWorkspaceIds: string[];
  authorizedFeedEpochRef: MutableRefObject<number>;
  gatedDetailFeedGenerationRef: MutableRefObject<number>;
  logStreamActivityRef: MutableRefObject<LogStreamActivityMap>;
  setDetail: Dispatch<SetStateAction<DetailState>>;
  setSelectedStreams: Dispatch<SetStateAction<string[]>>;
  setLogEntries: Dispatch<SetStateAction<LogEntry[]>>;
  setStreamOffsets: Dispatch<SetStateAction<Record<string, number>>>;
  setLogTailSignal: Dispatch<SetStateAction<number>>;
  setFullscreenWorkspaceIds: Dispatch<SetStateAction<string[]>>;
  setLogsFullscreen: Dispatch<SetStateAction<boolean>>;
};

/**
 * Selected-workspace log-tail fetch plus fullscreen open/reload helpers.
 * Extracted from console-dashboard.tsx for the first-party 1500-line guard.
 */
export function useWorkspaceLogTails({
  selectedId,
  selectedIdRef,
  setSelectedId,
  detailStreams,
  selectedStreams,
  workspaceLogSelection,
  filteredOverview,
  fullscreenWorkspaceIds,
  authorizedFeedEpochRef,
  gatedDetailFeedGenerationRef,
  logStreamActivityRef,
  setDetail,
  setSelectedStreams,
  setLogEntries,
  setStreamOffsets,
  setLogTailSignal,
  setFullscreenWorkspaceIds,
  setLogsFullscreen,
}: UseWorkspaceLogTailsArgs) {
  const loadLogTail = useCallback(
    async (workspaceId: string, stream: WorkspaceLogStream, selectedStreamIds: readonly string[]) => {
      const epoch = authorizedFeedEpochRef.current;
      const gatedGeneration = gatedDetailFeedGenerationRef.current;
      const offset = Math.max(stream.byte_count - 65_536, 0);
      const activity = logStreamActivityFor(logStreamActivityRef.current, workspaceId, stream);
      const result = await apiGet<WorkspaceLogRead>(
        awfPath(`workspaces/${workspaceId}/logs/${encodeURIComponent(stream.stream_id)}`, {
          offset,
          limit_bytes: 65536,
        }),
      );
      if (
        epoch !== authorizedFeedEpochRef.current ||
        gatedGeneration !== gatedDetailFeedGenerationRef.current ||
        selectedIdRef.current !== workspaceId
      ) {
        return;
      }
      if (!result.ok) {
        setLogEntries((current) =>
          trimLogEntries(
            [
            ...current.filter(
              (entry) => !(entry.workspaceId === workspaceId && entry.streamId === stream.stream_id),
            ),
            {
              key: `tail-error:${workspaceId}:${stream.stream_id}:${Date.now()}`,
              workspaceId,
              streamId: stream.stream_id,
              source: stream.source,
              fd: null,
              offset,
              data: `Unable to load log stream: ${result.message}`,
              occurredAt: new Date().toISOString(),
              order: Date.now(),
              kind: "tail",
            },
            ],
            selectedStreamIds,
          ),
        );
        return;
      }
      const tailEntry = {
        key: `tail:${workspaceId}:${stream.stream_id}:${result.data.offset}:${result.data.next_offset}`,
        workspaceId,
        streamId: stream.stream_id,
        source: stream.source,
        fd: null,
        offset: result.data.offset,
        data: result.data.data,
        occurredAt: new Date(activity).toISOString(),
        order: activity,
        kind: "tail" as const,
      };
      setLogEntries((current) =>
        trimLogEntries(
          [
            ...current.filter(
              (entry) =>
                entry.workspaceId !== workspaceId ||
                entry.streamId !== stream.stream_id ||
                (entry.kind === "live" && entry.offset >= result.data.next_offset),
            ),
            tailEntry,
          ],
          selectedStreamIds,
        ),
      );
      setStreamOffsets((current) => ({
        ...current,
        [stream.stream_id]: result.data.next_offset,
      }));
    },
    [
      authorizedFeedEpochRef,
      gatedDetailFeedGenerationRef,
      logStreamActivityRef,
      selectedIdRef,
      setLogEntries,
      setStreamOffsets,
    ],
  );

  const reloadSelectedLogs = useCallback(() => {
    if (!selectedId) {
      return;
    }
    setLogTailSignal((current) => current + 1);
    for (const stream of detailStreams) {
      if (selectedStreams.includes(stream.stream_id)) {
        void loadLogTail(selectedId, stream, selectedStreams);
      }
    }
  }, [detailStreams, loadLogTail, selectedId, selectedStreams, setLogTailSignal]);

  const openWorkspaceLogs = useCallback(
    (workspaceId: string) => {
      if (workspaceId !== selectedId) {
        setDetail(emptyDetail);
        setSelectedStreams([]);
        setLogEntries([]);
        setStreamOffsets({});
        setSelectedId(workspaceId);
      }
      setFullscreenWorkspaceIds([workspaceId]);
      setLogsFullscreen(true);
    },
    [
      selectedId,
      setDetail,
      setFullscreenWorkspaceIds,
      setLogEntries,
      setLogsFullscreen,
      setSelectedId,
      setSelectedStreams,
      setStreamOffsets,
    ],
  );

  const openCurrentWorkspaceLogs = useCallback(() => {
    if (!selectedId) {
      return;
    }
    setFullscreenWorkspaceIds([selectedId]);
    setLogsFullscreen(true);
  }, [selectedId, setFullscreenWorkspaceIds, setLogsFullscreen]);

  const openSelectedWorkspaceLogs = useCallback(() => {
    if (workspaceLogSelection.length === 0) {
      return;
    }
    setFullscreenWorkspaceIds(orderFullscreenWorkspaceIds(workspaceLogSelection, filteredOverview));
    setLogsFullscreen(true);
  }, [filteredOverview, setFullscreenWorkspaceIds, setLogsFullscreen, workspaceLogSelection]);

  const removeFullscreenWorkspace = useCallback(
    (workspaceId: string) => {
      const next = fullscreenWorkspaceIds.filter((id) => id !== workspaceId);
      setFullscreenWorkspaceIds(next);
      if (next.length === 0) {
        setLogsFullscreen(false);
      }
    },
    [fullscreenWorkspaceIds, setFullscreenWorkspaceIds, setLogsFullscreen],
  );

  return {
    loadLogTail,
    reloadSelectedLogs,
    openWorkspaceLogs,
    openCurrentWorkspaceLogs,
    openSelectedWorkspaceLogs,
    removeFullscreenWorkspace,
  };
}
