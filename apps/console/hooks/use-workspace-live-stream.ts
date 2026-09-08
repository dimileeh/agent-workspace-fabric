"use client";

import { useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
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
  streamAuthProbeDelayMs,
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
  workspaceBaseDetailAuthDeniedRef: MutableRefObject<boolean>;
  workspaceStreamAuthDeniedRef: MutableRefObject<boolean>;
  noteWorkspaceStreamAuthorizationDenied: () => void;
  noteWorkspaceStreamAuthorizationRecovered: () => void;
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
  workspaceBaseDetailAuthDeniedRef,
  workspaceStreamAuthDeniedRef,
  noteWorkspaceStreamAuthorizationDenied,
  noteWorkspaceStreamAuthorizationRecovered,
  eventFeedAuthDeniedRef,
  setStreamState,
  setDetail,
  setLogEntries,
  setStreamOffsets,
  setError,
}: UseWorkspaceLiveStreamArgs): void {
  // Bumped when a delayed /stream probe is due. Zero means the route latch
  // still holds EventSource closed; a non-zero nonce is the one later probe.
  const [streamProbeNonce, setStreamProbeNonce] = useState(0);
  const streamProbeTimerRef = useRef<number | null>(null);

  const clearStreamAuthProbe = () => {
    if (streamProbeTimerRef.current !== null) {
      window.clearTimeout(streamProbeTimerRef.current);
      streamProbeTimerRef.current = null;
    }
  };

  const scheduleStreamAuthProbe = () => {
    if (streamProbeTimerRef.current !== null) {
      return;
    }
    streamProbeTimerRef.current = window.setTimeout(() => {
      streamProbeTimerRef.current = null;
      if (!workspaceStreamAuthDeniedRef.current) {
        return;
      }
      setStreamProbeNonce((current) => current + 1);
    }, streamAuthProbeDelayMs);
  };

  useEffect(() => {
    return () => {
      clearStreamAuthProbe();
      setStreamProbeNonce(0);
    };
  }, [selectedId]);

  useEffect(() => {
    // Listing, tail, or base-detail 401/403 while workspace_stream stays
    // advertised must close /stream, not only drop frames after they arrive.
    // The capability gate stays true on those paths, so the denial latch tears
    // the EventSource down. Tail and base-detail denial are separate: a later
    // listing 200 must not reopen /stream, and a snapshot must not write
    // revoked workspace metadata back until a successful detail GET recovers.
    // A route-scoped /stream 401/403 is different: GET must not recover it,
    // but a later probe may open and clear that latch after it connects.
    if (!selectedId || logListingAuthDenied || logTailAuthDenied) {
      setStreamState("idle");
      return;
    }
    const probingDeniedStream =
      workspaceDetailAuthDenied &&
      workspaceStreamAuthDeniedRef.current &&
      streamProbeNonce > 0;
    if (workspaceDetailAuthDenied && !probingDeniedStream) {
      setStreamState("idle");
      if (workspaceStreamAuthDeniedRef.current) {
        scheduleStreamAuthProbe();
      }
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
    let probeRejected = false;

    const streamAuthDenied = () =>
      workspaceDetailAuthDeniedRef.current ||
      workspaceBaseDetailAuthDeniedRef.current ||
      logListingAuthDeniedRef.current ||
      logTailAuthDeniedRef.current;

    const rejectFailedStreamProbe = () => {
      // A delayed probe that receives a non-auth error or closed frame has
      // not proved the route recovered. close() does not fire onerror, so
      // reschedule here: leaving the route latch and a nonzero nonce holds
      // the inspector blank after the outage and authorization recover.
      if (!workspaceStreamAuthDeniedRef.current) {
        return;
      }
      probeRejected = true;
      setStreamState("idle");
      source.close();
      setStreamProbeNonce(0);
      scheduleStreamAuthProbe();
    };

    const acceptStreamProbe = () => {
      if (!workspaceStreamAuthDeniedRef.current) {
        return;
      }
      // The probe connected. Clear the route latch so a later detail GET may
      // refresh, but leave the revoke watermark covering in-flight GETs.
      // A base-detail GET 401/403 is a separate latch: this handshake must not
      // hide that denial or let the next snapshot write workspace metadata.
      noteWorkspaceStreamAuthorizationRecovered();
      setStreamProbeNonce(0);
      if (workspaceBaseDetailAuthDeniedRef.current) {
        return;
      }
      workspaceDetailAuthDeniedRef.current = false;
      setWorkspaceDetailAuthDenied(false);
      // Re-read at flush: a base-detail 401/403 can latch after this handshake
      // and must keep its banner. Clearing the banner unconditionally would
      // hide that still-denied GET.
      setError((current) => (workspaceBaseDetailAuthDeniedRef.current ? current : null));
    };

    const applyStreamAuthorizationDenial = (message: string) => {
      // Route-level /stream 401/403 is authorization revocation, not an outage.
      // Raise the detail revoke watermark before close() so an in-flight or
      // later authorized /workspaces/{id} GET cannot clear this latch, restore
      // the revoked snapshot, and reopen EventSource on the next poll.
      // Hold the probe closed until the delayed retry; do not reconnect here.
      noteWorkspaceStreamAuthorizationDenied();
      clearStreamAuthProbe();
      setStreamProbeNonce(0);
      workspaceDetailAuthDeniedRef.current = true;
      setWorkspaceDetailAuthDenied(true);
      scheduleStreamAuthProbe();
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
        logListingAuthDeniedRef.current ||
        logTailAuthDeniedRef.current
      ) {
        return;
      }
      // Base-detail denial without a stream-route probe must still drop
      // frames. During a probe the route latch is held until this connection
      // proves authorized, so that latch alone must not discard the handshake.
      if (workspaceDetailAuthDeniedRef.current && !workspaceStreamAuthDeniedRef.current) {
        return;
      }
      const frame = parseFrame(message.data);
      if (!frame) {
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
        // Do not accept the probe or drop this frame here. The terminal
        // error/closed close below must reset the nonce and schedule the
        // next probe; returning early leaves that close to skip the retry.
      } else if (workspaceStreamAuthDeniedRef.current && !probeRejected) {
        acceptStreamProbe();
      }
      if (
        (probeRejected || workspaceStreamAuthDeniedRef.current) &&
        frame.type !== "error" &&
        frame.type !== "closed"
      ) {
        // The handshake has not proved /stream. Drop this frame so a later
        // heartbeat or snapshot cannot accept the probe or restore metadata.
        // Error and closed frames fall through so the close below can
        // reschedule a failed probe. close() does not fire onerror.
        return;
      }
      if (workspaceBaseDetailAuthDeniedRef.current && !workspaceStreamAuthDeniedRef.current) {
        // The handshake proved /stream, not /workspaces/{id}. Drop this frame
        // so a snapshot cannot restore metadata the GET still denies.
        // A still-latched route probe has not proved /stream; a generic
        // error or closed frame must reach the close below and reschedule.
        setStreamState("idle");
        source.close();
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
          // updater, including after a stream probe cleared only the route
          // latch. Do not write revoked workspace metadata back.
          if (workspaceDetailAuthDeniedRef.current || workspaceBaseDetailAuthDeniedRef.current) {
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
          if (
            workspaceDetailAuthDeniedRef.current ||
            workspaceBaseDetailAuthDeniedRef.current ||
            eventFeedAuthDeniedRef.current
          ) {
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
      if (frame.type === "error") {
        // A delayed probe that receives a generic error frame has not proved
        // the route recovered. close() does not fire onerror, so reset the
        // nonce and schedule another probe instead of leaving the latch
        // held with a nonzero nonce.
        if (workspaceStreamAuthDeniedRef.current) {
          rejectFailedStreamProbe();
          return;
        }
        terminalError = true;
        setStreamState("error");
        setError(frame.message);
        source.close();
        return;
      }
      if (frame.type === "closed") {
        if (workspaceStreamAuthDeniedRef.current) {
          rejectFailedStreamProbe();
          return;
        }
        closedByServer = true;
        setStreamState("idle");
        source.close();
      }
    };

    source.onerror = () => {
      // close() from an authorization denial fires error; do not flip the
      // cleared inspector back to connecting while the latch is held.
      // A probe that dies without an authorized frame must wait for the
      // delayed retry rather than letting EventSource reconnect immediately.
      if (workspaceStreamAuthDeniedRef.current) {
        rejectFailedStreamProbe();
        return;
      }
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
    workspaceBaseDetailAuthDeniedRef,
    workspaceStreamAuthDeniedRef,
    setWorkspaceDetailAuthDenied,
    noteWorkspaceStreamAuthorizationDenied,
    noteWorkspaceStreamAuthorizationRecovered,
    streamProbeNonce,
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
