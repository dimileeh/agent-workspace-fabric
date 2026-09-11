import type { Dispatch, MutableRefObject, SetStateAction } from "react";

import type { DetailFeedName } from "@/hooks/workspace-detail-feed-settlement";

type OptionalFeedWithdrawal = {
  runtime: boolean;
  operations: boolean;
  events: boolean;
};

type SettledDetailOutage = { generation: number; message: string };

export type OptionalFeedReleaseDeps = {
  revokedRuntimeGenerationRef: MutableRefObject<number>;
  appliedRuntimeGenerationRef: MutableRefObject<number>;
  revokedOperationsGenerationRef: MutableRefObject<number>;
  appliedOperationsGenerationRef: MutableRefObject<number>;
  settledDetailOutagesRef: MutableRefObject<Partial<Record<DetailFeedName, SettledDetailOutage>>>;
  workspaceDetailRequestGenerationRef: MutableRefObject<number>;
  runtimeDenialReleasedThroughRef: MutableRefObject<number>;
  operationsDenialReleasedThroughRef: MutableRefObject<number>;
  eventsOutageReleasedThroughRef: MutableRefObject<number>;
  appliedWorkspaceDetailGenerationRef: MutableRefObject<number>;
  appliedEventFeedGenerationRef: MutableRefObject<number>;
  appliedLogListingGenerationRef: MutableRefObject<number>;
  appliedDetailFailureGenerationRef: MutableRefObject<number>;
  workspaceDetailAuthDeniedRef: MutableRefObject<boolean>;
  eventFeedAuthDeniedRef: MutableRefObject<boolean>;
  logListingAuthDeniedRef: MutableRefObject<boolean>;
  setError: Dispatch<SetStateAction<string | null>>;
};

/**
 * Same-identity withdrawal (and capabilities 404) drops the feed with no
 * later /runtime, /operations, or /events read that can recover a settled
 * 401/403 or 5xx. Zero the watermark the way event and log withdrawal clear
 * their latches, drop that feed's settled outage, and republish any still-
 * advertised sibling warning. Otherwise runtimeAuthDenialHeld /
 * operationsAuthDenialHeld or the withdrawn 5xx keeps the inspector banner
 * until the workspace changes, and later advertised-feed outages stay hidden.
 *
 * Extracted from use-workspace-detail-loader.ts for the first-party 1500-line guard.
 */
