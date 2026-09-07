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
  capabilityRouteToAwfPath,
  isDiagnosticAvailable,
  isWidgetAvailable,
  parseConsoleCapabilities,
  resolveCapabilityParseFailureClear,
  widgetRoute,
} from "@/lib/console-capabilities";
import { parseCloudRuntimeSummary } from "@/lib/console-cloud-runtime";
import { fleetKpisFromDashboardSummary, parseDashboardSummary } from "@/lib/console-dashboard-summary";
import { awfPath, configuredContextFingerprint } from "@/lib/console-urls";
import { collectOverviewPages, overviewListPath } from "@/lib/overview-list";
import { useCapabilityGatedPoll } from "@/hooks/use-capability-gated-poll";
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
  capabilityFeedWithdrawalCleared,
  filterAndSortOverview,
  planCapabilityFeedWithdrawal,
  resolveDashboardPanelVisibility,
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
fallbackResourceSaturation,
mergeQueueLimit,
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
  const [error, setError] = useState<string | null>(null);
  // Overview truncation must not share the inspector/workspace error slot:
  // loadWorkspace / live-stream clear `error` on success and would otherwise
  // dismiss the 5k-row prefix warning, flickering against the overview poll.
  const [overviewTruncationWarning, setOverviewTruncationWarning] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();
  const logStreamActivityRef = useRef<LogStreamActivityMap>({});
  const selectedStreamsRef = useRef<string[]>([]);
  // Listing 401/403 while workspace_logs stays advertised. Live log frames and
  // in-flight tails must not refill caches the detail loader just cleared.
  const logListingAuthDeniedRef = useRef(false);
  // Bumped on auth/tenant clear so in-flight feed responses cannot restore wiped data.
  const authorizedFeedEpochRef = useRef(0);
  // Sync auth-denial latch (React state lags behind clearAuthorizedConsoleFeeds).
  const consoleAuthDeniedRef = useRef(false);
  // Capability poll generation: discard stale 200 after a newer 401/403 (or vice versa).
  const capabilityRequestGenerationRef = useRef(0);
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
  // malformed clears without touching authorizedFeedEpochRef (overview stays valid).
  const gatedDetailFeedGenerationRef = useRef(0);

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
          setError(pageError);
        }
        if (pageAuthDenied) {
          // Overview feed auth denial: drop the rail and close dependent workspace
          // surfaces (selection, inspector, logs, fullscreen). Do not call
          // clearAuthorizedConsoleFeeds — other feeds clear themselves, and
          // capabilities may still succeed without an auth-denial latch thrashing
          // overview refill. Bump gated-detail generation so in-flight
          // loadWorkspace / log-tail cannot restore revoked caches.
          gatedDetailFeedGenerationRef.current += 1;
          setOverview([]);
          setOverviewTruncationWarning(null);
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
      // Keep this off the shared `error` slot so selected-workspace polls cannot
      // clear it (loadWorkspace setError(null) on success).
      setOverviewTruncationWarning(
        collected.truncated
          ? "Workspace list truncated: more matching workspaces exist beyond the loaded pages. Narrow filters or raise the overview page budget."
          : null,
      );
      setError(null);
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
  // (CONSOLE_BACKEND_CONTRACT). Bump gatedDetailFeedGenerationRef so in-flight
  // loadWorkspace / log-tail / gated inventory responses cannot restore cleared
  // feeds. Retain lastCapabilityIdentityKeyRef so a later identity switch is not
  // treated as bootstrap.
  const clearCapabilityGatedInventories = useCallback(() => {
    dashboardSummaryRequestGenerationRef.current += 1;
    cloudRuntimeRequestGenerationRef.current += 1;
    mergeQueueRequestGenerationRef.current += 1;
    resourceSaturationRequestGenerationRef.current += 1;
    workspaceSummaryRequestGenerationRef.current += 1;
    failureSummaryRequestGenerationRef.current += 1;
    gatedDetailFeedGenerationRef.current += 1;
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
      // bump gated-detail generation so in-flight loadWorkspace cannot restore
      // withdrawn data (mutations keep their authorized epoch).
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
          selectedStreamsRef.current = [];
          setSelectedStreams([]);
          setLogEntries([]);
          setStreamOffsets({});
          // Close (not only omit) fullscreen when listing is withdrawn mid-view.
          setLogsFullscreen(false);
          setFullscreenWorkspaceIds([]);
        }
      }
      if (capabilityFeedWithdrawalCleared(plan)) {
        gatedDetailFeedGenerationRef.current += 1;
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
    capabilityLoadInFlightRef.current = true;
    try {
      const result = await apiGet<ConsoleCapabilities>(awfPath("console/capabilities"));
      if (generation !== capabilityRequestGenerationRef.current) {
        return null;
      }
      if (!result.ok) {
        if (result.status === 401 || result.status === 403) {
          clearAuthorizedConsoleFeeds({ clearCapabilities: true, authDenied: true });
          setCapabilityError(result.message);
          setCapabilities(null);
          setCapabilitiesReady(true);
          return null;
        }
        if (result.status === 404) {
          // Missing/rolled-back negotiation: clear gated inventories so optional
          // feeds stop polling, without wiping legacy-safe workspace navigation
          // (CONSOLE_BACKEND_CONTRACT — no inferred privileges).
          clearCapabilityGatedInventories();
          setCapabilityError(result.message);
          setCapabilitiesReady(true);
          return null;
        }
        // Transient capability-endpoint outage (5xx/network): keep the last successful
        // negotiation so fleet KPIs and inspector detail retain last-good snapshots
        // while the error is shown. Mutating controls fail closed via
        // capabilitiesForMutatingControls(capabilities, capabilityError) until
        // negotiation succeeds again. Auth denial and never-negotiated stay fail-closed.
        setCapabilityError(result.message);
        setCapabilitiesReady(true);
        const retained = appliedCapabilitiesRef.current;
        if (retained === null) {
          setCapabilities(null);
          return null;
        }
        return retained;
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
      appliedCapabilitiesRef.current = nextCapabilities;
      lastCapabilityIdentityKeyRef.current = parsed.identityKey;
      setCapabilities(nextCapabilities);
      setCapabilityError(null);
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
  }, []);

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
  }, [capabilities]);

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
  }, [capabilities]);

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
  }, []);

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
  }, []);

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
      if (result.status === 404 || result.status === 503) {
        setFailureSummaryStatus("unavailable");
      } else {
        setFailureSummaryStatus("error");
        setFailureSummaryError(result.message);
      }
      return;
    }
    setFailureSummary(result.data);
    setFailureSummaryStatus("success");
    setFailureSummaryError(null);
  }, []);

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

  const { loadWorkspace } = useWorkspaceDetailLoader({
    selectedId,
    selectedIdRef,
    capabilities,
    authorizedFeedEpochRef,
    gatedDetailFeedGenerationRef,
    logStreamActivityRef,
    selectedStreamsRef,
    logListingAuthDeniedRef,
    setError,
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
    selectedStreamsRef.current = [];
    setDetail(emptyDetail);
    setSelectedStreams([]);
    setLogEntries([]);
    setStreamOffsets({});
    setRetryState({ status: "idle" });
    setOperatorActionState({ status: "idle" });
  }, [selectedId]);

  useWorkspaceLiveStream({
    selectedId,
    capabilities,
    authorizedFeedEpochRef,
    selectedIdRef,
    selectedStreamsRef,
    logListingAuthDeniedRef,
    setStreamState,
    setDetail,
    setLogEntries,
    setStreamOffsets,
    setError,
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
    loadLogTail,
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
    logStreamActivityRef,
    logListingAuthDeniedRef,
    setDetail,
    setSelectedStreams,
    setLogEntries,
    setStreamOffsets,
    setLogTailSignal,
    setFullscreenWorkspaceIds,
    setLogsFullscreen,
  });

  useEffect(() => {
    if (!selectedId || selectedStreams.length === 0) {
      return;
    }
    for (const stream of detail.streams) {
      if (selectedStreams.includes(stream.stream_id)) {
        void loadLogTail(selectedId, stream, selectedStreams);
      }
    }
  }, [detail.streams, loadLogTail, selectedId, selectedStreams]);

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
        // fleet_summary (clearNewlyUnsupportedCapabilityFeeds also wipes + bumps
        // dashboard-summary request generation / gated-detail generation).
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
          {error ? <ErrorBanner message={error} /> : null}
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
