"use client";

import { useEffect, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { fallbackLlmUsage } from "@/lib/format";
import { resolveWorkspaceLogStreamAccess } from "@/lib/console-capabilities";
import { awfPath } from "@/lib/console-urls";
import type { ConsoleCapabilities } from "@/lib/types";
import {
  type DetailState,
  type LogEntry,
  mergeEvent,
  parseFrame,
  trimLogEntries,
} from "@/components/console-dashboard-shared";

type StreamState = "idle" | "connecting" | "live" | "error";

type UseWorkspaceLiveStreamArgs = {
  selectedId: string | null;
  capabilities: ConsoleCapabilities | null;
  authorizedFeedEpochRef: MutableRefObject<number>;
  selectedIdRef: MutableRefObject<string | null>;
  selectedStreamsRef: MutableRefObject<string[]>;
  logListingAuthDenied: boolean;
  logListingAuthDeniedRef: MutableRefObject<boolean>;
  logTailAuthDenied: boolean;
  logTailAuthDeniedRef: MutableRefObject<boolean>;
  setStreamState: Dispatch<SetStateAction<StreamState>>;
  setDetail: Dispatch<SetStateAction<DetailState>>;
  setLogEntries: Dispatch<SetStateAction<LogEntry[]>>;
  setStreamOffsets: Dispatch<SetStateAction<Record<string, number>>>;
  setError: Dispatch<SetStateAction<string | null>>;
};

/**
 * Selected-workspace EventSource subscription (snapshot/event/log frames).
 * Extracted from console-dashboard.tsx for the first-party line-limit guard.
 */
export function useWorkspaceLiveStream({
  selectedId,
  capabilities,
  authorizedFeedEpochRef,
  selectedIdRef,
  selectedStreamsRef,
  logListingAuthDenied,
  logListingAuthDeniedRef,
  logTailAuthDenied,
  logTailAuthDeniedRef,
  setStreamState,
  setDetail,
  setLogEntries,
  setStreamOffsets,
  setError,
}: UseWorkspaceLiveStreamArgs): void {
  useEffect(() => {
    // Listing or tail 401/403 while workspace_logs stays advertised must close
    // /stream, not only drop frames after they arrive. The capability gate
    // stays true on that path, so the denial latch tears the EventSource down.
    // Tail denial is separate: a later listing 200 must not reopen /stream.
    if (!selectedId || logListingAuthDenied || logTailAuthDenied) {
      setStreamState("idle");
      return;
    }
    const { allowStream, allowStreamLogs } = resolveWorkspaceLogStreamAccess(capabilities);
    if (!allowStream) {
      setStreamState("idle");
      return;
    }
    const epoch = authorizedFeedEpochRef.current;
    setStreamState("connecting");
    const source = new EventSource(
      awfPath(`workspaces/${selectedId}/stream`, {
        channels: "events,agent,validation,services",
        tail_bytes: 65536,
      }),
    );
    let closedByServer = false;
    let terminalError = false;

    source.onmessage = (message) => {
      if (epoch !== authorizedFeedEpochRef.current || selectedIdRef.current !== selectedId) {
        return;
      }
      const frame = parseFrame(message.data);
      if (!frame) {
        return;
      }
      if (frame.type === "connected" || frame.type === "heartbeat") {
        setStreamState("live");
        return;
      }
      if (frame.type === "snapshot") {
        setStreamState("live");
        setDetail((current) => ({
          ...current,
          workspace: {
            ...frame.workspace,
            lifecycle: frame.workspace.lifecycle ?? [],
            llm_usage: fallbackLlmUsage(frame.workspace.llm_usage),
            recovery: frame.workspace.recovery ?? null,
          },
        }));
        return;
      }
      if (frame.type === "event") {
        setStreamState("live");
        setDetail((current) => ({
          ...current,
          events: mergeEvent(current.events, frame.event),
        }));
        return;
      }
      if (frame.type === "log") {
        // Without workspace_logs listing the UI cannot pick/surface streams —
        // ignore log frames rather than silently buffering them. A later
        // listing 401/403 leaves the capability gate true; drop frames until
        // the denial latch closes this EventSource.
        if (!allowStreamLogs) {
          return;
        }
        if (logListingAuthDeniedRef.current || logTailAuthDeniedRef.current) {
          return;
        }
        setStreamState("live");
        setLogEntries((current) => {
          if (logListingAuthDeniedRef.current || logTailAuthDeniedRef.current) {
            return current;
          }
          return trimLogEntries(
            [
              ...current,
              {
                key: `live:${frame.workspace_id}:${frame.stream_id}:${frame.offset}:${frame.next_offset ?? frame.offset}:${frame.seq}`,
                workspaceId: frame.workspace_id,
                streamId: frame.stream_id,
                source: frame.source,
                fd: frame.fd,
                offset: frame.offset,
                data: frame.data,
                occurredAt: frame.occurred_at ?? new Date().toISOString(),
                order: Date.parse(frame.occurred_at ?? "") || Date.now(),
                kind: "live",
              },
            ],
            selectedStreamsRef.current,
          );
        });
        setStreamOffsets((current) => {
          if (logListingAuthDeniedRef.current || logTailAuthDeniedRef.current) {
            return current;
          }
          return {
            ...current,
            [frame.stream_id]: Math.max(current[frame.stream_id] ?? 0, frame.next_offset ?? 0),
          };
        });
        return;
      }
      if (frame.type === "error") {
        terminalError = true;
        setStreamState("error");
        setError(frame.message);
        source.close();
        return;
      }
      if (frame.type === "closed") {
        closedByServer = true;
        setStreamState("idle");
        source.close();
      }
    };

    source.onerror = () => {
      // close() from an authorization denial fires error; do not flip the
      // cleared inspector back to connecting while the latch is held.
      if (logListingAuthDeniedRef.current || logTailAuthDeniedRef.current) {
        setStreamState("idle");
        return;
      }
      if (terminalError) {
        setStreamState("error");
        return;
      }
      setStreamState(closedByServer || source.readyState === EventSource.CLOSED ? "idle" : "connecting");
    };

    return () => source.close();
  }, [
    authorizedFeedEpochRef,
    capabilities,
    selectedId,
    logListingAuthDenied,
    logListingAuthDeniedRef,
    logTailAuthDenied,
    logTailAuthDeniedRef,
    selectedIdRef,
    selectedStreamsRef,
    setDetail,
    setError,
    setLogEntries,
    setStreamOffsets,
    setStreamState,
  ]);
}
