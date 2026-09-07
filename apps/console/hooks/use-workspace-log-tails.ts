"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
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

function isLogTailAuthFailure(status: number): boolean {
  return status === 401 || status === 403;
}

function logTailRefreshErrorKey(workspaceId: string, streamId: string): string {
  return `${workspaceId}:${streamId}`;
}

function omitLogTailRefreshError(
  current: Record<string, string>,
  workspaceId: string,
  streamId: string,
): Record<string, string> {
  const key = logTailRefreshErrorKey(workspaceId, streamId);
  if (!(key in current)) {
    return current;
  }
  const next = { ...current };
  delete next[key];
  return next;
}

function formatLogTailRefreshError(
  errors: Record<string, string>,
  workspaceId: string,
  selectedStreamIds: readonly string[],
): string | null {
  const messages = [
    ...new Set(
      selectedStreamIds
        .map((streamId) => errors[logTailRefreshErrorKey(workspaceId, streamId)])
        .filter((message): message is string => Boolean(message)),
    ),
  ];
  return messages.length > 0 ? messages.join("; ") : null;
}

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
  logListingAuthDenied: boolean;
  logListingAuthDeniedRef: MutableRefObject<boolean>;
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
  logListingAuthDenied,
  logListingAuthDeniedRef,
  setDetail,
  setSelectedStreams,
  setLogEntries,
  setStreamOffsets,
  setLogTailSignal,
  setFullscreenWorkspaceIds,
  setLogsFullscreen,
}: UseWorkspaceLogTailsArgs) {
  // Per-stream tail request generation: overlapping reloads of the same stream
  // stay monotonic so a newer 401/403 denial cannot lose to an older in-flight
  // 200 (epoch/gated refs alone do not advance on that path).
  const logTailRequestGenerationRef = useRef<Record<string, number>>({});
  const [logTailRefreshErrors, setLogTailRefreshErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    setLogTailRefreshErrors({});
  }, [selectedId]);

  useEffect(() => {
    if (logListingAuthDenied) {
      setLogTailRefreshErrors({});
    }
  }, [logListingAuthDenied]);

  const loadLogTail = useCallback(
    async (workspaceId: string, stream: WorkspaceLogStream, selectedStreamIds: readonly string[]) => {
      const epoch = authorizedFeedEpochRef.current;
      const gatedGeneration = gatedDetailFeedGenerationRef.current;
      const generationKey = `${workspaceId}:${stream.stream_id}`;
      const generation = (logTailRequestGenerationRef.current[generationKey] ?? 0) + 1;
      logTailRequestGenerationRef.current[generationKey] = generation;
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
        generation !== logTailRequestGenerationRef.current[generationKey] ||
        selectedIdRef.current !== workspaceId ||
        logListingAuthDeniedRef.current
      ) {
        return;
      }
      if (!result.ok) {
        if (isLogTailAuthFailure(result.status)) {
          // Feed-level 401/403 is auth revocation for this stream, not a
          // transient outage: drop prior tail and live contents.
          setLogTailRefreshErrors((current) =>
            omitLogTailRefreshError(current, workspaceId, stream.stream_id),
          );
          setLogEntries((current) => {
            if (logListingAuthDeniedRef.current) {
              return current;
            }
            return trimLogEntries(
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
            );
          });
          return;
        }
        // Transient network/5xx (and other non-auth) failures: keep the
        // last-successful tail and live entries. Stream-metadata polling
        // retriggers these reads, so replacing diagnostics with an error
        // line would hide the snapshot the feed-outage contract requires.
        setLogTailRefreshErrors((current) => ({
          ...current,
          [logTailRefreshErrorKey(workspaceId, stream.stream_id)]:
            `Unable to load log stream: ${result.message}`,
        }));
        return;
      }
      setLogTailRefreshErrors((current) => omitLogTailRefreshError(current, workspaceId, stream.stream_id));
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
      setLogEntries((current) => {
        if (logListingAuthDeniedRef.current) {
          return current;
        }
        return trimLogEntries(
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
        );
      });
      setStreamOffsets((current) => {
        if (logListingAuthDeniedRef.current) {
          return current;
        }
        return {
          ...current,
          [stream.stream_id]: result.data.next_offset,
        };
      });
    },
    [
      authorizedFeedEpochRef,
      gatedDetailFeedGenerationRef,
      logListingAuthDeniedRef,
      logStreamActivityRef,
      logTailRequestGenerationRef,
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

  const logTailRefreshError =
    selectedId == null
      ? null
      : formatLogTailRefreshError(logTailRefreshErrors, selectedId, selectedStreams);

  return {
    loadLogTail,
    logTailRefreshError,
    reloadSelectedLogs,
    openWorkspaceLogs,
    openCurrentWorkspaceLogs,
    openSelectedWorkspaceLogs,
    removeFullscreenWorkspace,
  };
}
