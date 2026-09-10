"use client";

import {
  useCallback,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
} from "react";

import {
  DROP_ALL_GATED_DETAIL_FEEDS,
  noteGatedDetailDrop,
  type GatedDetailDropStamp,
} from "@/lib/console-dashboard-derived";
import { awfPath } from "@/lib/console-urls";
import { fallbackLlmUsage } from "@/lib/format";
import {
  appendUniqueOverviewItems,
  overviewItemMatchesQuery,
  overviewListPath,
  reconcileOverviewRetainedItems,
  retainedOverviewIdBatch,
  usableContinuationCursor,
} from "@/lib/overview-list";
import type { ListEnvelope, WorkspaceOverview } from "@/lib/types";
import {
  type DetailState,
  type LogEntry,
  type LogStreamActivityMap,
  type OperatorActionState,
  type RetryActionState,
  apiGet,
  apiPost,
  apiPostWithDeadline,
  emptyDetail,
  pollMs,
} from "@/components/console-dashboard-shared";

type OverviewQuery = {
  statusFilters: string[];
  agentFilters: string[];
  repoFilter: string;
};

type OverviewPagination = {
  query: unknown;
  nextCursor: string | null;
  complete: boolean;
  fetchedCursors: Set<string>;
};

type OverviewBatchResponse = {
  items: WorkspaceOverview[];
  missing_workspace_ids: string[];
};

type Setter<T> = Dispatch<SetStateAction<T>>;
type Ref<T> = MutableRefObject<T>;

type UseConsoleOverviewLoaderArgs = {
  authorizedFeedEpochRef: Ref<number>;
  consoleAuthDeniedRef: Ref<boolean>;
  overviewQueryRef: Ref<OverviewQuery>;
  selectedIdRef: Ref<string | null>;
  setSelectedId: (workspaceId: string | null) => void;
  overviewRequestGenerationRef: Ref<number>;
  appliedOverviewGenerationRef: Ref<number>;
  appliedOverviewFailureGenerationRef: Ref<number>;
  revokedOverviewGenerationRef: Ref<number>;
  overviewLoadInFlightRef: Ref<boolean>;
  overviewRetainedRefreshAbortControllerRef: Ref<AbortController | null>;
  overviewRetainedRefreshCursorRef: Ref<{ query: unknown; offset: number }>;
  overviewItemsRef: Ref<WorkspaceOverview[]>;
  overviewPaginationRef: Ref<OverviewPagination | null>;
  overviewSelectionLookupRef: Ref<{ query: unknown; workspaceId: string } | null>;
  overviewIsolatedSelectionRef: Ref<{ query: unknown; workspaceId: string } | null>;
  gatedDetailFeedGenerationRef: Ref<number>;
  gatedDetailDroppedFeedsRef: Ref<GatedDetailDropStamp[]>;
  workspaceDetailAuthDeniedRef: Ref<boolean>;
  workspaceBaseDetailAuthDeniedRef: Ref<boolean>;
  eventFeedAuthDeniedRef: Ref<boolean>;
  logListingAuthDeniedRef: Ref<boolean>;
  logTailAuthDeniedRef: Ref<boolean>;
  selectedStreamsRef: Ref<string[]>;
  logStreamActivityRef: Ref<LogStreamActivityMap>;
  setOverview: Setter<WorkspaceOverview[]>;
  setOverviewError: Setter<string | null>;
  setOverviewTruncationWarning: Setter<string | null>;
  setOverviewHasMore: Setter<boolean>;
  setOverviewHistoryLoading: Setter<boolean>;
  setOverviewHistoryComplete: Setter<boolean>;
  setOverviewHistoryError: Setter<boolean>;
  setOverviewLoadSettledVersion: Setter<number>;
  setLastRefresh: Setter<Date | null>;
  setApiState: Setter<"checking" | "ok" | "error">;
  setWorkspaceDetailError: Setter<string | null>;
  setWorkspaceDetailAuthDenied: Setter<boolean>;
  setEventFeedAuthDenied: Setter<boolean>;
  setRetainedAgents: Setter<string[]>;
  setRetainedModels: Setter<string[]>;
  setAgentFilters: Setter<string[]>;
  setModelFilters: Setter<string[]>;
  setRepoFilter: Setter<string>;
  setSearchText: Setter<string>;
  setDetail: Setter<DetailState>;
  setLogListingAuthDenied: Setter<boolean>;
  setLogTailAuthDenied: Setter<boolean>;
  setSelectedStreams: Setter<string[]>;
  setLogEntries: Setter<LogEntry[]>;
  setStreamOffsets: Setter<Record<string, number>>;
  setLogsFullscreen: Setter<boolean>;
  setWorkspaceLogSelection: Setter<string[]>;
  setFullscreenWorkspaceIds: Setter<string[]>;
  setTaskDetailsWorkspaceId: Setter<string | null>;
  setStreamState: Setter<"idle" | "connecting" | "live" | "error">;
  setRetryState: Setter<RetryActionState>;
  setOperatorActionState: Setter<OperatorActionState>;
};

