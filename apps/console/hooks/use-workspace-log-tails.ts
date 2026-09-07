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
import {
  DROP_ALL_GATED_DETAIL_FEEDS,
  noteGatedDetailDrop,
  type GatedDetailDropStamp,
  orderFullscreenWorkspaceIds,
} from "@/lib/console-dashboard-derived";
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

function workspaceHasDeniedLogTail(deniedStreamKeys: ReadonlySet<string>, workspaceId: string): boolean {
  const prefix = `${workspaceId}:`;
  for (const key of deniedStreamKeys) {
    if (key.startsWith(prefix)) {
      return true;
    }
  }
  return false;
}

function logTailRefreshErrorKey(workspaceId: string, streamId: string): string {
  return `${workspaceId}:${streamId}`;
}

/** Fields that change the automatic tail read. Array identity is not one of them. */
function automaticLogTailPart(stream: WorkspaceLogStream): string {
  return [stream.byte_count, stream.line_count, stream.opened_at, stream.closed_at ?? ""].join(":");
}

/**
 * Drop a recorded automatic tail only when it is still the attempt that failed.
 * A later poll with the same byte/line/open/close metadata must retry a
 * transient 5xx or network failure. A newer part already recorded while this
 * read was in flight must stay so its pending follow-up is not discarded.
 */
function forgetRecordedAutomaticTailPart(parts: Map<string, string>, streamId: string, part: string): void {
  if (parts.get(streamId) === part) {
    parts.delete(streamId);
  }
}

type PendingAutomaticLogTail = {
  workspaceId: string;
  stream: WorkspaceLogStream;
  selectedStreamIds: readonly string[];
  part: string;
};

function settleLogTailInFlight(
  inFlightStreamKeys: Set<string>,
  requestGeneration: Readonly<Record<string, number>>,
  workspaceId: string,
  streamId: string,
  generation: number,
): void {
  const key = logTailRefreshErrorKey(workspaceId, streamId);
  if (requestGeneration[key] === generation) {
    inFlightStreamKeys.delete(key);
  }
}

/**
 * A completed 401/403 is not the only stream that must stay latched. Sibling
 * tails that already started can still be unauthorized or hanging; recording
 * them now stops a later 200 for only the denied stream from reopening
 * EventSource before those requests settle.
 */