export function releaseWithdrawnOptionalDetailFeeds(
  feeds: OptionalFeedWithdrawal,
  deps: OptionalFeedReleaseDeps,
): void {
  if (!feeds.runtime && !feeds.operations && !feeds.events) {
    return;
  }
  const {
    revokedRuntimeGenerationRef,
    appliedRuntimeGenerationRef,
    revokedOperationsGenerationRef,
    appliedOperationsGenerationRef,
    settledDetailOutagesRef,
    workspaceDetailRequestGenerationRef,
    runtimeDenialReleasedThroughRef,
    operationsDenialReleasedThroughRef,
    eventsOutageReleasedThroughRef,
    appliedWorkspaceDetailGenerationRef,
    appliedEventFeedGenerationRef,
    appliedLogListingGenerationRef,
    appliedDetailFailureGenerationRef,
    workspaceDetailAuthDeniedRef,
    eventFeedAuthDeniedRef,
    logListingAuthDeniedRef,
    setError,
  } = deps;
  const runtimeHeld =
    revokedRuntimeGenerationRef.current > 0 &&
    appliedRuntimeGenerationRef.current <= revokedRuntimeGenerationRef.current;
  const operationsHeld =
    revokedOperationsGenerationRef.current > 0 &&
    appliedOperationsGenerationRef.current <= revokedOperationsGenerationRef.current;
  const runtimeOutage = feeds.runtime ? settledDetailOutagesRef.current.runtime : undefined;
  const operationsOutage = feeds.operations
    ? settledDetailOutagesRef.current.operations
    : undefined;
  const eventsOutage = feeds.events ? settledDetailOutagesRef.current.events : undefined;
  const releasedRuntimeOutage = runtimeOutage != null;
  const releasedOperationsOutage = operationsOutage != null;
  const releasedEventsOutage = eventsOutage != null;
  const releasedOutageGeneration = Math.max(
    runtimeOutage?.generation ?? 0,
    operationsOutage?.generation ?? 0,
    eventsOutage?.generation ?? 0,
  );
  const releasedThrough = workspaceDetailRequestGenerationRef.current;
  if (feeds.runtime) {
    runtimeDenialReleasedThroughRef.current = Math.max(
      runtimeDenialReleasedThroughRef.current,
      releasedThrough,
    );
    revokedRuntimeGenerationRef.current = 0;
    delete settledDetailOutagesRef.current.runtime;
  }
  if (feeds.operations) {
    operationsDenialReleasedThroughRef.current = Math.max(
      operationsDenialReleasedThroughRef.current,
      releasedThrough,
    );
    revokedOperationsGenerationRef.current = 0;
    delete settledDetailOutagesRef.current.operations;
  }
  if (feeds.events) {
    eventsOutageReleasedThroughRef.current = Math.max(
      eventsOutageReleasedThroughRef.current,
      releasedThrough,
    );
    delete settledDetailOutagesRef.current.events;
  }
  const withdrawnDenialHeld =
    (feeds.runtime && runtimeHeld) || (feeds.operations && operationsHeld);
  const runtimeStillHeld = !feeds.runtime && runtimeHeld;
  const operationsStillHeld = !feeds.operations && operationsHeld;
  const appliedGeneration = {
    workspace: appliedWorkspaceDetailGenerationRef.current,
    runtime: appliedRuntimeGenerationRef.current,
    events: appliedEventFeedGenerationRef.current,
    operations: appliedOperationsGenerationRef.current,
    logs: appliedLogListingGenerationRef.current,
  };
  const order = ["workspace", "runtime", "events", "operations", "logs"] as const;
  const remainingOutageEligible = (
    feed: (typeof order)[number],
    record: { generation: number; message: string },
  ) => {
    if (record.generation < appliedGeneration[feed]) {
      return false;
    }
    // Same fences as preferredOutstandingOutage. A withdrawn feed is
    // deleted above; a leftover record at or below its release watermark
    // must not outrank a still-advertised newer failure.
    if (
      feed === "runtime" &&
      record.generation <= runtimeDenialReleasedThroughRef.current
    ) {
      return false;
    }
    if (
      feed === "operations" &&
      record.generation <= operationsDenialReleasedThroughRef.current
    ) {
      return false;
    }
    if (
      feed === "events" &&
      record.generation <= eventsOutageReleasedThroughRef.current
    ) {
      return false;
    }
    return true;
  };
  // Two-pass, matching preferredOutstandingOutage: discover the newest
  // eligible generation, then take that generation's first feed message.
  // Pairing message with the first eligible feed hides a newer logs
  // outage behind an older workspace warning after this withdrawal,
  // including when workspace recovery hangs.
  let highest = -1;
  for (const feed of order) {
    const record = settledDetailOutagesRef.current[feed];
    if (record == null || !remainingOutageEligible(feed, record)) {
      continue;
    }
    if (record.generation > highest) {
      highest = record.generation;
    }
  }
  let message: string | null = null;
  if (highest >= 0) {
    for (const feed of order) {
      const record = settledDetailOutagesRef.current[feed];
      if (
        record == null ||
        record.generation !== highest ||
        !remainingOutageEligible(feed, record)
      ) {
        continue;
      }
      message = record.message;
      break;
    }
  }
  const remaining = { highest: Math.max(highest, 0), message };
  // A withdrawn 5xx must not keep the failure watermark above a still-
  // advertised sibling that settles later. Leave a newer advertised
  // warning's watermark alone.
  if (
    (releasedRuntimeOutage || releasedOperationsOutage || releasedEventsOutage) &&
    releasedOutageGeneration >= appliedDetailFailureGenerationRef.current
  ) {
    appliedDetailFailureGenerationRef.current = remaining.highest;
  }
  if (
    (withdrawnDenialHeld ||
      releasedRuntimeOutage ||
      releasedOperationsOutage ||
      releasedEventsOutage) &&
    !workspaceDetailAuthDeniedRef.current &&
    !eventFeedAuthDeniedRef.current &&
    !logListingAuthDeniedRef.current &&
    !runtimeStillHeld &&
    !operationsStillHeld
  ) {
    setError((current) => {
      if (
        workspaceDetailAuthDeniedRef.current ||
        eventFeedAuthDeniedRef.current ||
        logListingAuthDeniedRef.current ||
        (revokedRuntimeGenerationRef.current > 0 &&
          appliedRuntimeGenerationRef.current <= revokedRuntimeGenerationRef.current) ||
        (revokedOperationsGenerationRef.current > 0 &&
          appliedOperationsGenerationRef.current <= revokedOperationsGenerationRef.current)
      ) {
        return current;
      }
      return remaining.message;
    });
  }
}