/**
 * Overview pagination, selected-row lookup, and retained-history refresh.
 * Extracted from console-dashboard.tsx for the first-party 1500-line guard.
 */
export function useConsoleOverviewLoader({
  authorizedFeedEpochRef,
  consoleAuthDeniedRef,
  overviewQueryRef,
  selectedIdRef,
  setSelectedId,
  overviewRequestGenerationRef,
  appliedOverviewGenerationRef,
  appliedOverviewFailureGenerationRef,
  revokedOverviewGenerationRef,
  overviewLoadInFlightRef,
  overviewRetainedRefreshAbortControllerRef,
  overviewRetainedRefreshCursorRef,
  overviewItemsRef,
  overviewPaginationRef,
  overviewSelectionLookupRef,
  overviewIsolatedSelectionRef,
  gatedDetailFeedGenerationRef,
  gatedDetailDroppedFeedsRef,
  workspaceDetailAuthDeniedRef,
  workspaceBaseDetailAuthDeniedRef,
  eventFeedAuthDeniedRef,
  logListingAuthDeniedRef,
  logTailAuthDeniedRef,
  selectedStreamsRef,
  logStreamActivityRef,
  setOverview,
  setOverviewError,
  setOverviewTruncationWarning,
  setOverviewHasMore,
  setOverviewHistoryLoading,
  setOverviewHistoryComplete,
  setOverviewHistoryError,
  setOverviewLoadSettledVersion,
  setLastRefresh,
  setApiState,
  setWorkspaceDetailError,
  setWorkspaceDetailAuthDenied,
  setEventFeedAuthDenied,
  setRetainedAgents,
  setRetainedModels,
  setAgentFilters,
  setModelFilters,
  setRepoFilter,
  setSearchText,
  setDetail,
  setLogListingAuthDenied,
  setLogTailAuthDenied,
  setSelectedStreams,
  setLogEntries,
  setStreamOffsets,
  setLogsFullscreen,
  setWorkspaceLogSelection,
  setFullscreenWorkspaceIds,
  setTaskDetailsWorkspaceId,
  setStreamState,
  setRetryState,
  setOperatorActionState,
}: UseConsoleOverviewLoaderArgs) {
  const loadOverview = useCallback(async (
    continuation = false,
    selectedLookupId: string | null = null,
  ) => {
    const epoch = authorizedFeedEpochRef.current;
    // Auth revocation must not refill previously authorized workspace rows.
    // Non-auth capability failures keep legacy-safe overview navigation.
    if (consoleAuthDeniedRef.current) {
      overviewItemsRef.current = [];
      overviewPaginationRef.current = null;
      overviewSelectionLookupRef.current = null;
      overviewIsolatedSelectionRef.current = null;
      setOverview([]);
      setOverviewHasMore(false);
      setOverviewHistoryComplete(false);
      setOverviewHistoryError(false);
      return;
    }
    const capturedQuery = overviewQueryRef.current;
    const capturedPagination = overviewPaginationRef.current;
    const capturedIsolatedSelection = overviewIsolatedSelectionRef.current;
    const replaceEmptyOverviewWithSelection =
      capturedPagination === null && overviewItemsRef.current.length === 0;
    const shouldIsolateSelection = (workspaceId: string) =>
      replaceEmptyOverviewWithSelection ||
      (capturedIsolatedSelection?.query === capturedQuery &&
        capturedIsolatedSelection.workspaceId === workspaceId);
    if (
      selectedLookupId &&
      overviewItemsRef.current.some((item) => item.workspace_id === selectedLookupId)
    ) {
      return;
    }
    if (
      continuation &&
      (overviewLoadInFlightRef.current ||
        capturedPagination?.query !== capturedQuery ||
        capturedPagination.complete ||
        !usableContinuationCursor(capturedPagination.nextCursor))
    ) {
      return;
    }
    const requestedCursor = continuation ? capturedPagination?.nextCursor ?? null : null;
    // Stamp the captured query after the denial/no-continuation early returns so
    // filter races stay pinned without cancelling useful work for a no-op scroll.
    const generation = ++overviewRequestGenerationRef.current;
    overviewLoadInFlightRef.current = true;
    if (continuation) {
      setOverviewHistoryLoading(true);
      setOverviewHistoryError(false);
    }
    try {
      if (!continuation && !selectedLookupId) {
        const health = await apiGet<{ status: string }>(awfPath("health"));
        if (
          epoch !== authorizedFeedEpochRef.current ||
          consoleAuthDeniedRef.current ||
          generation !== overviewRequestGenerationRef.current ||
          overviewQueryRef.current !== capturedQuery
        ) {
          return;
        }
        setApiState(health.ok ? "ok" : "error");
      }

      const { statusFilters: statuses, agentFilters: agents, repoFilter: repo } =
        capturedQuery;
      const filters = {
        status: statuses.length === 1 ? statuses[0] : undefined,
        agent: agents.length === 1 ? agents[0] : undefined,
        repo_url: repo.trim() || undefined,
      };
      let pageError: string | null = null;
      let pageAuthDenied = false;
      let pageOutage = false;
      const applyOverviewAuthDenial = (deniedGeneration: number, message: string): boolean => {
        // A tenant/backend switch or console-level denial already wiped
        // authorized surfaces. An older context's 401/403 must not latch onto
        // the new epoch.
        if (epoch !== authorizedFeedEpochRef.current || consoleAuthDeniedRef.current) {
          return false;
        }
        // A newer successful overview already owns the rail. A late 401/403
        // from an older request must not clear it.
        if (deniedGeneration < appliedOverviewGenerationRef.current) {
          return false;
        }
        // A newer first-page outage owns the retained last-good rail. A late
        // 401/403 from the older best-effort history batch must not wipe it.
        if (deniedGeneration < appliedOverviewFailureGenerationRef.current) {
          return false;
        }
        // This request started inside an already-applied denial window.
        // Raising the watermark would reject a recovery request that started
        // after the original denial.
        if (deniedGeneration <= revokedOverviewGenerationRef.current) {
          return false;
        }
        // Cover every overview request that has already started so an in-flight
        // refresh cannot restore cleared rail, inspector, or logs. A request
        // that starts after this watermark may recover.
        revokedOverviewGenerationRef.current = Math.max(
          revokedOverviewGenerationRef.current,
          overviewRequestGenerationRef.current,
        );
        // Overview feed auth denial: drop the rail and close dependent workspace
        // surfaces (selection, inspector, logs, fullscreen). Do not call
        // clearAuthorizedConsoleFeeds — other feeds clear themselves, and
        // capabilities may still succeed without an auth-denial latch thrashing
        // overview refill. Bump gated-detail generation so in-flight
        // loadWorkspace / log-tail cannot restore revoked caches.
        noteGatedDetailDrop(gatedDetailDroppedFeedsRef, gatedDetailFeedGenerationRef, DROP_ALL_GATED_DETAIL_FEEDS);
        overviewItemsRef.current = [];
        overviewPaginationRef.current = null;
        overviewSelectionLookupRef.current = null;
        overviewIsolatedSelectionRef.current = null;
        setOverviewError(message);
        setOverview([]);
        setOverviewHasMore(false);
        setOverviewHistoryComplete(false);
        setOverviewHistoryError(false);
        setOverviewTruncationWarning(null);
        // Inspector surfaces are wiped with the rail; drop the detail warning
        // so a retained diagnostic error does not outlive the cleared snapshot.
        setWorkspaceDetailError(null);
        workspaceDetailAuthDeniedRef.current = false;
        setWorkspaceDetailAuthDenied(false);
        workspaceBaseDetailAuthDeniedRef.current = false;
        eventFeedAuthDeniedRef.current = false;
        setEventFeedAuthDenied(false);
        // Same tenant-learned filter wipe as clearAuthorizedConsoleFeeds:
        // retained agent/model options stay visible on the rail, and an
        // active prior filter can keep a later recovered list empty.
        setRetainedAgents([]);
        setRetainedModels([]);
        setAgentFilters([]);
        setModelFilters([]);
        setRepoFilter("");
        setSearchText("");
        setSelectedId(null);
        setDetail(emptyDetail);
        logListingAuthDeniedRef.current = false;
        setLogListingAuthDenied(false);
        logTailAuthDeniedRef.current = false;
        setLogTailAuthDenied(false);
        selectedStreamsRef.current = [];
        setSelectedStreams([]);
        setLogEntries([]);
        setStreamOffsets({});
        setLogsFullscreen(false);
        setWorkspaceLogSelection([]);
        setFullscreenWorkspaceIds([]);
        setTaskDetailsWorkspaceId(null);
        setStreamState("idle");
        setRetryState({ status: "idle" });
        setOperatorActionState({ status: "idle" });
        logStreamActivityRef.current = {};
        return true;
      };
      const applyOverviewOutage = (failedGeneration: number, message: string): boolean => {
        // A tenant/backend switch or console-level denial already wiped
        // authorized surfaces. An older context's network/5xx must not latch
        // an outage onto the new epoch or replace the authorization reason.
        if (epoch !== authorizedFeedEpochRef.current || consoleAuthDeniedRef.current) {
          return false;
        }
        // A newer successful overview already owns the rail. A late 5xx from
        // an older request must not re-latch overviewError.
        if (failedGeneration < appliedOverviewGenerationRef.current) {
          return false;
        }
        // A newer outage already owns the warning.
        if (failedGeneration < appliedOverviewFailureGenerationRef.current) {
          return false;
        }
        // A 401/403 already covers this generation. Do not replace the
        // authorization reason.
        if (failedGeneration <= revokedOverviewGenerationRef.current) {
          return false;
        }
        appliedOverviewFailureGenerationRef.current = Math.max(
          appliedOverviewFailureGenerationRef.current,
          failedGeneration,
        );
        // Re-check in the updater: a newer success or denial can settle after
        // this outage is queued. Retain the last-good rail; only 401/403 clears it.
        setOverviewError((current) =>
          epoch !== authorizedFeedEpochRef.current ||
          failedGeneration < appliedOverviewGenerationRef.current ||
          failedGeneration < appliedOverviewFailureGenerationRef.current ||
          failedGeneration <= revokedOverviewGenerationRef.current ||
          consoleAuthDeniedRef.current
            ? current
            : message,
        );
        if (continuation) {
          setOverviewHistoryError(true);
        }
        return true;
      };
      const normalizeOverview = (items: WorkspaceOverview[]) =>
        items.map((item) => ({
          ...item,
          task_prompt: item.task_prompt ?? "",
          lifecycle: item.lifecycle ?? [],
          llm_usage: fallbackLlmUsage(item.llm_usage),
          recovery: item.recovery ?? null,
        }));
      const abortRetainedRefreshForPageFailure = () => {
        if (
          epoch !== authorizedFeedEpochRef.current ||
          consoleAuthDeniedRef.current ||
          generation < appliedOverviewGenerationRef.current ||
          generation < appliedOverviewFailureGenerationRef.current ||
          generation <= revokedOverviewGenerationRef.current
        ) {
          return;
        }
        overviewRetainedRefreshAbortControllerRef.current?.abort();
        overviewRetainedRefreshAbortControllerRef.current = null;
      };
      const fetchOverviewPage = async (cursor: string | null) => {
        if (
          epoch !== authorizedFeedEpochRef.current ||
          consoleAuthDeniedRef.current ||
          generation !== overviewRequestGenerationRef.current ||
          overviewQueryRef.current !== capturedQuery
        ) {
          return null;
        }
        const result = await apiGet<ListEnvelope<WorkspaceOverview>>(
          overviewListPath(filters, cursor),
        );
        // A completed 401/403 is route-level revocation. Suppress it only after
        // a newer successful overview has applied — a newer Refresh that has
        // merely started, or is hanging, is not recovery.
        if (!result.ok && (result.status === 401 || result.status === 403)) {
          pageError = result.message;
          pageAuthDenied = true;
          abortRetainedRefreshForPageFailure();
          return null;
        }
        // Transient page failures (5xx/network) retain the last-good rail.
        // Record the completed outage before the generation guard: suppress it
        // only after a newer successful overview has applied. A newer Refresh
        // that has merely started, or is hanging, is not recovery.
        if (!result.ok) {
          pageError = result.message;
          pageOutage = true;
          abortRetainedRefreshForPageFailure();
          return null;
        }
        if (
          epoch !== authorizedFeedEpochRef.current ||
          consoleAuthDeniedRef.current ||
          generation !== overviewRequestGenerationRef.current ||
          overviewQueryRef.current !== capturedQuery
        ) {
          return null;
        }
        return result.data;
      };
      const fetchSelectedOverview = async (
        workspaceId: string,
        replaceCurrentOverview = false,
      ) => {
        if (replaceCurrentOverview) {
          overviewIsolatedSelectionRef.current = { query: capturedQuery, workspaceId };
        }
        const result = await apiPostWithDeadline<OverviewBatchResponse>(
          awfPath("workspaces/overview/batch"), { workspace_ids: [workspaceId] },
        );
        if (!result.ok && (result.status === 401 || result.status === 403)) {
          applyOverviewAuthDenial(generation, result.message);
          return;
        }
        if (!result.ok) {
          overviewSelectionLookupRef.current = { query: capturedQuery, workspaceId };
          applyOverviewOutage(generation, result.message);
          return;
        }
        if (
          epoch !== authorizedFeedEpochRef.current ||
          consoleAuthDeniedRef.current ||
          generation !== overviewRequestGenerationRef.current ||
          overviewQueryRef.current !== capturedQuery ||
          generation <= revokedOverviewGenerationRef.current ||
          generation < appliedOverviewFailureGenerationRef.current
        ) {
          const lookup = overviewSelectionLookupRef.current;
          if (lookup?.query === capturedQuery && lookup.workspaceId === workspaceId) {
            overviewSelectionLookupRef.current = null;
          }
          return;
        }
        const isolatedSelection = overviewIsolatedSelectionRef.current;
        if (
          isolatedSelection?.query === capturedQuery &&
          isolatedSelection.workspaceId === workspaceId
        ) {
          overviewIsolatedSelectionRef.current = null;
        }
        overviewRetainedRefreshAbortControllerRef.current?.abort();
        overviewRetainedRefreshAbortControllerRef.current = null;
        appliedOverviewGenerationRef.current = Math.max(
          appliedOverviewGenerationRef.current,
          generation,
        );
        const selectedItems = normalizeOverview(result.data.items).filter((item) =>
          overviewItemMatchesQuery(item, capturedQuery),
        );
        const replaceCurrent =
          replaceCurrentOverview &&
          selectedItems.some((item) => item.workspace_id === workspaceId);
        const withSelected = replaceCurrent
          ? selectedItems
          : appendUniqueOverviewItems(overviewItemsRef.current, selectedItems);
        if (replaceCurrent) {
          overviewPaginationRef.current = null;
          setOverviewHasMore(false);
          setOverviewHistoryComplete(false);
          setOverviewHistoryError(false);
          setOverviewTruncationWarning(null);
        }
        overviewItemsRef.current = withSelected;
        setOverview(withSelected);
        if (
          result.data.missing_workspace_ids.includes(workspaceId) ||
          !selectedItems.some((item) => item.workspace_id === workspaceId)
        ) {
          setSelectedId(null);
        }
      };
      if (selectedLookupId) {
        await fetchSelectedOverview(selectedLookupId, true);
        return;
      }
      const page = await fetchOverviewPage(requestedCursor);
      if (pageAuthDenied) {
        applyOverviewAuthDenial(generation, pageError ?? "");
        return;
      }
      if (pageOutage) {
        applyOverviewOutage(generation, pageError ?? "");
        return;
      }
      if (
        epoch !== authorizedFeedEpochRef.current ||
        consoleAuthDeniedRef.current ||
        generation !== overviewRequestGenerationRef.current ||
        overviewQueryRef.current !== capturedQuery
      ) {
        return;
      }
      if (page === null) {
        return;
      }
      // A newer 401/403 already covers this generation, a newer success
      // already owns the rail, or a newer outage already owns the warning.
      // Do not restore revoked rows or clear a failure that landed after a
      // newer request started.
      if (
        generation <= revokedOverviewGenerationRef.current ||
        generation < appliedOverviewGenerationRef.current ||
        generation < appliedOverviewFailureGenerationRef.current
      ) {
        return;
      }
      const pageItems = normalizeOverview(page.items);
      const sameQuery = capturedPagination?.query === capturedQuery;
      const retainedRefreshCursor = overviewRetainedRefreshCursorRef.current;
      const retainedBatch = !continuation && sameQuery && page.has_more
        ? retainedOverviewIdBatch(
            overviewItemsRef.current,
            pageItems,
            retainedRefreshCursor.query === capturedQuery ? retainedRefreshCursor.offset : 0,
          )
        : { workspaceIds: [], nextOffset: 0 };
      // A successful newer live page supersedes older best-effort history.
      // Wait until success is known before aborting so a prior completed
      // authorization denial still wins over a merely-started request.
      overviewRetainedRefreshAbortControllerRef.current?.abort();
      overviewRetainedRefreshAbortControllerRef.current = null;
      appliedOverviewGenerationRef.current = Math.max(
        appliedOverviewGenerationRef.current,
        generation,
      );
      if (!continuation) {
        overviewRetainedRefreshCursorRef.current = {
          query: capturedQuery,
          offset: retainedBatch.nextOffset,
        };
      }
      if (continuation) {
        const fetchedCursors = new Set(capturedPagination?.fetchedCursors ?? []);
        if (usableContinuationCursor(requestedCursor)) {
          fetchedCursors.add(requestedCursor);
        }
        const nextCursor = usableContinuationCursor(page.next_cursor) ? page.next_cursor : null;
        const repeatedCursor =
          page.has_more &&
          nextCursor !== null &&
          (nextCursor === requestedCursor || fetchedCursors.has(nextCursor));
        overviewPaginationRef.current = {
          query: capturedQuery,
          nextCursor: page.has_more && !repeatedCursor ? nextCursor : null,
          complete: !page.has_more,
          fetchedCursors,
        };
        const appended = appendUniqueOverviewItems(overviewItemsRef.current, pageItems);
        overviewItemsRef.current = appended;
        setOverview(appended);
        setOverviewHasMore(page.has_more && nextCursor !== null && !repeatedCursor);
        setOverviewHistoryComplete(!page.has_more);
        setOverviewHistoryError(false);
        setOverviewTruncationWarning(
          page.has_more && !usableContinuationCursor(page.next_cursor)
            ? "Workspace list truncated: the overview feed reported more workspaces but omitted a continuation cursor, so later workspaces cannot be loaded."
            : repeatedCursor
              ? "Workspace list truncated: the overview feed repeated a continuation cursor, so later workspaces cannot be loaded safely."
              : null,
        );
      } else {
        const firstCursor = usableContinuationCursor(page.next_cursor) ? page.next_cursor : null;
        const retainedWorkspaceIds = new Set(
          overviewItemsRef.current.map((item) => item.workspace_id),
        );
        const refreshedPageOverlapsRetained = sameQuery && pageItems.some(
          (item) => retainedWorkspaceIds.has(item.workspace_id),
        );
        const pagination = !page.has_more
          ? {
              query: capturedQuery,
              nextCursor: null,
              complete: true,
              fetchedCursors: new Set<string>(),
            }
          : refreshedPageOverlapsRetained && capturedPagination?.complete
            ? capturedPagination
            : refreshedPageOverlapsRetained &&
                usableContinuationCursor(capturedPagination?.nextCursor)
              ? capturedPagination
              : {
                  query: capturedQuery,
                  nextCursor: firstCursor,
                  complete: false,
                  fetchedCursors: new Set<string>(),
                };
        overviewPaginationRef.current = pagination;
        const refreshed = sameQuery && page.has_more
          ? reconcileOverviewRetainedItems(
              overviewItemsRef.current,
              pageItems,
              [],
              [],
            )
          : appendUniqueOverviewItems([], pageItems);
        overviewItemsRef.current = refreshed;
        setOverview(refreshed);
        setOverviewHasMore(!pagination.complete && pagination.nextCursor !== null);
        setOverviewHistoryComplete(pagination.complete);
        setOverviewHistoryError(false);
        setOverviewTruncationWarning(
          page.has_more && !pagination.complete && !usableContinuationCursor(pagination.nextCursor)
            ? "Workspace list truncated: the overview feed reported more workspaces but omitted a continuation cursor, so later workspaces cannot be loaded."
            : null,
        );
      }
      // Clear only this feed's warning, and not over a newer failure or denial.
      setOverviewError((current) =>
        generation < appliedOverviewFailureGenerationRef.current ||
        generation <= revokedOverviewGenerationRef.current ||
        consoleAuthDeniedRef.current
          ? current
          : null,
      );
      if (!continuation) {
        setLastRefresh(new Date());
      }
      if (retainedBatch.workspaceIds.length > 0) {
        const retainedRefreshController = new AbortController();
        const retainedRefreshTimeout = window.setTimeout(
          () => retainedRefreshController.abort(),
          pollMs,
        );
        const refreshRetainedOverview = async () => {
          const refreshedRetainedItems: WorkspaceOverview[] = [];
          const missingRetainedIds: string[] = [];
          try {
            const result = await apiPost<OverviewBatchResponse>(
              awfPath("workspaces/overview/batch"),
              { workspace_ids: retainedBatch.workspaceIds },
              { signal: retainedRefreshController.signal },
            );
            if (retainedRefreshController.signal.aborted) {
              return;
            }
            if (!result.ok && (result.status === 401 || result.status === 403)) {
              applyOverviewAuthDenial(generation, result.message);
              return;
            }
            if (!result.ok) {
              applyOverviewOutage(generation, result.message);
              return;
            }
            if (
              epoch !== authorizedFeedEpochRef.current ||
              consoleAuthDeniedRef.current ||
              generation !== overviewRequestGenerationRef.current ||
              overviewQueryRef.current !== capturedQuery
            ) {
              return;
            }
            for (const item of normalizeOverview(result.data.items)) {
              if (overviewItemMatchesQuery(item, capturedQuery)) {
                refreshedRetainedItems.push(item);
              } else {
                missingRetainedIds.push(item.workspace_id);
              }
            }
            missingRetainedIds.push(...result.data.missing_workspace_ids);
            if (
              retainedRefreshController.signal.aborted ||
              epoch !== authorizedFeedEpochRef.current ||
              consoleAuthDeniedRef.current ||
              generation !== overviewRequestGenerationRef.current ||
              generation <= revokedOverviewGenerationRef.current ||
              generation < appliedOverviewFailureGenerationRef.current ||
              overviewQueryRef.current !== capturedQuery
            ) {
              return;
            }
            const refreshed = reconcileOverviewRetainedItems(
              overviewItemsRef.current,
              pageItems,
              refreshedRetainedItems,
              missingRetainedIds,
            );
            overviewItemsRef.current = refreshed;
            setOverview(refreshed);
            const currentSelectedId = selectedIdRef.current;
            if (
              currentSelectedId &&
              !overviewItemsRef.current.some((item) => item.workspace_id === currentSelectedId)
            ) {
              await fetchSelectedOverview(
                currentSelectedId,
                shouldIsolateSelection(currentSelectedId),
              );
            }
          } catch (error) {
            if (!retainedRefreshController.signal.aborted) {
              applyOverviewOutage(
                generation,
                error instanceof Error ? error.message : String(error),
              );
            }
          } finally {
            window.clearTimeout(retainedRefreshTimeout);
            if (overviewRetainedRefreshAbortControllerRef.current === retainedRefreshController) {
              overviewRetainedRefreshAbortControllerRef.current = null;
            }
          }
        };
        overviewRetainedRefreshAbortControllerRef.current = retainedRefreshController;
        void refreshRetainedOverview();
      }
      const currentSelectedId = selectedIdRef.current;
      if (
        currentSelectedId &&
        !page.has_more &&
        !overviewItemsRef.current.some((item) => item.workspace_id === currentSelectedId)
      ) {
        setSelectedId(null);
      }
      if (
        continuation ||
        !currentSelectedId ||
        !page.has_more ||
        overviewItemsRef.current.some((item) => item.workspace_id === currentSelectedId)
      ) {
        return;
      }
      // An initial deep link should still avoid mounting the intervening fleet.
      // Once its selected row has been installed, however, a later first-page
      // refresh must append that row without erasing the newly installed cursor.
      await fetchSelectedOverview(currentSelectedId, shouldIsolateSelection(currentSelectedId));
    } finally {
      // A superseded load must not clear the latch while a newer filter or
      // refresh load is still paging; periodic polls skip while this stays true.
      if (generation === overviewRequestGenerationRef.current) {
        overviewLoadInFlightRef.current = false;
        setOverviewHistoryLoading(false);
        setOverviewLoadSettledVersion((current) => current + 1);
      }
    }
  }, [
    authorizedFeedEpochRef,
    consoleAuthDeniedRef,
    overviewQueryRef,
    selectedIdRef,
    setSelectedId,
    overviewRequestGenerationRef,
    appliedOverviewGenerationRef,
    appliedOverviewFailureGenerationRef,
    revokedOverviewGenerationRef,
    overviewLoadInFlightRef,
    overviewRetainedRefreshAbortControllerRef,
    overviewRetainedRefreshCursorRef,
    overviewItemsRef,
    overviewPaginationRef,
    overviewSelectionLookupRef,
    overviewIsolatedSelectionRef,
    gatedDetailFeedGenerationRef,
    gatedDetailDroppedFeedsRef,
    workspaceDetailAuthDeniedRef,
    workspaceBaseDetailAuthDeniedRef,
    eventFeedAuthDeniedRef,
    logListingAuthDeniedRef,
    logTailAuthDeniedRef,
    selectedStreamsRef,
    logStreamActivityRef,
    setOverview,
    setOverviewError,
    setOverviewTruncationWarning,
    setOverviewHasMore,
    setOverviewHistoryLoading,
    setOverviewHistoryComplete,
    setOverviewHistoryError,
    setOverviewLoadSettledVersion,
    setLastRefresh,
    setApiState,
    setWorkspaceDetailError,
    setWorkspaceDetailAuthDenied,
    setEventFeedAuthDenied,
    setRetainedAgents,
    setRetainedModels,
    setAgentFilters,
    setModelFilters,
    setRepoFilter,
    setSearchText,
    setDetail,
    setLogListingAuthDenied,
    setLogTailAuthDenied,
    setSelectedStreams,
    setLogEntries,
    setStreamOffsets,
    setLogsFullscreen,
    setWorkspaceLogSelection,
    setFullscreenWorkspaceIds,
    setTaskDetailsWorkspaceId,
    setStreamState,
    setRetryState,
    setOperatorActionState,
  ]);

  return loadOverview;
}
