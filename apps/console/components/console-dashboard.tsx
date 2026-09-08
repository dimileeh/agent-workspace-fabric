"use client";

import { useSearchParams } from "next/navigation";
import {
useCallback,
useEffect,
useLayoutEffect,
useMemo,
useRef,
useState,
useTransition,
} from "react";
import { fallbackLlmUsage } from "@/lib/format";
import {
  capabilitiesForMutatingControls,
  sameCapabilityNegotiation,
  isDiagnosticAvailable,
  isWidgetAvailable,
  parseConsoleCapabilities,
  resolveCapabilityParseFailureClear,
} from "@/lib/console-capabilities";
import { fleetKpisFromDashboardSummary } from "@/lib/console-dashboard-summary";
import { awfPath, configuredContextFingerprint } from "@/lib/console-urls";
import { collectOverviewPages, overviewListPath } from "@/lib/overview-list";
import { useCapabilityGatedPoll } from "@/hooks/use-capability-gated-poll";
import { useConsoleFleetFeeds } from "@/hooks/use-console-fleet-feeds";
import { useSerializedPeriodicLoad } from "@/hooks/use-serialized-periodic-load";
import { useWorkspaceDetailLoader } from "@/hooks/use-workspace-detail-loader";
import { useOperatorThemePreferences, useWorkspaceSelectionUrl } from "@/hooks/use-operator-theme-preferences";
import { useOverviewQueryRef } from "@/hooks/use-overview-query-ref";
import { useWorkspaceLiveStream } from "@/hooks/use-workspace-live-stream";
import { useWorkspaceLogTails } from "@/hooks/use-workspace-log-tails";
import { useWorkspaceMutatingControls } from "@/hooks/use-workspace-mutating-controls";
import type {
  CloudRuntimeSummary,
  ConsoleCapabilities,
  ConsoleDashboardSummary,
  FailureSummaryResponse,
ListEnvelope,
MergeQueueItem,
ResourceSaturationSummary,
WorkspaceOverview,
WorkspaceReliabilitySummary,
} from "@/lib/types";
import { getWorkspaceOperatorControls } from "@/lib/workspace-operator-controls";
import { ConsoleDashboardFleetPanels } from "./console-dashboard-fleet-panels";
import { ConsoleDashboardInspector } from "./console-dashboard-inspector";
import { ConsoleDashboardOverlays } from "./console-dashboard-overlays";
import { type FleetKpi,FleetHealthStrip,SectionNav,TopBar } from "./console-dashboard-overview";
import { ConsoleDashboardWorkspaceRail } from "./console-dashboard-workspace-rail";
import {
  DROP_ALL_GATED_DETAIL_FEEDS,
  filterAndSortOverview,
  gatedDetailDropFromWithdrawal,
  noteGatedDetailDrop,
  planCapabilityFeedWithdrawal,
  resolveDashboardPanelVisibility,
  type GatedDetailDropStamp,
} from "@/lib/console-dashboard-derived";
import {
type DetailState,
type LogEntry,
type LogStreamActivityMap,
type MergeQueueStatus,
type OperatorActionState,
type RetryActionState,
type SortDirection,
type WorkspaceSortKey,
ErrorBanner,
apiGet,
compareLogEntries,
emptyDetail,
toLogWorkspaceTarget,
toggleStream,
toggleWorkspaceSelection,
} from "./console-dashboard-shared";