function recordDeniedLogTailAndInFlightSiblings(
  deniedStreamKeys: Set<string>,
  inFlightStreamKeys: ReadonlySet<string>,
  workspaceId: string,
  streamId: string,
): void {
  const prefix = `${workspaceId}:`;
  for (const key of inFlightStreamKeys) {
    if (key.startsWith(prefix)) {
      deniedStreamKeys.add(key);
    }
  }
  deniedStreamKeys.add(logTailRefreshErrorKey(workspaceId, streamId));
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
  gatedDetailDroppedFeedsRef: MutableRefObject<GatedDetailDropStamp[]>;
  logStreamActivityRef: MutableRefObject<LogStreamActivityMap>;
  logListingAuthDenied: boolean;
  logListingAuthDeniedRef: MutableRefObject<boolean>;
  logTailAuthDeniedRef: MutableRefObject<boolean>;
  setLogTailAuthDenied: Dispatch<SetStateAction<boolean>>;
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
  gatedDetailDroppedFeedsRef,
  logStreamActivityRef,
  logListingAuthDenied,
  logListingAuthDeniedRef,
  logTailAuthDeniedRef,
  setLogTailAuthDenied,
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
  // A 200 from a sibling stream must not clear a 401/403 latched for another
  // selected tail. EventSource is workspace-wide, so the latch stays held until
  // every denied stream itself succeeds (a hang or 5xx retry does not recover).
  const logTailDeniedStreamKeysRef = useRef<Set<string>>(new Set());
  // Selected tails started together. Until each one settles, a 200 for a
  // stream that already returned 401/403 must not treat the workspace as
  // recovered — a sibling may still be unauthorized or hanging.
  const logTailInFlightStreamKeysRef = useRef<Set<string>>(new Set());
  // Automatic inspector refreshes must not bump generation while a tail is
  // already in flight. A detail poll installs a new streams array even when
  // metadata is unchanged; restarting that read discards the slower success
  // and, with workspace_stream unsupported, leaves the inspector empty.
  const previousAutomaticTailWorkspaceRef = useRef<string | null>(null);
  const previousAutomaticTailPartsRef = useRef<Map<string, string>>(new Map());
  const pendingAutomaticTailsRef = useRef<Map<string, PendingAutomaticLogTail>>(new Map());
  const automaticSelectedStreamsRef = useRef(selectedStreams);
  const loadLogTailRef = useRef<
    (workspaceId: string, stream: WorkspaceLogStream, selectedStreamIds: readonly string[]) => Promise<void>
  >(async () => undefined);
  const [logTailRefreshErrors, setLogTailRefreshErrors] = useState<Record<string, string>>({});

  const drainPendingAutomaticTail = useCallback((generationKey: string) => {
    const pending = pendingAutomaticTailsRef.current.get(generationKey);
    if (!pending) {
      return;
    }
    if (logTailInFlightStreamKeysRef.current.has(generationKey)) {
      return;
    }
    pendingAutomaticTailsRef.current.delete(generationKey);
    const selectedStreamIds = automaticSelectedStreamsRef.current;
    if (
      selectedIdRef.current !== pending.workspaceId ||
      logListingAuthDeniedRef.current ||
      logTailDeniedStreamKeysRef.current.has(generationKey) ||
      !selectedStreamIds.includes(pending.stream.stream_id) ||
      previousAutomaticTailPartsRef.current.get(pending.stream.stream_id) !== pending.part
    ) {
      return;
    }
    void loadLogTailRef.current(pending.workspaceId, pending.stream, selectedStreamIds);
  }, [logListingAuthDeniedRef, selectedIdRef]);

  useEffect(() => {
    logTailDeniedStreamKeysRef.current.clear();
    logTailInFlightStreamKeysRef.current.clear();
    pendingAutomaticTailsRef.current.clear();
    previousAutomaticTailPartsRef.current = new Map();
    previousAutomaticTailWorkspaceRef.current = selectedId;
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
      logTailInFlightStreamKeysRef.current.add(generationKey);
      const settleInFlight = () => {
        settleLogTailInFlight(
          logTailInFlightStreamKeysRef.current,
          logTailRequestGenerationRef.current,
          workspaceId,
          stream.stream_id,
          generation,
        );
      };
      try {
        const offset = Math.max(stream.byte_count - 65_536, 0);
        const activity = logStreamActivityFor(logStreamActivityRef.current, workspaceId, stream);
        // Capture the fingerprint the automatic effect recorded before the
        // read returns. A later mutation of this stream object must not
        // prevent a failed attempt from being retried.
        const scheduledAutomaticPart = automaticLogTailPart(stream);
        const result = await apiGet<WorkspaceLogRead>(
          awfPath(`workspaces/${workspaceId}/logs/${encodeURIComponent(stream.stream_id)}`, {
            offset,
            limit_bytes: 65536,
          }),
        );
        if (
          epoch !== authorizedFeedEpochRef.current ||
          generation !== logTailRequestGenerationRef.current[generationKey] ||
          selectedIdRef.current !== workspaceId ||
          logListingAuthDeniedRef.current
        ) {
          settleInFlight();
          return;
        }
        // The first tail 401/403 calls noteGatedDetailDrop, which advances
        // gatedDetailFeedGenerationRef. A sibling denial that already captured
        // the prior generation must still be recorded; discarding it here lets
        // a later 200 for only the recorded stream reopen EventSource while
        // another selected stream is still unauthorized. Epoch, per-stream
        // generation, selection, and listing denial remain hard discards.
        const gatedGenerationAdvanced =
          gatedGeneration !== gatedDetailFeedGenerationRef.current;
        // A sibling 401 advances gated generation and discards this non-auth
        // result. Forget the recorded part anyway so the next metadata poll
        // retries a transient 5xx even when byte/line/open/close are unchanged.
        if (
          gatedGenerationAdvanced &&
          !result.ok &&
          !isLogTailAuthFailure(result.status)
        ) {
          forgetRecordedAutomaticTailPart(
            previousAutomaticTailPartsRef.current,
            stream.stream_id,
            scheduledAutomaticPart,
          );
        }
        if (gatedGenerationAdvanced && (result.ok || !isLogTailAuthFailure(result.status))) {
          settleInFlight();
          return;
        }
        if (!result.ok) {
          if (isLogTailAuthFailure(result.status)) {
            // Tail 401/403 while listing stays reachable is still auth revocation
            // for this workspace's log output. Drop prior contents and latch so
            // the still-open EventSource cannot append new frames. Listing
            // success must not clear this latch (it only clears listing denial).
            // Snapshot in-flight siblings before this request settles so a later
            // 200 for only this stream cannot reopen EventSource while another
            // selected tail is still unauthorized or hanging.
            if (!logTailAuthDeniedRef.current) {
              noteGatedDetailDrop(
                gatedDetailDroppedFeedsRef,
                gatedDetailFeedGenerationRef,
                DROP_ALL_GATED_DETAIL_FEEDS,
              );
            }
            recordDeniedLogTailAndInFlightSiblings(
              logTailDeniedStreamKeysRef.current,
              logTailInFlightStreamKeysRef.current,
              workspaceId,
              stream.stream_id,
            );
            logTailDeniedStreamKeysRef.current.add(logTailRefreshErrorKey(workspaceId, stream.stream_id));
            settleInFlight();
            logTailAuthDeniedRef.current = true;
            setLogTailAuthDenied(true);
            setLogTailRefreshErrors((current) =>
              omitLogTailRefreshError(current, workspaceId, stream.stream_id),
            );
            setLogEntries((current) => {
              if (logListingAuthDeniedRef.current) {
                return current;
              }
              return trimLogEntries(
                [
                  ...current.filter((entry) => entry.workspaceId !== workspaceId),
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
            setStreamOffsets({});
            return;
          }
          // Transient network/5xx (and other non-auth) failures: keep the
          // last-successful tail and live entries. Forget the recorded
          // automatic part so the next metadata poll retries even when
          // byte/line/open/close are unchanged. Replacing diagnostics with
          // an error line would hide the snapshot the feed-outage contract
          // requires. 401/403 stays latched and is not retried here.
          settleInFlight();
          forgetRecordedAutomaticTailPart(
            previousAutomaticTailPartsRef.current,
            stream.stream_id,
            scheduledAutomaticPart,
          );
          setLogTailRefreshErrors((current) => ({
            ...current,
            [logTailRefreshErrorKey(workspaceId, stream.stream_id)]:
              `Unable to load log stream: ${result.message}`,
          }));
          return;
        }
        // A 200 recovers only the stream that returned it. Clearing the
        // workspace latch on any sibling success reopens EventSource and lets
        // frames for a still-denied tail land. A sibling snapshotted while
        // in-flight stays denied until that stream itself succeeds. Apply this
        // stream's snapshot anyway: the latch gates EventSource, not inspector
        // tails that already succeeded. Skipping the write while the latch is
        // held drops every recovered stream except the last one.
        settleInFlight();
        const recoveredKey = logTailRefreshErrorKey(workspaceId, stream.stream_id);
        logTailDeniedStreamKeysRef.current.delete(recoveredKey);
        const stillDenied = workspaceHasDeniedLogTail(logTailDeniedStreamKeysRef.current, workspaceId);
        if (logTailAuthDeniedRef.current !== stillDenied) {
          logTailAuthDeniedRef.current = stillDenied;
          setLogTailAuthDenied(stillDenied);
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
        // Functional updaters can flush after a newer listing 401/403, or after
        // this stream is denied again. A sibling latch must not discard the
        // snapshot; only a denial that still includes this stream does.
        const recoveredTailStillAuthorized = () =>
          !logListingAuthDeniedRef.current && !logTailDeniedStreamKeysRef.current.has(recoveredKey);
        setLogEntries((current) => {
          if (!recoveredTailStillAuthorized()) {
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
          if (!recoveredTailStillAuthorized()) {
            return current;
          }
          return {
            ...current,
            [stream.stream_id]: result.data.next_offset,
          };
        });
      } finally {
        drainPendingAutomaticTail(generationKey);
      }
    },
    [
      authorizedFeedEpochRef,
      drainPendingAutomaticTail,
      gatedDetailDroppedFeedsRef,
      gatedDetailFeedGenerationRef,
      logListingAuthDeniedRef,
      logStreamActivityRef,
      logTailAuthDeniedRef,
      logTailRequestGenerationRef,
      selectedIdRef,
      setLogEntries,
      setLogTailAuthDenied,
      setStreamOffsets,
    ],
  );

  useEffect(() => {
    automaticSelectedStreamsRef.current = selectedStreams;
    loadLogTailRef.current = loadLogTail;
  }, [loadLogTail, selectedStreams]);

  useEffect(() => {
    if (!selectedId || selectedStreams.length === 0) {
      previousAutomaticTailPartsRef.current = new Map();
      pendingAutomaticTailsRef.current.clear();
      return;
    }
    if (previousAutomaticTailWorkspaceRef.current !== selectedId) {
      previousAutomaticTailPartsRef.current = new Map();
      pendingAutomaticTailsRef.current.clear();
      previousAutomaticTailWorkspaceRef.current = selectedId;
    }
    const selectedIds = new Set(selectedStreams);
    const nextParts = new Map<string, string>();
    for (const stream of detailStreams) {
      if (!selectedIds.has(stream.stream_id)) {
        continue;
      }
      const part = automaticLogTailPart(stream);
      nextParts.set(stream.stream_id, part);
      if (previousAutomaticTailPartsRef.current.get(stream.stream_id) === part) {
        continue;
      }
      const generationKey = `${selectedId}:${stream.stream_id}`;
      if (logTailInFlightStreamKeysRef.current.has(generationKey)) {
        pendingAutomaticTailsRef.current.set(generationKey, {
          workspaceId: selectedId,
          stream,
          selectedStreamIds: selectedStreams,
          part,
        });
        continue;
      }
      pendingAutomaticTailsRef.current.delete(generationKey);
      void loadLogTail(selectedId, stream, selectedStreams);
    }
    previousAutomaticTailPartsRef.current = nextParts;
  }, [detailStreams, loadLogTail, selectedId, selectedStreams]);

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
