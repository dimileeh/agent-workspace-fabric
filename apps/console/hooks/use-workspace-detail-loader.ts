"use client";

import {
  useCallback,
  useRef,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
} from "react";

import { useSerializedPeriodicLoad } from "@/hooks/use-serialized-periodic-load";
import {
  isDiagnosticAvailable,
  resolveWorkspaceLogStreamAccess,
} from "@/lib/console-capabilities";
import { awfPath } from "@/lib/console-urls";
import { fallbackLlmUsage, pickWorkspaceLogStreams } from "@/lib/format";
import type {
  ApiEnvelope,
  ConsoleCapabilities,
  ListEnvelope,
  Operation,
  Workspace,
  WorkspaceEvent,
  WorkspaceLogStream,
  WorkspaceRuntime,
} from "@/lib/types";
import {
  type DetailState,
  type LogEntry,
  type LogStreamActivityMap,
  apiGet,
  updateLogStreamActivity,
} from "@/components/console-dashboard-shared";

type UseWorkspaceDetailLoaderArgs = {
  selectedId: string | null;
  selectedIdRef: MutableRefObject<string | null>;
  capabilities: ConsoleCapabilities | null;
  authorizedFeedEpochRef: MutableRefObject<number>;
  gatedDetailFeedGenerationRef: MutableRefObject<number>;
  logStreamActivityRef: MutableRefObject<LogStreamActivityMap>;
  selectedStreamsRef: MutableRefObject<string[]>;
  logListingAuthDeniedRef: MutableRefObject<boolean>;
  setLogListingAuthDenied: Dispatch<SetStateAction<boolean>>;
  setError: Dispatch<SetStateAction<string | null>>;
  setDetail: Dispatch<SetStateAction<DetailState>>;
  setSelectedStreams: Dispatch<SetStateAction<string[]>>;
  setLogEntries: Dispatch<SetStateAction<LogEntry[]>>;
  setStreamOffsets: Dispatch<SetStateAction<Record<string, number>>>;
};

/**
 * Selected-workspace detail fetch plus serialized periodic polling.
 * Extracted from console-dashboard.tsx for the first-party 1500-line guard.
 *
 * Periodic ticks chain after the previous invocation settles and skip while a
 * detail load is in flight, so a request slower than pollMs can still apply.
 * Explicit refresh, selection changes, and post-mutation callers use the
 * returned `loadWorkspace`, which advances generation and supersedes safely.
 * A newer feed-level 401/403 still wins over an older in-flight 200.
 */
