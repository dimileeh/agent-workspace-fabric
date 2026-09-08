"use client";

import { useCallback, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import {
  capabilityRouteToAwfPath,
  isDiagnosticAvailable,
  isWidgetAvailable,
  widgetRoute,
} from "@/lib/console-capabilities";
import { parseCloudRuntimeSummary } from "@/lib/console-cloud-runtime";
import { parseDashboardSummary } from "@/lib/console-dashboard-summary";
import { awfPath } from "@/lib/console-urls";
import type {
  CloudRuntimeSummary,
  ConsoleCapabilities,
  ConsoleDashboardSummary,
  FailureSummaryResponse,
  ListEnvelope,
  MergeQueueItem,
  ResourceSaturationSummary,
  WorkspaceReliabilitySummary,
} from "@/lib/types";
import {
  type MergeQueueStatus,
  apiGet,
  fallbackResourceSaturation,
  mergeQueueLimit,
} from "@/components/console-dashboard-shared";

type UseConsoleFleetFeedsArgs = {
  capabilities: ConsoleCapabilities | null;
  authorizedFeedEpochRef: MutableRefObject<number>;
  gatedDetailFeedGenerationRef: MutableRefObject<number>;
  dashboardSummaryRequestGenerationRef: MutableRefObject<number>;
  cloudRuntimeRequestGenerationRef: MutableRefObject<number>;
  mergeQueueRequestGenerationRef: MutableRefObject<number>;
  resourceSaturationRequestGenerationRef: MutableRefObject<number>;
  workspaceSummaryRequestGenerationRef: MutableRefObject<number>;
  failureSummaryRequestGenerationRef: MutableRefObject<number>;
  setResourceSaturation: Dispatch<SetStateAction<ResourceSaturationSummary | null>>;
  setResourceError: Dispatch<SetStateAction<string | null>>;
  setDashboardSummary: Dispatch<SetStateAction<ConsoleDashboardSummary | null>>;
  setDashboardSummaryError: Dispatch<SetStateAction<string | null>>;
  setCloudRuntime: Dispatch<SetStateAction<CloudRuntimeSummary | null>>;
  setCloudRuntimeError: Dispatch<SetStateAction<string | null>>;
  setWorkspaceSummary: Dispatch<SetStateAction<WorkspaceReliabilitySummary | null>>;
  setWorkspaceSummaryError: Dispatch<SetStateAction<string | null>>;
  setMergeQueue: Dispatch<SetStateAction<MergeQueueItem[]>>;
  setMergeQueueHasMore: Dispatch<SetStateAction<boolean>>;
  setMergeQueueStatus: Dispatch<SetStateAction<MergeQueueStatus>>;
  setMergeQueueError: Dispatch<SetStateAction<string | null>>;
  setFailureSummary: Dispatch<SetStateAction<FailureSummaryResponse | null>>;
  setFailureSummaryStatus: Dispatch<SetStateAction<"loading" | "success" | "error" | "unavailable">>;
  setFailureSummaryError: Dispatch<SetStateAction<string | null>>;
};

/**
 * Capability-gated fleet snapshots (summary, capacity, runtime, reliability,
 * merge queue, failures). Extracted from console-dashboard.tsx for the
 * first-party 1500-line guard. Request-generation refs stay owned by the
 * dashboard so auth clear and same-identity withdrawal can still bump them.
 */
export function useConsoleFleetFeeds({
  capabilities,
  authorizedFeedEpochRef,
  gatedDetailFeedGenerationRef,
  dashboardSummaryRequestGenerationRef,
  cloudRuntimeRequestGenerationRef,
  mergeQueueRequestGenerationRef,
  resourceSaturationRequestGenerationRef,
  workspaceSummaryRequestGenerationRef,
  failureSummaryRequestGenerationRef,
  setResourceSaturation,
  setResourceError,
  setDashboardSummary,
  setDashboardSummaryError,
  setCloudRuntime,
  setCloudRuntimeError,
  setWorkspaceSummary,
  setWorkspaceSummaryError,
  setMergeQueue,
  setMergeQueueHasMore,
  setMergeQueueStatus,
  setMergeQueueError,
  setFailureSummary,
  setFailureSummaryStatus,
  setFailureSummaryError,
}: UseConsoleFleetFeedsArgs) {
  const loadResourceSaturation = useCallback(async () => {
    const epoch = authorizedFeedEpochRef.current;
    const gatedGeneration = gatedDetailFeedGenerationRef.current;
    const generation = ++resourceSaturationRequestGenerationRef.current;
    const result = await apiGet<ResourceSaturationSummary>(awfPath("metrics/resources/saturation"));
    if (
      epoch !== authorizedFeedEpochRef.current ||
      gatedGeneration !== gatedDetailFeedGenerationRef.current ||
      generation !== resourceSaturationRequestGenerationRef.current
    ) {
      return;
    }
    if (!result.ok) {
      // Feed-level 401/403 is auth revocation for this snapshot, not a transient
      // outage: drop last-good saturation even when capabilities still negotiate
      // (CONSOLE_BACKEND_CONTRACT). Do not call clearAuthorizedConsoleFeeds —
      // capabilities may still succeed and would thrash overview refill.
      if (result.status === 401 || result.status === 403) {
        setResourceSaturation(null);
        setResourceError(result.message);
        return;
      }
      setResourceError(result.message);
      return;
    }
    setResourceError(null);
    setResourceSaturation(fallbackResourceSaturation(result.data));
  }, [authorizedFeedEpochRef, gatedDetailFeedGenerationRef, resourceSaturationRequestGenerationRef, setResourceError, setResourceSaturation]);

  const loadDashboardSummary = useCallback(async (caps?: ConsoleCapabilities | null) => {
    const epoch = authorizedFeedEpochRef.current;
    const generation = ++dashboardSummaryRequestGenerationRef.current;
    const active = caps ?? capabilities;
    const route = widgetRoute(active, "fleet_summary");
    const path = route
      ? capabilityRouteToAwfPath(route)
      : awfPath("console/dashboard-summary");
    const result = await apiGet<ConsoleDashboardSummary>(path);
    if (
      epoch !== authorizedFeedEpochRef.current ||
      generation !== dashboardSummaryRequestGenerationRef.current
    ) {
      return;
    }
    if (!result.ok) {
      // Feed-level 401/403 is auth revocation for this snapshot, not a transient
      // outage: drop last-good counters even when capabilities still negotiate
      // (CONSOLE_BACKEND_CONTRACT). Do not call clearAuthorizedConsoleFeeds —
      // capabilities may still succeed and would thrash overview refill.
      if (result.status === 401 || result.status === 403) {
        setDashboardSummary(null);
        setDashboardSummaryError(result.message);
        return;
      }
      setDashboardSummaryError(result.message);
      return;
    }
    const parsed = parseDashboardSummary(result.data, active?.backend_kind ?? null);
    if (!parsed) {
      setDashboardSummaryError("Dashboard summary payload malformed.");
      return;
    }
    setDashboardSummaryError(null);
    setDashboardSummary(parsed);
  }, [authorizedFeedEpochRef, capabilities, dashboardSummaryRequestGenerationRef, setDashboardSummary, setDashboardSummaryError]);

  const loadCloudRuntime = useCallback(async (caps?: ConsoleCapabilities | null) => {
    const epoch = authorizedFeedEpochRef.current;
    const generation = ++cloudRuntimeRequestGenerationRef.current;
    const active = caps ?? capabilities;
    const route = widgetRoute(active, "cloud_runtime");
    if (!route) {
      return;
    }
    const result = await apiGet<CloudRuntimeSummary>(capabilityRouteToAwfPath(route));
    if (
      epoch !== authorizedFeedEpochRef.current ||
      generation !== cloudRuntimeRequestGenerationRef.current
    ) {
      return;
    }
    if (!result.ok) {
      // Feed-level 401/403 is auth revocation for this snapshot, not a transient
      // outage: drop last-good cloud runtime facts even when capabilities still
      // negotiate (CONSOLE_BACKEND_CONTRACT). Do not call clearAuthorizedConsoleFeeds —
      // capabilities may still succeed and would thrash overview refill.
      if (result.status === 401 || result.status === 403) {
        setCloudRuntime(null);
        setCloudRuntimeError(result.message);
        return;
      }
      setCloudRuntimeError(result.message);
      return;
    }
    const parsed = parseCloudRuntimeSummary(result.data);
    if (!parsed) {
      setCloudRuntimeError("Cloud runtime payload malformed.");
      return;
    }
    setCloudRuntimeError(null);
    setCloudRuntime(parsed);
  }, [authorizedFeedEpochRef, capabilities, cloudRuntimeRequestGenerationRef, setCloudRuntime, setCloudRuntimeError]);

  const loadWorkspaceSummary = useCallback(async () => {
    const epoch = authorizedFeedEpochRef.current;
    const gatedGeneration = gatedDetailFeedGenerationRef.current;
    const generation = ++workspaceSummaryRequestGenerationRef.current;
    const result = await apiGet<WorkspaceReliabilitySummary>(awfPath("metrics/workspaces/summary"));
    if (
      epoch !== authorizedFeedEpochRef.current ||
      gatedGeneration !== gatedDetailFeedGenerationRef.current ||
      generation !== workspaceSummaryRequestGenerationRef.current
    ) {
      return;
    }
    if (!result.ok) {
      // Feed-level 401/403 is auth revocation for this snapshot, not a transient
      // outage: drop last-good reliability facts even when capabilities still
      // negotiate (CONSOLE_BACKEND_CONTRACT). Do not call clearAuthorizedConsoleFeeds —
      // capabilities may still succeed and would thrash overview refill.
      if (result.status === 401 || result.status === 403) {
        setWorkspaceSummary(null);
        setWorkspaceSummaryError(result.message);
        return;
      }
      setWorkspaceSummaryError(result.message);
      return;
    }
    setWorkspaceSummaryError(null);
    setWorkspaceSummary(result.data);
  }, [authorizedFeedEpochRef, gatedDetailFeedGenerationRef, setWorkspaceSummary, setWorkspaceSummaryError, workspaceSummaryRequestGenerationRef]);

  const loadMergeQueue = useCallback(async () => {
    const epoch = authorizedFeedEpochRef.current;
    const gatedGeneration = gatedDetailFeedGenerationRef.current;
    const generation = ++mergeQueueRequestGenerationRef.current;
    const result = await apiGet<ListEnvelope<MergeQueueItem>>(
      awfPath("merge-queue", { limit: mergeQueueLimit }),
    );
    if (
      epoch !== authorizedFeedEpochRef.current ||
      gatedGeneration !== gatedDetailFeedGenerationRef.current ||
      generation !== mergeQueueRequestGenerationRef.current
    ) {
      return;
    }
    if (!result.ok) {
      // Feed-level 401/403 is auth revocation for this snapshot, not a transient
      // outage: drop last-good queue rows even when capabilities still negotiate
      // (CONSOLE_BACKEND_CONTRACT). Do not call clearAuthorizedConsoleFeeds —
      // capabilities may still succeed and would thrash overview refill.
      if (result.status === 401 || result.status === 403) {
        setMergeQueue([]);
        setMergeQueueHasMore(false);
        setMergeQueueError(result.message);
        setMergeQueueStatus("error");
        return;
      }
      setMergeQueueError(result.message);
      setMergeQueueStatus("error");
      return;
    }
    setMergeQueueError(null);
    setMergeQueue(result.data.items);
    setMergeQueueHasMore(result.data.has_more);
    setMergeQueueStatus("success");
  }, [authorizedFeedEpochRef, gatedDetailFeedGenerationRef, mergeQueueRequestGenerationRef, setMergeQueue, setMergeQueueError, setMergeQueueHasMore, setMergeQueueStatus]);

  const loadFailureSummary = useCallback(async () => {
    const epoch = authorizedFeedEpochRef.current;
    const gatedGeneration = gatedDetailFeedGenerationRef.current;
    const generation = ++failureSummaryRequestGenerationRef.current;
    const result = await apiGet<FailureSummaryResponse>(awfPath("metrics/failures/summary"));
    if (
      epoch !== authorizedFeedEpochRef.current ||
      gatedGeneration !== gatedDetailFeedGenerationRef.current ||
      generation !== failureSummaryRequestGenerationRef.current
    ) {
      return;
    }
    if (!result.ok) {
      // Feed-level 401/403 is auth revocation for this snapshot, not a transient
      // outage: drop last-good failure examples even when capabilities still
      // negotiate (CONSOLE_BACKEND_CONTRACT). Do not call clearAuthorizedConsoleFeeds —
      // capabilities may still succeed and would thrash overview refill.
      if (result.status === 401 || result.status === 403) {
        setFailureSummary(null);
        setFailureSummaryStatus("error");
        setFailureSummaryError(result.message);
        return;
      }
      // Advertised-feed 404/503 are refresh outages, not capability withdrawal.
      // Withdrawal clears via clearNewlyUnsupportedCapabilityFeeds and bumps
      // generation so this response cannot restore withdrawn data. Keep the last
      // snapshot and record the error (CONSOLE_BACKEND_CONTRACT).
      setFailureSummaryStatus("error");
      setFailureSummaryError(result.message);
      return;
    }
    setFailureSummary(result.data);
    setFailureSummaryStatus("success");
    setFailureSummaryError(null);
  }, [authorizedFeedEpochRef, failureSummaryRequestGenerationRef, gatedDetailFeedGenerationRef, setFailureSummary, setFailureSummaryError, setFailureSummaryStatus]);

  const reloadAvailableFeeds = useCallback(
    async (caps: ConsoleCapabilities | null) => {
      if (!caps) {
        return;
      }
      const loads: Promise<void>[] = [];
      if (isWidgetAvailable(caps, "fleet_summary")) {
        loads.push(loadDashboardSummary(caps));
      }
      if (isWidgetAvailable(caps, "resource_capacity")) {
        loads.push(loadResourceSaturation());
      }
      if (isWidgetAvailable(caps, "cloud_runtime")) {
        loads.push(loadCloudRuntime(caps));
      }
      if (isDiagnosticAvailable(caps, "reliability")) {
        loads.push(loadWorkspaceSummary());
      }
      if (isDiagnosticAvailable(caps, "merge_queue")) {
        loads.push(loadMergeQueue());
      }
      if (isDiagnosticAvailable(caps, "failures")) {
        loads.push(loadFailureSummary());
      }
      if (loads.length > 0) {
        await Promise.all(loads);
      }
    },
    [
      loadCloudRuntime,
      loadDashboardSummary,
      loadFailureSummary,
      loadMergeQueue,
      loadResourceSaturation,
      loadWorkspaceSummary,
    ],
  );

  return {
    loadResourceSaturation,
    loadDashboardSummary,
    loadCloudRuntime,
    loadWorkspaceSummary,
    loadMergeQueue,
    loadFailureSummary,
    reloadAvailableFeeds,
  };
}
