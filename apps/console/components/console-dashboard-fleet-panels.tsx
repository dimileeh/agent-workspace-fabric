"use client";

import type {
  CloudRuntimeSummary,
  FailureSummaryResponse,
  MergeQueueItem,
  ResourceSaturationSummary,
  WorkspaceReliabilitySummary,
} from "@/lib/types";
import {
  CloudRuntimePanel,
  MergeQueuePanel,
  ReliabilityPanel,
  ResourceCapacityPanel,
} from "./console-dashboard-capacity";
import { FailureAnalysisPanel } from "./console-dashboard-security";
import type { MergeQueueStatus } from "./console-dashboard-shared";

type ConsoleDashboardFleetPanelsProps = {
  showCapacitySection: boolean;
  showReliability: boolean;
  showResourceCapacity: boolean;
  showCloudRuntime: boolean;
  showMergeQueue: boolean;
  showFailures: boolean;
  workspaceSummary: WorkspaceReliabilitySummary | null;
  workspaceSummaryError: string | null;
  summaryStale: boolean;
  resourceSaturation: ResourceSaturationSummary | null;
  resourceError: string | null;
  capacityStale: boolean;
  cloudRuntime: CloudRuntimeSummary | null;
  cloudRuntimeError: string | null;
  cloudRuntimeStale: boolean;
  mergeQueue: MergeQueueItem[];
  mergeQueueHasMore: boolean;
  mergeQueueStatus: MergeQueueStatus;
  mergeQueueError: string | null;
  mergeStale: boolean;
  failureSummary: FailureSummaryResponse | null;
  failureSummaryStatus: "loading" | "success" | "error" | "unavailable";
  failureSummaryError: string | null;
  failureStale: boolean;
};

/**
 * Fleet diagnostic widgets. The 2xl two-column track exists only to place
 * capacity beside merge-queue. Without capacity, stay one column and use
 * normal flow so the advertised queue keeps the full row instead of collapsing
 * at the absolute overlay breakpoint or occupying only the first track.
 */
export function ConsoleDashboardFleetPanels(props: ConsoleDashboardFleetPanelsProps) {
  const {
    showCapacitySection,
    showReliability,
    showResourceCapacity,
    showCloudRuntime,
    showMergeQueue,
    showFailures,
    workspaceSummary,
    workspaceSummaryError,
    summaryStale,
    resourceSaturation,
    resourceError,
    capacityStale,
    cloudRuntime,
    cloudRuntimeError,
    cloudRuntimeStale,
    mergeQueue,
    mergeQueueHasMore,
    mergeQueueStatus,
    mergeQueueError,
    mergeStale,
    failureSummary,
    failureSummaryStatus,
    failureSummaryError,
    failureStale,
  } = props;

  return (
    <div
      className={
        showCapacitySection
          ? "grid min-w-0 gap-4 p-4 pb-0 2xl:grid-cols-[minmax(0,1fr)_minmax(460px,0.85fr)]"
          : "grid min-w-0 gap-4 p-4 pb-0"
      }
    >
      {showCapacitySection ? (
        <div id="awf-capacity" className="min-w-0 scroll-mt-14 grid gap-4">
          {showReliability ? (
            <ReliabilityPanel
              workspaceSummary={workspaceSummary}
              error={workspaceSummaryError}
              stale={summaryStale}
            />
          ) : null}
          {showResourceCapacity ? (
            <ResourceCapacityPanel
              saturation={resourceSaturation}
              error={resourceError}
              stale={capacityStale}
            />
          ) : null}
          {showCloudRuntime ? (
            <CloudRuntimePanel
              summary={cloudRuntime}
              error={cloudRuntimeError}
              stale={cloudRuntimeStale}
            />
          ) : null}
        </div>
      ) : null}
      {/* 2xl + capacity: overlay the cell (absolute) so the long merge
          list never drives the row height — Capacity sets the height and
          the list scrolls to fill it. Without a capacity column there is
          nothing to size the row, so stay in normal flow. Below 2xl it is
          always normal flow. */}
      {showMergeQueue ? (
        <div
          id="awf-merge-queue"
          className={
            showCapacitySection
              ? "min-w-0 scroll-mt-14 2xl:relative"
              : "min-w-0 scroll-mt-14"
          }
        >
          <div className={showCapacitySection ? "2xl:absolute 2xl:inset-0" : undefined}>
            <MergeQueuePanel
              items={mergeQueue}
              hasMore={mergeQueueHasMore}
              status={mergeQueueStatus}
              error={mergeQueueError}
              stale={mergeStale}
            />
          </div>
        </div>
      ) : null}
      {showFailures ? (
        <div id="awf-failures" className="scroll-mt-14 2xl:col-span-2">
          <FailureAnalysisPanel
            summary={failureSummary}
            status={failureSummaryStatus}
            error={failureSummaryError}
            stale={failureStale}
          />
        </div>
      ) : null}
    </div>
  );
}