export function useWorkspaceDetailLoader({
  selectedId,
  selectedIdRef,
  capabilities,
  authorizedFeedEpochRef,
  gatedDetailFeedGenerationRef,
  logStreamActivityRef,
  selectedStreamsRef,
  logListingAuthDeniedRef,
  setLogListingAuthDenied,
  setError,
  setDetail,
  setSelectedStreams,
  setLogEntries,
  setStreamOffsets,
}: UseWorkspaceDetailLoaderArgs) {
  // Overlapping explicit refresh / selection / post-mutation loads stay monotonic
  // so a newer feed-level 401/403 clear cannot lose to an older in-flight 200
  // (epoch/gated refs alone do not advance on that path). Periodic ticks do not
  // bump this — they skip while a detail load is in flight.
  const workspaceDetailRequestGenerationRef = useRef(0);
  const workspaceDetailLoadInFlightRef = useRef(false);

  const loadWorkspace = useCallback(async (workspaceId: string) => {
    const epoch = authorizedFeedEpochRef.current;
    const gatedGeneration = gatedDetailFeedGenerationRef.current;
    const generation = ++workspaceDetailRequestGenerationRef.current;
    workspaceDetailLoadInFlightRef.current = true;
    try {
      const caps = capabilities;
      // Omitted workspace_* diagnostics stay disabled — do not treat absence as
      // legacy Core support (fail closed for optional detail feeds).
      const allowDetail = (id: "workspace_runtime" | "workspace_events" | "workspace_operations") => {
        if (!caps) {
          // Capability failure / not ready: keep basic workspace GET only.
          return false;
        }
        return isDiagnosticAvailable(caps, id);
      };
      const allowRuntime = allowDetail("workspace_runtime");
      const allowEvents = allowDetail("workspace_events");
      const allowOperations = allowDetail("workspace_operations");
      const { allowLogs } = resolveWorkspaceLogStreamAccess(caps);

      const [workspace, runtime, events, operations, streams] = await Promise.all([
        apiGet<Workspace>(awfPath(`workspaces/${workspaceId}`)),
        allowRuntime
          ? apiGet<WorkspaceRuntime>(awfPath(`workspaces/${workspaceId}/runtime`))
          : Promise.resolve(null),
        allowEvents
          ? apiGet<ListEnvelope<WorkspaceEvent>>(
              awfPath(`workspaces/${workspaceId}/events`, { limit: 100 }),
            )
          : Promise.resolve(null),
        allowOperations
          ? apiGet<ListEnvelope<Operation>>(
              awfPath(`workspaces/${workspaceId}/operations`, { limit: 50 }),
            )
          : Promise.resolve(null),
        allowLogs
          ? apiGet<ListEnvelope<WorkspaceLogStream>>(awfPath(`workspaces/${workspaceId}/logs`))
          : Promise.resolve(null),
      ]);

      if (
        epoch !== authorizedFeedEpochRef.current ||
        gatedGeneration !== gatedDetailFeedGenerationRef.current ||
        generation !== workspaceDetailRequestGenerationRef.current ||
        selectedIdRef.current !== workspaceId
      ) {
        return;
      }

      const firstFailure = [workspace, runtime, events, operations, streams].find(
        (item) => item != null && !item.ok,
      );
      // This setter is the workspace-detail slot only. Do not clear overview
      // errors here — an independent overview success must not clear this
      // warning either (CONSOLE_BACKEND_CONTRACT).
      if (firstFailure && !firstFailure.ok) {
        setError(firstFailure.message);
      } else {
        setError(null);
      }

      // Gated-off feeds resolve to null and clear; transient network/5xx keep
      // last-successful inspector snapshots while the error banner stays visible
      // (CONSOLE_BACKEND_CONTRACT). Feed-level 401/403 drops that feed's cache.
      // Accept the full envelope (including listing success and gated-off null)
      // so the post-merge listing check can call this without a false-only cast.
      const feedAuthDenied = (result: ApiEnvelope<unknown> | null | undefined) =>
        result != null && result.ok === false && (result.status === 401 || result.status === 403);

      setDetail((current) => {
        const nextWorkspace = workspace.ok
          ? {
              ...workspace.data,
              lifecycle: workspace.data.lifecycle ?? [],
              llm_usage: fallbackLlmUsage(workspace.data.llm_usage),
              recovery: workspace.data.recovery ?? null,
            }
          : feedAuthDenied(workspace)
            ? null
            : current.workspace;

        const nextRuntime = !allowRuntime
          ? null
          : runtime != null && runtime.ok
            ? runtime.data
            : feedAuthDenied(runtime)
              ? null
              : current.runtime;

        const nextEvents = !allowEvents
          ? []
          : events != null && events.ok
            ? events.data.items
            : feedAuthDenied(events)
              ? []
              : current.events;

        const nextOperations = !allowOperations
          ? []
          : operations != null && operations.ok
            ? operations.data.items
            : feedAuthDenied(operations)
              ? []
              : current.operations;

        const nextStreams = !allowLogs
          ? []
          : streams != null && streams.ok
            ? streams.data.items
            : feedAuthDenied(streams)
              ? []
              : current.streams;

        return {
          workspace: nextWorkspace,
          runtime: nextRuntime,
          events: nextEvents,
          operations: nextOperations,
          streams: nextStreams,
        };
      });

      if (allowLogs && feedAuthDenied(streams)) {
        // Listing 401/403 while workspace_logs stays advertised is auth
        // revocation for this column, not a transient outage. detail.streams
        // is already cleared above; also drop retained selection, tail caches,
        // and the live-log latch so the still-open EventSource cannot keep
        // appending previously authorized frames (CONSOLE_BACKEND_CONTRACT).
        if (!logListingAuthDeniedRef.current) {
          gatedDetailFeedGenerationRef.current += 1;
        }
        logListingAuthDeniedRef.current = true;
        selectedStreamsRef.current = [];
        // State (not only the ref) so the live-stream effect tears down the
        // still-open EventSource instead of leaving it connected under a true
        // workspace_logs capability gate.
        setLogListingAuthDenied(true);
        setSelectedStreams([]);
        setLogEntries([]);
        setStreamOffsets({});
      } else if (streams?.ok) {
        logListingAuthDeniedRef.current = false;
        setLogListingAuthDenied(false);
        logStreamActivityRef.current = updateLogStreamActivity(
          logStreamActivityRef.current,
          workspaceId,
          streams.data.items,
        );
        setSelectedStreams((current) => {
          return pickWorkspaceLogStreams(streams.data.items, current);
        });
      }
    } finally {
      // A superseded explicit refresh, selection change, or post-mutation load
      // must not clear the latch while that newer request is still in flight.
      if (generation === workspaceDetailRequestGenerationRef.current) {
        workspaceDetailLoadInFlightRef.current = false;
      }
    }
  }, [
    authorizedFeedEpochRef,
    capabilities,
    gatedDetailFeedGenerationRef,
    logListingAuthDeniedRef,
    logStreamActivityRef,
    setLogListingAuthDenied,
    selectedIdRef,
    selectedStreamsRef,
    setDetail,
    setError,
    setLogEntries,
    setSelectedStreams,
    setStreamOffsets,
  ]);

  const loadSelectedWorkspace = useCallback(() => {
    if (!selectedId) {
      return;
    }
    return loadWorkspace(selectedId);
  }, [loadWorkspace, selectedId]);

  // Selection changes restart this load immediately so the new workspace
  // supersedes an in-flight detail request. Periodic ticks skip while that
  // request is slower than pollMs.
  useSerializedPeriodicLoad(
    selectedId !== null,
    loadSelectedWorkspace,
    workspaceDetailLoadInFlightRef,
    selectedId ?? "",
  );

  return { loadWorkspace };
}
