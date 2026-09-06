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
import { WorkspaceInspector } from "./workspace-inspector";

import { fallbackLlmUsage,pickWorkspaceLogStreams } from "@/lib/format";
import {
  capabilityRouteToAwfPath,
  isDiagnosticAvailable,
  isWidgetAvailable,
  parseConsoleCapabilities,
  widgetRoute,
} from "@/lib/console-capabilities";
import { parseCloudRuntimeSummary } from "@/lib/console-cloud-runtime";
import { fleetKpisFromDashboardSummary, parseDashboardSummary } from "@/lib/console-dashboard-summary";
import { awfPath } from "@/lib/console-urls";
import type { OperatorPreferences,ResolvedOperatorTheme } from "@/lib/operator-preferences";
import {
DEFAULT_OPERATOR_PREFERENCES,
normalizeOperatorPreferences,
} from "@/lib/operator-preferences";
import { formatProviderReadinessRetryError } from "@/lib/provider-readiness-format";
import type {
  CloudRuntimeSummary,
  ConsoleCapabilities,
  ConsoleDashboardSummary,
  FailureSummaryResponse,
ListEnvelope,
MergeQueueItem,
Operation,
ResourceSaturationSummary,
Workspace,
WorkspaceControlResponse,
WorkspaceEvent,
WorkspaceLogRead,
WorkspaceLogStream,
WorkspaceOperatorAction,
WorkspaceOperatorRequest,
WorkspaceOverview,
WorkspaceReliabilitySummary,
WorkspaceRetryResponse,
WorkspaceRuntime,
} from "@/lib/types";
import {
getWorkspaceOperatorControls,
summarizeWorkspaceOperatorFailure,
summarizeWorkspaceOperatorSuccess,
} from "@/lib/workspace-operator-controls";
import {
CloudRuntimePanel,
EventsPanel,
LifecycleRail,
MergeQueuePanel,
OperationsPanel,
ReliabilityPanel,
ResourceCapacityPanel,
RuntimePanel,
terminalLifecycleSourceStage,
} from "./console-dashboard-capacity";
import { LogsPanel,MultiWorkspaceLogsFullscreen } from "./console-dashboard-logs";
import { type FleetKpi,FleetHealthStrip,SectionNav,TopBar,WorkspaceFilters,WorkspaceList,WorkspaceSelectionToolbar } from "./console-dashboard-overview";
import { TaskDetailsModal,WorkspaceSummary } from "./console-dashboard-workspace-detail";
import { FailureAnalysisPanel,SecretsLeasesPanel,SecurityEgressPanel } from "./console-dashboard-security";
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
PanelContext,
apiGet,
apiPost,
applyOperatorPreferenceAttributes,
compareLogEntries,
compareWorkspaceDates,
emptyDetail,
fallbackResourceSaturation,
logStreamActivityFor,
mergeEvent,
mergeQueueLimit,
operatorActionPath,
operatorActionReason,
operatorIdempotencyKey,
parseFrame,
pollMs,
readStoredOperatorPreferences,
toLogWorkspaceTarget,
toggleStream,
toggleWorkspaceSelection,
trimLogEntries,
updateLogStreamActivity,
writeStoredOperatorPreferences
} from "./console-dashboard-shared";

