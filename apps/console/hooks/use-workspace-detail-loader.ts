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
import {
  DROP_ALL_GATED_DETAIL_FEEDS,
  allGatedDetailFeedsDropped,
  gatedDetailDropsSince,
  noteGatedDetailDrop,
  type GatedDetailDropStamp,
} from "@/lib/console-dashboard-derived";
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
  gatedDetailDroppedFeedsRef: MutableRefObject<GatedDetailDropStamp[]>;
  logStreamActivityRef: MutableRefObject<LogStreamActivityMap>;
  selectedStreamsRef: MutableRefObject<string[]>;
  logListingAuthDeniedRef: MutableRefObject<boolean>;
  setLogListingAuthDenied: Dispatch<SetStateAction<boolean>>;
  workspaceDetailAuthDeniedRef: MutableRefObject<boolean>;
  setWorkspaceDetailAuthDenied: Dispatch<SetStateAction<boolean>>;
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
 * A 401/403 on the basic /workspaces/{id} GET latches denial and closes the
 * live stream until a successful detail read recovers it. That denial is
 * authoritative unless a newer successful GET has already applied — a newer
 * request merely starting, hanging, or failing transiently is not recovery.
 * Snapshot frames must not write revoked workspace metadata back while that
 * latch is held.
 * A gated-detail generation bump drops the union of every stamp recorded after
 * this load captured generation (all of them on capabilities 404). The basic
 * workspace GET still applies. Failures from diagnostics that remain advertised
 * are kept.
 */
