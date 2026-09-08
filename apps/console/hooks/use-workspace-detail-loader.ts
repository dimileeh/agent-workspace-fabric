"use client";

import {
  useCallback,
  useLayoutEffect,
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
  eventFeedAuthDeniedRef: MutableRefObject<boolean>;
  setEventFeedAuthDenied: Dispatch<SetStateAction<boolean>>;
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
 * The same is true of a /logs 401/403: both are applied as soon as that
 * request settles, even if a sibling runtime/events/operations request hangs,
 * and even if Refresh has only started a newer detail load. A newer listing
 * 200 that has already applied is recovery; a newer request that is still
 * in flight, hanging, or failing transiently is not.
 * A base-detail denial also drops cached listing and tail text immediately;
 * a later sibling 200 must not write that log data back while the latch is
 * held. apiGet has no timeout, so waiting for every sibling would leave the
 * inspector EventSource and cached workspace or log data available after
 * authorization was revoked.
 * Snapshot frames must not write revoked workspace metadata back while that
 * latch is held.
 * A 401/403 on /workspaces/{id}/events latches event-feed denial and clears
 * detail.events as soon as that request settles. Live event frames are ignored
 * until a later successful /events read recovers the feed. A newer request
 * merely starting, hanging, or failing transiently is not recovery.
 * Runtime and operations 401/403 apply on settlement the same way: an older
 * denial must not clear a newer recovered snapshot, and a denial after
 * workspace_runtime / workspace_operations withdrawal must not stamp a detail
 * error no later read of that feed will clear.
 * Selection changes start a new visit and advance request generation. A late
 * 401/403 from the previous visit must not latch denial or stamp the watermark
 * onto the re-opened workspace's in-flight GET, even when selectedId matches
 * again and that GET has not yet become the latest generation.
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
  eventFeedAuthDeniedRef,
  setEventFeedAuthDenied,
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
  // Highest detail generation that applied a successful /events GET. An older
  // event-feed 401/403 must not clear events this newer success already owns.
  const appliedEventFeedGenerationRef = useRef(0);
  // Highest detail generation covered by an applied /events 401/403. An older
  // or in-flight 200 must not restore revoked events or let live frames refill
  // the panel. A request that starts after this watermark may recover.
  const revokedEventFeedGenerationRef = useRef(0);
  // Highest detail generation that applied a successful /runtime or
  // /operations GET. An older 401/403 must not clear a snapshot this newer
  // success already owns, including when a sibling hang keeps Promise.all
  // from repairing the overwrite.
  const appliedRuntimeGenerationRef = useRef(0);
  const appliedOperationsGenerationRef = useRef(0);
  // Highest detail generation covered by an applied runtime or operations
  // 401/403. An older in-flight 200 must not restore the cleared feed. A
  // request that starts after this watermark may recover.
  const revokedRuntimeGenerationRef = useRef(0);
  const revokedOperationsGenerationRef = useRef(0);
  // Highest detail generation that applied a successful /logs listing. An
  // older 401/403 must not clear caches this newer success already owns.
  // A newer request merely starting is not recovery.
  const appliedLogListingGenerationRef = useRef(0);
  // Highest detail generation covered by an applied /logs 401/403. An older
  // or in-flight 200 must not restore cleared listing or leave /stream open.
  // A request that starts after this watermark may recover. Re-applying a
  // denial already inside this window must not raise the watermark.
  const revokedLogListingGenerationRef = useRef(0);
  // Selection visit that owns the watermarks above. A late 401/403 may still
  // see the same selectedId after the operator leaves and re-opens that
  // workspace; it must not stamp the new visit's in-flight GET.
  const workspaceDetailVisitRef = useRef(0);
  const workspaceDetailVisitSelectionRef = useRef<string | null | undefined>(undefined);
  // Request generation at the start of the current visit. A 401/403 at or
  // below this floor belongs to a previous visit and must not raise the
  // watermark, even if it is still the latest started GET until the new visit
  // issues its own request.
  const workspaceDetailVisitGenerationFloorRef = useRef(0);
  const workspaceDetailLoadInFlightRef = useRef(false);

  useLayoutEffect(() => {
    if (workspaceDetailVisitSelectionRef.current === selectedId) {
      return;
    }
    workspaceDetailVisitSelectionRef.current = selectedId;
    workspaceDetailVisitRef.current += 1;
    // The previous visit's in-flight GET is still the latest request generation
    // until this visit starts its own load. Advance past it so that late
    // 401/403 cannot stamp revoked onto the generation the re-opened GET will
    // take, including the window before that GET begins.
    workspaceDetailVisitGenerationFloorRef.current = ++workspaceDetailRequestGenerationRef.current;
    // Previous visit's denial/success must not cover this inspector visit.
    revokedWorkspaceDetailGenerationRef.current = 0;
    appliedWorkspaceDetailGenerationRef.current = 0;
    revokedEventFeedGenerationRef.current = 0;
    appliedEventFeedGenerationRef.current = 0;
    revokedRuntimeGenerationRef.current = 0;
    appliedRuntimeGenerationRef.current = 0;
    revokedOperationsGenerationRef.current = 0;
    appliedOperationsGenerationRef.current = 0;
    revokedLogListingGenerationRef.current = 0;
    appliedLogListingGenerationRef.current = 0;
  }, [selectedId]);

  const loadWorkspace = useCallback(async (workspaceId: string) => {
    const epoch = authorizedFeedEpochRef.current;
    const gatedGeneration = gatedDetailFeedGenerationRef.current;
    const visit = workspaceDetailVisitRef.current;
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

      const publishWorkspaceDetailRecovered = (): boolean => {
        // A denial that landed after this 200 passed the generation check owns
        // the inspector. Clearing the latch here would reopen /stream for a
        // request that started before that revocation. An older success must
        // not record recovery after a newer one already owns the snapshot.
        if (
          generation <= revokedWorkspaceDetailGenerationRef.current ||
          generation < appliedWorkspaceDetailGenerationRef.current
        ) {
          return false;
        }
        appliedWorkspaceDetailGenerationRef.current = Math.max(
          appliedWorkspaceDetailGenerationRef.current,
          generation,
        );
        publishWorkspaceDetailAuthDenied(false);
        return true;
      };

      const workspaceFromDetailResult = (
        current: Workspace | null,
        result: ApiEnvelope<Workspace>,
      ): Workspace | null => {
        if (result.ok) {
          // A base-detail 401/403 may latch between this apply and the updater.
          // Do not write revoked workspace metadata back, and do not treat a
          // newer request that only hung or failed as recovery.
          if (workspaceDetailAuthDeniedRef.current) {
            return current;
          }
          return {
            ...result.data,
            lifecycle: result.data.lifecycle ?? [],
            llm_usage: fallbackLlmUsage(result.data.llm_usage),
            recovery: result.data.recovery ?? null,
          };
        }
        if (feedAuthDenied(result)) {
          return workspaceDetailAuthDeniedRef.current ? null : current;
        }
        return current;
      };

      // Apply even if a newer request has started but has not yet established
      // recovery. A newer request merely starting, hanging, or failing
      // transiently is not recovery — dropping this denial leaves /stream open
      // and lets snapshot frames write revoked workspace metadata back.
      const applyAuthoritativeWorkspaceDetailDenial = (deniedGeneration: number, message: string) => {
        // A selection change starts a new visit. Do not latch or raise the
        // watermark to the current generation when this 401/403 belongs to
        // the visit the operator already left, even if they re-opened the
        // same workspace and selectedId matches again.
        if (
          epoch !== authorizedFeedEpochRef.current ||
          selectedIdRef.current !== workspaceId ||
          visit !== workspaceDetailVisitRef.current ||
          deniedGeneration <= workspaceDetailVisitGenerationFloorRef.current
        ) {
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
        // Authorization for this workspace is revoked. Drop cached listing and
        // tail text now — a hanging runtime/events/operations sibling must not
        // leave previously authorized log data on screen, and a later sibling
        // 200 must not write it back while this latch is held.
        selectedStreamsRef.current = [];
        setSelectedStreams([]);
        setLogEntries([]);
        setStreamOffsets({});
        setDetail((current) => {
          // A recovery GET may land between this denial and the updater.
          if (!workspaceDetailAuthDeniedRef.current) {
            return current;
          }
          return {
            ...current,
            workspace: null,
            streams: [],
          };
        });
        return true;
      };

      let denialApplied = false;
      // Generation this load recorded for a /logs 401/403. That bump is not an
      // external capabilities drop; treating it as one would clear the listing
      // denial banner when the remaining siblings later settle.
      let ownListingDropGeneration: number | null = null;

      const applyWorkspaceDenialIfSettled = (result: ApiEnvelope<Workspace>) => {
        if (result.ok || !feedAuthDenied(result)) {
          return;
        }
        if (applyAuthoritativeWorkspaceDetailDenial(generation, result.message)) {
          denialApplied = true;
        }
      };

      const publishLogListingRecovered = (): boolean => {
        // A denial that landed after this 200 passed the generation check owns
        // the listing. An older success must not record recovery after a newer
        // one already owns the inspector logs.
        if (
          generation <= revokedLogListingGenerationRef.current ||
          generation < appliedLogListingGenerationRef.current
        ) {
          return false;
        }
        appliedLogListingGenerationRef.current = Math.max(
          appliedLogListingGenerationRef.current,
          generation,
        );
        logListingAuthDeniedRef.current = false;
        setLogListingAuthDenied(false);
        return true;
      };

      const applyAcceptedLogListing = (items: WorkspaceLogStream[]) => {
        logStreamActivityRef.current = updateLogStreamActivity(
          logStreamActivityRef.current,
          workspaceId,
          items,
        );
        setSelectedStreams((current) => pickWorkspaceLogStreams(items, current));
        setDetail((current) => {
          if (
            logListingAuthDeniedRef.current ||
            generation < appliedLogListingGenerationRef.current
          ) {
            return current;
          }
          return { ...current, streams: items };
        });
      };

      // Apply even if a newer detail load has started but has not yet applied
      // a listing 200. generation !== current treats that start as recovery
      // and leaves cached log text and the inspector EventSource open while
      // the newer /logs request hangs.
      const applyLogListingAuthDenial = (result: ApiEnvelope<ListEnvelope<WorkspaceLogStream>>) => {
        if (!allowLogs || !feedAuthDenied(result) || result.ok) {
          return;
        }
        if (
          epoch !== authorizedFeedEpochRef.current ||
          selectedIdRef.current !== workspaceId ||
          visit !== workspaceDetailVisitRef.current ||
          generation <= workspaceDetailVisitGenerationFloorRef.current
        ) {
          return;
        }
        // A newer listing 200 already recovered access. A late 401/403 from
        // an older load must not clear caches or close /stream.
        if (generation < appliedLogListingGenerationRef.current) {
          return;
        }
        // This request started inside an already-applied denial window.
        // Raising the watermark here would reject a recovery that started
        // after the original denial.
        if (
          logListingAuthDeniedRef.current &&
          generation <= revokedLogListingGenerationRef.current
        ) {
          return;
        }
        // A newer base-detail denial already covers this generation. Keep the
        // same skip the post-merge path uses unless this request applied it.
        if (generation <= revokedWorkspaceDetailGenerationRef.current && !denialApplied) {
          return;
        }
        // Cover every detail request that has already started so an in-flight
        // refresh cannot restore cleared listing or leave /stream open. A
        // request that starts after this watermark may recover. A later 401
        // must not raise that watermark to the current generation.
        const denialWatermark = logListingAuthDeniedRef.current
          ? generation
          : workspaceDetailRequestGenerationRef.current;
        revokedLogListingGenerationRef.current = Math.max(
          revokedLogListingGenerationRef.current,
          denialWatermark,
        );
        if (!logListingAuthDeniedRef.current) {
          noteGatedDetailDrop(
            gatedDetailDroppedFeedsRef,
            gatedDetailFeedGenerationRef,
            DROP_ALL_GATED_DETAIL_FEEDS,
          );
          ownListingDropGeneration = gatedDetailFeedGenerationRef.current;
        }
        logListingAuthDeniedRef.current = true;
        selectedStreamsRef.current = [];
        setLogListingAuthDenied(true);
        setSelectedStreams([]);
        setLogEntries([]);
        setStreamOffsets({});
        setDetail((current) => {
          if (!logListingAuthDeniedRef.current) {
            return current;
          }
          return { ...current, streams: [] };
        });
        // A latched base-detail 401/403 owns the banner. Do not replace it
        // with the listing reason while that revocation is still in force.
        if (!workspaceDetailAuthDeniedRef.current) {
          setError(result.message);
        }
      };

      const applyLogListingSuccessIfSettled = (
        result: ApiEnvelope<ListEnvelope<WorkspaceLogStream>>,
      ) => {
        if (!result.ok || !allowLogs) {
          return;
        }
        // Only the latest load may apply a listing 200. A superseded success
        // must not restore caches after Refresh has started a newer request.
        // Settlement records that success immediately so an older 401/403
        // cannot treat a hanging sibling as recovered access.
        if (
          epoch !== authorizedFeedEpochRef.current ||
          generation !== workspaceDetailRequestGenerationRef.current ||
          selectedIdRef.current !== workspaceId ||
          visit !== workspaceDetailVisitRef.current ||
          generation <= workspaceDetailVisitGenerationFloorRef.current ||
          workspaceDetailAuthDeniedRef.current
        ) {
          return;
        }
        if (!publishLogListingRecovered()) {
          return;
        }
        applyAcceptedLogListing(result.data.items);
      };

      const publishEventFeedRecovered = (): boolean => {
        // A denial that landed after this 200 passed the generation check owns
        // the Events panel. Clearing the latch here would accept live frames
        // for a request that started before that revocation. An older success
        // must not record recovery after a newer one already owns the panel.
        if (
          generation <= revokedEventFeedGenerationRef.current ||
          generation < appliedEventFeedGenerationRef.current
        ) {
          return false;
        }
        appliedEventFeedGenerationRef.current = Math.max(
          appliedEventFeedGenerationRef.current,
          generation,
        );
        eventFeedAuthDeniedRef.current = false;
        setEventFeedAuthDenied(false);
        return true;
      };

      // /events 401/403 while workspace_events stays advertised clears
      // detail.events, but EventSource event frames merge unless this latch
      // is held. Apply as soon as the request settles so a hanging sibling
      // cannot leave previously authorized events on screen or let live
      // frames refill the panel. A newer request merely starting is not
      // recovery.
      const applyEventFeedAuthDenial = (result: ApiEnvelope<ListEnvelope<WorkspaceEvent>>) => {
        if (!allowEvents || !feedAuthDenied(result) || result.ok) {
          return;
        }
        if (
          epoch !== authorizedFeedEpochRef.current ||
          selectedIdRef.current !== workspaceId ||
          visit !== workspaceDetailVisitRef.current ||
          generation <= workspaceDetailVisitGenerationFloorRef.current
        ) {
          return;
        }
        if (generation < appliedEventFeedGenerationRef.current) {
          return;
        }
        // This request started inside an already-applied denial window.
        // Raising the watermark here would reject a recovery that started
        // after the original denial.
        if (
          eventFeedAuthDeniedRef.current &&
          generation <= revokedEventFeedGenerationRef.current
        ) {
          return;
        }
        // The first denial covers every detail request that has already
        // started so an in-flight 200 cannot restore cleared events. A later
        // 401 must not raise that watermark to the current generation, or it
        // would reject a recovery that started after the original denial.
        const denialWatermark = eventFeedAuthDeniedRef.current
          ? generation
          : workspaceDetailRequestGenerationRef.current;
        revokedEventFeedGenerationRef.current = Math.max(
          revokedEventFeedGenerationRef.current,
          denialWatermark,
        );
        eventFeedAuthDeniedRef.current = true;
        setEventFeedAuthDenied(true);
        setDetail((current) => {
          if (!eventFeedAuthDeniedRef.current) {
            return current;
          }
          return { ...current, events: [] };
        });
        // A latched base-detail 401/403 owns the banner. Do not replace it
        // with the event-feed reason while that revocation is still in force.
        if (!workspaceDetailAuthDeniedRef.current) {
          setError(result.message);
        }
      };

      const applyWorkspaceSuccessIfSettled = (result: ApiEnvelope<Workspace>) => {
        if (!result.ok) {
          return;
        }
        // Record and apply a successful GET as soon as it settles. A sibling
        // hang must not keep this response "in hand" until Promise.all, or an
        // older 401/403 can stamp the revoke watermark over this generation
        // and leave the inspector cleared while access is already restored.
        if (
          epoch !== authorizedFeedEpochRef.current ||
          selectedIdRef.current !== workspaceId ||
          visit !== workspaceDetailVisitRef.current ||
          generation <= workspaceDetailVisitGenerationFloorRef.current ||
          generation <= revokedWorkspaceDetailGenerationRef.current
        ) {
          return;
        }
        // publish* records applied* only when this generation still owns the
        // snapshot. Writing afterward would let an older in-flight 200 replace
        // a newer inspector even though the watermark update was skipped. A
        // hanging sibling means Promise.all never repairs that overwrite.
        if (!publishWorkspaceDetailRecovered()) {
          return;
        }
        setDetail((current) => {
          if (
            generation < appliedWorkspaceDetailGenerationRef.current ||
            workspaceDetailAuthDeniedRef.current
          ) {
            return current;
          }
          return {
            ...current,
            workspace: workspaceFromDetailResult(current.workspace, result),
          };
        });
      };

      const applyEventFeedSuccessIfSettled = (
        result: ApiEnvelope<ListEnvelope<WorkspaceEvent>>,
      ) => {
        if (!result.ok) {
          return;
        }
        if (
          epoch !== authorizedFeedEpochRef.current ||
          selectedIdRef.current !== workspaceId ||
          visit !== workspaceDetailVisitRef.current ||
          generation <= workspaceDetailVisitGenerationFloorRef.current ||
          generation <= revokedEventFeedGenerationRef.current
        ) {
          return;
        }
        // Same ownership gate as the workspace 200 handler: skip the payload
        // when a newer /events success already applied, including inside the
        // updater so a queued older write cannot land after that snapshot.
        if (!publishEventFeedRecovered()) {
          return;
        }
        setDetail((current) => {
          if (
            eventFeedAuthDeniedRef.current ||
            generation < appliedEventFeedGenerationRef.current
          ) {
            return current;
          }
          return { ...current, events: result.data.items };
        });
      };

      const optionalFeedContextCurrent = () =>
        epoch === authorizedFeedEpochRef.current &&
        selectedIdRef.current === workspaceId &&
        visit === workspaceDetailVisitRef.current &&
        generation > workspaceDetailVisitGenerationFloorRef.current &&
        (generation > revokedWorkspaceDetailGenerationRef.current || denialApplied);

      // Capability withdrawal (or a capabilities 404) drops the feed after this
      // load captured generation. A listing 401/403 also bumps the generation
      // with DROP_ALL; that stamp is this load's own, not a withdrawal, and
      // must not suppress a still-advertised runtime/operations denial.
      const optionalFeedStillAdvertised = (feed: "runtime" | "operations") => {
        if (gatedGeneration === gatedDetailFeedGenerationRef.current) {
          return true;
        }
        const stampsForExternalDrop =
          ownListingDropGeneration == null
            ? gatedDetailDroppedFeedsRef.current
            : gatedDetailDroppedFeedsRef.current.filter(
                (stamp) => stamp.generation !== ownListingDropGeneration,
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
        if (feed === "runtime" ? !allowRuntime : !allowOperations) {
          return;
        }
        if (!optionalFeedContextCurrent() || !optionalFeedStillAdvertised(feed)) {
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
        // The first denial covers every detail request that has already
        // started so an in-flight 200 cannot restore the cleared feed. A
        // later 401 must not raise that watermark to the current generation.
        revokedRef.current = Math.max(
          revokedRef.current,
          workspaceDetailRequestGenerationRef.current,
        );
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
        if (!allowRuntime || !result.ok) {
          return;
        }
        if (!optionalFeedContextCurrent() || !optionalFeedStillAdvertised("runtime")) {
          return;
        }
        if (generation <= revokedRuntimeGenerationRef.current) {
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
      };

      const applyOperationsSuccessIfSettled = (
        result: ApiEnvelope<ListEnvelope<Operation>>,
      ) => {
        if (!allowOperations || !result.ok) {
          return;
        }
        if (!optionalFeedContextCurrent() || !optionalFeedStillAdvertised("operations")) {
          return;
        }
        if (generation <= revokedOperationsGenerationRef.current) {
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
      };

      const workspacePromise = apiGet<Workspace>(awfPath(`workspaces/${workspaceId}`));
      const runtimePromise = allowRuntime
        ? apiGet<WorkspaceRuntime>(awfPath(`workspaces/${workspaceId}/runtime`))
        : Promise.resolve(null);
      const eventsPromise = allowEvents
        ? apiGet<ListEnvelope<WorkspaceEvent>>(
            awfPath(`workspaces/${workspaceId}/events`, { limit: 100 }),
          )
        : Promise.resolve(null);
      const operationsPromise = allowOperations
        ? apiGet<ListEnvelope<Operation>>(
            awfPath(`workspaces/${workspaceId}/operations`, { limit: 50 }),
          )
        : Promise.resolve(null);
      const streamsPromise = allowLogs
        ? apiGet<ListEnvelope<WorkspaceLogStream>>(awfPath(`workspaces/${workspaceId}/logs`))
        : Promise.resolve(null);

      // 401/403 closes /stream and drops cached workspace or log data as soon
      // as that request settles. Promise.all never runs the handlers below if
      // a sibling runtime/events/operations request hangs, and apiGet has no
      // timeout, so revoked inspector data would stay available indefinitely.
      // A successful GET is recorded here too, so an in-hand 200 is not
      // discarded when an older denial stamps the watermark while a sibling
      // is still outstanding.
      void workspacePromise.then((result) => {
        if (result.ok) {
          applyWorkspaceSuccessIfSettled(result);
        } else {
          applyWorkspaceDenialIfSettled(result);
        }
      });
      void runtimePromise.then((result) => {
        if (result == null) {
          return;
        }
        if (result.ok) {
          applyRuntimeSuccessIfSettled(result);
        } else {
          applyRuntimeAuthDenial(result);
        }
      });
      void operationsPromise.then((result) => {
        if (result == null) {
          return;
        }
        if (result.ok) {
          applyOperationsSuccessIfSettled(result);
        } else {
          applyOperationsAuthDenial(result);
        }
      });
      void streamsPromise.then((result) => {
        if (result == null) {
          return;
        }
        if (result.ok) {
          applyLogListingSuccessIfSettled(result);
        } else {
          applyLogListingAuthDenial(result);
        }
      });
      void eventsPromise.then((result) => {
        if (result == null) {
          return;
        }
        if (result.ok) {
          applyEventFeedSuccessIfSettled(result);
        } else {
          applyEventFeedAuthDenial(result);
        }
      });

      const [workspace, fetchedRuntime, fetchedEvents, fetchedOperations, fetchedStreams] =
        await Promise.all([
          workspacePromise,
          runtimePromise,
          eventsPromise,
          operationsPromise,
          streamsPromise,
        ]);
      let runtime = fetchedRuntime;
      let events = fetchedEvents;
      let operations = fetchedOperations;
      let streams = fetchedStreams;

      applyWorkspaceDenialIfSettled(workspace);
      // Honor a /logs 401/403 that settled after Refresh started a newer
      // detail load. The equality check below drops the rest of this merge,
      // and a newer request merely starting is not authorization recovery.
      // applyLogListingAuthDenial still skips the denial when a newer listing
      // 200 has already been applied.
      if (streams != null) {
        applyLogListingAuthDenial(streams);
      }

      if (
        epoch !== authorizedFeedEpochRef.current ||
        generation !== workspaceDetailRequestGenerationRef.current ||
        selectedIdRef.current !== workspaceId
      ) {
        return;
      }
      // The visit boundary advances request generation, so a previous-visit
      // response is no longer latest. Also drop it if the visit moved before
      // that bump is visible to this closure.
      if (
        visit !== workspaceDetailVisitRef.current ||
        generation <= workspaceDetailVisitGenerationFloorRef.current
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
        const stampsForExternalDrop =
          ownListingDropGeneration == null
            ? gatedDetailDroppedFeedsRef.current
            : gatedDetailDroppedFeedsRef.current.filter(
                (stamp) => stamp.generation !== ownListingDropGeneration,
              );
        const hasExternalDrop = stampsForExternalDrop.some((stamp) => stamp.generation > gatedGeneration);
        // Only this load's /logs 401/403 advanced the generation. Fall through
        // so sibling results still merge without treating the listing denial
        // as a capabilities withdrawal.
        if (hasExternalDrop) {
          const dropped = gatedDetailDropsSince(stampsForExternalDrop, gatedGeneration);
          if (allGatedDetailFeedsDropped(dropped)) {
            // publish* owns the applied* watermark. A declined recovery must
            // not write this payload — an older 200 that lost to a newer
            // success would replace the inspector, and a hanging sibling
            // means nothing later repairs that snapshot.
            const workspaceRecoveryOwned = workspace.ok
              ? publishWorkspaceDetailRecovered()
              : false;
            if (workspaceRecoveryOwned) {
              // A latched listing 401/403 owns this banner. A newer request
              // that only hangs or fails transiently is not recovery.
              if (!workspaceDetailAuthDeniedRef.current && !logListingAuthDeniedRef.current) {
                setError(null);
              }
            } else if (feedAuthDenied(workspace)) {
              setError(workspace.message);
              publishWorkspaceDetailAuthDenied(true);
            } else if (
              !workspace.ok &&
              !workspaceDetailAuthDeniedRef.current &&
              !logListingAuthDeniedRef.current
            ) {
              // A transient failure is not recovery. Keep the latched denial
              // banner so a newer 5xx cannot hide the revocation.
              setError(workspace.message);
            }
            if (workspace.ok && !workspaceRecoveryOwned) {
              return;
            }
            setDetail((current) => {
              if (
                workspace.ok &&
                (generation < appliedWorkspaceDetailGenerationRef.current ||
                  workspaceDetailAuthDeniedRef.current)
              ) {
                return current;
              }
              return {
                ...current,
                workspace: workspaceFromDetailResult(current.workspace, workspace),
                streams: logListingAuthDeniedRef.current ? [] : current.streams,
              };
            });
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
      }

      const firstFailure = [workspace, runtime, events, operations, streams].find(
        (item) => item != null && !item.ok,
      );
      // A successful /workspaces/{id} GET is recovery. Clear the latch before
      // the error update so the denial banner does not outlive the read.
      // Latch before setDetail so a snapshot frame cannot restore workspace
      // between this apply and the live-stream effect cleanup.
      // Same ownership rule as the immediate 200 handlers: record applied*
      // only when this generation still owns the snapshot, and only then
      // write the payload. A declined publish must not replace a newer
      // inspector when this merge runs after that newer success.
      let workspaceRecoveryOwned = false;
      if (workspace.ok) {
        workspaceRecoveryOwned = publishWorkspaceDetailRecovered();
      } else if (feedAuthDenied(workspace)) {
        publishWorkspaceDetailAuthDenied(true);
      }

      let runtimeRecoveryOwned = false;
      if (allowRuntime && runtime != null && feedAuthDenied(runtime)) {
        applyRuntimeAuthDenial(runtime);
      } else if (allowRuntime && runtime?.ok) {
        runtimeRecoveryOwned = publishOptionalFeedRecovered(
          appliedRuntimeGenerationRef,
          revokedRuntimeGenerationRef,
        );
      }

      let operationsRecoveryOwned = false;
      if (allowOperations && operations != null && feedAuthDenied(operations)) {
        applyOperationsAuthDenial(operations);
      } else if (allowOperations && operations?.ok) {
        operationsRecoveryOwned = publishOptionalFeedRecovered(
          appliedOperationsGenerationRef,
          revokedOperationsGenerationRef,
        );
      }

      let eventRecoveryOwned = false;
      if (allowEvents && events != null && feedAuthDenied(events)) {
        // Event-feed 401/403 while workspace_events stays advertised is auth
        // revocation for the Events panel, not a transient outage. Apply even
        // if the settlement handler already latched it, so a sibling 200
        // cannot restore events or let live frames refill the panel.
        applyEventFeedAuthDenial(events);
      } else if (allowEvents && events?.ok) {
        // Clear the latch before setDetail so this successful /events read is
        // what restores the panel. A denied generation stays latched and must
        // not write items back.
        eventRecoveryOwned = publishEventFeedRecovered();
      }

      // Listing 401/403 while workspace_logs stays advertised is auth
      // revocation for this column, not a transient outage. Apply before
      // setDetail so an in-flight listing 200 cannot restore selection or
      // leave /stream open. A newer listing 200 records recovery the same
      // way, so an older denial cannot clear caches that success owns.
      let listingRecoveryOwned = false;
      if (allowLogs && streams != null && feedAuthDenied(streams)) {
        applyLogListingAuthDenial(streams);
      } else if (allowLogs && streams?.ok && !workspaceDetailAuthDeniedRef.current) {
        listingRecoveryOwned = publishLogListingRecovered();
        if (listingRecoveryOwned) {
          applyAcceptedLogListing(streams.data.items);
        }
      }

      // This setter is the workspace-detail slot only. Do not clear overview
      // errors here — an independent overview success must not clear this
      // warning either (CONSOLE_BACKEND_CONTRACT).
      if (firstFailure && !firstFailure.ok) {
        // A latched base-detail 401/403 owns this banner. A newer request that
        // only hangs or fails transiently is not recovery and must not replace
        // the revocation reason. A latched event-feed or listing 401/403 has
        // the same precedence over an earlier-listed sibling outage.
        const eventDenialOwnsBanner =
          eventFeedAuthDeniedRef.current &&
          !(events != null && feedAuthDenied(events) && firstFailure === events);
        const listingDenialOwnsBanner =
          logListingAuthDeniedRef.current &&
          !(streams != null && feedAuthDenied(streams) && firstFailure === streams);
        if (
          (!workspaceDetailAuthDeniedRef.current || feedAuthDenied(workspace)) &&
          !eventDenialOwnsBanner &&
          !listingDenialOwnsBanner
        ) {
          setError(firstFailure.message);
        }
      } else if (
        !workspaceDetailAuthDeniedRef.current &&
        !eventFeedAuthDeniedRef.current &&
        !logListingAuthDeniedRef.current
      ) {
        setError(null);
      }

      // Gated-off feeds resolve to null and clear; transient network/5xx keep
      // last-successful inspector snapshots while the error banner stays visible
      // (CONSOLE_BACKEND_CONTRACT). Feed-level 401/403 drops that feed's cache.
      // Re-read the latch in the updater: a superseded /workspaces/{id} 401/403
      // can apply after this request passed the generation check, and must not
      // lose to this write or to a newer hang/5xx.
      setDetail((current) => {
        const staleWorkspaceSuccess =
          workspace.ok &&
          (!workspaceRecoveryOwned ||
            generation < appliedWorkspaceDetailGenerationRef.current ||
            workspaceDetailAuthDeniedRef.current);
        const nextWorkspace = staleWorkspaceSuccess
          ? current.workspace
          : workspaceFromDetailResult(current.workspace, workspace);

        const staleRuntimeSuccess =
          runtime != null &&
          runtime.ok &&
          (!runtimeRecoveryOwned || generation < appliedRuntimeGenerationRef.current);
        const nextRuntime = !allowRuntime
          ? null
          : staleRuntimeSuccess
            ? current.runtime
            : runtime != null && runtime.ok
              ? runtime.data
              : feedAuthDenied(runtime) && generation >= appliedRuntimeGenerationRef.current
                ? null
                : current.runtime;

        const staleEventSuccess =
          events != null &&
          events.ok &&
          !eventFeedAuthDeniedRef.current &&
          !feedAuthDenied(events) &&
          (!eventRecoveryOwned || generation < appliedEventFeedGenerationRef.current);
        const nextEvents = !allowEvents
          ? []
          : staleEventSuccess
            ? current.events
            : events != null && events.ok && !eventFeedAuthDeniedRef.current
              ? events.data.items
              : feedAuthDenied(events) || eventFeedAuthDeniedRef.current
                ? []
                : current.events;

        const staleOperationsSuccess =
          operations != null &&
          operations.ok &&
          (!operationsRecoveryOwned || generation < appliedOperationsGenerationRef.current);
        const nextOperations = !allowOperations
          ? []
          : staleOperationsSuccess
            ? current.operations
            : operations != null && operations.ok
              ? operations.data.items
              : feedAuthDenied(operations) && generation >= appliedOperationsGenerationRef.current
                ? []
                : current.operations;

        const nextStreams = !allowLogs
          ? []
          : workspaceDetailAuthDeniedRef.current || logListingAuthDeniedRef.current
            ? []
            : streams != null && streams.ok && listingRecoveryOwned
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
    eventFeedAuthDeniedRef,
    setEventFeedAuthDenied,
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