export function ConsoleDashboard() {
  const [operatorPreferences, setOperatorPreferences] = useState<OperatorPreferences>(
    DEFAULT_OPERATOR_PREFERENCES,
  );
  const [operatorPreferencesHydrated, setOperatorPreferencesHydrated] = useState(false);
  const [systemTheme, setSystemTheme] = useState<ResolvedOperatorTheme>("light");
  const [overview, setOverview] = useState<WorkspaceOverview[]>([]);
const searchParams = useSearchParams();
  const [selectedId, setSelectedIdState] = useState<string | null>(searchParams.get("workspaceId"));
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
  const [capabilityIdentityKey, setCapabilityIdentityKey] = useState<string | null>(null);
  const [consoleAuthDenied, setConsoleAuthDenied] = useState(false);
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
  const [isPending, startTransition] = useTransition();
  const selectedIdRef = useRef<string | null>(selectedId);
  const logStreamActivityRef = useRef<LogStreamActivityMap>({});
  const selectedStreamsRef = useRef<string[]>([]);
  // Bumped on auth denial / tenant identity clear so in-flight feed responses
  // cannot restore data that clearAuthorizedConsoleFeeds just wiped.
  const authorizedFeedEpochRef = useRef(0);

  const setSelectedId = useCallback((workspaceId: string | null) => {
    selectedIdRef.current = workspaceId;
    setSelectedIdState(workspaceId);
  }, []);

  useEffect(() => {
    const urlWorkspaceId = searchParams.get("workspaceId");
    if (selectedIdRef.current !== urlWorkspaceId) {
      setSelectedId(urlWorkspaceId);
    }
  }, [searchParams, setSelectedId]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const currentParam = params.get("workspaceId");
    if (selectedId !== currentParam) {
      if (selectedId) {
        params.set("workspaceId", selectedId);
      } else {
        params.delete("workspaceId");
      }
      const newQuery = params.toString();
      window.history.replaceState(null, "", newQuery ? `?${newQuery}` : window.location.pathname);
    }
  }, [selectedId]);

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

  useEffect(() => {
    setOperatorPreferences(readStoredOperatorPreferences());
    setOperatorPreferencesHydrated(true);
  }, []);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const updateSystemTheme = () => setSystemTheme(media.matches ? "dark" : "light");
    updateSystemTheme();
    media.addEventListener("change", updateSystemTheme);
    return () => media.removeEventListener("change", updateSystemTheme);
  }, []);

  useEffect(() => {
    if (!operatorPreferencesHydrated) {
      return;
    }
    applyOperatorPreferenceAttributes(operatorPreferences, systemTheme);
    writeStoredOperatorPreferences(operatorPreferences);
  }, [operatorPreferences, operatorPreferencesHydrated, systemTheme]);

  const updateOperatorPreferences = useCallback((next: Partial<OperatorPreferences>) => {
    setOperatorPreferences((current) => normalizeOperatorPreferences({ ...current, ...next }));
  }, []);

  const overviewPath = useMemo(() => {
    const params: Record<string, string | number> = { limit: 100 };
    if (statusFilters.length === 1) {
      params.status = statusFilters[0];
    }
    if (agentFilters.length === 1) {
      params.agent = agentFilters[0];
    }
    if (repoFilter.trim()) {
      params.repo_url = repoFilter.trim();
    }
    return awfPath("workspaces/overview", params);
  }, [agentFilters, repoFilter, statusFilters]);

  const loadOverview = useCallback(async () => {
    const epoch = authorizedFeedEpochRef.current;
    const health = await apiGet<{ status: string }>(awfPath("health"));
    if (epoch !== authorizedFeedEpochRef.current) {
      return;
    }
    setApiState(health.ok ? "ok" : "error");

    // Auth revocation must not refill previously authorized workspace rows.
    // Non-auth capability failures keep legacy-safe overview navigation.
    if (consoleAuthDenied) {
      setOverview([]);
      return;
    }

    const result = await apiGet<ListEnvelope<WorkspaceOverview>>(overviewPath);
    if (epoch !== authorizedFeedEpochRef.current) {
      return;
    }
    if (!result.ok) {
      setError(result.message);
      setOverview([]);
      return;
    }
    setError(null);
    setOverview(
      result.data.items.map((item) => ({
        ...item,
        task_prompt: item.task_prompt ?? "",
        lifecycle: item.lifecycle ?? [],
        llm_usage: fallbackLlmUsage(item.llm_usage),
        recovery: item.recovery ?? null,
      })),
    );
    setLastRefresh(new Date());
    const currentSelectedId = selectedIdRef.current;
    if (currentSelectedId && !result.data.items.some((item) => item.workspace_id === currentSelectedId)) {
      setSelectedId(null);
    }
  }, [consoleAuthDenied, overviewPath, setSelectedId]);

  const clearAuthorizedConsoleFeeds = useCallback((options?: { clearCapabilities?: boolean; authDenied?: boolean }) => {
    authorizedFeedEpochRef.current += 1;
    if (options?.authDenied) {
      setConsoleAuthDenied(true);
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
    setSelectedId(null);
    setDetail(emptyDetail);
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
      setCapabilities(null);
      setCapabilityIdentityKey(null);
    }
  }, [setSelectedId]);

  const loadCapabilities = useCallback(async (): Promise<ConsoleCapabilities | null> => {
    const result = await apiGet<ConsoleCapabilities>(awfPath("console/capabilities"));
    if (!result.ok) {
      if (result.status === 401 || result.status === 403) {
        clearAuthorizedConsoleFeeds({ clearCapabilities: true, authDenied: true });
        setCapabilityError(result.message);
        setCapabilities(null);
        setCapabilitiesReady(true);
        return null;
      }
      setCapabilityError(result.message);
      setCapabilities(null);
      setCapabilitiesReady(true);
      return null;
    }

    const parsed = parseConsoleCapabilities(result.data);
    if (!parsed.ok) {
      setCapabilityError(parsed.message);
      setCapabilities(null);
      setCapabilitiesReady(true);
      return null;
    }

    // Skip bootstrap (null → first key) so the parallel overview fetch is not wiped.
    if (capabilityIdentityKey !== null && parsed.identityKey !== capabilityIdentityKey) {
      clearAuthorizedConsoleFeeds();
    }
    setConsoleAuthDenied(false);
    setCapabilities(parsed.capabilities);
    setCapabilityIdentityKey(parsed.identityKey);
    setCapabilityError(null);
    setCapabilitiesReady(true);
    return parsed.capabilities;
  }, [capabilityIdentityKey, clearAuthorizedConsoleFeeds]);

  const loadResourceSaturation = useCallback(async () => {
    const epoch = authorizedFeedEpochRef.current;
    const result = await apiGet<ResourceSaturationSummary>(awfPath("metrics/resources/saturation"));
    if (epoch !== authorizedFeedEpochRef.current) {
      return;
    }
    if (!result.ok) {
      setResourceError(result.message);
      return;
    }
    setResourceError(null);
    setResourceSaturation(fallbackResourceSaturation(result.data));
  }, []);

  const loadDashboardSummary = useCallback(async (caps?: ConsoleCapabilities | null) => {
    const epoch = authorizedFeedEpochRef.current;
    const active = caps ?? capabilities;
    const route = widgetRoute(active, "fleet_summary");
    const path = route
      ? capabilityRouteToAwfPath(route)
      : awfPath("console/dashboard-summary");
    const result = await apiGet<ConsoleDashboardSummary>(path);
    if (epoch !== authorizedFeedEpochRef.current) {
      return;
    }
    if (!result.ok) {
      setDashboardSummaryError(result.message);
      return;
    }
    const parsed = parseDashboardSummary(result.data);
    if (!parsed) {
      setDashboardSummaryError("Dashboard summary payload malformed.");
      return;
    }
    setDashboardSummaryError(null);
    setDashboardSummary(parsed);
  }, [capabilities]);

  const loadCloudRuntime = useCallback(async (caps?: ConsoleCapabilities | null) => {
    const epoch = authorizedFeedEpochRef.current;
    const active = caps ?? capabilities;
    const route = widgetRoute(active, "cloud_runtime");
    if (!route) {
      return;
    }
    const result = await apiGet<CloudRuntimeSummary>(capabilityRouteToAwfPath(route));
    if (epoch !== authorizedFeedEpochRef.current) {
      return;
    }
    if (!result.ok) {
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
    const result = await apiGet<WorkspaceReliabilitySummary>(awfPath("metrics/workspaces/summary"));
    if (epoch !== authorizedFeedEpochRef.current) {
      return;
    }
    if (!result.ok) {
      setWorkspaceSummaryError(result.message);
      return;
    }
    setWorkspaceSummaryError(null);
    setWorkspaceSummary(result.data);
  }, []);

  const loadMergeQueue = useCallback(async () => {
    const epoch = authorizedFeedEpochRef.current;
    const result = await apiGet<ListEnvelope<MergeQueueItem>>(
      awfPath("merge-queue", { limit: mergeQueueLimit }),
    );
    if (epoch !== authorizedFeedEpochRef.current) {
      return;
    }
    if (!result.ok) {
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
    const result = await apiGet<FailureSummaryResponse>(awfPath("metrics/failures/summary"));
    if (epoch !== authorizedFeedEpochRef.current) {
      return;
    }
    if (!result.ok) {
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

  const loadWorkspace = useCallback(async (workspaceId: string) => {
    const epoch = authorizedFeedEpochRef.current;
    const caps = capabilities;
    const detailFeedsAdvertised =
      caps?.diagnostics.some((item) => item.id.startsWith("workspace_")) ?? false;
    const allowDetail = (id: "workspace_runtime" | "workspace_events" | "workspace_operations" | "workspace_logs") => {
      if (!caps) {
        // Capability failure / not ready: keep basic workspace GET only.
        return false;
      }
      if (!detailFeedsAdvertised) {
        // Pre-advertisement stubs: retain Core-native detail polls.
        return true;
      }
      return isDiagnosticAvailable(caps, id);
    };
    const allowRuntime = allowDetail("workspace_runtime");
    const allowEvents = allowDetail("workspace_events");
    const allowOperations = allowDetail("workspace_operations");
    const allowLogs = allowDetail("workspace_logs");

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

    if (epoch !== authorizedFeedEpochRef.current || selectedIdRef.current !== workspaceId) {
      return;
    }

    const firstFailure = [workspace, runtime, events, operations, streams].find(
      (item) => item != null && !item.ok,
    );
    if (firstFailure && !firstFailure.ok) {
      setError(firstFailure.message);
    } else {
      setError(null);
    }

    setDetail({
      workspace: workspace.ok
        ? {
            ...workspace.data,
            lifecycle: workspace.data.lifecycle ?? [],
            llm_usage: fallbackLlmUsage(workspace.data.llm_usage),
            recovery: workspace.data.recovery ?? null,
          }
        : null,
      runtime: runtime?.ok ? runtime.data : null,
      events: events?.ok ? events.data.items : [],
      operations: operations?.ok ? operations.data.items : [],
      streams: streams?.ok ? streams.data.items : [],
    });

    if (streams?.ok) {
      logStreamActivityRef.current = updateLogStreamActivity(
        logStreamActivityRef.current,
        workspaceId,
        streams.data.items,
      );
      setSelectedStreams((current) => {
        return pickWorkspaceLogStreams(streams.data.items, current);
      });
    }
  }, [capabilities]);

  const retrySelectedWorkspace = useCallback(async () => {
    const workspaceId = selectedId;
    if (!workspaceId) {
      return;
    }
    setRetryState({ status: "submitting" });
    const result = await apiPost<WorkspaceRetryResponse>(
      awfPath(`workspaces/${encodeURIComponent(workspaceId)}/retry`),
    );
    if (!result.ok) {
      if (selectedIdRef.current !== workspaceId) {
        return;
      }
      setRetryState({ status: "error", message: formatProviderReadinessRetryError(result) });
      return;
    }
    if (selectedIdRef.current !== workspaceId) {
      const caps = await loadCapabilities();
      if (caps) {
        await Promise.all([loadOverview(), reloadAvailableFeeds(caps)]);
      }
      return;
    }
    setRetryState({
      status: "success",
      newWorkspaceId: result.data.new_workspace_id,
      operationId: result.data.operation_id,
    });
    {
      const caps = await loadCapabilities();
      if (caps) {
        await Promise.all([loadOverview(), reloadAvailableFeeds(caps)]);
      }
    }
  }, [loadCapabilities, loadOverview, reloadAvailableFeeds, selectedId]);

  const runWorkspaceOperatorAction = useCallback(
    async (action: WorkspaceOperatorAction, requestedTier?: number) => {
      const workspaceId = selectedId;
      if (!workspaceId || operatorActionState.status === "submitting") {
        return;
      }
      setOperatorActionState({ status: "submitting", action });
      const payload: WorkspaceOperatorRequest = {
        reason: operatorActionReason(action),
        workspace_version: detail.workspace?.version,
        idempotency_key: operatorIdempotencyKey(action, workspaceId),
      };
      if (action === "revalidate") {
        payload.requested_tier = requestedTier === 1 || requestedTier === 2 || requestedTier === 3 ? requestedTier : 1;
      }

      const result = await apiPost<WorkspaceControlResponse | Operation>(
        operatorActionPath(action, workspaceId),
        payload,
      );
      if (!result.ok) {
        if (selectedIdRef.current !== workspaceId) {
          return;
        }
        const failure = summarizeWorkspaceOperatorFailure(result);
        setOperatorActionState({
          status: "error",
          action,
          errorCode: failure.errorCode,
          message: failure.message,
        });
        return;
      }

      const success = summarizeWorkspaceOperatorSuccess(action, result.data);
      if (selectedIdRef.current !== workspaceId) {
        const caps = await loadCapabilities();
        if (caps) {
          await Promise.all([loadOverview(), reloadAvailableFeeds(caps)]);
        }
        return;
      }
      setOperatorActionState({
        status: "success",
        action,
        operationId: success.operationId,
        operationStatus: success.status,
        message: success.message,
        warnings: success.warnings,
      });
      {
        const caps = await loadCapabilities();
        if (caps) {
          await Promise.all([
            loadOverview(),
            reloadAvailableFeeds(caps),
            loadWorkspace(workspaceId),
          ]);
        }
      }
    },
    [
      detail.workspace?.version,
      loadCapabilities,
      loadOverview,
      loadWorkspace,
      operatorActionState.status,
      reloadAvailableFeeds,
      selectedId,
    ],
  );

  const loadLogTail = useCallback(
    async (workspaceId: string, stream: WorkspaceLogStream, selectedStreamIds: readonly string[]) => {
      const epoch = authorizedFeedEpochRef.current;
      const offset = Math.max(stream.byte_count - 65_536, 0);
      const activity = logStreamActivityFor(logStreamActivityRef.current, workspaceId, stream);
      const result = await apiGet<WorkspaceLogRead>(
        awfPath(`workspaces/${workspaceId}/logs/${encodeURIComponent(stream.stream_id)}`, {
          offset,
          limit_bytes: 65536,
        }),
      );
      if (epoch !== authorizedFeedEpochRef.current || selectedIdRef.current !== workspaceId) {
        return;
      }
      if (!result.ok) {
        setLogEntries((current) =>
          trimLogEntries(
            [
            ...current.filter(
              (entry) => !(entry.workspaceId === workspaceId && entry.streamId === stream.stream_id),
            ),
            {
              key: `tail-error:${workspaceId}:${stream.stream_id}:${Date.now()}`,
              workspaceId,
              streamId: stream.stream_id,
              source: stream.source,
              fd: null,
              offset,
              data: `Unable to load log stream: ${result.message}`,
              occurredAt: new Date().toISOString(),
              order: Date.now(),
              kind: "tail",
            },
            ],
            selectedStreamIds,
          ),
        );
        return;
      }
      const tailEntry = {
        key: `tail:${workspaceId}:${stream.stream_id}:${result.data.offset}:${result.data.next_offset}`,
        workspaceId,
        streamId: stream.stream_id,
        source: stream.source,
        fd: null,
        offset: result.data.offset,
        data: result.data.data,
        occurredAt: new Date(activity).toISOString(),
        order: activity,
        kind: "tail" as const,
      };
      setLogEntries((current) =>
        trimLogEntries(
          [
            ...current.filter(
              (entry) =>
                entry.workspaceId !== workspaceId ||
                entry.streamId !== stream.stream_id ||
                (entry.kind === "live" && entry.offset >= result.data.next_offset),
            ),
            tailEntry,
          ],
          selectedStreamIds,
        ),
      );
      setStreamOffsets((current) => ({
        ...current,
        [stream.stream_id]: result.data.next_offset,
      }));
    },
    [],
  );

  useEffect(() => {
    void loadOverview();
    const interval = window.setInterval(() => void loadOverview(), pollMs);
    return () => window.clearInterval(interval);
  }, [loadOverview]);

  useEffect(() => {
    void loadCapabilities();
    const interval = window.setInterval(() => void loadCapabilities(), pollMs);
    return () => window.clearInterval(interval);
  }, [loadCapabilities]);

  useEffect(() => {
    if (!capabilitiesReady || !capabilities || !isWidgetAvailable(capabilities, "fleet_summary")) {
      return;
    }
    void loadDashboardSummary(capabilities);
    const interval = window.setInterval(() => void loadDashboardSummary(capabilities), pollMs);
    return () => window.clearInterval(interval);
  }, [capabilities, capabilitiesReady, loadDashboardSummary]);

  useEffect(() => {
    if (!capabilitiesReady || !capabilities || !isWidgetAvailable(capabilities, "resource_capacity")) {
      return;
    }
    void loadResourceSaturation();
    const interval = window.setInterval(() => void loadResourceSaturation(), pollMs);
    return () => window.clearInterval(interval);
  }, [capabilities, capabilitiesReady, loadResourceSaturation]);

  useEffect(() => {
    if (!capabilitiesReady || !capabilities || !isWidgetAvailable(capabilities, "cloud_runtime")) {
      return;
    }
    void loadCloudRuntime(capabilities);
    const interval = window.setInterval(() => void loadCloudRuntime(capabilities), pollMs);
    return () => window.clearInterval(interval);
  }, [capabilities, capabilitiesReady, loadCloudRuntime]);

  useEffect(() => {
    if (!capabilitiesReady || !capabilities || !isDiagnosticAvailable(capabilities, "reliability")) {
      return;
    }
    void loadWorkspaceSummary();
    const interval = window.setInterval(() => void loadWorkspaceSummary(), pollMs);
    return () => window.clearInterval(interval);
  }, [capabilities, capabilitiesReady, loadWorkspaceSummary]);

  useEffect(() => {
    if (!capabilitiesReady || !capabilities || !isDiagnosticAvailable(capabilities, "merge_queue")) {
      return;
    }
    void loadMergeQueue();
    const interval = window.setInterval(() => void loadMergeQueue(), pollMs);
    return () => window.clearInterval(interval);
  }, [capabilities, capabilitiesReady, loadMergeQueue]);

  useEffect(() => {
    if (!capabilitiesReady || !capabilities || !isDiagnosticAvailable(capabilities, "failures")) {
      return;
    }
    void loadFailureSummary();
    const interval = window.setInterval(() => void loadFailureSummary(), pollMs);
    return () => window.clearInterval(interval);
  }, [capabilities, capabilitiesReady, loadFailureSummary]);

  useLayoutEffect(() => {
    selectedIdRef.current = selectedId;
    setDetail(emptyDetail);
    setSelectedStreams([]);
    setLogEntries([]);
    setStreamOffsets({});
    setRetryState({ status: "idle" });
    setOperatorActionState({ status: "idle" });
  }, [selectedId]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(emptyDetail);
      return;
    }
    void loadWorkspace(selectedId);
    const interval = window.setInterval(() => void loadWorkspace(selectedId), pollMs);
    return () => window.clearInterval(interval);
  }, [loadWorkspace, selectedId]);

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
    if (!selectedId) {
      setStreamState("idle");
      return;
    }
    const detailFeedsAdvertised =
      capabilities?.diagnostics.some((item) => item.id.startsWith("workspace_")) ?? false;
    const allowStream = !capabilities
      ? false
      : !detailFeedsAdvertised
        ? true
        : isDiagnosticAvailable(capabilities, "workspace_stream");
    if (!allowStream) {
      setStreamState("idle");
      return;
    }
    const epoch = authorizedFeedEpochRef.current;
    setStreamState("connecting");
    const source = new EventSource(
      awfPath(`workspaces/${selectedId}/stream`, {
        channels: "events,agent,validation,services",
        tail_bytes: 65536,
      }),
    );
    let closedByServer = false;
    let terminalError = false;

    source.onmessage = (message) => {
      if (epoch !== authorizedFeedEpochRef.current || selectedIdRef.current !== selectedId) {
        return;
      }
      const frame = parseFrame(message.data);
      if (!frame) {
        return;
      }
      if (frame.type === "connected" || frame.type === "heartbeat") {
        setStreamState("live");
        return;
      }
      if (frame.type === "snapshot") {
        setStreamState("live");
        setDetail((current) => ({
          ...current,
          workspace: {
            ...frame.workspace,
            lifecycle: frame.workspace.lifecycle ?? [],
            llm_usage: fallbackLlmUsage(frame.workspace.llm_usage),
            recovery: frame.workspace.recovery ?? null,
          },
        }));
        return;
      }
      if (frame.type === "event") {
        setStreamState("live");
        setDetail((current) => ({
          ...current,
          events: mergeEvent(current.events, frame.event),
        }));
        return;
      }
      if (frame.type === "log") {
        setStreamState("live");
        setLogEntries((current) =>
          trimLogEntries(
            [
            ...current,
            {
              key: `live:${frame.workspace_id}:${frame.stream_id}:${frame.offset}:${frame.next_offset ?? frame.offset}:${frame.seq}`,
              workspaceId: frame.workspace_id,
              streamId: frame.stream_id,
              source: frame.source,
              fd: frame.fd,
              offset: frame.offset,
              data: frame.data,
              occurredAt: frame.occurred_at ?? new Date().toISOString(),
              order: Date.parse(frame.occurred_at ?? "") || Date.now(),
              kind: "live",
            },
            ],
            selectedStreamsRef.current,
          ),
        );
        setStreamOffsets((current) => ({
          ...current,
          [frame.stream_id]: Math.max(current[frame.stream_id] ?? 0, frame.next_offset ?? 0),
        }));
        return;
      }
      if (frame.type === "error") {
        terminalError = true;
        setStreamState("error");
        setError(frame.message);
        source.close();
        return;
      }
      if (frame.type === "closed") {
        closedByServer = true;
        setStreamState("idle");
        source.close();
      }
    };

    source.onerror = () => {
      if (terminalError) {
        setStreamState("error");
        return;
      }
      setStreamState(closedByServer || source.readyState === EventSource.CLOSED ? "idle" : "connecting");
    };

    return () => source.close();
  }, [capabilities, selectedId]);

  const filteredOverview = useMemo(() => {
    const needle = searchText.trim().toLowerCase();
    let filtered = overview;
    if (statusFilters.length > 0) {
      filtered = filtered.filter((item) => statusFilters.includes(item.status));
    }
    if (agentFilters.length > 0) {
      filtered = filtered.filter((item) => agentFilters.includes(item.agent));
    }
    if (modelFilters.length > 0) {
      filtered = filtered.filter((item) => item.agent_model !== null && modelFilters.includes(item.agent_model));
    }
    if (needle) {
      filtered = filtered.filter((item) =>
        [
          item.workspace_id,
          item.task_id,
          item.title,
          item.repo_url,
          item.base_branch,
          item.agent,
          item.agent_model ?? "",
          item.agent_effort ?? "",
          item.status,
          item.recovery?.reason_code ?? "",
          item.recovery?.recovery_mode ?? "",
        ]
          .join(" ")
          .toLowerCase()
          .includes(needle),
      );
    }
    return [...filtered].sort((left, right) => compareWorkspaceDates(left, right, sortKey, sortDirection));
  }, [overview, searchText, agentFilters, modelFilters, sortDirection, sortKey, statusFilters]);

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
            capabilities,
            capabilitiesReady,
          })
        : [],
    [capabilities, capabilitiesReady, detail.operations, detail.workspace, selectedMergeQueueItem, selectedOverview],
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
  const reloadSelectedLogs = useCallback(() => {
    if (!selectedId) {
      return;
    }
    setLogTailSignal((current) => current + 1);
    for (const stream of detail.streams) {
      if (selectedStreams.includes(stream.stream_id)) {
        void loadLogTail(selectedId, stream, selectedStreams);
      }
    }
  }, [detail.streams, loadLogTail, selectedId, selectedStreams]);
  const openWorkspaceLogs = useCallback(
    (workspaceId: string) => {
      if (workspaceId !== selectedId) {
        setDetail(emptyDetail);
        setSelectedStreams([]);
        setLogEntries([]);
        setStreamOffsets({});
        setSelectedId(workspaceId);
      }
      setFullscreenWorkspaceIds([workspaceId]);
      setLogsFullscreen(true);
    },
    [selectedId, setSelectedId],
  );
  const openCurrentWorkspaceLogs = useCallback(() => {
    if (!selectedId) {
      return;
    }
    setFullscreenWorkspaceIds([selectedId]);
    setLogsFullscreen(true);
  }, [selectedId]);
  const openSelectedWorkspaceLogs = useCallback(() => {
    if (workspaceLogSelection.length === 0) {
      return;
    }
    const selected = new Set(workspaceLogSelection);
    const orderedVisible = filteredOverview
      .filter((workspace) => selected.has(workspace.workspace_id))
      .map((workspace) => workspace.workspace_id);
    const remaining = workspaceLogSelection.filter((workspaceId) => !orderedVisible.includes(workspaceId));
    setFullscreenWorkspaceIds([...orderedVisible, ...remaining]);
    setLogsFullscreen(true);
  }, [filteredOverview, workspaceLogSelection]);
  const removeFullscreenWorkspace = useCallback(
    (workspaceId: string) => {
      const next = fullscreenWorkspaceIds.filter((id) => id !== workspaceId);
      setFullscreenWorkspaceIds(next);
      if (next.length === 0) {
        setLogsFullscreen(false);
      }
    },
    [fullscreenWorkspaceIds],
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

  const fleetKpis = useMemo<FleetKpi[]>(
    () =>
      fleetKpisFromDashboardSummary({
        summary: dashboardSummary,
        summaryStale: dashboardSummaryStale,
        saturation: resourceSaturation,
        saturationStale,
        showCapacity: isWidgetAvailable(capabilities, "resource_capacity"),
      }),
    [capabilities, dashboardSummary, dashboardSummaryStale, resourceSaturation, saturationStale],
  );

  // Panel-level stale dimming: a panel dims only when it is actually showing a
  // previously-loaded snapshot AND its feed errored. On first-load failures
  // there is no cached snapshot, so the panel shows its loading/error state
  // instead of a misleading "last snapshot" badge.
  const mergeErrored = mergeQueueError != null || mergeQueueStatus === "error";
  const failureErrored = failureSummaryStatus === "error";
  const capacityStale = saturationStale;
  const mergeStale = mergeErrored && mergeQueue.length > 0;
  const failureStale = failureErrored && failureSummary != null;
  const showResourceCapacity = isWidgetAvailable(capabilities, "resource_capacity");
  const showCloudRuntime = isWidgetAvailable(capabilities, "cloud_runtime");
  const showReliability = isDiagnosticAvailable(capabilities, "reliability");
  const showMergeQueue = isDiagnosticAvailable(capabilities, "merge_queue");
  const showFailures = isDiagnosticAvailable(capabilities, "failures");

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
              const caps = await loadCapabilities();
              if (!caps) {
                return;
              }
              await Promise.all([loadOverview(), reloadAvailableFeeds(caps)]);
            })();
          })
        }
        isPending={isPending}
      />

      <FleetHealthStrip kpis={fleetKpis} />
      <SectionNav />

      <div className="grid min-h-[calc(100vh-137px)] w-full max-w-full grid-cols-1 overflow-x-hidden border-t border-[var(--border)] xl:grid-cols-[440px_minmax(0,1fr)] 2xl:grid-cols-[500px_minmax(0,1fr)]">
        <aside
          id="awf-workspaces"
          className="min-w-0 scroll-mt-14 border-b border-[var(--border)] bg-surface xl:border-r xl:border-b-0"
        >
          <WorkspaceFilters
            statusFilters={statusFilters}
            agentFilters={agentFilters}
            modelFilters={modelFilters}
            availableModels={availableModels}
            availableAgents={availableAgents}
            repoFilter={repoFilter}
            searchText={searchText}
            sortKey={sortKey}
            sortDirection={sortDirection}
            onStatusFilters={setStatusFilters}
            onAgentFilters={setAgentFilters}
            onModelFilters={setModelFilters}
            onRepoFilter={setRepoFilter}
            onSearchText={setSearchText}
            onSortKey={setSortKey}
            onSortDirection={setSortDirection}
            expanded={filtersExpanded}
            onToggleExpanded={() => setFiltersExpanded((current) => !current)}
          />
          <WorkspaceSelectionToolbar
            selectedCount={workspaceLogSelection.length}
            onOpen={openSelectedWorkspaceLogs}
            onClear={() => setWorkspaceLogSelection([])}
          />
          <WorkspaceList
            items={filteredOverview}
            selectedId={selectedId}
            selectedWorkspaceIds={workspaceLogSelection}
            onSelect={setSelectedId}
            onToggleWorkspaceSelection={(workspaceId, checked) =>
              setWorkspaceLogSelection((current) => toggleWorkspaceSelection(current, workspaceId, checked))
            }
            onOpenDetails={setTaskDetailsWorkspaceId}
            onOpenLogs={openWorkspaceLogs}
          />
        </aside>

        <section className="min-w-0">
          {capabilityError ? <ErrorBanner message={capabilityError} /> : null}
          {error ? <ErrorBanner message={error} /> : null}
          <div className="grid min-w-0 gap-4 p-4 pb-0 2xl:grid-cols-[minmax(0,1fr)_minmax(460px,0.85fr)]">
            {showResourceCapacity || showCloudRuntime || showReliability ? (
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
            {/* 2xl: the panel overlays the cell (absolute) so the long merge
                list never drives the row height — Capacity sets the height and
                the list scrolls to fill it. Below 2xl it is normal flow. */}
            {showMergeQueue ? (
              <div id="awf-merge-queue" className="min-w-0 scroll-mt-14 2xl:relative">
                <div className="2xl:absolute 2xl:inset-0">
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
</section>

      <WorkspaceInspector
        isOpen={!!(selectedId && selectedOverview)}
        onClose={() => setSelectedId(null)}
        title={selectedOverview ? selectedOverview.title : "Workspace Details"}
      >
        <PanelContext.Provider value="ghost">
          {selectedId && selectedOverview ? (
            <div className="grid min-w-0 gap-4 min-[1700px]:grid-cols-[minmax(0,1fr)_minmax(400px,0.8fr)]">
              <div className="grid min-w-0 content-start gap-4">
                <WorkspaceSummary
                  overview={selectedOverview}
                  workspace={detail.workspace}
                  mergeQueueItem={selectedMergeQueueItem}
                  retryState={retryState}
                  operatorControls={operatorControls}
                  operatorActionState={operatorActionState}
                  onRetry={retrySelectedWorkspace}
                  onOperatorAction={runWorkspaceOperatorAction}
                />
                <LifecycleRail
                  status={selectedOverview.status}
                  lifecycle={detail.workspace?.lifecycle ?? selectedOverview.lifecycle ?? []}
                  terminalSourceStage={terminalLifecycleSourceStage(
                    selectedOverview.status,
                    detail.events,
                    selectedOverview.last_event,
                    selectedOverview.current_phase,
                  )}
                />
                <RuntimePanel runtime={detail.runtime} />
                <SecurityEgressPanel
                  resolvedProfile={detail.workspace?.resolved_profile ?? null}
                  policyFindings={detail.workspace?.policy_findings}
                  egressAudit={detail.workspace?.egress_audit}
                />
                <SecretsLeasesPanel
                  resolvedProfile={detail.workspace?.resolved_profile ?? null}
                  secretLeases={detail.workspace?.secret_leases ?? null}
                />
                <OperationsPanel operations={detail.operations} />
              </div>
              <div className="grid min-w-0 content-start gap-4">
                <EventsPanel events={detail.events} />
                <LogsPanel
                  streams={detail.streams}
                  selectedStreams={selectedStreams}
                  selectedStreamMetas={selectedStreamMetas}
                  entries={selectedLogEntries}
                  offsets={streamOffsets}
                  sortDirection={logSortDirection}
                  tailSignal={logTailSignal}
                  onToggleStream={(streamId, checked) =>
                    setSelectedStreams((current) => toggleStream(current, streamId, checked))
                  }
                  onSelectAll={() => setSelectedStreams(detail.streams.map((stream) => stream.stream_id))}
                  onClear={() => setSelectedStreams([])}
                  onReload={reloadSelectedLogs}
                  onOpenFullscreen={openCurrentWorkspaceLogs}
                  onToggleSortDirection={() =>
                    setLogSortDirection((current) => (current === "desc" ? "asc" : "desc"))
                  }
                />
              </div>
            </div>
          ) : null}
        </PanelContext.Provider>
      </WorkspaceInspector>
      </div>
      {logsFullscreen && fullscreenWorkspaces.length > 0 ? (
        <MultiWorkspaceLogsFullscreen
          workspaces={fullscreenWorkspaces}
          sortDirection={logSortDirection}
          tailSignal={fullscreenTailSignal}
          onTailAll={() => setFullscreenTailSignal((current) => current + 1)}
          onToggleSortDirection={() =>
            setLogSortDirection((current) => (current === "desc" ? "asc" : "desc"))
          }
          onRemoveWorkspace={removeFullscreenWorkspace}
          onClose={() => setLogsFullscreen(false)}
        />
      ) : null}
      {taskDetailsWorkspace ? (
        <TaskDetailsModal
          workspace={taskDetailsWorkspace}
          onClose={() => setTaskDetailsWorkspaceId(null)}
        />
      ) : null}
    </main>
  );
}
