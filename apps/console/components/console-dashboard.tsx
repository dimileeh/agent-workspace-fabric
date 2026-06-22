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

import { capacityUtilizationPct,compactDuration,fallbackLlmUsage,pickWorkspaceLogStreams } from "@/lib/format";
import type { OperatorPreferences,ResolvedOperatorTheme } from "@/lib/operator-preferences";
import {
DEFAULT_OPERATOR_PREFERENCES,
normalizeOperatorPreferences,
} from "@/lib/operator-preferences";
import { formatProviderReadinessRetryError } from "@/lib/provider-readiness-format";
import type {
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
EventsPanel,
LifecycleRail,
MergeQueuePanel,
OperationsPanel,
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

  const availableModels = useMemo(() => {
    return Array.from(new Set(overview.map((w) => w.agent_model).filter((m): m is string => Boolean(m)))).sort();
  }, [overview]);

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
    const params = new URLSearchParams({ limit: "100" });
    if (statusFilters.length === 1) {
      params.set("status", statusFilters[0]);
    }
    if (agentFilters.length === 1) {
      params.set("agent", agentFilters[0]);
    }
    if (repoFilter.trim()) {
      params.set("repo_url", repoFilter.trim());
    }
    return `/api/awf/workspaces/overview?${params}`;
  }, [agentFilters, repoFilter, statusFilters]);

  const loadOverview = useCallback(async () => {
    const health = await apiGet<{ status: string }>("/api/awf/health");
    setApiState(health.ok ? "ok" : "error");

    const result = await apiGet<ListEnvelope<WorkspaceOverview>>(overviewPath);
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
  }, [overviewPath, setSelectedId]);

  const loadResourceSaturation = useCallback(async () => {
    const result = await apiGet<ResourceSaturationSummary>("/api/awf/metrics/resources/saturation");
    if (!result.ok) {
      setResourceError(result.message);
      return;
    }
    setResourceError(null);
    setResourceSaturation(fallbackResourceSaturation(result.data));
  }, []);

  const loadWorkspaceSummary = useCallback(async () => {
    const result = await apiGet<WorkspaceReliabilitySummary>("/api/awf/metrics/workspaces/summary");
    if (!result.ok) {
      setWorkspaceSummaryError(result.message);
      return;
    }
    setWorkspaceSummaryError(null);
    setWorkspaceSummary(result.data);
  }, []);

  const loadMergeQueue = useCallback(async () => {
    const result = await apiGet<ListEnvelope<MergeQueueItem>>(`/api/awf/merge-queue?limit=${mergeQueueLimit}`);
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
    const result = await apiGet<FailureSummaryResponse>("/api/awf/metrics/failures/summary");
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

  const loadWorkspace = useCallback(async (workspaceId: string) => {
    const [workspace, runtime, events, operations, streams] = await Promise.all([
      apiGet<Workspace>(`/api/awf/workspaces/${workspaceId}`),
      apiGet<WorkspaceRuntime>(`/api/awf/workspaces/${workspaceId}/runtime`),
      apiGet<ListEnvelope<WorkspaceEvent>>(`/api/awf/workspaces/${workspaceId}/events?limit=100`),
      apiGet<ListEnvelope<Operation>>(`/api/awf/workspaces/${workspaceId}/operations?limit=50`),
      apiGet<ListEnvelope<WorkspaceLogStream>>(`/api/awf/workspaces/${workspaceId}/logs`),
    ]);

    if (selectedIdRef.current !== workspaceId) {
      return;
    }

    const firstFailure = [workspace, runtime, events, operations, streams].find((item) => !item.ok);
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
      runtime: runtime.ok ? runtime.data : null,
      events: events.ok ? events.data.items : [],
      operations: operations.ok ? operations.data.items : [],
      streams: streams.ok ? streams.data.items : [],
    });

    if (streams.ok) {
      logStreamActivityRef.current = updateLogStreamActivity(
        logStreamActivityRef.current,
        workspaceId,
        streams.data.items,
      );
      setSelectedStreams((current) => {
        return pickWorkspaceLogStreams(streams.data.items, current);
      });
    }
  }, []);

  const retrySelectedWorkspace = useCallback(async () => {
    const workspaceId = selectedId;
    if (!workspaceId) {
      return;
    }
    setRetryState({ status: "submitting" });
    const result = await apiPost<WorkspaceRetryResponse>(
      `/api/awf/workspaces/${encodeURIComponent(workspaceId)}/retry`,
    );
    if (!result.ok) {
      if (selectedIdRef.current !== workspaceId) {
        return;
      }
      setRetryState({ status: "error", message: formatProviderReadinessRetryError(result) });
      return;
    }
    if (selectedIdRef.current !== workspaceId) {
      await Promise.all([loadOverview(), loadResourceSaturation(), loadMergeQueue(), loadFailureSummary(), loadWorkspaceSummary()]);
      return;
    }
    setRetryState({
      status: "success",
      newWorkspaceId: result.data.new_workspace_id,
      operationId: result.data.operation_id,
    });
    await Promise.all([loadOverview(), loadResourceSaturation(), loadMergeQueue(), loadFailureSummary(), loadWorkspaceSummary()]);
  }, [loadMergeQueue, loadOverview, loadResourceSaturation, loadFailureSummary, loadWorkspaceSummary, selectedId]);

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
        await Promise.all([loadOverview(), loadResourceSaturation(), loadMergeQueue(), loadFailureSummary(), loadWorkspaceSummary()]);
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
      await Promise.all([
        loadOverview(),
        loadResourceSaturation(),
        loadMergeQueue(),
        loadFailureSummary(),
        loadWorkspaceSummary(),
        loadWorkspace(workspaceId),
      ]);
    },
    [
      detail.workspace?.version,
      loadFailureSummary,
      loadMergeQueue,
      loadOverview,
      loadResourceSaturation,
      loadWorkspace,
      loadWorkspaceSummary,
      operatorActionState.status,
      selectedId,
    ],
  );

  const loadLogTail = useCallback(
    async (workspaceId: string, stream: WorkspaceLogStream, selectedStreamIds: readonly string[]) => {
      const offset = Math.max(stream.byte_count - 65_536, 0);
      const activity = logStreamActivityFor(logStreamActivityRef.current, workspaceId, stream);
      const result = await apiGet<WorkspaceLogRead>(
        `/api/awf/workspaces/${workspaceId}/logs/${encodeURIComponent(
          stream.stream_id,
        )}?offset=${offset}&limit_bytes=65536`,
      );
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
    void loadResourceSaturation();
    const interval = window.setInterval(() => void loadResourceSaturation(), pollMs);
    return () => window.clearInterval(interval);
  }, [loadResourceSaturation]);

  useEffect(() => {
    void loadWorkspaceSummary();
    const interval = window.setInterval(() => void loadWorkspaceSummary(), pollMs);
    return () => window.clearInterval(interval);
  }, [loadWorkspaceSummary]);

  useEffect(() => {
    void loadMergeQueue();
    const interval = window.setInterval(() => void loadMergeQueue(), pollMs);
    return () => window.clearInterval(interval);
  }, [loadMergeQueue]);

  useEffect(() => {
    void loadFailureSummary();
    const interval = window.setInterval(() => void loadFailureSummary(), pollMs);
    return () => window.clearInterval(interval);
  }, [loadFailureSummary]);

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
    setStreamState("connecting");
    const source = new EventSource(
      `/api/awf/workspaces/${selectedId}/stream?channels=events,agent,validation,services&tail_bytes=65536`,
    );
    let closedByServer = false;
    let terminalError = false;

    source.onmessage = (message) => {
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
  }, [selectedId]);

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
          })
        : [],
    [detail.operations, detail.workspace, selectedMergeQueueItem, selectedOverview],
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

  const fleetKpis = useMemo<FleetKpi[]>(() => {
    const counts = resourceSaturation?.workspace_counts ?? null;
    const capacity = resourceSaturation ? capacityUtilizationPct(resourceSaturation) : null;
    const queued = resourceSaturation?.capacity_queue.queued_workspace_count ?? null;
    const oldestWait = resourceSaturation?.capacity_queue.oldest_wait_seconds ?? null;
    // Windowed reliability counts (default 24h) — actionable, unlike the
    // ever-growing cumulative failed total. Only meaningful once the summary
    // has loaded, so the window hint is omitted while the value is unknown.
    const windowHint = workspaceSummary ? `last ${workspaceSummary.since_hours}h` : undefined;
    const completed = workspaceSummary?.completed_count;
    const cancelled = workspaceSummary?.cancelled_count;
    const failed = workspaceSummary?.failed_count;
    const dash = "—";
    return [
      { id: "active", label: "Active", value: counts ? counts.active_total : dash, stale: saturationStale },
      {
        id: "running",
        label: "Running",
        // "Running" is the active-execution phase (running + validating + pushing) so a
        // workspace does not vanish from this KPI while it validates or pushes.
        value: counts ? counts.running + counts.validating + counts.pushing : dash,
        tone:
          counts && counts.running + counts.validating + counts.pushing > 0
            ? "info"
            : undefined,
        stale: saturationStale,
      },
      {
        id: "monitoring_pr",
        label: "Monitoring PR",
        value: counts ? counts.monitoring_pr : dash,
        tone: counts?.monitoring_pr ? "info" : undefined,
        stale: saturationStale,
      },
      {
        // Protected-file pause: a `blocked` workspace is awaiting an operator
        // guide decision while it still holds its slot + stack. It counts inside
        // Active (server-side, via active_total) but deliberately NOT inside
        // Running (running+validating+pushing, the PR #598 contract) — it is
        // halted, not executing.
        id: "blocked",
        label: "Awaiting operator",
        value: counts ? (counts.blocked ?? 0) : dash,
        tone: counts?.blocked ? "warn" : undefined,
        stale: saturationStale,
      },
      {
        // In-place provider retry: a `recovering` workspace auto-heals after the
        // provider cooldown (resumes in place, no operator action) while it still
        // holds its slot + stack. Like `blocked` it counts inside Active
        // (active_total) but NOT inside Running — it is paused, not executing.
        // The `info` tone (vs blocked's `warn`) signals "no action needed".
        id: "recovering",
        label: "Auto-retrying",
        value: counts ? (counts.recovering ?? 0) : dash,
        tone: counts?.recovering ? "info" : undefined,
        stale: saturationStale,
      },
      {
        // PR-monitor HUMAN_WAIT escalation: a `monitoring_pr` workspace flagged
        // awaiting a human (blocking review / deferred-human / merge BLOCKED). It
        // is NOT a pause — the monitor keeps polling and auto-recovers — so it
        // stays in `monitoring_pr` and counts inside Active (via active_total) but
        // deliberately NOT inside Running, the same non-Running rule as blocked.
        // `warn` tone because it needs an operator (mirrors "Awaiting operator").
        id: "awaiting_human",
        label: "Awaiting human",
        value: counts ? (counts.awaiting_human ?? 0) : dash,
        tone: counts?.awaiting_human ? "warn" : undefined,
        stale: saturationStale,
      },
      {
        id: "queued",
        label: "Queued",
        value: queued ?? dash,
        tone: queued ? "warn" : undefined,
        hint:
          queued && queued > 0
            ? oldestWait != null
              ? `oldest ${compactDuration(oldestWait)}`
              : "awaiting capacity"
            : undefined,
        stale: saturationStale,
      },
      {
        id: "completed",
        label: "Completed",
        value: completed ?? dash,
        tone: completed ? "good" : undefined,
        hint: completed != null ? windowHint : undefined,
        stale: summaryStale,
      },
      {
        id: "cancelled",
        label: "Cancelled",
        value: cancelled ?? dash,
        tone: cancelled ? "warn" : undefined,
        hint: cancelled != null ? windowHint : undefined,
        stale: summaryStale,
      },
      {
        id: "failed",
        label: "Failed",
        value: failed ?? dash,
        tone: failed ? "bad" : undefined,
        hint: failed != null ? windowHint : undefined,
        stale: summaryStale,
      },
      {
        id: "capacity",
        label: "Capacity",
        value: capacity ?? dash,
        suffix: capacity != null ? "%" : undefined,
        // Only flag pressure (warn/bad). Low/idle utilization stays neutral so a
        // value below the warn threshold is not styled like an active signal.
        tone: capacity != null ? (capacity >= 90 ? "bad" : capacity >= 75 ? "warn" : undefined) : undefined,
        stale: saturationStale,
      },
    ];
  }, [resourceSaturation, workspaceSummary, saturationStale, summaryStale]);

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
            void loadOverview();
            void loadResourceSaturation();
            void loadMergeQueue();
            void loadWorkspaceSummary();
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
          {error ? <ErrorBanner message={error} /> : null}
          <div className="grid min-w-0 gap-4 p-4 pb-0 2xl:grid-cols-[minmax(0,1fr)_minmax(460px,0.85fr)]">
            <div id="awf-capacity" className="min-w-0 scroll-mt-14">
              <ResourceCapacityPanel
                saturation={resourceSaturation}
                error={resourceError}
                stale={capacityStale}
                summaryStale={summaryStale}
                workspaceSummary={workspaceSummary}
                workspaceSummaryError={workspaceSummaryError}
              />
            </div>
            {/* 2xl: the panel overlays the cell (absolute) so the long merge
                list never drives the row height — Capacity sets the height and
                the list scrolls to fill it. Below 2xl it is normal flow. */}
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
            <div id="awf-failures" className="scroll-mt-14 2xl:col-span-2">
              <FailureAnalysisPanel
                summary={failureSummary}
                status={failureSummaryStatus}
                error={failureSummaryError}
                stale={failureStale}
              />
            </div>
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
