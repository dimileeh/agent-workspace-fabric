import type { Dispatch, MutableRefObject, SetStateAction } from "react";

import {
  allGatedDetailFeedsDropped,
  gatedDetailDropsSince,
  type GatedDetailDropStamp,
} from "@/lib/console-dashboard-derived";
import type { ApiEnvelope, ListEnvelope, Operation, WorkspaceRuntime } from "@/lib/types";
import type { DetailState } from "@/components/console-dashboard-shared";

export type DetailLoadFlags = {
  denialApplied: boolean;
  ownListingDropGeneration: number | null;
};

export type DetailLoadAccess = {
  allowRuntime: boolean;
  allowEvents: boolean;
  allowOperations: boolean;
  allowLogs: boolean;
};

export type DetailFeedName = "workspace" | "runtime" | "events" | "operations" | "logs";

type FeedAuthDenied = (result: ApiEnvelope<unknown> | null | undefined) => boolean;

export type WorkspaceDetailFeedSettlementDeps = {
  epoch: number;
  generation: number;
  visit: number;
  workspaceId: string;
  gatedGeneration: number;
  access: DetailLoadAccess;
  flags: DetailLoadFlags;
  feedAuthDenied: FeedAuthDenied;
  authorizedFeedEpochRef: MutableRefObject<number>;
  selectedIdRef: MutableRefObject<string | null>;
  workspaceDetailVisitRef: MutableRefObject<number>;
  workspaceDetailVisitGenerationFloorRef: MutableRefObject<number>;
  revokedWorkspaceDetailGenerationRef: MutableRefObject<number>;
  appliedWorkspaceDetailGenerationRef: MutableRefObject<number>;
  revokedRuntimeGenerationRef: MutableRefObject<number>;
  appliedRuntimeGenerationRef: MutableRefObject<number>;
  revokedOperationsGenerationRef: MutableRefObject<number>;
  appliedOperationsGenerationRef: MutableRefObject<number>;
  revokedEventFeedGenerationRef: MutableRefObject<number>;
  appliedEventFeedGenerationRef: MutableRefObject<number>;
  eventsOutageReleasedThroughRef: MutableRefObject<number>;
  runtimeDenialReleasedThroughRef: MutableRefObject<number>;
  operationsDenialReleasedThroughRef: MutableRefObject<number>;
  appliedLogListingGenerationRef: MutableRefObject<number>;
  revokedLogListingGenerationRef: MutableRefObject<number>;
  appliedDetailFailureGenerationRef: MutableRefObject<number>;
  settledDetailOutagesRef: MutableRefObject<
    Partial<Record<DetailFeedName, { generation: number; message: string }>>
  >;
  gatedDetailFeedGenerationRef: MutableRefObject<number>;
  gatedDetailDroppedFeedsRef: MutableRefObject<GatedDetailDropStamp[]>;
  workspaceDetailAuthDeniedRef: MutableRefObject<boolean>;
  eventFeedAuthDeniedRef: MutableRefObject<boolean>;
  logListingAuthDeniedRef: MutableRefObject<boolean>;
  workspaceDetailRequestGenerationRef: MutableRefObject<number>;
  setError: Dispatch<SetStateAction<string | null>>;
  setDetail: Dispatch<SetStateAction<DetailState>>;
};

/**
 * Runtime/operations settlement and inspector outage ownership for one detail load.
 * Extracted from use-workspace-detail-loader.ts for the first-party 1500-line guard.
 */