export function ConsoleDashboard() {
  const { operatorPreferences, updateOperatorPreferences } = useOperatorThemePreferences();
  const [overview, setOverview] = useState<WorkspaceOverview[]>([]);
  const searchParams = useSearchParams();
  const { selectedId, selectedIdRef, setSelectedId } = useWorkspaceSelectionUrl(
    searchParams,
    searchParams.get("workspaceId"),
  );
  const [detail, setDetail] = useState<DetailState>(emptyDetail);
  const [selectedStreams, setSelectedStreams] = useState<string[]>([]);
  const [logEntries, setLogEntries] = useState<LogEntry[]>([]);
  const [streamOffsets, setStreamOffsets] = useState<Record<string, number>>({});
  const [logsFullscreen, setLogsFullscreen] = useState(false);
  const [workspaceLogSelection, setWorkspaceLogSelection] = useState<string[]>([]);
  const [fullscreenWorkspaceIds, setFullscreenWorkspaceIds] = useState<string[]>([]);
  const [taskDetailsWorkspaceId, setTaskDetailsWorkspaceId] = useState<string | null>(null);
  const [logTailSignal, setLogTailSignal] = useState(0);
  const [fullscreenTailSignal, setFullscreenTailSignal] = useState(0);
  const [logSortDirection, setLogSortDirection] = useState<SortDirection>("asc");
  const [statusFilters, setStatusFilters] = useState<string[]>([]);
  const [agentFilters, setAgentFilters] = useState<string[]>([]);
  const [modelFilters, setModelFilters] = useState<string[]>([]);
  const [repoFilter, setRepoFilter] = useState("");

  const [searchText, setSearchText] = useState("");
  const [sortKey, setSortKey] = useState<WorkspaceSortKey>("updated_at");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [filtersExpanded, setFiltersExpanded] = useState(false);
  const [resourceSaturation, setResourceSaturation] = useState<ResourceSaturationSummary | null>(null);
  const [resourceError, setResourceError] = useState<string | null>(null);
  const [workspaceSummary, setWorkspaceSummary] = useState<WorkspaceReliabilitySummary | null>(null);
  const [workspaceSummaryError, setWorkspaceSummaryError] = useState<string | null>(null);
  const [mergeQueue, setMergeQueue] = useState<MergeQueueItem[]>([]);
  const [mergeQueueHasMore, setMergeQueueHasMore] = useState(false);
  const [mergeQueueStatus, setMergeQueueStatus] = useState<MergeQueueStatus>("loading");
  const [mergeQueueError, setMergeQueueError] = useState<string | null>(null);
  const [failureSummary, setFailureSummary] = useState<FailureSummaryResponse | null>(null);
  const [failureSummaryStatus, setFailureSummaryStatus] = useState<"loading" | "success" | "error" | "unavailable">("loading");
  const [failureSummaryError, setFailureSummaryError] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<ConsoleCapabilities | null>(null);
  const [capabilitiesReady, setCapabilitiesReady] = useState(false);
  const [capabilityError, setCapabilityError] = useState<string | null>(null);
  const [dashboardSummary, setDashboardSummary] = useState<ConsoleDashboardSummary | null>(null);
  const [dashboardSummaryError, setDashboardSummaryError] = useState<string | null>(null);
  const [cloudRuntime, setCloudRuntime] = useState<CloudRuntimeSummary | null>(null);
  const [cloudRuntimeError, setCloudRuntimeError] = useState<string | null>(null);
  const [retryState, setRetryState] = useState<RetryActionState>({ status: "idle" });
  const [operatorActionState, setOperatorActionState] = useState<OperatorActionState>({ status: "idle" });
  const [apiState, setApiState] = useState<"checking" | "ok" | "error">("checking");
  const [streamState, setStreamState] = useState<"idle" | "connecting" | "live" | "error">("idle");
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  // Overview and selected-workspace diagnostic errors are independent feeds.
  // A successful overview poll must not clear a retained runtime/events/
  // operations/logs/stream warning, and a recovered detail load must not
  // dismiss an overview outage. Truncation is a third slot for the same reason.
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [workspaceDetailError, setWorkspaceDetailError] = useState<string | null>(null);
  const [overviewTruncationWarning, setOverviewTruncationWarning] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();
  const logStreamActivityRef = useRef<LogStreamActivityMap>({});
  const selectedStreamsRef = useRef<string[]>([]);
  // Listing 401/403 while workspace_logs stays advertised. Live log frames and
  // in-flight tails must not refill caches the detail loader just cleared.
  const logListingAuthDeniedRef = useRef(false);
  // Mirrors the ref so the live-stream effect can close EventSource on listing
  // 401/403. The ref alone does not re-run the effect, so the source stayed open.
  const [logListingAuthDenied, setLogListingAuthDenied] = useState(false);
  // Tail 401/403 while listing stays reachable. Listing success clears the
  // listing latch, so this separate latch is what keeps /stream closed.
  const logTailAuthDeniedRef = useRef(false);
  const [logTailAuthDenied, setLogTailAuthDenied] = useState(false);
  // Base /workspaces/{id} 401/403 while workspace_stream stays advertised.
  // Listing/tail latches do not cover this path. Snapshot frames must not
  // write revoked workspace metadata back until a successful detail GET.
  const workspaceDetailAuthDeniedRef = useRef(false);
  const [workspaceDetailAuthDenied, setWorkspaceDetailAuthDenied] = useState(false);
  // /events 401/403 while workspace_stream stays advertised. The detail loader
  // clears detail.events, but EventSource event frames must not refill the
  // panel until a successful /events read recovers the feed.
  const eventFeedAuthDeniedRef = useRef(false);
  const [, setEventFeedAuthDenied] = useState(false);
  // Bumped on auth/tenant clear so in-flight feed responses cannot restore wiped data.
  const authorizedFeedEpochRef = useRef(0);
  // Sync auth-denial latch (React state lags behind clearAuthorizedConsoleFeeds).
  const consoleAuthDeniedRef = useRef(false);
  // Capability poll generation: discard stale 200/404 responses after a newer
  // request. A 401/403 and a network/5xx outage stay authoritative unless a
  // newer successful negotiation has already been applied — a newer request
  // merely starting is not recovery.
  const capabilityRequestGenerationRef = useRef(0);
  // Highest capability generation that applied a successful negotiation.
  // An older 401/403 must not clear feeds this newer success already owns.
  // An older network/5xx outage must not replace the error that success cleared.
  const appliedCapabilityGenerationRef = useRef(0);
  // Highest capability generation that applied a network/5xx outage. A newer
  // request merely starting is not recovery. An older success must not clear
  // an outage this newer failure already applied, or last-good capabilities
  // keep enabling mutating controls with no stale/error indication.
  const appliedCapabilityFailureGenerationRef = useRef(0);
  // Highest capability generation covered by an applied 401/403. An older
  // overlapping 200 (started before that denial) must not restore cleared
  // feeds. Re-applying a denial already inside this window must not raise
  // the watermark, or a recovery request that started after the original
  // denial would be rejected.
  const revokedCapabilityGenerationRef = useRef(0);
  // Periodic capability polls chain after the previous invocation settles and
  // skip while a request is still in flight. A wall-clock interval that calls
  // loadCapabilities would advance generation and discard every slower-than-
  // pollMs success, leaving the console permanently unnegotiated.
  const capabilityLoadInFlightRef = useRef(false);
  // Last applied inventory — detect available→unsupported under a stable identity.
  const appliedCapabilitiesRef = useRef<ConsoleCapabilities | null>(null);
  // Survives capabilities 404 gated clears (React identity state is nulled so
  // optional polls stop). Recovery compares against this so a backend/tenant
  // switch without URL-context change is not mistaken for bootstrap.
  const lastCapabilityIdentityKeyRef = useRef<string | null>(null);
  // Last configured context fingerprint; null = uninitialized ("" is valid locally).
  const configuredContextFingerprintRef = useRef<string | null>(null);
  const overviewQueryRef = useOverviewQueryRef(statusFilters, agentFilters, repoFilter);
  // Overview poll generation: overlapping filter/explicit loads stay monotonic.
  // repoFilter is server-side only (filterAndSortOverview does not reapply it), so a
  // superseded paginated response must not overwrite a newer filtered rail.
  const overviewRequestGenerationRef = useRef(0);
  // Periodic polls chain after the previous invocation settles and skip while a
  // collection is still paging. A wall-clock interval that calls loadOverview
  // would advance generation and cancel that collector; if every page walk
  // exceeds pollMs, the rail stays empty or permanently stale.
  const overviewLoadInFlightRef = useRef(false);
  // Summary poll generation: older success/error must not replace newer state.
  const dashboardSummaryRequestGenerationRef = useRef(0);
  // Cloud-runtime poll generation: overlapping interval/manual ticks stay monotonic.
  const cloudRuntimeRequestGenerationRef = useRef(0);
  // Merge-queue poll generation: feed-level 401/403 clear must not lose to an
  // older in-flight 200 (capabilities may still keep the panel mounted).
  const mergeQueueRequestGenerationRef = useRef(0);
  // Resource-capacity / reliability / failures poll generations: same feed-level
  // 401/403 + overlapping-poll contract as merge-queue / dashboard-summary.
  const resourceSaturationRequestGenerationRef = useRef(0);
  const workspaceSummaryRequestGenerationRef = useRef(0);
  const failureSummaryRequestGenerationRef = useRef(0);
  // Gated detail/inventory generation: bumped on capabilities 404 / same-identity
  // inspector-detail withdrawal without touching authorizedFeedEpochRef.
  // Optional feeds discard on mismatch; the basic workspace GET still applies.
  // Unrelated fleet withdrawals must not bump this — they already invalidate
  // their own request generations and would otherwise clear detail errors.
  const gatedDetailFeedGenerationRef = useRef(0);
  const gatedDetailDroppedFeedsRef = useRef<GatedDetailDropStamp[]>([]);

  const [retainedAgents, setRetainedAgents] = useState<string[]>([]);
  const [retainedModels, setRetainedModels] = useState<string[]>([]);

  useEffect(() => {
    const agents = overview.map((w) => w.agent).filter((a): a is string => Boolean(a));
    if (agents.length > 0) {
      setRetainedAgents((prev) => Array.from(new Set([...prev, ...agents])).sort());
    }
    const models = overview.map((w) => w.agent_model).filter((m): m is string => Boolean(m));
    if (models.length > 0) {
      setRetainedModels((prev) => Array.from(new Set([...prev, ...models])).sort());
    }
  }, [overview]);

  const availableModels = useMemo(() => {
    const currentModels = overview.map((w) => w.agent_model).filter((m): m is string => Boolean(m));
    return Array.from(new Set([...retainedModels, ...currentModels])).sort();
  }, [overview, retainedModels]);

  const availableAgents = useMemo(() => {
    const currentAgents = overview.map((w) => w.agent).filter((a): a is string => Boolean(a));
    return Array.from(new Set([...retainedAgents, ...currentAgents])).sort();
  }, [overview, retainedAgents]);

  useEffect(() => {
    selectedStreamsRef.current = selectedStreams;
  }, [selectedStreams]);

  const loadOverview = useCallback(async () => {
    const epoch = authorizedFeedEpochRef.current;
    // Auth revocation must not refill previously authorized workspace rows.
    // Non-auth capability failures keep legacy-safe overview navigation.
    if (consoleAuthDeniedRef.current) {
      setOverview([]);
      return;
    }
    // Stamp after the auth-denial early return so a denied no-op cannot invalidate
    // an in-flight recovery load that already cleared the latch and advanced.
    // Capture the query snapshot with the generation so pagination stays pinned to
    // the filters that started this load; repoFilter is server-side only.
    const generation = ++overviewRequestGenerationRef.current;
    overviewLoadInFlightRef.current = true;
    try {
      const capturedQuery = overviewQueryRef.current;
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

      // Build per request so hosted context query keys (org_id/project_id) are
      // read from the current page search after client-side tenant switches —
      // do not memoize on filter state alone. Filter values come from the captured
      // snapshot so this callback identity stays stable across filter edits.
      const { statusFilters: statuses, agentFilters: agents, repoFilter: repo } =
        capturedQuery;
      const filters = {
        status: statuses.length === 1 ? statuses[0] : undefined,
        agent: agents.length === 1 ? agents[0] : undefined,
        repo_url: repo.trim() || undefined,
      };
      // Overview is cursor-paginated; accumulate pages so the rail, client search,
      // multi-value filters, and log selection see every matching workspace.
      let pageError: string | null = null;
      let pageAuthDenied = false;
      const collected = await collectOverviewPages(async (cursor) => {
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
        if (
          epoch !== authorizedFeedEpochRef.current ||
          consoleAuthDeniedRef.current ||
          generation !== overviewRequestGenerationRef.current ||
          overviewQueryRef.current !== capturedQuery
        ) {
          return null;
        }
        if (!result.ok) {
          pageError = result.message;
          // Feed-level 401/403 is auth revocation for this snapshot, not a
          // transient pagination outage (CONSOLE_BACKEND_CONTRACT).
          if (result.status === 401 || result.status === 403) {
            pageAuthDenied = true;
          }
          return null;
        }
        return result.data;
      });
      if (
        epoch !== authorizedFeedEpochRef.current ||
        consoleAuthDeniedRef.current ||
        generation !== overviewRequestGenerationRef.current ||
        overviewQueryRef.current !== capturedQuery
      ) {
        return;
      }
      if (collected === null) {
        // Transient page failures (5xx/network) retain the last-good authorized
        // overview so rail/inspector stay usable; only 401/403 clears it.
        if (pageError !== null) {
          setOverviewError(pageError);
        }
        if (pageAuthDenied) {
          // Overview feed auth denial: drop the rail and close dependent workspace
          // surfaces (selection, inspector, logs, fullscreen). Do not call
          // clearAuthorizedConsoleFeeds — other feeds clear themselves, and
          // capabilities may still succeed without an auth-denial latch thrashing
          // overview refill. Bump gated-detail generation so in-flight
          // loadWorkspace / log-tail cannot restore revoked caches.
          noteGatedDetailDrop(gatedDetailDroppedFeedsRef, gatedDetailFeedGenerationRef, DROP_ALL_GATED_DETAIL_FEEDS);
          setOverview([]);
          setOverviewTruncationWarning(null);
          // Inspector surfaces are wiped with the rail; drop the detail warning
          // so a retained diagnostic error does not outlive the cleared snapshot.
          setWorkspaceDetailError(null);
          workspaceDetailAuthDeniedRef.current = false;
          setWorkspaceDetailAuthDenied(false);
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
        }
        return;
      }
      // Never treat a capped prefix as a complete fleet: surface truncation so
      // rail/search/log selection cannot silently omit later workspaces.
      // Keep this off both feed error slots so neither poll can clear it.
      setOverviewTruncationWarning(
        collected.truncated
          ? collected.truncationReason === "missing_cursor"
            ? "Workspace list truncated: the overview feed reported more workspaces but omitted a continuation cursor, so later workspaces cannot be loaded."
            : "Workspace list truncated: more matching workspaces exist beyond the loaded pages. Narrow filters or raise the overview page budget."
          : null,
      );
      // Clear only the overview warning. A still-failing workspace-detail feed
      // retains last-good inspector data and must keep its own banner.
      setOverviewError(null);
      setOverview(
        collected.items.map((item) => ({
          ...item,
          task_prompt: item.task_prompt ?? "",
          lifecycle: item.lifecycle ?? [],
          llm_usage: fallbackLlmUsage(item.llm_usage),
          recovery: item.recovery ?? null,
        })),
      );
      setLastRefresh(new Date());
      const currentSelectedId = selectedIdRef.current;
      if (currentSelectedId && !collected.items.some((item) => item.workspace_id === currentSelectedId)) {
        setSelectedId(null);
      }
    } finally {
      // A superseded load must not clear the latch while a newer filter or
      // refresh load is still paging; periodic polls skip while this stays true.
      if (generation === overviewRequestGenerationRef.current) {
        overviewLoadInFlightRef.current = false;
      }
    }
  }, [setSelectedId]);

  const clearAuthorizedConsoleFeeds = useCallback((options?: { clearCapabilities?: boolean; authDenied?: boolean }) => {
    authorizedFeedEpochRef.current += 1;
    if (options?.authDenied) {
      consoleAuthDeniedRef.current = true;
    }
    setResourceSaturation(null);
    setResourceError(null);
    setWorkspaceSummary(null);
    setWorkspaceSummaryError(null);
    setMergeQueue([]);
    setMergeQueueHasMore(false);
    setMergeQueueStatus("loading");
    setMergeQueueError(null);
    setFailureSummary(null);
    setFailureSummaryStatus("loading");
    setFailureSummaryError(null);
    setDashboardSummary(null);
    setDashboardSummaryError(null);
    setCloudRuntime(null);
    setCloudRuntimeError(null);
    // Workspace list / inspector / logs / events are authorized surfaces too —
    // wipe them on auth denial or tenant/backend identity change so revocation
    // and cross-context reuse cannot fail open with prior rows still on screen.
    setOverview([]);
    setOverviewError(null);
    setWorkspaceDetailError(null);
    workspaceDetailAuthDeniedRef.current = false;
    setWorkspaceDetailAuthDenied(false);
    eventFeedAuthDeniedRef.current = false;
    setEventFeedAuthDenied(false);
    setOverviewTruncationWarning(null);
    setRetainedAgents([]);
    setRetainedModels([]);
    // Selected agent/model filters are tenant-learned identifiers; WorkspaceFilters
    // re-injects them into option lists, so leave them active across auth/tenant
    // clears and the prior context keeps filtering (and often emptying) the new one.
    // repoFilter is applied server-side on the next overview request; searchText
    // filters client-side — both must reset or a prior tenant's criteria hide the new list.
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
    if (options?.clearCapabilities) {
      appliedCapabilitiesRef.current = null;
      lastCapabilityIdentityKeyRef.current = null;
      setCapabilities(null);
    }
  }, [setSelectedId]);

  // Missing/rolled-back negotiation (capabilities 404): drop optional inventories so
  // gated polls stop, but keep overview/selection/basic detail. Do not bump
  // authorizedFeedEpochRef — a five-second 404 poll would otherwise invalidate
  // concurrent overview loads and blank legacy-safe navigation
  // (CONSOLE_BACKEND_CONTRACT). Bump gatedDetailFeedGenerationRef only when
  // leaving a negotiated snapshot, so in-flight optional detail feeds,
  // log-tails, and gated inventories cannot restore cleared data. A persistent
  // 404 poll must not bump that generation again — doing so discards
  // overlapping /workspaces/{id} loads whose latency exceeds the capability
  // interval and leaves the inspector empty. The basic workspace GET still
  // applies when only that generation changed. Retain
  // lastCapabilityIdentityKeyRef so a later identity switch is not treated as
  // bootstrap.
  const clearCapabilityGatedInventories = useCallback(() => {
    dashboardSummaryRequestGenerationRef.current += 1;
    cloudRuntimeRequestGenerationRef.current += 1;
    mergeQueueRequestGenerationRef.current += 1;
    resourceSaturationRequestGenerationRef.current += 1;
    workspaceSummaryRequestGenerationRef.current += 1;
    failureSummaryRequestGenerationRef.current += 1;
    // Already-cleared negotiation: another 404/malformed poll has no optional
    // snapshot left to invalidate. Repeating this bump is what starves the
    // basic workspace GET.
    if (appliedCapabilitiesRef.current !== null) {
      noteGatedDetailDrop(gatedDetailDroppedFeedsRef, gatedDetailFeedGenerationRef, DROP_ALL_GATED_DETAIL_FEEDS);
    }
    setResourceSaturation(null);
    setResourceError(null);
    setWorkspaceSummary(null);
    setWorkspaceSummaryError(null);
    setMergeQueue([]);
    setMergeQueueHasMore(false);
    setMergeQueueStatus("loading");
    setMergeQueueError(null);
    setFailureSummary(null);
    setFailureSummaryStatus("loading");
    setFailureSummaryError(null);
    setDashboardSummary(null);
    setDashboardSummaryError(null);
    setCloudRuntime(null);
    setCloudRuntimeError(null);
    setDetail((current) => ({
      ...current,
      runtime: null,
      events: [],
      operations: [],
      streams: [],
    }));
    logListingAuthDeniedRef.current = false;
    setLogListingAuthDenied(false);
    logTailAuthDeniedRef.current = false;
    setLogTailAuthDenied(false);
    if (eventFeedAuthDeniedRef.current && !workspaceDetailAuthDeniedRef.current) {
      setWorkspaceDetailError(null);
    }
    eventFeedAuthDeniedRef.current = false;
    setEventFeedAuthDenied(false);
    selectedStreamsRef.current = [];
    setSelectedStreams([]);
    setLogEntries([]);
    setStreamOffsets({});
    // Missing/malformed negotiation drops workspace_logs — close fullscreen so
    // allowFullscreenLogs false does not leave logsFullscreen latched for remount.
    setLogsFullscreen(false);
    setFullscreenWorkspaceIds([]);
    appliedCapabilitiesRef.current = null;
    setCapabilities(null);
  }, []);

  // Same-identity inventory can withdraw a feed without changing the epoch key.
  // Clear that feed's cache and bump gated/read generations so in-flight responses
  // cannot restore withdrawn data — without advancing authorizedFeedEpochRef, which
  // would strand in-flight retry/operator mutations in `submitting`.
  const clearNewlyUnsupportedCapabilityFeeds = useCallback(
    (previous: ConsoleCapabilities, next: ConsoleCapabilities) => {
      const plan = planCapabilityFeedWithdrawal(previous, next);
      if (plan.clearDashboardSummary) {
        dashboardSummaryRequestGenerationRef.current += 1;
        setDashboardSummary(null);
        setDashboardSummaryError(null);
      }
      if (plan.clearResourceCapacity) {
        resourceSaturationRequestGenerationRef.current += 1;
        setResourceSaturation(null);
        setResourceError(null);
      }
      if (plan.clearCloudRuntime) {
        cloudRuntimeRequestGenerationRef.current += 1;
        setCloudRuntime(null);
        setCloudRuntimeError(null);
      }
      if (plan.clearReliability) {
        workspaceSummaryRequestGenerationRef.current += 1;
        setWorkspaceSummary(null);
        setWorkspaceSummaryError(null);
      }
      if (plan.clearMergeQueue) {
        mergeQueueRequestGenerationRef.current += 1;
        setMergeQueue([]);
        setMergeQueueHasMore(false);
        setMergeQueueStatus("loading");
        setMergeQueueError(null);
      }
      if (plan.clearFailures) {
        failureSummaryRequestGenerationRef.current += 1;
        setFailureSummary(null);
        setFailureSummaryStatus("loading");
        setFailureSummaryError(null);
      }
      // Inspector detail feeds: same-identity withdrawal must clear caches and
      // bump gated-detail generation so in-flight optional feeds cannot restore
      // withdrawn data. The basic workspace GET still applies (mutations keep
      // their authorized epoch).
      if (plan.clearRuntime || plan.clearEvents || plan.clearOperations || plan.clearLogs) {
        setDetail((current) => ({
          ...current,
          runtime: plan.clearRuntime ? null : current.runtime,
          events: plan.clearEvents ? [] : current.events,
          operations: plan.clearOperations ? [] : current.operations,
          streams: plan.clearLogs ? [] : current.streams,
        }));
        if (plan.clearLogs) {
          logListingAuthDeniedRef.current = false;
          setLogListingAuthDenied(false);
          logTailAuthDeniedRef.current = false;
          setLogTailAuthDenied(false);
          selectedStreamsRef.current = [];
          setSelectedStreams([]);
          setLogEntries([]);
          setStreamOffsets({});
          // Close (not only omit) fullscreen when listing is withdrawn mid-view.
          setLogsFullscreen(false);
          setFullscreenWorkspaceIds([]);
        }
        if (plan.clearEvents) {
          // workspace_events withdrawal leaves no later /events read that can
          // clear this latch, so a basic-detail 200 must not keep the denial
          // banner until the workspace or context changes.
          if (eventFeedAuthDeniedRef.current && !workspaceDetailAuthDeniedRef.current) {
            setWorkspaceDetailError(null);
          }
          eventFeedAuthDeniedRef.current = false;
          setEventFeedAuthDenied(false);
        }
        // Unrelated fleet/capacity withdrawals bump only their own request
        // generations. Sharing this generation would make an in-flight detail
        // load ignore still-advertised runtime/events/operations/log failures.
        noteGatedDetailDrop(gatedDetailDroppedFeedsRef, gatedDetailFeedGenerationRef, gatedDetailDropFromWithdrawal(plan));
      }
    },
    [],
  );

  const invalidateAuthorizedFeedsIfContextChanged = useCallback(
    (pageSearch?: string): boolean => {
      const next = configuredContextFingerprint(pageSearch);
      const previous = configuredContextFingerprintRef.current;
      configuredContextFingerprintRef.current = next;
      if (previous === null || previous === next) {
        return false;
      }
      clearAuthorizedConsoleFeeds({ clearCapabilities: true });
      return true;
    },
    [clearAuthorizedConsoleFeeds],
  );

  const loadCapabilities = useCallback(async (): Promise<ConsoleCapabilities | null> => {
    // Soft tenant switches update the URL before capabilities return; clear
    // authorized surfaces immediately so prior-tenant rows/controls cannot linger.
    invalidateAuthorizedFeedsIfContextChanged();
    const generation = ++capabilityRequestGenerationRef.current;
    const contextFingerprint = configuredContextFingerprintRef.current;
    capabilityLoadInFlightRef.current = true;
    try {
      const result = await apiGet<ConsoleCapabilities>(awfPath("console/capabilities"));
      const applyAuthoritativeCapabilityDenial = (deniedGeneration: number, message: string) => {
        // A soft tenant switch already owns the console. An older context's
        // 401/403 must not latch denial onto the new fingerprint.
        if (contextFingerprint !== configuredContextFingerprintRef.current) {
          return;
        }
        // A newer successful negotiation already owns the console. A late
        // 401/403 from an older request must not clear it.
        if (deniedGeneration < appliedCapabilityGenerationRef.current) {
          return;
        }
        // This request started inside an already-applied denial window.
        // Raising the watermark here would reject a recovery request that
        // started after the original denial.
        if (
          consoleAuthDeniedRef.current &&
          deniedGeneration <= revokedCapabilityGenerationRef.current
        ) {
          return;
        }
        // Cover every capability request that has already started so an
        // in-flight refresh cannot restore cleared feeds. A request that
        // starts after this watermark may recover.
        revokedCapabilityGenerationRef.current = Math.max(
          revokedCapabilityGenerationRef.current,
          capabilityRequestGenerationRef.current,
        );
        clearAuthorizedConsoleFeeds({ clearCapabilities: true, authDenied: true });
        setCapabilityError(message);
        setCapabilities(null);
        setCapabilitiesReady(true);
      };
      const applyTransientCapabilityOutage = (failedGeneration: number, message: string) => {
        // A soft tenant switch already owns the console. An older context's
        // network/5xx must not latch an outage onto the new fingerprint.
        if (contextFingerprint !== configuredContextFingerprintRef.current) {
          return false;
        }
        // A newer successful negotiation already owns the console. A late
        // 5xx from an older request must not re-latch capabilityError.
        if (failedGeneration < appliedCapabilityGenerationRef.current) {
          return false;
        }
        // A newer outage already owns the warning.
        if (failedGeneration < appliedCapabilityFailureGenerationRef.current) {
          return false;
        }
        // A 401/403 already covers this generation. Do not replace the
        // authorization reason or restore cleared capabilities.
        if (
          failedGeneration <= revokedCapabilityGenerationRef.current ||
          consoleAuthDeniedRef.current
        ) {
          return false;
        }
        appliedCapabilityFailureGenerationRef.current = Math.max(
          appliedCapabilityFailureGenerationRef.current,
          failedGeneration,
        );
        // Re-check in the updater: a newer success or denial can settle after
        // this outage is queued.
        setCapabilityError((current) =>
          failedGeneration < appliedCapabilityGenerationRef.current ||
          failedGeneration < appliedCapabilityFailureGenerationRef.current ||
          consoleAuthDeniedRef.current
            ? current
            : message,
        );
        setCapabilitiesReady(true);
        // Transient capability-endpoint outage (5xx/network): keep the last successful
        // negotiation so fleet KPIs and inspector detail retain last-good snapshots
        // while the error is shown. Mutating controls fail closed via
        // capabilitiesForMutatingControls(capabilities, capabilityError) until
        // negotiation succeeds again. Auth denial and never-negotiated stay fail-closed.
        const retained = appliedCapabilitiesRef.current;
        if (retained === null) {
          setCapabilities(null);
        }
        return true;
      };
      // Apply even if a newer request has started but has not yet established
      // recovery. A newer request merely starting, hanging, or failing
      // transiently is not recovery.
      if (!result.ok && (result.status === 401 || result.status === 403)) {
        applyAuthoritativeCapabilityDenial(generation, result.message);
        return null;
      }
      // Transient failures follow the same rule as 401/403: suppress only after
      // a newer successful negotiation has applied. Discarding a completed
      // network/5xx only because Refresh started a newer request leaves
      // capabilityError null, so retained capabilities keep enabling mutating
      // controls if that newer request hangs.
      if (!result.ok && result.status !== 404) {
        const applied = applyTransientCapabilityOutage(generation, result.message);
        return applied ? appliedCapabilitiesRef.current : null;
      }
      if (
        generation !== capabilityRequestGenerationRef.current ||
        generation <= revokedCapabilityGenerationRef.current ||
        generation < appliedCapabilityGenerationRef.current
      ) {
        return null;
      }
      // A newer network/5xx already applied the outage. This older 404/200
      // must not clear it, or last-good capabilities stay current with no error.
      if (generation < appliedCapabilityFailureGenerationRef.current) {
        return null;
      }
      if (!result.ok) {
        // Missing/rolled-back negotiation: clear gated inventories so optional
        // feeds stop polling, without wiping legacy-safe workspace navigation
        // (CONSOLE_BACKEND_CONTRACT — no inferred privileges). Non-404 failures
        // already applied above; this return keeps result narrowed for success.
        if (result.status === 404) {
          clearCapabilityGatedInventories();
          setCapabilityError(result.message);
          setCapabilitiesReady(true);
        }
        return null;
      }

      const parsed = parseConsoleCapabilities(result.data);
      if (!parsed.ok) {
        // Identity is extracted independently of inventory malformations. Preserve
        // legacy-safe overview nav only when the failed payload still carries the
        // same trusted identity; otherwise wipe authorized feeds and advance the
        // epoch so late prior-tenant overview rows cannot apply
        // (CONSOLE_BACKEND_CONTRACT — malformed ≡ missing/404 only for unchanged
        // trusted identity).
        const clearAction = resolveCapabilityParseFailureClear({
          priorIdentityKey: lastCapabilityIdentityKeyRef.current,
          trustedIdentityKey: parsed.trustedIdentityKey,
        });
        if (clearAction === "clear_authorized") {
          clearAuthorizedConsoleFeeds({ clearCapabilities: true });
        } else {
          clearCapabilityGatedInventories();
        }
        setCapabilityError(parsed.message);
        setCapabilitiesReady(true);
        return null;
      }

      // Keep the prior object when only generated_at (or equivalent) changed so
      // effects that depend on `capabilities` do not restart every poll cycle
      // (dashboard feeds + selected workspace SSE reconnect / missed events).
      const previous = appliedCapabilitiesRef.current;
      const nextCapabilities =
        previous !== null && sameCapabilityNegotiation(previous, parsed.capabilities)
          ? previous
          : parsed.capabilities;

      // Skip bootstrap (null → first key) so the parallel overview fetch is not wiped.
      // Identity clear advances the feed epoch; a concurrent loadOverview that
      // captured the prior epoch must be restarted or the new tenant list stays
      // blank until the next poll tick. Compare the retained ref — not React state —
      // so a 404 gap cannot disguise a different backend/tenant as bootstrap.
      let identityChanged = false;
      const priorIdentityKey = lastCapabilityIdentityKeyRef.current;
      if (priorIdentityKey !== null && parsed.identityKey !== priorIdentityKey) {
        clearAuthorizedConsoleFeeds();
        identityChanged = true;
      } else if (previous !== null && nextCapabilities !== previous) {
        clearNewlyUnsupportedCapabilityFeeds(previous, nextCapabilities);
      }
      // Capture before clear: a concurrent loadOverview (context sync / poll) may
      // still have refused while the latch was set; refill immediately so recovery
      // does not wait for the next overview poll tick.
      const wasAuthDenied = consoleAuthDeniedRef.current;
      consoleAuthDeniedRef.current = false;
      appliedCapabilityGenerationRef.current = Math.max(
        appliedCapabilityGenerationRef.current,
        generation,
      );
      appliedCapabilitiesRef.current = nextCapabilities;
      lastCapabilityIdentityKeyRef.current = parsed.identityKey;
      setCapabilities(nextCapabilities);
      setCapabilityError((current) =>
        generation < appliedCapabilityFailureGenerationRef.current ? current : null,
      );
      setCapabilitiesReady(true);
      if (wasAuthDenied || identityChanged) {
        void loadOverview();
      }
      return nextCapabilities;
    } finally {
      // A superseded explicit refresh or context sync must not clear the latch
      // while that newer request is still in flight; periodic polls skip while
      // this stays true.
      if (generation === capabilityRequestGenerationRef.current) {
        capabilityLoadInFlightRef.current = false;
      }
    }
  }, [
    clearAuthorizedConsoleFeeds,
    clearCapabilityGatedInventories,
    clearNewlyUnsupportedCapabilityFeeds,
    invalidateAuthorizedFeedsIfContextChanged,
    loadOverview,
  ]);

  const {
    loadResourceSaturation,
    loadDashboardSummary,
    loadCloudRuntime,
    loadWorkspaceSummary,
    loadMergeQueue,
    loadFailureSummary,
    reloadAvailableFeeds,
  } = useConsoleFleetFeeds({
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
  });

  const { loadWorkspace } = useWorkspaceDetailLoader({
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
    setError: setWorkspaceDetailError,
    setDetail,
    setSelectedStreams,
    setLogEntries,
    setStreamOffsets,
  });

  const mutatingCapabilities = useMemo(
    () => capabilitiesForMutatingControls(capabilities, capabilityError),
    [capabilities, capabilityError],
  );

  const { retrySelectedWorkspace, runWorkspaceOperatorAction } = useWorkspaceMutatingControls({
    selectedId,
    selectedIdRef,
    authorizedFeedEpochRef,
    mutatingCapabilities,
    capabilitiesReady,
    workspaceVersion: detail.workspace?.version,
    operatorActionState,
    setRetryState,
    setOperatorActionState,
    loadCapabilities,
    loadOverview,
    loadWorkspace,
    reloadAvailableFeeds,
  });

  // status/agent/repo are read via overviewQueryRef inside loadOverview; listing
  // them here refreshes overview on filter edits without recreating loadOverview
  // (which would restart capability polling through loadCapabilities).
  useSerializedPeriodicLoad(
    true,
    loadOverview,
    overviewLoadInFlightRef,
    `${statusFilters.join("\0")}\n${agentFilters.join("\0")}\n${repoFilter}`,
  );

  // Capability polls chain after settle. A wall-clock interval would advance
  // generation on every pollMs tick and discard slower successes, so the
  // console stays unnegotiated. Explicit refresh and context-sync still call
  // loadCapabilities directly so a newer request can supersede.
  useSerializedPeriodicLoad(true, loadCapabilities, capabilityLoadInFlightRef, "");

  useEffect(() => {
    const syncConfiguredContext = () => {
      if (!invalidateAuthorizedFeedsIfContextChanged()) {
        return;
      }
      // Sequence overview after capabilities so an identity-change clear cannot
      // advance the epoch mid-flight and discard a concurrent overview response
      // (blank tenant list until the next poll). loadCapabilities also restarts
      // overview on identity clear for the independent poll-effect race.
      void (async () => {
        await loadCapabilities();
        await loadOverview();
      })();
    };
    // Seed fingerprint from the current URL without clearing on first mount.
    invalidateAuthorizedFeedsIfContextChanged();

    window.addEventListener("popstate", syncConfiguredContext);

    const { history } = window;
    const originalPushState = history.pushState.bind(history);
    const originalReplaceState = history.replaceState.bind(history);
    history.pushState = ((data: unknown, unused: string, url?: string | URL | null) => {
      originalPushState(data, unused, url);
      syncConfiguredContext();
    }) as History["pushState"];
    history.replaceState = ((data: unknown, unused: string, url?: string | URL | null) => {
      originalReplaceState(data, unused, url);
      syncConfiguredContext();
    }) as History["replaceState"];

    return () => {
      window.removeEventListener("popstate", syncConfiguredContext);
      history.pushState = originalPushState;
      history.replaceState = originalReplaceState;
    };
  }, [invalidateAuthorizedFeedsIfContextChanged, loadCapabilities, loadOverview]);

  const pollDashboardSummary = useCallback(() => {
    if (!capabilities) {
      return;
    }
    return loadDashboardSummary(capabilities);
  }, [capabilities, loadDashboardSummary]);

  const pollCloudRuntime = useCallback(() => {
    if (!capabilities) {
      return;
    }
    return loadCloudRuntime(capabilities);
  }, [capabilities, loadCloudRuntime]);

  useCapabilityGatedPoll(
    Boolean(capabilitiesReady && capabilities && isWidgetAvailable(capabilities, "fleet_summary")),
    pollDashboardSummary,
  );

  useCapabilityGatedPoll(
    Boolean(capabilitiesReady && capabilities && isWidgetAvailable(capabilities, "resource_capacity")),
    loadResourceSaturation,
  );

  useCapabilityGatedPoll(
    Boolean(capabilitiesReady && capabilities && isWidgetAvailable(capabilities, "cloud_runtime")),
    pollCloudRuntime,
  );

  useCapabilityGatedPoll(
    Boolean(capabilitiesReady && capabilities && isDiagnosticAvailable(capabilities, "reliability")),
    loadWorkspaceSummary,
  );

  useCapabilityGatedPoll(
    Boolean(capabilitiesReady && capabilities && isDiagnosticAvailable(capabilities, "merge_queue")),
    loadMergeQueue,
  );

  useCapabilityGatedPoll(
    Boolean(capabilitiesReady && capabilities && isDiagnosticAvailable(capabilities, "failures")),
    loadFailureSummary,
  );

  useLayoutEffect(() => {
    selectedIdRef.current = selectedId;
    logListingAuthDeniedRef.current = false;
    setLogListingAuthDenied(false);
    logTailAuthDeniedRef.current = false;
    setLogTailAuthDenied(false);
    workspaceDetailAuthDeniedRef.current = false;
    setWorkspaceDetailAuthDenied(false);
    eventFeedAuthDeniedRef.current = false;
    setEventFeedAuthDenied(false);
    selectedStreamsRef.current = [];
    setDetail(emptyDetail);
    setSelectedStreams([]);
    setLogEntries([]);
    setStreamOffsets({});
    setWorkspaceDetailError(null);
    setRetryState({ status: "idle" });
    setOperatorActionState({ status: "idle" });
  }, [selectedId]);

  useWorkspaceLiveStream({
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
    eventFeedAuthDeniedRef,
    setStreamState,
    setDetail,
    setLogEntries,
    setStreamOffsets,
    setError: setWorkspaceDetailError,
  });

  const filteredOverview = useMemo(
    () =>
      filterAndSortOverview(overview, {
        searchText,
        statusFilters,
        agentFilters,
        modelFilters,
        sortKey,
        sortDirection,
      }),
    [overview, searchText, agentFilters, modelFilters, sortDirection, sortKey, statusFilters],
  );

  const {
    logTailRefreshError,
    reloadSelectedLogs,
    openWorkspaceLogs,
    openCurrentWorkspaceLogs,
    openSelectedWorkspaceLogs,
    removeFullscreenWorkspace,
  } = useWorkspaceLogTails({
    selectedId,
    selectedIdRef,
    setSelectedId,
    detailStreams: detail.streams,
    selectedStreams,
    workspaceLogSelection,
    filteredOverview,
    fullscreenWorkspaceIds,
    authorizedFeedEpochRef,
    gatedDetailFeedGenerationRef,
    gatedDetailDroppedFeedsRef,
    logStreamActivityRef,
    logListingAuthDenied,
    logListingAuthDeniedRef,
    workspaceDetailAuthDeniedRef,
    logTailAuthDeniedRef,
    setLogTailAuthDenied,
    setDetail,
    setSelectedStreams,
    setLogEntries,
    setStreamOffsets,
    setLogTailSignal,
    setFullscreenWorkspaceIds,
    setLogsFullscreen,
  });

  useEffect(() => {
    if (overview.length > 0 && selectedId && !filteredOverview.some((item) => item.workspace_id === selectedId)) {
      setSelectedId(filteredOverview[0]?.workspace_id ?? null);
    }
  }, [overview.length, filteredOverview, selectedId, setSelectedId]);

  const selectedOverview = overview.find((item) => item.workspace_id === selectedId) ?? null;
  const selectedMergeQueueItem = useMemo(
    () => mergeQueue.find((item) => item.workspace_id === selectedId) ?? null,
    [mergeQueue, selectedId],
  );
  const operatorControls = useMemo(
    () =>
      selectedOverview
        ? getWorkspaceOperatorControls({
            overview: selectedOverview,
            workspace: detail.workspace,
            mergeQueueItem: selectedMergeQueueItem,
            operations: detail.operations,
            capabilities: mutatingCapabilities,
            capabilitiesReady,
          })
        : [],
    [
      capabilitiesReady,
      detail.operations,
      detail.workspace,
      mutatingCapabilities,
      selectedMergeQueueItem,
      selectedOverview,
    ],
  );
  const selectedLogEntries = useMemo(
    () => {
      const entries = logEntries
        .filter(
          (entry) =>
            entry.workspaceId === selectedId && selectedStreams.includes(entry.streamId) && entry.data.length > 0,
        )
        .sort(compareLogEntries);

      return logSortDirection === "desc" ? entries.reverse() : entries;
    },
    [logEntries, logSortDirection, selectedId, selectedStreams],
  );
  const selectedStreamMetas = useMemo(
    () => detail.streams.filter((stream) => selectedStreams.includes(stream.stream_id)),
    [detail.streams, selectedStreams],
  );
  const fullscreenWorkspaces = useMemo(
    () => fullscreenWorkspaceIds.map((workspaceId) => toLogWorkspaceTarget(workspaceId, overview)),
    [fullscreenWorkspaceIds, overview],
  );
  const taskDetailsWorkspace = useMemo(
    () => overview.find((workspace) => workspace.workspace_id === taskDetailsWorkspaceId) ?? null,
    [overview, taskDetailsWorkspaceId],
  );

  // Per-source stale flags: each feed polls on its own timer, so staleness is
  // keyed off that feed's OWN refresh error (showing a cached snapshot), not the
  // shared /health check (which is surfaced separately via the API pill). This
  // keeps freshly-refreshed values bright even if /health blips, and a real
  // outage still fails each feed's poll and sets its own error.
  const saturationStale = resourceError != null && resourceSaturation != null;
  const summaryStale = workspaceSummaryError != null && workspaceSummary != null;
  const dashboardSummaryStale = dashboardSummaryError != null && dashboardSummary != null;
  const cloudRuntimeStale = cloudRuntimeError != null && cloudRuntime != null;

  const {
    showResourceCapacity,
    showCloudRuntime,
    showReliability,
    showMergeQueue,
    showFailures,
    showCapacitySection,
    showWorkspaceRuntime,
    showWorkspaceEvents,
    showWorkspaceOperations,
    showWorkspaceLogs,
    allowFullscreenLogs,
    allowFullscreenStreamLogs,
    fleetSummaryAvailable,
  } = resolveDashboardPanelVisibility(capabilities);
  const fleetKpis = useMemo<FleetKpi[]>(
    () =>
      fleetKpisFromDashboardSummary({
        // Render-time gate: never surface a retained summary after inventory withdraws
        // fleet_summary (clearNewlyUnsupportedCapabilityFeeds wipes + bumps
        // dashboard-summary request generation only — not gated-detail).
        // Unsupported/omitted fleet_summary omits summary counters; capacity stays independent.
        summary: fleetSummaryAvailable ? dashboardSummary : null,
        summaryStale: fleetSummaryAvailable && dashboardSummaryStale,
        saturation: resourceSaturation,
        saturationStale,
        showCapacity: showResourceCapacity,
        includeSummary: fleetSummaryAvailable,
      }),
    [
      dashboardSummary,
      dashboardSummaryStale,
      fleetSummaryAvailable,
      resourceSaturation,
      saturationStale,
      showResourceCapacity,
    ],
  );
  const showFleetHealthStrip =
    fleetKpis.length > 0 || (fleetSummaryAvailable && Boolean(dashboardSummaryError));

  // Panel-level stale dimming: a panel dims only when it is actually showing a
  // previously-loaded snapshot AND its feed errored. On first-load failures
  // there is no cached snapshot, so the panel shows its loading/error state
  // instead of a misleading "last snapshot" badge.
  const mergeErrored = mergeQueueError != null || mergeQueueStatus === "error";
  const failureErrored = failureSummaryStatus === "error";
  const capacityStale = saturationStale;
  const mergeStale = mergeErrored && mergeQueue.length > 0;
  const failureStale = failureErrored && failureSummary != null;

  return (
    <main className="min-h-screen w-full max-w-[100vw] overflow-x-hidden bg-[var(--background)] text-[var(--foreground)]">
      <TopBar
        apiState={apiState}
        streamState={streamState}
        lastRefresh={lastRefresh}
        selectedId={selectedId}
        preferences={operatorPreferences}
        onPreferencesChange={updateOperatorPreferences}
        onRefresh={() =>
          startTransition(() => {
            void (async () => {
              const selectedWorkspaceId = selectedIdRef.current;
              // Supersede an in-flight periodic detail load immediately. Waiting
              // for capabilities would let a slow poll apply before this refresh.
              const detailReload = selectedWorkspaceId
                ? loadWorkspace(selectedWorkspaceId)
                : Promise.resolve();
              const caps = await loadCapabilities();
              // Always reload overview; capability errors must not skip the list refresh.
              await Promise.all([loadOverview(), detailReload]);
              if (caps) {
                await reloadAvailableFeeds(caps);
              }
            })();
          })
        }
        isPending={isPending}
      />

      {showFleetHealthStrip ? (
        <FleetHealthStrip
          kpis={fleetKpis}
          error={fleetSummaryAvailable ? dashboardSummaryError : null}
          lastSuccessAt={
            fleetSummaryAvailable ? (dashboardSummary?.last_success_at ?? null) : null
          }
          coverageStatus={
            fleetSummaryAvailable ? (dashboardSummary?.coverage.status ?? null) : null
          }
          coverageNotes={
            fleetSummaryAvailable ? (dashboardSummary?.coverage.notes ?? null) : null
          }
        />
      ) : null}
      <SectionNav
        showCapacity={showCapacitySection}
        showMergeQueue={showMergeQueue}
        showFailures={showFailures}
      />

      <div className="grid min-h-[calc(100vh-137px)] w-full max-w-full grid-cols-1 overflow-x-hidden border-t border-[var(--border)] xl:grid-cols-[440px_minmax(0,1fr)] 2xl:grid-cols-[500px_minmax(0,1fr)]">
        <ConsoleDashboardWorkspaceRail
          statusFilters={statusFilters}
          agentFilters={agentFilters}
          modelFilters={modelFilters}
          availableModels={availableModels}
          availableAgents={availableAgents}
          repoFilter={repoFilter}
          searchText={searchText}
          sortKey={sortKey}
          sortDirection={sortDirection}
          filtersExpanded={filtersExpanded}
          onStatusFilters={setStatusFilters}
          onAgentFilters={setAgentFilters}
          onModelFilters={setModelFilters}
          onRepoFilter={setRepoFilter}
          onSearchText={setSearchText}
          onSortKey={setSortKey}
          onSortDirection={setSortDirection}
          onToggleExpanded={() => setFiltersExpanded((current) => !current)}
          showWorkspaceLogs={showWorkspaceLogs}
          workspaceLogSelection={workspaceLogSelection}
          onOpenSelectedLogs={openSelectedWorkspaceLogs}
          onClearLogSelection={() => setWorkspaceLogSelection([])}
          filteredOverview={filteredOverview}
          selectedId={selectedId}
          onSelect={setSelectedId}
          onToggleWorkspaceSelection={(workspaceId, checked) =>
            setWorkspaceLogSelection((current) => toggleWorkspaceSelection(current, workspaceId, checked))
          }
          onOpenDetails={setTaskDetailsWorkspaceId}
          onOpenLogs={openWorkspaceLogs}
        />

        <section className="min-w-0">
          {capabilityError ? <ErrorBanner message={capabilityError} /> : null}
          {overviewTruncationWarning ? <ErrorBanner message={overviewTruncationWarning} /> : null}
          {overviewError ? <ErrorBanner message={overviewError} /> : null}
          {workspaceDetailError ? <ErrorBanner message={workspaceDetailError} /> : null}
          <ConsoleDashboardFleetPanels
            showCapacitySection={showCapacitySection}
            showReliability={showReliability}
            showResourceCapacity={showResourceCapacity}
            showCloudRuntime={showCloudRuntime}
            showMergeQueue={showMergeQueue}
            showFailures={showFailures}
            workspaceSummary={workspaceSummary}
            workspaceSummaryError={workspaceSummaryError}
            summaryStale={summaryStale}
            resourceSaturation={resourceSaturation}
            resourceError={resourceError}
            capacityStale={capacityStale}
            cloudRuntime={cloudRuntime}
            cloudRuntimeError={cloudRuntimeError}
            cloudRuntimeStale={cloudRuntimeStale}
            mergeQueue={mergeQueue}
            mergeQueueHasMore={mergeQueueHasMore}
            mergeQueueStatus={mergeQueueStatus}
            mergeQueueError={mergeQueueError}
            mergeStale={mergeStale}
            failureSummary={failureSummary}
            failureSummaryStatus={failureSummaryStatus}
            failureSummaryError={failureSummaryError}
            failureStale={failureStale}
          />
</section>

      <ConsoleDashboardInspector
        selectedId={selectedId}
        selectedOverview={selectedOverview}
        selectedMergeQueueItem={selectedMergeQueueItem}
        detail={detail}
        retryState={retryState}
        operatorControls={operatorControls}
        operatorActionState={operatorActionState}
        capabilities={mutatingCapabilities}
        capabilitiesReady={capabilitiesReady}
        showWorkspaceRuntime={showWorkspaceRuntime}
        showWorkspaceEvents={showWorkspaceEvents}
        showWorkspaceOperations={showWorkspaceOperations}
        showWorkspaceLogs={showWorkspaceLogs}
        selectedStreams={selectedStreams}
        selectedStreamMetas={selectedStreamMetas}
        selectedLogEntries={selectedLogEntries}
        streamOffsets={streamOffsets}
        logSortDirection={logSortDirection}
        logTailSignal={logTailSignal}
        logTailRefreshError={logTailRefreshError}
        workspaceDetailError={workspaceDetailError}
        onClose={() => setSelectedId(null)}
        onRetry={() => {
          void retrySelectedWorkspace();
        }}
        onOperatorAction={(action, requestedTier) => {
          void runWorkspaceOperatorAction(action, requestedTier);
        }}
        onToggleStream={(streamId, checked) =>
          setSelectedStreams((current) => toggleStream(current, streamId, checked))
        }
        onSelectAllStreams={() =>
          setSelectedStreams(
            showWorkspaceLogs ? detail.streams.map((stream) => stream.stream_id) : [],
          )
        }
        onClearStreams={() => setSelectedStreams([])}
        onReloadLogs={reloadSelectedLogs}
        onOpenFullscreen={openCurrentWorkspaceLogs}
        onToggleSortDirection={() =>
          setLogSortDirection((current) => (current === "desc" ? "asc" : "desc"))
        }
      />
      </div>
      <ConsoleDashboardOverlays
        logsFullscreen={logsFullscreen}
        fullscreenWorkspaces={fullscreenWorkspaces}
        logSortDirection={logSortDirection}
        fullscreenTailSignal={fullscreenTailSignal}
        allowFullscreenLogs={allowFullscreenLogs}
        allowFullscreenStreamLogs={allowFullscreenStreamLogs}
        onTailAll={() => setFullscreenTailSignal((current) => current + 1)}
        onToggleSortDirection={() =>
          setLogSortDirection((current) => (current === "desc" ? "asc" : "desc"))
        }
        onRemoveWorkspace={removeFullscreenWorkspace}
        onCloseFullscreen={() => setLogsFullscreen(false)}
        taskDetailsWorkspace={taskDetailsWorkspace}
        onCloseTaskDetails={() => setTaskDetailsWorkspaceId(null)}
      />
    </main>
  );
}
