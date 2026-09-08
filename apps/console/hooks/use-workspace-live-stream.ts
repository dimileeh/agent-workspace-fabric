"use client";

import { useEffect, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { fallbackLlmUsage } from "@/lib/format";
import {
  resolveWorkspaceLogStreamAccess,
  resolveWorkspaceStreamSubscription,
} from "@/lib/console-capabilities";
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
  workspaceDetailAuthDenied: boolean;
  workspaceDetailAuthDeniedRef: MutableRefObject<boolean>;
  setWorkspaceDetailAuthDenied: Dispatch<SetStateAction<boolean>>;
  eventFeedAuthDeniedRef: MutableRefObject<boolean>;
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
  workspaceDetailAuthDenied,
  workspaceDetailAuthDeniedRef,
  setWorkspaceDetailAuthDenied,
  eventFeedAuthDeniedRef,
  setStreamState,
  setDetail,
  setLogEntries,
  setStreamOffsets,
  setError,
}: UseWorkspaceLiveStreamArgs): void {
  useEffect(() => {
    // Listing, tail, or base-detail 401/403 while workspace_stream stays
    // advertised must close /stream, not only drop frames after they arrive.
    // The capability gate stays true on those paths, so the denial latch tears
    // the EventSource down. Tail and base-detail denial are separate: a later
    // listing 200 must not reopen /stream, and a snapshot must not write
    // revoked workspace metadata back until a successful detail GET recovers.
    if (!selectedId || logListingAuthDenied || logTailAuthDenied || workspaceDetailAuthDenied) {
      setStreamState("idle");
      return;
    }
    const { allowStream, allowStreamLogs } = resolveWorkspaceLogStreamAccess(capabilities);
    if (!allowStream) {
      setStreamState("idle");
      return;
    }
    // workspace_stream may stay up while events or logs are unsupported.
    // Request only the negotiated feeds so Core does not read them, and do
    // not retain event frames that would surface after a later enable.
    const { allowEvents, channels, tailBytes } = resolveWorkspaceStreamSubscription(capabilities);
    const epoch = authorizedFeedEpochRef.current;
    setStreamState("connecting");
    const source = new EventSource(
      awfPath(`workspaces/${selectedId}/stream`, {
        channels,
        tail_bytes: tailBytes,
      }),
    );
    let closedByServer = false;
    let terminalError = false;

    const streamAuthDenied = () =>
      workspaceDetailAuthDeniedRef.current ||
      logListingAuthDeniedRef.current ||
      logTailAuthDeniedRef.current;

    const applyStreamAuthorizationDenial = (message: string) => {
      // Route-level /stream 401/403 is authorization revocation, not an outage.
      // Latch before close() so the follow-up error event cannot reopen the
      // inspector or leave the previous snapshot, events, and logs visible.
      workspaceDetailAuthDeniedRef.current = true;
      setWorkspaceDetailAuthDenied(true);
      setStreamState("idle");
      setError(message);
      setLogEntries([]);
      setStreamOffsets({});
      setDetail((current) => {
        if (!workspaceDetailAuthDeniedRef.current) {
          return current;
        }
        return {
          ...current,
          workspace: null,
          events: [],
        };
      });
    };

    const streamAuthorizationDenied = (frame: { status?: number }) =>
      frame.status === 401 || frame.status === 403;

    source.onmessage = (message) => {
      if (
        epoch !== authorizedFeedEpochRef.current ||
        selectedIdRef.current !== selectedId ||
        streamAuthDenied()
      ) {
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
        setDetail((current) => {
          // A base-detail 401/403 may land between the frame check and this
          // updater. Do not write revoked workspace metadata back.
          if (workspaceDetailAuthDeniedRef.current) {
            return current;
          }
          return {
            ...current,
            workspace: {
              ...frame.workspace,
              lifecycle: frame.workspace.lifecycle ?? [],
              llm_usage: fallbackLlmUsage(frame.workspace.llm_usage),
              recovery: frame.workspace.recovery ?? null,
            },
          };
        });
        return;
      }
      if (frame.type === "event") {
        // /events 401/403 clears detail.events but leaves workspace_stream
        // advertised. Ignore the event channel until that feed recovers;
        // snapshots and logs stay on this EventSource. An unsupported
        // workspace_events gate (including policy_disabled) must not merge
        // frames either — retained events would appear when the gate opens.
        if (!allowEvents || eventFeedAuthDeniedRef.current) {
          return;
        }
        setStreamState("live");
        setDetail((current) => {
          // An event-feed or base-detail 401/403 may land between the frame
          // check and this updater. Do not refill the cleared Events panel.
          if (workspaceDetailAuthDeniedRef.current || eventFeedAuthDeniedRef.current) {
            return current;
          }
          return {
            ...current,
            events: mergeEvent(current.events, frame.event),
          };
        });
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
        if (streamAuthDenied()) {
          return;
        }
        setStreamState("live");
        setLogEntries((current) => {
          if (streamAuthDenied()) {
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
          if (streamAuthDenied()) {
            return current;
          }
          return {
            ...current,
            [frame.stream_id]: Math.max(current[frame.stream_id] ?? 0, frame.next_offset ?? 0),
          };
        });
        return;
      }
      if (frame.type === "error" || frame.type === "closed") {
        if (streamAuthorizationDenied(frame)) {
          applyStreamAuthorizationDenial(
            frame.type === "error" ? frame.message : "Workspace stream authorization denied.",
          );
          source.close();
          return;
        }
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
      if (streamAuthDenied()) {
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
    workspaceDetailAuthDenied,
    workspaceDetailAuthDeniedRef,
    setWorkspaceDetailAuthDenied,
    eventFeedAuthDeniedRef,
    selectedIdRef,
    selectedStreamsRef,
    setDetail,
    setError,
    setLogEntries,
    setStreamOffsets,
    setStreamState,
  ]);
}