export function createWorkspaceDetailFeedSettlement(deps: WorkspaceDetailFeedSettlementDeps) {
  const {
    epoch,
    generation,
    visit,
    workspaceId,
    gatedGeneration,
    access,
    flags,
    feedAuthDenied,
    authorizedFeedEpochRef,
    selectedIdRef,
    workspaceDetailVisitRef,
    workspaceDetailVisitGenerationFloorRef,
    revokedWorkspaceDetailGenerationRef,
    appliedWorkspaceDetailGenerationRef,
    revokedRuntimeGenerationRef,
    appliedRuntimeGenerationRef,
    revokedOperationsGenerationRef,
    appliedOperationsGenerationRef,
    revokedEventFeedGenerationRef,
    appliedEventFeedGenerationRef,
    eventsOutageReleasedThroughRef,
    runtimeDenialReleasedThroughRef,
    operationsDenialReleasedThroughRef,
    appliedLogListingGenerationRef,
    revokedLogListingGenerationRef,
    appliedDetailFailureGenerationRef,
    settledDetailOutagesRef,
    gatedDetailFeedGenerationRef,
    gatedDetailDroppedFeedsRef,
    workspaceDetailAuthDeniedRef,
    eventFeedAuthDeniedRef,
    logListingAuthDeniedRef,
    workspaceDetailRequestGenerationRef,
    setError,
    setDetail,
  } = deps;

const optionalFeedContextCurrent = () =>
  epoch === authorizedFeedEpochRef.current &&
  selectedIdRef.current === workspaceId &&
  visit === workspaceDetailVisitRef.current &&
  generation > workspaceDetailVisitGenerationFloorRef.current &&
  (generation > revokedWorkspaceDetailGenerationRef.current || flags.denialApplied);

// Capability withdrawal (or a capabilities 404) drops the feed after this
// load captured generation. A listing 401/403 also bumps the generation
// with DROP_ALL; that stamp is this load's own, not a withdrawal, and
// must not suppress a still-advertised runtime/operations denial.
const optionalFeedStillAdvertised = (feed: "runtime" | "operations") => {
  if (gatedGeneration === gatedDetailFeedGenerationRef.current) {
    return true;
  }
  const stampsForExternalDrop =
    flags.ownListingDropGeneration == null
      ? gatedDetailDroppedFeedsRef.current
      : gatedDetailDroppedFeedsRef.current.filter(
          (stamp) => stamp.generation !== flags.ownListingDropGeneration,
        );
  const hasExternalDrop = stampsForExternalDrop.some(
    (stamp) => stamp.generation > gatedGeneration,
  );
  if (!hasExternalDrop) {
    return true;
  }
  const dropped = gatedDetailDropsSince(stampsForExternalDrop, gatedGeneration);
  if (allGatedDetailFeedsDropped(dropped)) {
    return false;
  }
  return feed === "runtime" ? !dropped.runtime : !dropped.operations;
};

const externalInspectorDrops = () => {
  if (gatedGeneration === gatedDetailFeedGenerationRef.current) {
    return null;
  }
  const stampsForExternalDrop =
    flags.ownListingDropGeneration == null
      ? gatedDetailDroppedFeedsRef.current
      : gatedDetailDroppedFeedsRef.current.filter(
          (stamp) => stamp.generation !== flags.ownListingDropGeneration,
        );
  const hasExternalDrop = stampsForExternalDrop.some(
    (stamp) => stamp.generation > gatedGeneration,
  );
  if (!hasExternalDrop) {
    return null;
  }
  return gatedDetailDropsSince(stampsForExternalDrop, gatedGeneration);
};

// A settled runtime or operations 401/403 owns the banner until a later
// successful read of that feed recovers it. A sibling network/5xx is
// not that recovery — including when another request still hangs, so
// Promise.all never restores the authorization reason.
const optionalFeedAuthDenialHeld = (
  revokedRef: MutableRefObject<number>,
  appliedRef: MutableRefObject<number>,
) => revokedRef.current > 0 && appliedRef.current <= revokedRef.current;
const runtimeAuthDenialHeld = () =>
  optionalFeedAuthDenialHeld(revokedRuntimeGenerationRef, appliedRuntimeGenerationRef);
const operationsAuthDenialHeld = () =>
  optionalFeedAuthDenialHeld(
    revokedOperationsGenerationRef,
    appliedOperationsGenerationRef,
  );

// This load's settled non-401/403 messages, in firstFailure order.
// Overlapping loads keep their own maps; the failure watermark decides
// which load owns the shared banner.
const settledTransientOutages: {
  workspace?: string;
  runtime?: string;
  events?: string;
  operations?: string;
  logs?: string;
} = {};

const detailFeedOutageSuppressed = (
  feed: "workspace" | "runtime" | "events" | "operations" | "logs",
) => {
  if (
    epoch !== authorizedFeedEpochRef.current ||
    selectedIdRef.current !== workspaceId ||
    visit !== workspaceDetailVisitRef.current ||
    generation <= workspaceDetailVisitGenerationFloorRef.current
  ) {
    return true;
  }
  // Authorization denial ownership is a display latch, not a reason to
  // drop the record. applyDetailFeedTransientOutage still stores a
  // concurrent sibling network/5xx so releaseRecoveredDetailOutage can
  // republish it after the denial clears. Discarding it here lets an
  // explicit refresh recover the denied feed, clear the banner, and
  // leave the retained snapshot looking current while a replacement
  // request hangs — serialized polling cannot resume until that hang
  // settles.
  const dropped = externalInspectorDrops();
  if (dropped != null && allGatedDetailFeedsDropped(dropped)) {
    return feed !== "workspace";
  }
  if (feed === "workspace") {
    return (
      generation < appliedWorkspaceDetailGenerationRef.current ||
      generation <= revokedWorkspaceDetailGenerationRef.current
    );
  }
  // Withdrawal leaves no later /runtime read that can clear a 5xx this
  // request started before the fence. Do not re-record it.
  if (feed === "runtime") {
    return (
      !access.allowRuntime ||
      (dropped != null && dropped.runtime) ||
      generation < appliedRuntimeGenerationRef.current ||
      generation <= revokedRuntimeGenerationRef.current ||
      generation <= runtimeDenialReleasedThroughRef.current
    );
  }
  if (feed === "events") {
    return (
      !access.allowEvents ||
      (dropped != null && dropped.events) ||
      generation < appliedEventFeedGenerationRef.current ||
      generation <= revokedEventFeedGenerationRef.current ||
      generation <= eventsOutageReleasedThroughRef.current
    );
  }
  if (feed === "operations") {
    return (
      !access.allowOperations ||
      (dropped != null && dropped.operations) ||
      generation < appliedOperationsGenerationRef.current ||
      generation <= revokedOperationsGenerationRef.current ||
      generation <= operationsDenialReleasedThroughRef.current
    );
  }
  return (
    !access.allowLogs ||
    (dropped != null && dropped.logs) ||
    generation < appliedLogListingGenerationRef.current ||
    generation <= revokedLogListingGenerationRef.current
  );
};

const appliedSuccessGeneration = (
  feed: "workspace" | "runtime" | "events" | "operations" | "logs",
) => {
  if (feed === "workspace") {
    return appliedWorkspaceDetailGenerationRef.current;
  }
  if (feed === "runtime") {
    return appliedRuntimeGenerationRef.current;
  }
  if (feed === "events") {
    return appliedEventFeedGenerationRef.current;
  }
  if (feed === "operations") {
    return appliedOperationsGenerationRef.current;
  }
  return appliedLogListingGenerationRef.current;
};

const authorizationOwnsDetailBanner = () =>
  workspaceDetailAuthDeniedRef.current ||
  eventFeedAuthDeniedRef.current ||
  logListingAuthDeniedRef.current ||
  runtimeAuthDenialHeld() ||
  operationsAuthDenialHeld();

// Cross-generation view of outstanding outages. The local map only sees
// this load; a newer success must drop a feed the older load already stamped.
const outstandingRecordEligible = (
  feed: "workspace" | "runtime" | "events" | "operations" | "logs",
  record: { generation: number; message: string },
) => {
  if (record.generation < appliedSuccessGeneration(feed)) {
    return false;
  }
  // A withdrawn runtime/operations/events 5xx must not stay the preferred
  // warning after the fence. A sibling success would otherwise republish
  // it and hide a still-advertised outage.
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

// The newest eligible failure owns the banner. Feed order only breaks
// ties within that generation, so a sibling success of the same or a
// still-newer load cannot republish a retained older runtime warning
// over a newer events outage that has not recovered.
const preferredOutstandingOutage = () => {
  const order = ["workspace", "runtime", "events", "operations", "logs"] as const;
  let newestGeneration = -1;
  for (const feed of order) {
    const record = settledDetailOutagesRef.current[feed];
    if (record == null || !outstandingRecordEligible(feed, record)) {
      continue;
    }
    if (record.generation > newestGeneration) {
      newestGeneration = record.generation;
    }
  }
  if (newestGeneration < 0) {
    return null;
  }
  for (const feed of order) {
    const record = settledDetailOutagesRef.current[feed];
    if (
      record == null ||
      record.generation !== newestGeneration ||
      !outstandingRecordEligible(feed, record)
    ) {
      continue;
    }
    return record.message;
  }
  return null;
};

const preferredSettledOutage = () => {
  const order = ["workspace", "runtime", "events", "operations", "logs"] as const;
  for (const feed of order) {
    const message = settledTransientOutages[feed];
    if (message != null && !detailFeedOutageSuppressed(feed)) {
      return message;
    }
  }
  return null;
};

// A newer failure of another feed may own the banner, but it must not
// erase this feed's record. After that newer outage recovers, the
// retained record is what preferredOutstandingOutage republishes.
const outstandingOutageNewerThan = (settledGeneration: number) => {
  const order = ["workspace", "runtime", "events", "operations", "logs"] as const;
  for (const name of order) {
    const record = settledDetailOutagesRef.current[name];
    if (record == null || record.generation <= settledGeneration) {
      continue;
    }
    if (record.generation < appliedSuccessGeneration(name)) {
      continue;
    }
    if (
      name === "runtime" &&
      record.generation <= runtimeDenialReleasedThroughRef.current
    ) {
      continue;
    }
    if (
      name === "operations" &&
      record.generation <= operationsDenialReleasedThroughRef.current
    ) {
      continue;
    }
    if (
      name === "events" &&
      record.generation <= eventsOutageReleasedThroughRef.current
    ) {
      continue;
    }
    return true;
  }
  return false;
};

// Network/5xx must warn as soon as this request settles. firstFailure
// only runs after Promise.all, and apiGet has no timeout, so a hanging
// sibling would leave the last-successful snapshot looking current.
const applyDetailFeedTransientOutage = (
  feed: "workspace" | "runtime" | "events" | "operations" | "logs",
  result: ApiEnvelope<unknown>,
) => {
  if (result.ok || feedAuthDenied(result)) {
    return;
  }
  if (detailFeedOutageSuppressed(feed)) {
    return;
  }
  // Same-feed recency only. A newer outage on a different feed must not
  // drop this failure: no later success of that other feed recovers it.
  const existing = settledDetailOutagesRef.current[feed];
  if (existing != null && generation < existing.generation) {
    return;
  }
  settledTransientOutages[feed] = result.message;
  settledDetailOutagesRef.current[feed] = { generation, message: result.message };
  appliedDetailFailureGenerationRef.current = Math.max(
    appliedDetailFailureGenerationRef.current,
    generation,
  );
  // Higher-priority denial keeps the banner. The record above is what
  // releaseRecoveredDetailOutage republishes when that denial clears,
  // including when Promise.all never runs because a sibling hangs.
  if (authorizationOwnsDetailBanner()) {
    return;
  }
  setError((current) => {
    // A newer outstanding outage owns the banner until that feed
    // recovers. A newer success of this feed already recovered the
    // snapshot; do not restamp the warning that success cleared.
    if (
      outstandingOutageNewerThan(generation) ||
      generation < appliedSuccessGeneration(feed)
    ) {
      return current;
    }
    return preferredOutstandingOutage() ?? preferredSettledOutage() ?? current;
  });
};

const publishOptionalFeedRecovered = (
  appliedRef: MutableRefObject<number>,
  revokedRef: MutableRefObject<number>,
): boolean => {
  // A denial that landed after this 200 passed the generation check owns
  // the feed. An older success must not record recovery after a newer
  // one already owns the snapshot.
  if (generation <= revokedRef.current || generation < appliedRef.current) {
    return false;
  }
  appliedRef.current = Math.max(appliedRef.current, generation);
  return true;
};

// A recovered feed must drop its outage as soon as the 200 owns the
// snapshot. Promise.all never reaches setError(null) if a sibling of
// this load hangs, so the matching success handler is the only writer
// that can clear a banner an older generation already stamped.
const releaseRecoveredDetailOutage = (
  feed: "workspace" | "runtime" | "events" | "operations" | "logs",
) => {
  const recorded = settledDetailOutagesRef.current[feed];
  if (recorded != null && recorded.generation <= generation) {
    delete settledDetailOutagesRef.current[feed];
  }
  if (authorizationOwnsDetailBanner()) {
    return;
  }
  setError((current) => {
    if (authorizationOwnsDetailBanner()) {
      return current;
    }
    // A newer outstanding outage can own the banner before this update
    // flushes. A retained older failure of another feed must still
    // publish once that newer warning is gone.
    if (outstandingOutageNewerThan(generation)) {
      return current;
    }
    return preferredOutstandingOutage();
  });
};

const applyOptionalFeedAuthDenial = (
  feed: "runtime" | "operations",
  result: ApiEnvelope<unknown>,
  appliedRef: MutableRefObject<number>,
  revokedRef: MutableRefObject<number>,
  clearFeed: (current: DetailState) => DetailState,
) => {
  if (result.ok || !feedAuthDenied(result)) {
    return;
  }
  if (feed === "runtime" ? !access.allowRuntime : !access.allowOperations) {
    return;
  }
  if (!optionalFeedContextCurrent() || !optionalFeedStillAdvertised(feed)) {
    return;
  }
  const releasedThrough =
    feed === "runtime"
      ? runtimeDenialReleasedThroughRef.current
      : operationsDenialReleasedThroughRef.current;
  // Withdrawal zeros the watermark so a basic-detail 200 can drop the
  // banner. A request that started before that release must not raise
  // it again — no later read of the withdrawn feed will recover it.
  if (generation <= releasedThrough) {
    return;
  }
  // A newer successful read already owns this snapshot. Applying the
  // older 401/403 would wipe it and stamp an error the recovered feed
  // already replaced.
  if (generation < appliedRef.current) {
    return;
  }
  // This request started inside an already-applied denial window.
  // Raising the watermark here would reject a recovery that started
  // after the original denial.
  if (revokedRef.current > 0 && generation <= revokedRef.current) {
    return;
  }
  // The first denial, and a denial after a successful recovery, covers
  // every detail request that has already started so an in-flight 200
  // cannot restore the cleared feed. A later 401 while that denial is
  // still in force must stamp only this request's generation. Raising
  // the watermark to the latest started generation would reject a newer
  // in-flight refresh that started after this recovery request.
  const denialAlreadyHeld =
    revokedRef.current > 0 && appliedRef.current <= revokedRef.current;
  const denialWatermark = denialAlreadyHeld
    ? generation
    : workspaceDetailRequestGenerationRef.current;
  revokedRef.current = Math.max(revokedRef.current, denialWatermark);
  setDetail((current) => {
    if (generation < appliedRef.current) {
      return current;
    }
    return clearFeed(current);
  });
  // A recovered read can own the snapshot between the generation check
  // and this write. Do not stamp an error the newer success already
  // replaced, and do not replace a latched workspace or event denial.
  if (
    generation < appliedRef.current ||
    workspaceDetailAuthDeniedRef.current ||
    eventFeedAuthDeniedRef.current
  ) {
    return;
  }
  setError(result.message);
};

const applyRuntimeAuthDenial = (result: ApiEnvelope<WorkspaceRuntime>) => {
  applyOptionalFeedAuthDenial(
    "runtime",
    result,
    appliedRuntimeGenerationRef,
    revokedRuntimeGenerationRef,
    (current) => ({ ...current, runtime: null }),
  );
};

const applyOperationsAuthDenial = (result: ApiEnvelope<ListEnvelope<Operation>>) => {
  applyOptionalFeedAuthDenial(
    "operations",
    result,
    appliedOperationsGenerationRef,
    revokedOperationsGenerationRef,
    (current) => ({ ...current, operations: [] }),
  );
};

const applyRuntimeSuccessIfSettled = (result: ApiEnvelope<WorkspaceRuntime>) => {
  if (!access.allowRuntime || !result.ok) {
    return;
  }
  if (!optionalFeedContextCurrent() || !optionalFeedStillAdvertised("runtime")) {
    return;
  }
  if (
    generation <= revokedRuntimeGenerationRef.current ||
    generation <= runtimeDenialReleasedThroughRef.current
  ) {
    return;
  }
  if (!publishOptionalFeedRecovered(appliedRuntimeGenerationRef, revokedRuntimeGenerationRef)) {
    return;
  }
  setDetail((current) => {
    if (generation < appliedRuntimeGenerationRef.current) {
      return current;
    }
    return { ...current, runtime: result.data };
  });
  releaseRecoveredDetailOutage("runtime");
};

const applyOperationsSuccessIfSettled = (
  result: ApiEnvelope<ListEnvelope<Operation>>,
) => {
  if (!access.allowOperations || !result.ok) {
    return;
  }
  if (!optionalFeedContextCurrent() || !optionalFeedStillAdvertised("operations")) {
    return;
  }
  if (
    generation <= revokedOperationsGenerationRef.current ||
    generation <= operationsDenialReleasedThroughRef.current
  ) {
    return;
  }
  if (
    !publishOptionalFeedRecovered(
      appliedOperationsGenerationRef,
      revokedOperationsGenerationRef,
    )
  ) {
    return;
  }
  setDetail((current) => {
    if (generation < appliedOperationsGenerationRef.current) {
      return current;
    }
    return { ...current, operations: result.data.items };
  });
  releaseRecoveredDetailOutage("operations");
};

  return {
    runtimeAuthDenialHeld,
    operationsAuthDenialHeld,
    preferredOutstandingOutage,
    applyDetailFeedTransientOutage,
    releaseRecoveredDetailOutage,
    publishOptionalFeedRecovered,
    applyRuntimeAuthDenial,
    applyOperationsAuthDenial,
    applyRuntimeSuccessIfSettled,
    applyOperationsSuccessIfSettled,
  };
}