export function useWorkspaceDetailLoader({
  selectedId,
  selectedIdRef,
  capabilities,
  authorizedFeedEpochRef,
  gatedDetailFeedGenerationRef,
  gatedDetailDroppedFeedsRef,
  logStreamActivityRef,
  selectedStreamsRef,
  logListingAuthDeniedRef,
  setLogListingAuthDenied,
  workspaceDetailAuthDeniedRef,
  setWorkspaceDetailAuthDenied,
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
  // Highest detail generation that applied a successful /workspaces/{id} GET.
  // An older 401/403 must not clear workspace metadata this newer success
  // already owns, and must not close a stream that recovery already reopened.
  const appliedWorkspaceDetailGenerationRef = useRef(0);
  // Highest detail generation covered by an applied base-detail 401/403. An
  // older or in-flight 200 (started before that denial) must not restore
  // revoked workspace metadata or leave /stream open. A request that starts
  // after this watermark may recover. Re-applying a denial already inside
  // this window must not raise the watermark.
  const revokedWorkspaceDetailGenerationRef = useRef(0);
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
      let allowRuntime = allowDetail("workspace_runtime");
      let allowEvents = allowDetail("workspace_events");
      let allowOperations = allowDetail("workspace_operations");
      let { allowLogs } = resolveWorkspaceLogStreamAccess(caps);

      const [workspace, fetchedRuntime, fetchedEvents, fetchedOperations, fetchedStreams] =
        await Promise.all([
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
      let runtime = fetchedRuntime;
      let events = fetchedEvents;
      let operations = fetchedOperations;
      let streams = fetchedStreams;

      // Accept the full envelope (including listing success and gated-off null)
      // so the post-merge listing check can call this without a false-only cast.
      const feedAuthDenied = (result: ApiEnvelope<unknown> | null | undefined) =>
        result != null && result.ok === false && (result.status === 401 || result.status === 403);

      // Base-detail 401/403 clears detail.workspace but the inspector EventSource
      // stays authorized unless we latch it. Snapshot frames write frame.workspace
      // straight back. State (not only the ref) re-runs the live-stream effect
      // so the source is closed until a successful GET recovers it.
      const publishWorkspaceDetailAuthDenied = (denied: boolean) => {
        workspaceDetailAuthDeniedRef.current = denied;
        setWorkspaceDetailAuthDenied(denied);
      };

      const publishWorkspaceDetailRecovered = () => {
        appliedWorkspaceDetailGenerationRef.current = Math.max(
          appliedWorkspaceDetailGenerationRef.current,
          generation,
        );
        publishWorkspaceDetailAuthDenied(false);
      };

      // Apply even if a newer request has started but has not yet established
      // recovery. A newer request merely starting, hanging, or failing
      // transiently is not recovery — dropping this denial leaves /stream open
      // and lets snapshot frames write revoked workspace metadata back.
      const applyAuthoritativeWorkspaceDetailDenial = (deniedGeneration: number, message: string) => {
        if (epoch !== authorizedFeedEpochRef.current || selectedIdRef.current !== workspaceId) {
          return false;
        }
        // A newer successful detail GET already owns the inspector.
        if (deniedGeneration < appliedWorkspaceDetailGenerationRef.current) {
          return false;
        }
        // This request started inside an already-applied denial window.
        // Raising the watermark here would reject a recovery that started
        // after the original denial.
        if (
          workspaceDetailAuthDeniedRef.current &&
          deniedGeneration <= revokedWorkspaceDetailGenerationRef.current
        ) {
          return false;
        }
        // Cover every detail request that has already started so an in-flight
        // refresh cannot restore cleared workspace metadata. A request that
        // starts after this watermark may recover.
        revokedWorkspaceDetailGenerationRef.current = Math.max(
          revokedWorkspaceDetailGenerationRef.current,
          workspaceDetailRequestGenerationRef.current,
        );
        publishWorkspaceDetailAuthDenied(true);
        setError(message);
        setDetail((current) => {
          // A recovery GET may land between this denial and the updater.
          if (!workspaceDetailAuthDeniedRef.current) {
            return current;
          }
          return {
            ...current,
            workspace: null,
          };
        });
        return true;
      };

      let denialApplied = false;
      if (!workspace.ok && feedAuthDenied(workspace)) {
        denialApplied = applyAuthoritativeWorkspaceDetailDenial(generation, workspace.message);
      }

      if (
        epoch !== authorizedFeedEpochRef.current ||
        generation !== workspaceDetailRequestGenerationRef.current ||
        selectedIdRef.current !== workspaceId
      ) {
        return;
      }

      // A newer base-detail 401/403 already covers this generation. Do not
      // restore revoked workspace metadata or replace the denial with a
      // transient failure. The request that just applied the denial continues
      // so remaining latest-generation feed writes stay consistent.
      if (generation <= revokedWorkspaceDetailGenerationRef.current && !denialApplied) {
        return;
      }

      // Capabilities 404 / auth revocation bump gatedDetailFeedGenerationRef and
      // drop every optional inspector feed. The basic /workspaces/{id} GET is not
      // gated — apply it when that full drop is the only change. Otherwise a
      // persistent 404 poll discards every overlapping detail load and the
      // inspector stays empty (CONSOLE_BACKEND_CONTRACT).
      // Union every stamp after this load's captured generation. A later partial
      // withdrawal must not replace an earlier DROP_ALL or inspector drop, or
      // withdrawn feeds are written back. Still-advertised failures remain the
      // detail error; deriving that solely from the workspace GET presents a
      // retained snapshot as current.
      if (gatedGeneration !== gatedDetailFeedGenerationRef.current) {
        const dropped = gatedDetailDropsSince(gatedDetailDroppedFeedsRef.current, gatedGeneration);
        if (allGatedDetailFeedsDropped(dropped)) {
          if (workspace.ok) {
            setError(null);
            publishWorkspaceDetailRecovered();
          } else if (feedAuthDenied(workspace)) {
            setError(workspace.message);
            publishWorkspaceDetailAuthDenied(true);
          } else if (!workspaceDetailAuthDeniedRef.current) {
            // A transient failure is not recovery. Keep the latched denial
            // banner so a newer 5xx cannot hide the revocation.
            setError(workspace.message);
          }
          setDetail((current) => ({
            ...current,
            workspace: workspace.ok
              ? {
                  ...workspace.data,
                  lifecycle: workspace.data.lifecycle ?? [],
                  llm_usage: fallbackLlmUsage(workspace.data.llm_usage),
                  recovery: workspace.data.recovery ?? null,
                }
              : feedAuthDenied(workspace)
                ? null
                : current.workspace,
          }));
          return;
        }
        if (dropped.runtime) {
          allowRuntime = false;
          runtime = null;
        }
        if (dropped.events) {
          allowEvents = false;
          events = null;
        }
        if (dropped.operations) {
          allowOperations = false;
          operations = null;
        }
        if (dropped.logs) {
          allowLogs = false;
          streams = null;
        }
      }

      const firstFailure = [workspace, runtime, events, operations, streams].find(
        (item) => item != null && !item.ok,
      );
      // A successful /workspaces/{id} GET is recovery. Clear the latch before
      // the error update so the denial banner does not outlive the read.
      // Latch before setDetail so a snapshot frame cannot restore workspace
      // between this apply and the live-stream effect cleanup.
      if (workspace.ok) {
        publishWorkspaceDetailRecovered();
      } else if (feedAuthDenied(workspace)) {
        publishWorkspaceDetailAuthDenied(true);
      }

      // This setter is the workspace-detail slot only. Do not clear overview
      // errors here — an independent overview success must not clear this
      // warning either (CONSOLE_BACKEND_CONTRACT).
      if (firstFailure && !firstFailure.ok) {
        // A latched base-detail 401/403 owns this banner. A newer request that
        // only hangs or fails transiently is not recovery and must not replace
        // the revocation reason.
        if (!workspaceDetailAuthDeniedRef.current || feedAuthDenied(workspace)) {
          setError(firstFailure.message);
        }
      } else {
        setError(null);
      }

      // Gated-off feeds resolve to null and clear; transient network/5xx keep
      // last-successful inspector snapshots while the error banner stays visible
      // (CONSOLE_BACKEND_CONTRACT). Feed-level 401/403 drops that feed's cache.
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
          noteGatedDetailDrop(
            gatedDetailDroppedFeedsRef,
            gatedDetailFeedGenerationRef,
            DROP_ALL_GATED_DETAIL_FEEDS,
          );
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
    gatedDetailDroppedFeedsRef,
    gatedDetailFeedGenerationRef,
    logListingAuthDeniedRef,
    setWorkspaceDetailAuthDenied,
    workspaceDetailAuthDeniedRef,
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
