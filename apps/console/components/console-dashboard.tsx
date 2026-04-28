"use client";

import {
  Activity,
  AlertCircle,
  ArrowDown,
  ArrowUp,
  Bot,
  Boxes,
  ChevronDown,
  ChevronUp,
  CheckCircle2,
  CircleDot,
  Clock3,
  ExternalLink,
  FileText,
  GitPullRequest,
  HeartPulse,
  ListFilter,
  Loader2,
  Maximize2,
  Radio,
  RefreshCw,
  Search,
  Server,
  Terminal,
  X,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, useTransition } from "react";
import {
  bytes,
  compactDuration,
  compactId,
  fallbackLifecycleStages,
  fallbackLlmUsage,
  formatDateTime,
  lifecycleStages,
  relativeTime,
  renderLogEntries,
  statusTone,
  toneClass,
} from "@/lib/format";
import { formatAgentEffort, formatAgentLabel, formatAgentTitle } from "@/lib/agent-format";
import {
  formatRequiredNextAction,
  mergeQueueMergedAt,
  requiredNextActionTone,
  summarizeReadiness,
  summarizeRecovery,
  summarizeStaleReasons,
  summarizeValidation,
} from "@/lib/merge-queue-format";
import {
  formatRecoveryBadge,
  formatRecoveryCallout,
  isReverseWorkspaceTransition,
} from "@/lib/recovery-format";
import type {
  ApiEnvelope,
  AwfStreamFrame,
  FailureSummaryResponse,
  ListEnvelope,
  MergeQueueItem,
  Operation,
  ConcurrencyLane,
  ResourceSaturationSummary,
  RuntimeService,
  Workspace,
  WorkspaceEvent,
  WorkspaceLifecycleStage,
  WorkspaceLogRead,
  WorkspaceLogStream,
  WorkspaceOverview,
  WorkspaceRuntime,
  WorkspaceRetryResponse,
  WorkspaceStatus,
  WorkspaceReliabilitySummary,
} from "@/lib/types";

const pollMs = Number(process.env.NEXT_PUBLIC_AWF_CONSOLE_POLL_MS || "5000");
const maxLogChars = 180_000;
const mergeQueueLimit = 20;

type WorkspaceSortKey = "created_at" | "updated_at";
type SortDirection = "asc" | "desc";
type MergeQueueStatus = "loading" | "success" | "error";

type DetailState = {
  workspace: Workspace | null;
  runtime: WorkspaceRuntime | null;
  events: WorkspaceEvent[];
  operations: Operation[];
  streams: WorkspaceLogStream[];
};

type LogEntry = {
  key: string;
  workspaceId: string;
  streamId: string;
  source: string;
  fd: string | null;
  offset: number;
  data: string;
  occurredAt: string;
  order: number;
  kind: "tail" | "live";
};

type LogWorkspaceTarget = Pick<
  WorkspaceOverview,
  | "workspace_id"
  | "title"
  | "repo_url"
  | "base_branch"
  | "agent"
  | "agent_model"
  | "agent_effort"
  | "agent_model_source"
  | "agent_effort_source"
  | "status"
  | "pr_url"
>;

type RetryActionState =
  | { status: "idle" }
  | { status: "submitting" }
  | { status: "success"; newWorkspaceId: string; operationId: string }
  | { status: "error"; message: string };

const emptyDetail: DetailState = {
  workspace: null,
  runtime: null,
  events: [],
  operations: [],
  streams: [],
};

export function ConsoleDashboard() {
  const [overview, setOverview] = useState<WorkspaceOverview[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<DetailState>(emptyDetail);
  const [selectedStreams, setSelectedStreams] = useState<string[]>([]);
  const [logEntries, setLogEntries] = useState<LogEntry[]>([]);
  const [streamOffsets, setStreamOffsets] = useState<Record<string, number>>({});
  const [logsFullscreen, setLogsFullscreen] = useState(false);
  const [workspaceLogSelection, setWorkspaceLogSelection] = useState<string[]>([]);
  const [fullscreenWorkspaceIds, setFullscreenWorkspaceIds] = useState<string[]>([]);
  const [fullscreenTailSignal, setFullscreenTailSignal] = useState(0);
  const [logSortDirection, setLogSortDirection] = useState<SortDirection>("desc");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [agentFilter, setAgentFilter] = useState<string>("all");
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
  const [apiState, setApiState] = useState<"checking" | "ok" | "error">("checking");
  const [streamState, setStreamState] = useState<"idle" | "connecting" | "live" | "error">("idle");
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  const overviewPath = useMemo(() => {
    const params = new URLSearchParams({ limit: "100" });
    if (statusFilter !== "all") {
      params.set("status", statusFilter);
    }
    if (agentFilter !== "all") {
      params.set("agent", agentFilter);
    }
    if (repoFilter.trim()) {
      params.set("repo_url", repoFilter.trim());
    }
    return `/api/awf/workspaces/overview?${params}`;
  }, [agentFilter, repoFilter, statusFilter]);

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
        lifecycle: item.lifecycle ?? [],
        llm_usage: fallbackLlmUsage(item.llm_usage),
        recovery: item.recovery ?? null,
      })),
    );
    setLastRefresh(new Date());
    setSelectedId((current) =>
      current && result.data.items.some((item) => item.workspace_id === current)
        ? current
        : result.data.items[0]?.workspace_id ?? null,
    );
  }, [overviewPath]);

  const loadResourceSaturation = useCallback(async () => {
    const result = await apiGet<ResourceSaturationSummary>("/api/awf/metrics/resources/saturation");
    if (!result.ok) {
      setResourceError(result.message);
      return;
    }
    setResourceError(null);
    setResourceSaturation(result.data);
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
      setSelectedStreams((current) => {
        const streamItems = streams.data.items;
        const available = new Set(streamItems.map((stream) => stream.stream_id));
        const retained = current.filter((streamId) => available.has(streamId));
        const preferred =
          streamItems.find((stream) => stream.stream_id === "agent.stdout") ??
          streamItems.find((stream) => stream.byte_count > 0) ??
          streamItems[0];
        return retained.length > 0 ? retained : preferred ? [preferred.stream_id] : [];
      });
    }
  }, []);

  const retrySelectedWorkspace = useCallback(async () => {
    if (!selectedId) {
      return;
    }
    setRetryState({ status: "submitting" });
    const result = await apiPost<WorkspaceRetryResponse>(
      `/api/awf/workspaces/${encodeURIComponent(selectedId)}/retry`,
    );
    if (!result.ok) {
      setRetryState({ status: "error", message: retryErrorMessage(result) });
      return;
    }
    setRetryState({
      status: "success",
      newWorkspaceId: result.data.new_workspace_id,
      operationId: result.data.operation_id,
    });
    await Promise.all([loadOverview(), loadResourceSaturation(), loadMergeQueue(), loadFailureSummary(), loadWorkspaceSummary()]);
  }, [loadMergeQueue, loadOverview, loadResourceSaturation, loadFailureSummary, loadWorkspaceSummary, selectedId]);

  const loadLogTail = useCallback(
    async (workspaceId: string, stream: WorkspaceLogStream) => {
      const offset = Math.max(stream.byte_count - 65_536, 0);
      const result = await apiGet<WorkspaceLogRead>(
        `/api/awf/workspaces/${workspaceId}/logs/${encodeURIComponent(
          stream.stream_id,
        )}?offset=${offset}&limit_bytes=65536`,
      );
      if (!result.ok) {
      setLogEntries((current) =>
        trimLogEntries([
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
        ]),
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
        occurredAt: stream.closed_at ?? stream.opened_at,
        order: Date.parse(stream.closed_at ?? stream.opened_at) || Date.now(),
        kind: "tail" as const,
      };
      setLogEntries((current) =>
        trimLogEntries([
          ...current.filter(
            (entry) =>
              entry.workspaceId !== workspaceId ||
              entry.streamId !== stream.stream_id ||
              (entry.kind === "live" && entry.offset >= result.data.next_offset),
          ),
          tailEntry,
        ]),
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

  useEffect(() => {
    setDetail(emptyDetail);
    setSelectedStreams([]);
    setLogEntries([]);
    setStreamOffsets({});
    setRetryState({ status: "idle" });
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
        void loadLogTail(selectedId, stream);
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
          trimLogEntries([
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
          ]),
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
    const filtered = needle
      ? overview.filter((item) =>
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
        )
      : overview;
    return [...filtered].sort((left, right) => compareWorkspaceDates(left, right, sortKey, sortDirection));
  }, [overview, searchText, sortDirection, sortKey]);

  const selectedOverview = overview.find((item) => item.workspace_id === selectedId) ?? null;
  const selectedMergeQueueItem = useMemo(
    () => mergeQueue.find((item) => item.workspace_id === selectedId) ?? null,
    [mergeQueue, selectedId],
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
    for (const stream of detail.streams) {
      if (selectedStreams.includes(stream.stream_id)) {
        void loadLogTail(selectedId, stream);
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
    [selectedId],
  );
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

  return (
    <main className="min-h-screen bg-[var(--background)] text-[var(--foreground)]">
      <TopBar
        apiState={apiState}
        streamState={streamState}
        lastRefresh={lastRefresh}
        selectedId={selectedId}
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

      <div className="grid min-h-[calc(100vh-57px)] grid-cols-1 border-t border-[var(--border)] xl:grid-cols-[440px_minmax(0,1fr)] 2xl:grid-cols-[500px_minmax(0,1fr)]">
        <aside className="min-w-0 border-b border-[var(--border)] bg-white xl:border-r xl:border-b-0">
          <WorkspaceFilters
            statusFilter={statusFilter}
            agentFilter={agentFilter}
            repoFilter={repoFilter}
            searchText={searchText}
            sortKey={sortKey}
            sortDirection={sortDirection}
            onStatusFilter={setStatusFilter}
            onAgentFilter={setAgentFilter}
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
            onOpenLogs={openWorkspaceLogs}
          />
        </aside>

        <section className="min-w-0">
          {error ? <ErrorBanner message={error} /> : null}
          <div className="grid gap-4 p-4 pb-0 2xl:grid-cols-[minmax(0,1fr)_minmax(460px,0.85fr)]">
            <ResourceCapacityPanel 
              saturation={resourceSaturation} 
              error={resourceError} 
              workspaceSummary={workspaceSummary}
              workspaceSummaryError={workspaceSummaryError}
            />
            <MergeQueuePanel
              items={mergeQueue}
              hasMore={mergeQueueHasMore}
              status={mergeQueueStatus}
              error={mergeQueueError}
            />
            <div className="2xl:col-span-2">
              <FailureAnalysisPanel
                summary={failureSummary}
                status={failureSummaryStatus}
                error={failureSummaryError}
              />
            </div>
          </div>
          {selectedId && selectedOverview ? (
            <div className="grid gap-4 p-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(420px,0.9fr)]">
              <div className="grid min-w-0 gap-4">
                <WorkspaceSummary
                  overview={selectedOverview}
                  workspace={detail.workspace}
                  mergeQueueItem={selectedMergeQueueItem}
                  retryState={retryState}
                  onRetry={retrySelectedWorkspace}
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
                <OperationsPanel operations={detail.operations} />
              </div>
              <div className="grid min-w-0 gap-4">
                <EventsPanel events={detail.events} />
                <LogsPanel
                  streams={detail.streams}
                  selectedStreams={selectedStreams}
                  selectedStreamMetas={selectedStreamMetas}
                  entries={selectedLogEntries}
                  offsets={streamOffsets}
                  sortDirection={logSortDirection}
                  onToggleStream={(streamId, checked) =>
                    setSelectedStreams((current) => toggleStream(current, streamId, checked))
                  }
                  onSelectAll={() => setSelectedStreams(detail.streams.map((stream) => stream.stream_id))}
                  onClear={() => setSelectedStreams([])}
                  onReload={reloadSelectedLogs}
                  onOpenFullscreen={() => setLogsFullscreen(true)}
                  onToggleSortDirection={() =>
                    setLogSortDirection((current) => (current === "desc" ? "asc" : "desc"))
                  }
                />
              </div>
            </div>
          ) : (
            <EmptyState apiState={apiState} />
          )}
        </section>
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
    </main>
  );
}

function TopBar({
  apiState,
  streamState,
  lastRefresh,
  selectedId,
  isPending,
  onRefresh,
}: {
  apiState: "checking" | "ok" | "error";
  streamState: "idle" | "connecting" | "live" | "error";
  lastRefresh: Date | null;
  selectedId: string | null;
  isPending: boolean;
  onRefresh: () => void;
}) {
  return (
    <header className="flex min-h-14 flex-wrap items-center justify-between gap-3 bg-white px-4 py-2">
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-md border border-slate-200 bg-slate-950 text-white">
          <Boxes size={18} aria-hidden />
        </div>
        <div>
          <h1 className="text-sm font-semibold">AWF Console</h1>
          <p className="mono text-[11px] text-[var(--muted)]">
            {selectedId ? compactId(selectedId, 10) : "no workspace selected"}
          </p>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <StatePill icon={<HeartPulse size={13} />} label="API" state={apiState} />
        <StatePill icon={<Radio size={13} />} label="Stream" state={streamState} />
        <span className="rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1 text-slate-600">
          refreshed {lastRefresh ? relativeTime(lastRefresh.toISOString()) : "—"}
        </span>
        <button
          type="button"
          onClick={onRefresh}
          className="inline-flex h-8 items-center gap-2 rounded-md border border-slate-300 bg-white px-3 text-slate-800 transition hover:bg-slate-50"
        >
          <RefreshCw size={14} className={isPending ? "animate-spin" : ""} aria-hidden />
          Refresh
        </button>
      </div>
    </header>
  );
}

function StatePill({
  icon,
  label,
  state,
}: {
  icon: React.ReactNode;
  label: string;
  state: string;
}) {
  const tone = state === "ok" || state === "live" ? "good" : state === "error" ? "bad" : "info";
  return (
    <span className={`inline-flex h-8 items-center gap-1.5 rounded-md border px-2.5 ${toneClass(tone)}`}>
      {icon}
      {label}: {state}
    </span>
  );
}

function WorkspaceFilters({
  statusFilter,
  agentFilter,
  repoFilter,
  searchText,
  sortKey,
  sortDirection,
  onStatusFilter,
  onAgentFilter,
  onRepoFilter,
  onSearchText,
  onSortKey,
  onSortDirection,
  expanded,
  onToggleExpanded,
}: {
  statusFilter: string;
  agentFilter: string;
  repoFilter: string;
  searchText: string;
  sortKey: WorkspaceSortKey;
  sortDirection: SortDirection;
  onStatusFilter: (value: string) => void;
  onAgentFilter: (value: string) => void;
  onRepoFilter: (value: string) => void;
  onSearchText: (value: string) => void;
  onSortKey: (value: WorkspaceSortKey) => void;
  onSortDirection: (value: SortDirection) => void;
  expanded: boolean;
  onToggleExpanded: () => void;
}) {
  const activeFilters = workspaceFilterSummary({
    agentFilter,
    repoFilter,
    searchText,
    sortDirection,
    sortKey,
    statusFilter,
  });

  return (
    <div className="border-b border-[var(--border)]">
      <button
        type="button"
        onClick={onToggleExpanded}
        aria-expanded={expanded}
        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-xs text-slate-600 transition hover:bg-slate-50"
      >
        <span className="flex min-w-0 items-center gap-2">
          <ListFilter size={14} aria-hidden />
          <span className="font-semibold text-slate-900">Filters</span>
          <span className="min-w-0 truncate">{activeFilters}</span>
        </span>
        <span className="inline-flex h-7 shrink-0 items-center gap-1 rounded-md border border-slate-300 bg-white px-2 text-[11px] text-slate-700">
          {expanded ? <ChevronUp size={13} aria-hidden /> : <ChevronDown size={13} aria-hidden />}
          {expanded ? "Hide" : "Show"}
        </span>
      </button>
      <div className={expanded ? "grid gap-3 p-3 pt-1" : "hidden"}>
        <label className="relative block">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400"
            size={15}
            aria-hidden
          />
          <input
            value={searchText}
            onChange={(event) => onSearchText(event.target.value)}
            placeholder="Search workspaces"
            className="h-9 w-full rounded-md border border-slate-300 bg-white pr-3 pl-8 text-sm"
          />
        </label>
        <div className="grid grid-cols-2 gap-2">
          <Select
            label="Status"
            value={statusFilter}
            onChange={onStatusFilter}
            options={["all", ...lifecycleStages, "failed", "cancelled", "destroying", "destroyed"]}
          />
          <Select
            label="Agent"
            value={agentFilter}
            onChange={onAgentFilter}
            options={["all", "codex", "claude_code", "gemini"]}
          />
        </div>
        <div className="grid grid-cols-[minmax(0,1fr)_auto] items-end gap-2">
          <label htmlFor="workspace-sort-key" className="grid gap-1 text-[11px] font-medium text-slate-600">
            Sort
            <select
              id="workspace-sort-key"
              value={sortKey}
              onChange={(event) => onSortKey(event.target.value as WorkspaceSortKey)}
              className="h-8 rounded-md border border-slate-300 bg-white px-2 text-sm font-normal text-slate-900"
            >
              <option value="updated_at">updated date</option>
              <option value="created_at">created date</option>
            </select>
          </label>
          <button
            type="button"
            onClick={() => onSortDirection(sortDirection === "desc" ? "asc" : "desc")}
            className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-xs text-slate-800 transition hover:bg-slate-50"
            title={sortDirection === "desc" ? "Descending" : "Ascending"}
          >
            {sortDirection === "desc" ? <ArrowDown size={13} aria-hidden /> : <ArrowUp size={13} aria-hidden />}
            {sortDirection}
          </button>
        </div>
        <label className="grid gap-1 text-[11px] font-medium text-slate-600">
          Repo URL
          <input
            value={repoFilter}
            onChange={(event) => onRepoFilter(event.target.value)}
            placeholder="exact repo filter"
            className="h-8 rounded-md border border-slate-300 bg-white px-2 text-sm font-normal text-slate-900"
          />
        </label>
      </div>
    </div>
  );
}

function Select({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid gap-1 text-[11px] font-medium text-slate-600">
      {label}
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 rounded-md border border-slate-300 bg-white px-2 text-sm font-normal text-slate-900"
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}

function WorkspaceSelectionToolbar({
  selectedCount,
  onOpen,
  onClear,
}: {
  selectedCount: number;
  onOpen: () => void;
  onClear: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border)] px-3 py-2 text-xs">
      <span className="text-slate-500">{selectedCount} selected for logs</span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onOpen}
          disabled={selectedCount === 0}
          className="inline-flex h-7 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-[11px] text-slate-800 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Maximize2 size={12} aria-hidden />
          Open logs
        </button>
        <button
          type="button"
          onClick={onClear}
          disabled={selectedCount === 0}
          className="inline-flex h-7 items-center rounded-md border border-slate-300 bg-white px-2.5 text-[11px] text-slate-800 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Clear
        </button>
      </div>
    </div>
  );
}

function WorkspaceList({
  items,
  selectedId,
  selectedWorkspaceIds,
  onSelect,
  onToggleWorkspaceSelection,
  onOpenLogs,
}: {
  items: WorkspaceOverview[];
  selectedId: string | null;
  selectedWorkspaceIds: string[];
  onSelect: (workspaceId: string) => void;
  onToggleWorkspaceSelection: (workspaceId: string, checked: boolean) => void;
  onOpenLogs: (workspaceId: string) => void;
}) {
  if (items.length === 0) {
    return (
      <div className="grid min-h-64 place-items-center p-6 text-center text-sm text-[var(--muted)]">
        <div>
          <ListFilter className="mx-auto mb-3 text-slate-400" size={24} aria-hidden />
          No workspaces match the current filters.
        </div>
      </div>
    );
  }

  const selectedSet = new Set(selectedWorkspaceIds);
  return (
    <div className="max-h-[calc(100vh-205px)] overflow-y-auto overflow-x-hidden">
      {items.map((item) => {
        const recoveryBadge = formatRecoveryBadge(item.recovery);
        return (
          <div
            key={item.workspace_id}
            className={`grid min-w-0 gap-2 border-b border-slate-100 px-3 py-3 transition hover:bg-slate-50 ${
              selectedId === item.workspace_id ? "bg-blue-50" : "bg-white"
            }`}
          >
            <div className="flex min-w-0 items-start justify-between gap-3">
              <div className="flex min-w-0 flex-1 items-start gap-2">
                <input
                  type="checkbox"
                  checked={selectedSet.has(item.workspace_id)}
                  onChange={(event) => onToggleWorkspaceSelection(item.workspace_id, event.target.checked)}
                  aria-label={`Select ${item.title} for fullscreen logs`}
                  className="mt-0.5 h-4 w-4 shrink-0 rounded border-slate-300"
                />
                <button
                  type="button"
                  onClick={() => onSelect(item.workspace_id)}
                  className="grid min-w-0 flex-1 gap-2 text-left"
                >
                  <span className="line-clamp-2 text-sm font-semibold text-slate-950">{item.title}</span>
                  <div className="mono truncate text-[11px] text-[var(--muted)]">{item.workspace_id}</div>
                  <div className="grid gap-1 text-[11px] text-slate-500 sm:grid-cols-2">
                    <span className="truncate">created {formatDateTime(item.created_at)}</span>
                    <span className="truncate">updated {formatDateTime(item.updated_at)}</span>
                  </div>
                  <div className="flex items-center gap-2 text-xs text-slate-600">
                    <Bot size={13} aria-hidden />
                    <span className="truncate" title={formatAgentTitle(item)}>
                      {formatAgentLabel(item)}
                    </span>
                    <span className="text-slate-300">/</span>
                    <span className="truncate">{item.base_branch}</span>
                  </div>
                  <div className="truncate text-xs text-[var(--muted)]">{item.repo_url}</div>
                </button>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-1">
                <Badge value={item.status} />
                {recoveryBadge ? (
                  <span className="inline-flex h-6 max-w-44 items-center rounded-md border border-amber-200 bg-amber-50 px-2 text-[11px] font-medium text-amber-900">
                    <span className="truncate">{recoveryBadge}</span>
                  </span>
                ) : null}
                {item.pr_url ? <SmallExternalAnchor href={item.pr_url} label="PR" /> : null}
              </div>
            </div>
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => onOpenLogs(item.workspace_id)}
                className="inline-flex h-7 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-[11px] text-slate-800 transition hover:bg-slate-50"
              >
                <Terminal size={12} aria-hidden />
                Logs
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function WorkspaceSummary({
  overview,
  workspace,
  mergeQueueItem,
  retryState,
  onRetry,
}: {
  overview: WorkspaceOverview;
  workspace: Workspace | null;
  mergeQueueItem: MergeQueueItem | null;
  retryState: RetryActionState;
  onRetry: () => void;
}) {
  const canRetry = overview.status === "failed" || overview.status === "cancelled";
  const recovery = workspace?.recovery ?? overview.recovery ?? null;

  return (
    <Panel
      title="Workspace"
      icon={<Activity size={16} aria-hidden />}
      action={
        <div className="flex items-center gap-2">
          {canRetry ? (
            <button
              type="button"
              onClick={onRetry}
              disabled={retryState.status === "submitting"}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 text-xs text-slate-800 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <RefreshCw
                size={13}
                className={retryState.status === "submitting" ? "animate-spin" : ""}
                aria-hidden
              />
              Retry
            </button>
          ) : null}
          {overview.pr_url ? <ExternalAnchor href={overview.pr_url} label="PR" /> : null}
        </div>
      }
    >
      <div className="grid gap-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-lg font-semibold">{overview.title}</h2>
            <p className="mt-1 truncate text-sm text-[var(--muted)]">{overview.repo_url}</p>
          </div>
          <Badge value={overview.status} />
        </div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <Fact label="Workspace" value={overview.workspace_id} mono />
          <Fact label="Agent" value={formatAgentLabel(overview)} />
          <Fact label="Effort" value={formatAgentEffort(overview)} />
          <Fact label="Branch" value={workspace?.branch_name ?? overview.branch_name ?? "—"} mono />
          <Fact label="Base" value={overview.base_branch} mono />
          <Fact label="Phase" value={overview.current_phase} />
          <Fact label="Operation" value={overview.active_operation ?? "none"} />
          <Fact label="Updated" value={formatDateTime(overview.updated_at)} />
        </div>
        <WorkspaceRecoveryBlock item={mergeQueueItem} />
        <UsageSummaryBlock usage={workspace?.llm_usage ?? overview.llm_usage} />
        {recovery ? <RecoveryCallout recovery={recovery} status={overview.status} /> : null}
        {overview.failure_reason || overview.failure_message ? (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
            <div className="font-semibold">{overview.failure_reason ?? "failure"}</div>
            <div className="mt-1 text-red-800">{overview.failure_message ?? "No details."}</div>
          </div>
        ) : null}
        {retryState.status === "success" ? (
          <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
            Retry queued as{" "}
            <span className="mono font-semibold">{retryState.newWorkspaceId}</span>
            <span className="text-emerald-800"> / operation </span>
            <span className="mono">{compactId(retryState.operationId, 10)}</span>
          </div>
        ) : null}
        {retryState.status === "error" ? (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
            {retryState.message}
          </div>
        ) : null}
      </div>
    </Panel>
  );
}

function WorkspaceRecoveryBlock({ item }: { item: MergeQueueItem | null }) {
  if (!item) {
    return (
      <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
        <div className="font-semibold text-slate-900">Validation freshness</div>
        <div className="mt-1 text-slate-600">Workspace is not in the current merge queue snapshot.</div>
      </div>
    );
  }

  const recovery = summarizeRecovery(item);
  return (
    <div className="grid gap-2 rounded-md border border-slate-200 bg-white p-2 text-xs">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="font-semibold text-slate-900">Validation freshness</div>
          <div className="mono mt-0.5 truncate text-[11px] text-slate-500">
            {item.branch_name ?? "no branch"} / {item.base_branch}
          </div>
        </div>
        <Badge value={item.status} />
      </div>
      <div className="grid gap-1 sm:grid-cols-2 xl:grid-cols-3">
        <QueueChip
          label="Action"
          value={recovery.recommendedActionLabel}
          tone={requiredNextActionTone(item.required_next_action, item.merge_blocker_reason)}
        />
        <QueueChip
          label="Blocker"
          value={recovery.blockerLabel}
          detail={recovery.blockerDetail}
          tone={mergeBlockerTone(item.merge_blocker_reason)}
        />
        <QueueChip label="Required" value={recovery.requiredTierLabel} detail={item.task_class ?? "task class unknown"} />
        <QueueChip
          label="Satisfied"
          value={recovery.latestSatisfiedTierLabel}
          detail={recovery.latestSatisfiedTierDetail}
          tone={satisfiedTierTone(recovery.latestSatisfiedTierLabel)}
        />
        <QueueChip
          label="Freshness"
          value={recovery.freshnessLabel}
          detail={recovery.targetRangeLabel}
          tone={freshnessTone(recovery.freshnessLabel)}
        />
        <QueueChip
          label="Base"
          value={recovery.baseShaLabel}
          detail={item.latest_validation?.target_branch ?? item.base_branch}
          mono
        />
        <QueueChip
          label="Targets"
          value={recovery.targetRangeLabel}
          detail={`${recovery.validatedTargetShaLabel} / ${recovery.currentTargetShaLabel}`}
          mono
        />
        <QueueChip
          label="Stale"
          value={recovery.staleReasonLabel}
          detail={recovery.staleReasonDetail}
          tone={
            recovery.staleReasonBlockingCount > 0
              ? "warn"
              : recovery.staleReasonAdvisoryCount > 0
                ? "info"
                : "neutral"
          }
          mono={recovery.staleReasonCount > 0}
        />
        <QueueChip
          label="Queue"
          value={recovery.queueBlockerLabel}
          detail={recovery.queueBlockerDetail}
          tone={recovery.queueBlockerCount > 0 ? "warn" : "neutral"}
        />
      </div>
    </div>
  );
}

function UsageSummaryBlock({ usage }: { usage: Workspace["llm_usage"] | null | undefined }) {
  const safeUsage = fallbackLlmUsage(usage);
  return (
    <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold text-slate-900">LLM usage</span>
        <Badge value={safeUsage.status} />
      </div>
      <div className="mt-2 grid gap-2 sm:grid-cols-4">
        <UsageMetric label="Input" value={formatTokenCount(safeUsage.input_tokens)} />
        <UsageMetric label="Output" value={formatTokenCount(safeUsage.output_tokens)} />
        <UsageMetric label="Total" value={formatTokenCount(safeUsage.total_tokens)} />
        <UsageMetric label="Cost" value={formatCost(safeUsage.cost_estimate, safeUsage.currency)} />
      </div>
      <div className="mt-2 truncate text-[11px] text-slate-500">
        {safeUsage.status === "unavailable"
          ? (safeUsage.reason ?? "usage unavailable")
          : `${safeUsage.source}${safeUsage.reason ? ` / ${safeUsage.reason}` : ""}`}
      </div>
    </div>
  );
}

function RecoveryCallout({
  recovery,
  status,
}: {
  recovery: NonNullable<Workspace["recovery"]>;
  status: WorkspaceStatus;
}) {
  const callout = formatRecoveryCallout(recovery, status);
  return (
    <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2 font-semibold">
          <RefreshCw size={14} aria-hidden />
          <span className="truncate">{callout.title}</span>
        </div>
        <span className="mono rounded-md border border-amber-300 bg-white/70 px-2 py-0.5 text-[11px] text-amber-900">
          {callout.reason}
        </span>
      </div>
      <div className="mt-2 grid gap-2 text-xs sm:grid-cols-3">
        <Fact label="Action" value={callout.action} />
        <Fact label="Current" value={callout.current} />
        <Fact label="Started" value={formatDateTime(recovery.started_at)} />
      </div>
      <div className="mt-2 text-xs text-amber-900">{callout.body}</div>
    </div>
  );
}

function UsageMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] font-medium text-slate-500">{label}</div>
      <div className="mono truncate text-sm text-slate-950">{value}</div>
    </div>
  );
}

function ResourceCapacityPanel({
  saturation,
  error,
  workspaceSummary,
  workspaceSummaryError,
}: {
  saturation: ResourceSaturationSummary | null;
  error: string | null;
  workspaceSummary: WorkspaceReliabilitySummary | null;
  workspaceSummaryError: string | null;
}) {
  const totalReason = workspaceSummary ? workspaceSummary.actionable_reason_count + workspaceSummary.unactionable_reason_count : 0;
  const coverage = totalReason > 0 ? Math.round((workspaceSummary!.actionable_reason_count / totalReason) * 100) : 0;

  return (
    <Panel title="Resource / Capacity" icon={<Server size={16} aria-hidden />}>
      {!saturation ? (
        <MutedLine>{error ? `Unable to load capacity: ${error}` : "Capacity snapshot loading."}</MutedLine>
      ) : (
        <div className="grid gap-3">
          {error ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              Showing last capacity snapshot. Refresh failed: {error}
            </div>
          ) : null}
          {workspaceSummaryError ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              Unable to load workspace reliability metrics: {workspaceSummaryError}
            </div>
          ) : null}
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <Fact label="Active" value={`${saturation.workspace_counts.active_total} workspaces`} />
            <Fact 
              label="Stuck" 
              value={workspaceSummary ? `${workspaceSummary.stuck_count} workspaces` : "—"} 
            />
            <Fact 
              label="Reason Coverage" 
              value={workspaceSummary ? `${coverage}% (${totalReason} tracked)` : "—"} 
            />
            <Fact
              label="Reserved CPU"
              value={`${formatScalar(saturation.reserved_resources.steady_cpu)} steady / ${formatScalar(
                saturation.reserved_resources.peak_cpu,
              )} peak`}
            />
            <Fact
              label="Reserved memory"
              value={`${formatGb(saturation.reserved_resources.steady_memory_gb)} steady / ${formatGb(
                saturation.reserved_resources.peak_memory_gb,
              )} peak`}
            />
            <Fact
              label="Disk free"
              value={`${bytes(saturation.disk.free_bytes)} / ${formatPercent(saturation.disk.percent_free)}`}
            />
          </div>
          <StatusCountStrip counts={saturation.workspace_counts} />
          <div className="grid gap-2 md:grid-cols-2">
            <LaneMeter label="Provision" lane={saturation.concurrency.provision} />
            <LaneMeter label="Execution" lane={saturation.concurrency.execution} />
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            <div className={`rounded-md border px-3 py-2 text-xs ${toneClass(saturation.disk.ok ? "good" : "bad")}`}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold">Disk {saturation.disk.status}</span>
                <span className="mono">{saturation.disk.reason}</span>
              </div>
              <div className="mt-1 text-slate-600">
                threshold {bytes(saturation.disk.threshold_bytes)} / checked {saturation.disk.checked_path}
              </div>
            </div>
            <div
              className={`rounded-md border px-3 py-2 text-xs ${toneClass(
                saturation.admission.ok
                  ? saturation.admission.status === "saturated"
                    ? "warn"
                    : "good"
                  : "bad",
              )}`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold">Admission {saturation.admission.status}</span>
                <span className="mono">{saturation.admission.reason}</span>
              </div>
              {saturation.admission.detail ? (
                <div className="mt-1 text-slate-600">{saturation.admission.detail}</div>
              ) : null}
            </div>
          </div>
          <div className="text-[11px] text-slate-500">
            generated {relativeTime(saturation.generated_at)}
          </div>
        </div>
      )}
    </Panel>
  );
}

function MergeQueuePanel({
  items,
  hasMore,
  status,
  error,
}: {
  items: MergeQueueItem[];
  hasMore: boolean;
  status: MergeQueueStatus;
  error: string | null;
}) {
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());
  const hasSnapshot = items.length > 0;
  const isLoading = status === "loading";
  const isError = status === "error";
  const summary =
    isLoading && !hasSnapshot
      ? "loading"
      : isError && !hasSnapshot
        ? "error"
        : `${items.length}${hasMore ? "+" : ""} candidate${items.length === 1 ? "" : "s"}`;

  return (
    <Panel
      title="Merge Queue"
      icon={<GitPullRequest size={16} aria-hidden />}
      action={
        <span className="rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] text-slate-600">
          {summary}
        </span>
      }
    >
      {isLoading && !hasSnapshot ? (
        <MutedLine>Merge queue snapshot loading.</MutedLine>
      ) : isError && !hasSnapshot ? (
        <MutedLine>Unable to load merge queue: {error}</MutedLine>
      ) : !hasSnapshot ? (
        <MutedLine>No PR-backed merge candidates are queued.</MutedLine>
      ) : (
        <div className="grid gap-2">
          {error ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              Showing last merge queue snapshot. Refresh failed: {error}
            </div>
          ) : null}
          {hasMore ? (
            <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
              Showing first {items.length} merge candidates. More are queued.
            </div>
          ) : null}
          <div className="grid max-h-[460px] gap-2 overflow-auto pr-1">
            {items.map((item, index) => {
              const rowKey = item.candidate_id ?? item.workspace_id;
              const expanded = expandedRows.has(rowKey);
              return (
                <MergeQueueRow
                  key={rowKey}
                  item={item}
                  position={index + 1}
                  expanded={expanded}
                  onToggle={() =>
                    setExpandedRows((current) => {
                      const next = new Set(current);
                      if (next.has(rowKey)) {
                        next.delete(rowKey);
                      } else {
                        next.add(rowKey);
                      }
                      return next;
                    })
                  }
                />
              );
            })}
          </div>
        </div>
      )}
    </Panel>
  );
}

function MergeQueueRow({
  item,
  position,
  expanded,
  onToggle,
}: {
  item: MergeQueueItem;
  position: number;
  expanded: boolean;
  onToggle: () => void;
}) {
  const rowDetailsId = `merge-candidate-${item.candidate_id ?? item.workspace_id}`;
  const actionLabel = formatRequiredNextAction(item.required_next_action, item.merge_blocker_reason);
  const readiness = summarizeReadiness(item);
  const stale = summarizeStaleReasons(item);
  const validation = summarizeValidation(item);
  const recovery = summarizeRecovery(item);
  const mergedAt = mergeQueueMergedAt(item);
  return (
    <article className="grid gap-2 border-b border-slate-100 pb-2 text-xs last:border-b-0 last:pb-0">
      <div className="flex min-w-0 items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <span className="mono shrink-0 rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[10px] text-slate-500">
              #{position}
            </span>
            <h3 className="truncate text-sm font-semibold text-slate-950">{item.title}</h3>
          </div>
          <div className="mono mt-1 truncate text-[11px] text-slate-500">{item.workspace_id}</div>
          <div className="mt-1 flex min-w-0 flex-wrap gap-x-2 gap-y-1 text-[11px] text-slate-500">
            <span className="truncate">created {formatDateTime(item.created_at)}</span>
            <span className="truncate">merged {formatDateTime(mergedAt)}</span>
            <span className="truncate">
              base <span className="mono text-slate-700">{item.base_branch}</span>
            </span>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">
          <Badge value={item.status} />
          <SmallExternalAnchor href={item.pr_url} label="PR" />
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={expanded}
            aria-controls={rowDetailsId}
            className="inline-flex h-7 items-center gap-1 rounded-md border border-slate-200 bg-white px-2 text-[11px] font-medium text-slate-700 hover:bg-slate-50"
          >
            {expanded ? <ChevronUp size={13} aria-hidden /> : <ChevronDown size={13} aria-hidden />}
            {expanded ? "Hide" : "Details"}
          </button>
        </div>
      </div>
      <div className="grid gap-1 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-5">
        <QueueChip
          label="Action"
          value={recovery.recommendedActionLabel}
          tone={requiredNextActionTone(item.required_next_action, item.merge_blocker_reason)}
        />
        <QueueChip
          label="Blocker"
          value={recovery.blockerLabel}
          detail={recovery.blockerDetail}
          tone={mergeBlockerTone(item.merge_blocker_reason)}
        />
        <QueueChip label="Required" value={recovery.requiredTierLabel} detail={item.task_class ?? "task class unknown"} />
        <QueueChip
          label="Satisfied"
          value={recovery.latestSatisfiedTierLabel}
          detail={recovery.latestSatisfiedTierDetail}
          tone={satisfiedTierTone(recovery.latestSatisfiedTierLabel)}
        />
        <QueueChip
          label="Freshness"
          value={recovery.freshnessLabel}
          detail={recovery.targetRangeLabel}
          tone={freshnessTone(recovery.freshnessLabel)}
        />
        <QueueChip
          label="Readiness"
          value={`${readiness.canonicalLabel} / ${readiness.label}`}
          detail={readiness.detail}
          tone={readinessTone(readiness.label)}
        />
        <QueueChip
          label="Candidate"
          value={`${recovery.candidateLabel} / ${recovery.attemptLabel}`}
          detail={item.candidate_status ?? "unknown"}
          mono
        />
        <QueueChip
          label="Base"
          value={recovery.baseShaLabel}
          detail={item.latest_validation?.target_branch ?? item.base_branch}
          mono
        />
        <QueueChip
          label="Targets"
          value={recovery.targetRangeLabel}
          detail={`${recovery.validatedTargetShaLabel} / ${recovery.currentTargetShaLabel}`}
          mono
        />
        <QueueChip
          label="Stale"
          value={recovery.staleReasonLabel}
          detail={recovery.staleReasonDetail}
          tone={
            recovery.staleReasonBlockingCount > 0
              ? "warn"
              : recovery.staleReasonAdvisoryCount > 0
                ? "info"
                : "neutral"
          }
          mono={recovery.staleReasonCount > 0}
        />
        <QueueChip
          label="Queue"
          value={recovery.queueBlockerLabel}
          detail={recovery.queueBlockerDetail}
          tone={recovery.queueBlockerCount > 0 ? "warn" : "neutral"}
        />
        <QueueChip
          label="Policy"
          value={recovery.policyFindingLabel}
          detail={recovery.policyFindingDetail}
          tone={recovery.policyFindingCount > 0 ? "bad" : "neutral"}
        />
        <QueueChip label="Validation" value={validation.label} detail={validation.detail} tone={validationTone(item)} />
        <QueueChip label="Coverage" value={validation.coverageLabel} detail={validation.headLabel} mono />
      </div>
      {expanded ? (
        <div id={rowDetailsId} className="grid gap-2">
          <div className="grid gap-1 sm:grid-cols-3">
            <QueueDatum label="Candidate" value={item.candidate_id ? compactId(item.candidate_id, 10) : "legacy"} mono />
            <QueueDatum label="Attempt" value={item.attempt_id ? compactId(item.attempt_id, 10) : "none"} mono />
            <QueueDatum
              label="Canonical"
              value={readiness.canonicalLabel}
              tone={item.canonical ? statusTone("completed") : statusTone("failed")}
            />
            <QueueDatum label="Candidate status" value={item.candidate_status ?? "unknown"} mono />
            <QueueDatum
              label="Action"
              value={actionLabel}
              tone={requiredNextActionTone(item.required_next_action, item.merge_blocker_reason)}
            />
            <QueueDatum label="Readiness" value={readiness.detail} mono tone={readinessTone(readiness.label)} />
            <QueueDatum label="Mode" value={item.auto_merge ? "auto-merge" : "manual"} />
            <QueueDatum label="Created" value={formatDateTime(item.created_at)} />
            <QueueDatum label="Merged" value={formatDateTime(mergedAt)} />
            <QueueDatum label="Last event" value={lastEventReason(item.last_event)} mono />
            <QueueDatum label="Blocker" value={item.merge_blocker_reason} mono tone={mergeBlockerTone(item.merge_blocker_reason)} />
            <QueueDatum label="Required tier" value={recovery.requiredTierLabel} />
            <QueueDatum label="Latest satisfied" value={recovery.latestSatisfiedTierLabel} tone={satisfiedTierTone(recovery.latestSatisfiedTierLabel)} />
            <QueueDatum label="Validation" value={validation.label} tone={validationTone(item)} />
            <QueueDatum label="Freshness" value={recovery.freshnessLabel} tone={freshnessTone(recovery.freshnessLabel)} />
            <QueueDatum label="Base SHA" value={recovery.baseShaLabel} mono />
            <QueueDatum label="Validated target" value={recovery.validatedTargetShaLabel} mono />
            <QueueDatum label="Current target" value={recovery.currentTargetShaLabel} mono />
            <QueueDatum label="Validation heads" value={recovery.targetRangeLabel} mono />
            <QueueDatum label="Coverage" value={validation.coverageLabel} mono />
          </div>
          <div className="grid gap-1 text-[11px] text-slate-500 sm:grid-cols-2">
            <span className="truncate">
              base <span className="mono text-slate-700">{item.base_branch}</span>
            </span>
            <span className="truncate">
              branch <span className="mono text-slate-700">{item.branch_name ?? "none"}</span>
            </span>
            <span className="truncate">updated {formatDateTime(item.updated_at)}</span>
          </div>
          <MergeQueueBlockerDetails blockers={item.queue_blockers ?? []} />
          <MergeQueueStaleReasonDetails reasons={stale.activeReasons} />
          <MergeQueuePolicyFindingDetails findings={item.policy_findings ?? []} />
        </div>
      ) : null}
    </article>
  );
}

function QueueDatum({
  label,
  value,
  mono = false,
  tone,
}: {
  label: string;
  value: string;
  mono?: boolean;
  tone?: ReturnType<typeof statusTone>;
}) {
  return (
    <div className={`min-w-0 rounded-md border px-2 py-1.5 ${tone ? toneClass(tone) : "border-slate-200 bg-slate-50"}`}>
      <div className="text-[10px] font-medium text-slate-500">{label}</div>
      <div className={`${mono ? "mono" : ""} truncate text-[11px] text-slate-900`}>{value}</div>
    </div>
  );
}

function QueueChip({
  label,
  value,
  detail,
  mono = false,
  tone = "neutral",
}: {
  label: string;
  value: string;
  detail?: string;
  mono?: boolean;
  tone?: ReturnType<typeof statusTone>;
}) {
  return (
    <div
      title={detail ? `${label}: ${value} (${detail})` : `${label}: ${value}`}
      className={`min-w-0 rounded-md border px-2 py-1 ${toneClass(tone)}`}
    >
      <div className="text-[10px] font-medium text-slate-500">{label}</div>
      <div className={`${mono ? "mono" : ""} truncate text-[11px] font-medium text-slate-900`}>{value}</div>
      {detail ? <div className="truncate text-[10px] text-slate-500">{detail}</div> : null}
    </div>
  );
}

function MergeQueueBlockerDetails({ blockers }: { blockers: MergeQueueItem["queue_blockers"] }) {
  if (blockers.length === 0) {
    return null;
  }
  return (
    <div className="grid gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 py-1.5">
      <div className="text-[10px] font-medium text-amber-900">Queue blockers</div>
      {blockers.map((blocker) => (
        <div key={blocker.candidate_id} className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-amber-950">
          <span className="truncate font-medium">{blocker.title}</span>
          <span className="mono">{blocker.pr_number ? `#${blocker.pr_number}` : compactId(blocker.candidate_id, 10)}</span>
          <span className="mono">{blocker.blocker_state}</span>
          <span className="mono">{blocker.status}</span>
          <span className="mono truncate">{blocker.reason_code}</span>
          <SmallExternalAnchor href={blocker.pr_url} label="PR" />
        </div>
      ))}
    </div>
  );
}

function MergeQueueStaleReasonDetails({ reasons }: { reasons: MergeQueueItem["stale_reasons"] }) {
  if (reasons.length === 0) {
    return null;
  }
  return (
    <div className="grid gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 py-1.5">
      <div className="text-[10px] font-medium text-amber-900">Active stale reasons</div>
      {reasons.map((reason) => (
        <div key={reason.id} className="grid min-w-0 gap-0.5 text-[11px] text-amber-950">
          <div className="flex min-w-0 flex-wrap gap-x-2 gap-y-1">
            <span className="mono font-medium">{reason.reason_code}</span>
            <span className="mono">{reason.severity}</span>
            <span className="mono">{reason.trigger_type}</span>
            <span className="mono truncate">{reason.trigger_ref ?? "no trigger ref"}</span>
            <span>detected {formatDateTime(reason.detected_at)}</span>
          </div>
          <div className="truncate text-amber-900">{reason.explanation}</div>
        </div>
      ))}
    </div>
  );
}

function MergeQueuePolicyFindingDetails({ findings }: { findings: MergeQueueItem["policy_findings"] }) {
  if (findings.length === 0) {
    return null;
  }
  return (
    <div className="grid gap-1 rounded-md border border-red-200 bg-red-50 px-2 py-1.5">
      <div className="text-[10px] font-medium text-red-900">Policy findings</div>
      {findings.map((finding) => (
        <div key={finding.id} className="grid min-w-0 gap-0.5 text-[11px] text-red-950">
          <div className="flex min-w-0 flex-wrap gap-x-2 gap-y-1">
            <span className="mono font-medium">{finding.reason_code}</span>
            <span className="mono">{finding.severity}</span>
            <span className="mono truncate">{finding.subject_path ?? "no subject path"}</span>
            <span>detected {formatDateTime(finding.detected_at)}</span>
          </div>
          <div className="truncate text-red-900">{finding.explanation}</div>
        </div>
      ))}
    </div>
  );
}

function lastEventReason(event: WorkspaceEvent | null): string {
  if (!event) {
    return "none";
  }
  return event.reason_code ?? event.event_type;
}

function mergeBlockerTone(reason: MergeQueueItem["merge_blocker_reason"]): ReturnType<typeof statusTone> {
  switch (reason) {
    case "ready_to_merge_or_waiting_for_github":
    case "completed":
      return "good";
    case "manual_merge_required":
    case "waiting_for_monitor":
    case "waiting_for_older_candidate":
    case "stale":
      return "warn";
    case "failed_or_cancelled":
    case "not_canonical":
    case "policy_blocked":
      return "bad";
    case "workspace_not_terminal":
      return "info";
    default:
      return "info";
  }
}

function readinessTone(label: string): ReturnType<typeof statusTone> {
  switch (label) {
    case "ready":
    case "completed":
      return "good";
    case "manual":
    case "waiting":
    case "stale":
    case "blocked":
      return "warn";
    case "failed/cancelled":
    case "not canonical":
      return "bad";
    case "legacy":
      return "neutral";
    default:
      return "info";
  }
}

function validationTone(item: MergeQueueItem): ReturnType<typeof statusTone> {
  const validation = item.latest_validation;
  if (!validation) {
    return "neutral";
  }
  if (validation.status === "failed" || validation.coverage_status === "failed") {
    return "bad";
  }
  if (validation.fresh_for_target === false) {
    return "warn";
  }
  if (validation.status === "succeeded") {
    return "good";
  }
  if (validation.status === "running") {
    return "info";
  }
  return "neutral";
}

function freshnessTone(label: string): ReturnType<typeof statusTone> {
  if (label === "fresh") {
    return "good";
  }
  if (label.includes("stale")) {
    return "warn";
  }
  return "neutral";
}

function satisfiedTierTone(label: string): ReturnType<typeof statusTone> {
  if (label.startsWith("T")) {
    return "good";
  }
  if (label.startsWith("unknown")) {
    return "warn";
  }
  return "neutral";
}

function StatusCountStrip({ counts }: { counts: ResourceSaturationSummary["workspace_counts"] }) {
  const items: [string, number][] = [
    ["requested", counts.requested],
    ["provisioning", counts.provisioning],
    ["ready", counts.ready],
    ["running", counts.running],
    ["validating", counts.validating],
    ["pushing", counts.pushing],
    ["pr", counts.monitoring_pr],
    ["destroying", counts.destroying],
  ];

  return (
    <div className="grid grid-cols-2 gap-1 text-xs sm:grid-cols-4 xl:grid-cols-8">
      {items.map(([label, value]) => (
        <div key={label} className="min-w-0 rounded-md border border-slate-200 bg-slate-50 px-2 py-1.5">
          <div className="truncate text-[10px] font-medium text-slate-500">{label}</div>
          <div className="mono text-sm text-slate-950">{value}</div>
        </div>
      ))}
    </div>
  );
}

function LaneMeter({ label, lane }: { label: string; lane: ConcurrencyLane }) {
  const hasFiniteLimit = lane.limit > 0;
  const fill = hasFiniteLimit ? Math.min(100, Math.max(0, (lane.in_use / lane.limit) * 100)) : 0;
  const saturated = hasFiniteLimit && lane.in_use >= lane.limit;
  const limitLabel = hasFiniteLimit ? `${lane.in_use}/${lane.limit}` : `${lane.in_use} active`;

  return (
    <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold text-slate-900">{label}</span>
        <span className={saturated ? "font-semibold text-amber-800" : "text-slate-600"}>
          {hasFiniteLimit ? `${limitLabel} in use` : `${limitLabel} / no limit`}
        </span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-200">
        <div className={saturated ? "h-full bg-amber-500" : "h-full bg-blue-500"} style={{ width: `${fill}%` }} />
      </div>
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-slate-600">
        <span>{lane.queued} queued</span>
        <span>{lane.available} available</span>
      </div>
    </div>
  );
}

function LifecycleRail({
  status,
  lifecycle,
  terminalSourceStage,
}: {
  status: WorkspaceStatus;
  lifecycle: WorkspaceLifecycleStage[];
  terminalSourceStage: string | null;
}) {
  const terminal = status === "failed" || status === "cancelled";
  const stages: WorkspaceLifecycleStage[] =
    lifecycle.length > 0 ? lifecycle : fallbackLifecycleStages(status, terminalSourceStage);
  return (
    <Panel title="Lifecycle" icon={<GitPullRequest size={16} aria-hidden />}>
      <div className="grid gap-2 md:grid-cols-4 xl:grid-cols-8">
        {stages.map((stage) => {
          return (
            <div
              key={stage.stage}
              className={`min-h-16 rounded-md border p-2 ${
                stage.status === "active"
                  ? "border-blue-300 bg-blue-50"
                  : stage.status === "completed"
                    ? "border-emerald-200 bg-emerald-50"
                    : stage.status === "terminal_skipped"
                      ? "border-amber-200 bg-amber-50"
                      : "border-slate-200 bg-white"
              }`}
            >
              <div className="flex items-center gap-1.5">
                {stage.status === "completed" ? (
                  <CheckCircle2 size={14} className="text-emerald-700" aria-hidden />
                ) : stage.status === "active" ? (
                  <CircleDot size={14} className="text-blue-700" aria-hidden />
                ) : stage.status === "terminal_skipped" ? (
                  <AlertCircle size={14} className="text-amber-700" aria-hidden />
                ) : (
                  <Clock3 size={14} className="text-slate-400" aria-hidden />
                )}
                <span className="truncate text-xs font-medium">{stage.stage}</span>
              </div>
              <div className="mt-2 grid gap-1 text-[11px] text-slate-600">
                <div className="truncate">start {formatDateTime(stage.started_at)}</div>
                <div className="truncate">
                  end {stage.status === "active" ? "active" : formatDateTime(stage.ended_at)}
                </div>
                <div className="mono">{compactDuration(stage.duration_seconds)}</div>
              </div>
            </div>
          );
        })}
      </div>
      {terminal ? (
        <div className="mt-3 inline-flex items-center gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <AlertCircle size={14} aria-hidden />
          Terminal state: {status}
        </div>
      ) : null}
    </Panel>
  );
}

function terminalLifecycleSourceStage(
  status: WorkspaceStatus,
  events: WorkspaceEvent[],
  lastEvent: WorkspaceEvent | null,
  currentPhase: string,
): string | null {
  if (status !== "failed" && status !== "cancelled") {
    return null;
  }
  const terminalEvent =
    events.find((event) => event.event_type === "workspace.state_changed" && event.new_state === status) ??
    (lastEvent?.event_type === "workspace.state_changed" && lastEvent.new_state === status
      ? lastEvent
      : null);
  return terminalEvent?.old_state ?? currentPhase;
}

function RuntimePanel({ runtime }: { runtime: WorkspaceRuntime | null }) {
  return (
    <Panel title="Runtime" icon={<Server size={16} aria-hidden />}>
      {!runtime ? (
        <MutedLine>Runtime snapshot unavailable.</MutedLine>
      ) : (
        <div className="grid gap-3">
          <div className="grid gap-2 sm:grid-cols-3">
            <Fact label="Compose project" value={runtime.compose_project_name ?? "—"} mono />
            <Fact label="Stack" value={runtime.stack_state} />
            <Fact label="Services" value={String(runtime.services.length)} />
          </div>
          {runtime.reason ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              {runtime.reason}
            </div>
          ) : null}
          <div className="overflow-auto rounded-md border border-slate-200">
            <table className="w-full min-w-[720px] text-left text-xs">
              <thead className="bg-slate-50 text-slate-600">
                <tr>
                  <Th>Name</Th>
                  <Th>State</Th>
                  <Th>Health</Th>
                  <Th>Image</Th>
                  <Th>Ports</Th>
                  <Th>Started</Th>
                </tr>
              </thead>
              <tbody>
                {runtime.services.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center text-slate-500">
                      No active containers reported.
                    </td>
                  </tr>
                ) : (
                  runtime.services.map((service) => <RuntimeRow key={service.name} item={service} />)
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Panel>
  );
}

function RuntimeRow({ item }: { item: RuntimeService }) {
  return (
    <tr className="border-t border-slate-100">
      <Td>
        <div className="font-medium">{item.name}</div>
        <div className="mono text-[11px] text-slate-500">{compactId(item.container_id, 10)}</div>
      </Td>
      <Td>
        <Badge value={item.state} />
        <div className="mt-1 text-slate-500">{item.status ?? "—"}</div>
      </Td>
      <Td>{item.health ? <Badge value={item.health} /> : "—"}</Td>
      <Td className="mono max-w-[220px] truncate">{item.image ?? "—"}</Td>
      <Td>{item.ports.length > 0 ? item.ports.join(", ") : "—"}</Td>
      <Td>{item.started_at ?? "—"}</Td>
    </tr>
  );
}

function OperationsPanel({ operations }: { operations: Operation[] }) {
  return (
    <Panel title="Operations" icon={<Loader2 size={16} aria-hidden />}>
      {operations.length === 0 ? (
        <MutedLine>No operations recorded.</MutedLine>
      ) : (
        <div className="grid gap-2">
          {operations.slice(0, 8).map((operation) => (
            <div
              key={operation.id}
              className="grid gap-1 rounded-md border border-slate-200 bg-white px-3 py-2 text-xs sm:grid-cols-[1fr_auto]"
            >
              <div>
                <span className="font-medium">{operation.type}</span>
                <span className="mono ml-2 text-slate-500">{compactId(operation.id, 10)}</span>
              </div>
              <div className="flex items-center gap-2">
                <Badge value={operation.status} />
                <span className="text-slate-500">{formatDateTime(operation.created_at)}</span>
              </div>
              {operation.error_message ? (
                <div className="text-red-700 sm:col-span-2">{operation.error_message}</div>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function EventsPanel({ events }: { events: WorkspaceEvent[] }) {
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const sortedEvents = useMemo(() => {
    return [...events].sort((left, right) => {
      const leftTime = Date.parse(left.occurred_at) || 0;
      const rightTime = Date.parse(right.occurred_at) || 0;
      const timeDelta = sortDirection === "desc" ? rightTime - leftTime : leftTime - rightTime;

      if (timeDelta !== 0) {
        return timeDelta;
      }

      return sortDirection === "desc" ? right.id.localeCompare(left.id) : left.id.localeCompare(right.id);
    });
  }, [events, sortDirection]);

  return (
    <Panel
      title="Timeline"
      icon={<FileText size={16} aria-hidden />}
      action={
        <button
          type="button"
          onClick={() => setSortDirection((current) => (current === "desc" ? "asc" : "desc"))}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-xs text-slate-800 transition hover:bg-slate-50"
          title={sortDirection === "desc" ? "Descending" : "Ascending"}
        >
          {sortDirection === "desc" ? <ArrowDown size={13} aria-hidden /> : <ArrowUp size={13} aria-hidden />}
          {sortDirection}
        </button>
      }
    >
      {events.length === 0 ? (
        <MutedLine>No events recorded.</MutedLine>
      ) : (
        <div className="max-h-[360px] overflow-auto">
          {sortedEvents.map((event) => {
            const reverse = isReverseWorkspaceTransition(event);
            return (
              <div
                key={event.id}
                className={`grid gap-1 border-b py-2 text-xs ${
                  reverse
                    ? "border-amber-100 bg-amber-50/70 px-2"
                    : "border-slate-100"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{event.event_type}</span>
                  <span className="text-slate-500">{formatDateTime(event.occurred_at)}</span>
                </div>
                <div className="flex flex-wrap gap-2 text-slate-600">
                  <span>{event.old_state ?? "—"} → {event.new_state ?? "—"}</span>
                  {event.reason_code ? <span className="mono">{event.reason_code}</span> : null}
                  {reverse ? (
                    <span className="rounded-md border border-amber-200 bg-white px-1.5 py-0.5 text-[10px] font-medium text-amber-900">
                      step-back
                    </span>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

function LogsPanel({
  streams,
  selectedStreams,
  selectedStreamMetas,
  entries,
  offsets,
  sortDirection,
  onToggleStream,
  onSelectAll,
  onClear,
  onReload,
  onOpenFullscreen,
  onToggleSortDirection,
}: {
  streams: WorkspaceLogStream[];
  selectedStreams: string[];
  selectedStreamMetas: WorkspaceLogStream[];
  entries: LogEntry[];
  offsets: Record<string, number>;
  sortDirection: SortDirection;
  onToggleStream: (streamId: string, checked: boolean) => void;
  onSelectAll: () => void;
  onClear: () => void;
  onReload: () => void;
  onOpenFullscreen: () => void;
  onToggleSortDirection: () => void;
}) {
  return (
    <Panel
      title="Logs"
      icon={<Terminal size={16} aria-hidden />}
      action={
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onToggleSortDirection}
            className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-xs text-slate-800 transition hover:bg-slate-50"
            title={sortDirection === "desc" ? "Descending" : "Ascending"}
          >
            {sortDirection === "desc" ? <ArrowDown size={13} aria-hidden /> : <ArrowUp size={13} aria-hidden />}
            {sortDirection}
          </button>
          <button
            type="button"
            onClick={onOpenFullscreen}
            disabled={streams.length === 0}
            className="inline-flex h-8 items-center gap-2 rounded-md border border-slate-300 bg-white px-3 text-xs text-slate-800 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Maximize2 size={13} aria-hidden />
            Fullscreen
          </button>
          <button
            type="button"
            onClick={onReload}
            disabled={selectedStreams.length === 0}
            className="inline-flex h-8 items-center gap-2 rounded-md border border-slate-300 bg-white px-3 text-xs text-slate-800 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <RefreshCw size={13} aria-hidden />
            Tail
          </button>
        </div>
      }
    >
      <LogBrowser
        streams={streams}
        selectedStreams={selectedStreams}
        selectedStreamMetas={selectedStreamMetas}
        entries={entries}
        offsets={offsets}
        sortDirection={sortDirection}
        heightClass="h-[420px]"
        onToggleStream={onToggleStream}
        onSelectAll={onSelectAll}
        onClear={onClear}
      />
    </Panel>
  );
}

function MultiWorkspaceLogsFullscreen({
  workspaces,
  sortDirection,
  tailSignal,
  onTailAll,
  onToggleSortDirection,
  onRemoveWorkspace,
  onClose,
}: {
  workspaces: LogWorkspaceTarget[];
  sortDirection: SortDirection;
  tailSignal: number;
  onTailAll: () => void;
  onToggleSortDirection: () => void;
  onRemoveWorkspace: (workspaceId: string) => void;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const gridStyle = {
    gridTemplateColumns: `repeat(${workspaces.length}, minmax(280px, 1fr))`,
    minWidth: workspaces.length > 5 ? `${workspaces.length * 320}px` : "100%",
  };

  return (
    <div className="fixed inset-0 z-50 bg-slate-950/40 p-3 md:p-4">
      <section className="flex h-full min-h-0 flex-col overflow-hidden rounded-md border border-slate-300 bg-white shadow-2xl">
        <div className="flex min-h-12 flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              <Terminal size={16} aria-hidden />
              Logs
            </h2>
            <p className="text-[11px] text-slate-500">{workspaces.length} workspace columns</p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onToggleSortDirection}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-xs text-slate-800 transition hover:bg-slate-50"
              title={sortDirection === "desc" ? "Descending" : "Ascending"}
            >
              {sortDirection === "desc" ? <ArrowDown size={13} aria-hidden /> : <ArrowUp size={13} aria-hidden />}
              {sortDirection}
            </button>
            <button
              type="button"
              onClick={onTailAll}
              className="inline-flex h-8 items-center gap-2 rounded-md border border-slate-300 bg-white px-3 text-xs text-slate-800 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <RefreshCw size={13} aria-hidden />
              Tail all
            </button>
            <button
              type="button"
              onClick={onClose}
              className="inline-flex h-8 items-center gap-2 rounded-md border border-slate-300 bg-white px-3 text-xs text-slate-800 transition hover:bg-slate-50"
            >
              <X size={13} aria-hidden />
              Close
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-x-auto p-3">
          <div className="grid h-full gap-3" style={gridStyle}>
            {workspaces.map((workspace) => (
              <WorkspaceLogColumn
                key={workspace.workspace_id}
                workspace={workspace}
                sortDirection={sortDirection}
                tailSignal={tailSignal}
                onRemove={() => onRemoveWorkspace(workspace.workspace_id)}
              />
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}

function WorkspaceLogColumn({
  workspace,
  sortDirection,
  tailSignal,
  onRemove,
}: {
  workspace: LogWorkspaceTarget;
  sortDirection: SortDirection;
  tailSignal: number;
  onRemove: () => void;
}) {
  const [streams, setStreams] = useState<WorkspaceLogStream[]>([]);
  const [selectedStreams, setSelectedStreams] = useState<string[]>([]);
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [offsets, setOffsets] = useState<Record<string, number>>({});
  const [streamState, setStreamState] = useState<"idle" | "connecting" | "live" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const previousTailSignal = useRef(tailSignal);

  const selectedStreamMetas = useMemo(
    () => streams.filter((stream) => selectedStreams.includes(stream.stream_id)),
    [selectedStreams, streams],
  );
  const selectedEntries = useMemo(() => {
    const ordered = entries
      .filter((entry) => selectedStreams.includes(entry.streamId) && entry.data.length > 0)
      .sort(compareLogEntries);
    return sortDirection === "desc" ? ordered.reverse() : ordered;
  }, [entries, selectedStreams, sortDirection]);

  const loadSelectedTails = useCallback(async () => {
    const selected = streams.filter((stream) => selectedStreams.includes(stream.stream_id));
    if (selected.length === 0) {
      return;
    }
    const results = await Promise.all(
      selected.map((stream) => readLogTailEntry(workspace.workspace_id, stream)),
    );
    const byStream = new Map(results.map((result) => [result.entry.streamId, result]));
    setEntries((current) =>
      trimLogEntries([
        ...current.filter((entry) => {
          const result = byStream.get(entry.streamId);
          if (!result) {
            return true;
          }
          return entry.kind === "live" && entry.offset >= result.nextOffset;
        }),
        ...results.map((result) => result.entry),
      ]),
    );
    setOffsets((current) => {
      const next = { ...current };
      for (const result of results) {
        next[result.entry.streamId] = result.nextOffset;
      }
      return next;
    });
  }, [selectedStreams, streams, workspace.workspace_id]);

  const loadStreams = useCallback(async () => {
    setLoading(true);
    const result = await apiGet<ListEnvelope<WorkspaceLogStream>>(
      `/api/awf/workspaces/${workspace.workspace_id}/logs`,
    );
    if (!result.ok) {
      setError(result.message);
      setStreams([]);
      setSelectedStreams([]);
      setLoading(false);
      return;
    }
    setError(null);
    setStreams(result.data.items);
    setSelectedStreams((current) => pickWorkspaceLogStreams(result.data.items, current));
    setLoading(false);
  }, [workspace.workspace_id]);

  useEffect(() => {
    void loadStreams();
    const interval = window.setInterval(() => void loadStreams(), pollMs);
    return () => window.clearInterval(interval);
  }, [loadStreams]);

  useEffect(() => {
    if (!loading) {
      void loadSelectedTails();
    }
  }, [loadSelectedTails, loading]);

  useEffect(() => {
    if (previousTailSignal.current === tailSignal) {
      return;
    }
    previousTailSignal.current = tailSignal;
    void loadSelectedTails();
  }, [loadSelectedTails, tailSignal]);

  useEffect(() => {
    setStreamState("connecting");
    const source = new EventSource(
      `/api/awf/workspaces/${workspace.workspace_id}/stream?channels=events,agent,validation,services&tail_bytes=65536`,
    );
    let closedByServer = false;
    let terminalError = false;

    source.onmessage = (message) => {
      const frame = parseFrame(message.data);
      if (!frame) {
        return;
      }
      if (frame.type === "connected" || frame.type === "heartbeat" || frame.type === "snapshot" || frame.type === "event") {
        setStreamState("live");
        return;
      }
      if (frame.type === "log") {
        setStreamState("live");
        const entry: LogEntry = {
          key: `fullscreen:${frame.workspace_id}:${frame.stream_id}:${frame.offset}:${frame.next_offset ?? frame.offset}:${frame.seq}`,
          workspaceId: frame.workspace_id,
          streamId: frame.stream_id,
          source: frame.source,
          fd: frame.fd,
          offset: frame.offset,
          data: frame.data,
          occurredAt: frame.occurred_at ?? new Date().toISOString(),
          order: Date.parse(frame.occurred_at ?? "") || Date.now(),
          kind: frame.seq === 0 ? "tail" : "live",
        };
        setEntries((current) =>
          trimLogEntries([
            ...current.filter(
              (item) =>
                item.streamId !== frame.stream_id ||
                item.kind !== "tail" ||
                entry.kind !== "tail",
            ),
            entry,
          ]),
        );
        setOffsets((current) => ({
          ...current,
          [frame.stream_id]: Math.max(current[frame.stream_id] ?? 0, frame.next_offset ?? 0),
        }));
        return;
      }
      if (frame.type === "error") {
        terminalError = true;
        setStreamState("error");
        setError(frame.message);
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
  }, [workspace.workspace_id]);

  return (
    <section className="flex min-h-0 flex-col overflow-hidden rounded-md border border-slate-200 bg-white">
      <div className="flex min-h-14 items-start justify-between gap-2 border-b border-slate-100 px-3 py-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-slate-950">{workspace.title}</h3>
          <p className="mono truncate text-[11px] text-slate-500">{workspace.workspace_id}</p>
          <p className="truncate text-[11px] text-slate-500">
            {formatAgentLabel(workspace)} / {workspace.status} / stream {streamState}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {workspace.pr_url ? <SmallExternalAnchor href={workspace.pr_url} label="PR" /> : null}
          <button
            type="button"
            onClick={onRemove}
            className="inline-flex h-6 items-center rounded-md border border-slate-300 bg-white px-2 text-[11px] text-slate-700 transition hover:bg-slate-50"
          >
            <X size={11} aria-hidden />
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 p-2">
        {error ? (
          <div className="mb-2 rounded-md border border-red-200 bg-red-50 px-2 py-1.5 text-xs text-red-800">
            {error}
          </div>
        ) : null}
        <LogBrowser
          streams={streams}
          selectedStreams={selectedStreams}
          selectedStreamMetas={selectedStreamMetas}
          entries={selectedEntries}
          offsets={offsets}
          sortDirection={sortDirection}
          heightClass="h-full"
          onToggleStream={(streamId, checked) =>
            setSelectedStreams((current) => toggleStream(current, streamId, checked))
          }
          onSelectAll={() => setSelectedStreams(streams.map((stream) => stream.stream_id))}
          onClear={() => setSelectedStreams([])}
        />
      </div>
    </section>
  );
}

function LogBrowser({
  streams,
  selectedStreams,
  selectedStreamMetas,
  entries,
  offsets,
  sortDirection,
  heightClass,
  onToggleStream,
  onSelectAll,
  onClear,
}: {
  streams: WorkspaceLogStream[];
  selectedStreams: string[];
  selectedStreamMetas: WorkspaceLogStream[];
  entries: LogEntry[];
  offsets: Record<string, number>;
  sortDirection: SortDirection;
  heightClass: string;
  onToggleStream: (streamId: string, checked: boolean) => void;
  onSelectAll: () => void;
  onClear: () => void;
}) {
  const renderedLog = useMemo(() => renderLogEntries(entries, sortDirection), [entries, sortDirection]);
  const selectedBytes = selectedStreamMetas.reduce((total, stream) => total + stream.byte_count, 0);
  const selectedLines = selectedStreamMetas.reduce((total, stream) => total + stream.line_count, 0);

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-3">
      <div className="grid gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
          <span>
            {selectedStreams.length} selected / {bytes(selectedBytes)} / {selectedLines} lines
          </span>
          <span>
            offset{" "}
            {selectedStreamMetas.length === 1 ? offsets[selectedStreamMetas[0].stream_id] ?? "—" : "mixed"}
          </span>
        </div>
        <LogStreamPicker
          streams={streams}
          selectedStreams={selectedStreams}
          onToggleStream={onToggleStream}
          onSelectAll={onSelectAll}
          onClear={onClear}
        />
      </div>
      <LogOutput value={renderedLog} heightClass={heightClass} sortDirection={sortDirection} />
    </div>
  );
}

function LogStreamPicker({
  streams,
  selectedStreams,
  onToggleStream,
  onSelectAll,
  onClear,
}: {
  streams: WorkspaceLogStream[];
  selectedStreams: string[];
  onToggleStream: (streamId: string, checked: boolean) => void;
  onSelectAll: () => void;
  onClear: () => void;
}) {
  if (streams.length === 0) {
    return <MutedLine>No log streams recorded.</MutedLine>;
  }

  const selectedSet = new Set(selectedStreams);
  return (
    <div className="rounded-md border border-slate-200 bg-slate-50">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-2 py-1.5">
        <span className="text-[11px] font-medium text-slate-600">Streams</span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onSelectAll}
            className="h-6 rounded-md border border-slate-300 bg-white px-2 text-[11px] text-slate-700 hover:bg-slate-50"
          >
            All
          </button>
          <button
            type="button"
            onClick={onClear}
            className="h-6 rounded-md border border-slate-300 bg-white px-2 text-[11px] text-slate-700 hover:bg-slate-50"
          >
            Clear
          </button>
        </div>
      </div>
      <div className="grid max-h-32 gap-1 overflow-auto p-2 md:grid-cols-2">
        {streams.map((stream) => (
          <label
            key={stream.stream_id}
            className="flex min-w-0 items-start gap-2 rounded-md border border-slate-200 bg-white px-2 py-1.5 text-xs"
          >
            <input
              type="checkbox"
              checked={selectedSet.has(stream.stream_id)}
              onChange={(event) => onToggleStream(stream.stream_id, event.target.checked)}
              className="mt-0.5 h-3.5 w-3.5 shrink-0"
            />
            <span className="min-w-0">
              <span className="mono block truncate text-slate-950">{stream.stream_id}</span>
              <span className="block truncate text-[11px] text-slate-500">
                {bytes(stream.byte_count)} / {stream.line_count} lines
              </span>
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}

function LogOutput({
  value,
  heightClass,
  sortDirection,
}: {
  value: string;
  heightClass: string;
  sortDirection: SortDirection;
}) {
  const ref = useRef<HTMLPreElement | null>(null);

  useEffect(() => {
    const node = ref.current;
    if (node) {
      node.scrollTop = sortDirection === "desc" ? 0 : node.scrollHeight;
    }
  }, [sortDirection, value]);

  return (
    <pre
      ref={ref}
      className={`mono min-h-0 overflow-auto whitespace-pre-wrap rounded-md bg-[var(--terminal)] p-3 text-[11px] leading-relaxed break-words text-slate-100 ${heightClass}`}
    >
      {value || "No log data loaded."}
    </pre>
  );
}

function Panel({
  title,
  icon,
  action,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-md border border-[var(--border)] bg-white">
      <div className="flex min-h-11 items-center justify-between gap-3 border-b border-slate-100 px-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          {icon}
          {title}
        </h2>
        {action}
      </div>
      <div className="p-3">{children}</div>
    </section>
  );
}

function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0 rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
      <div className="text-[11px] font-medium text-slate-500">{label}</div>
      <div className={`${mono ? "mono" : ""} truncate text-sm text-slate-950`}>{value}</div>
    </div>
  );
}

function Badge({ value }: { value: string }) {
  return (
    <span
      className={`inline-flex h-6 shrink-0 items-center rounded-md border px-2 text-[11px] font-medium ${toneClass(
        statusTone(value),
      )}`}
    >
      {value}
    </span>
  );
}

function ExternalAnchor({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      onClick={(event) => {
        event.preventDefault();
        openExternalHref(href);
      }}
      className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 text-xs text-slate-800 transition hover:bg-slate-50"
    >
      {label}
      <ExternalLink size={13} aria-hidden />
    </a>
  );
}

function SmallExternalAnchor({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      onClick={(event) => {
        event.preventDefault();
        openExternalHref(href);
      }}
      className="inline-flex h-6 items-center gap-1 rounded-md border border-slate-300 bg-white px-2 text-[11px] text-slate-700 transition hover:bg-slate-50"
    >
      {label}
      <ExternalLink size={11} aria-hidden />
    </a>
  );
}

function openExternalHref(href: string) {
  const opened = window.open(href, "_blank", "noopener,noreferrer");
  if (!opened) {
    window.location.assign(href);
  }
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="m-4 flex items-start gap-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
      <XCircle className="mt-0.5 shrink-0" size={16} aria-hidden />
      <div>{message}</div>
    </div>
  );
}

function EmptyState({ apiState }: { apiState: "checking" | "ok" | "error" }) {
  return (
    <div className="grid min-h-[calc(100vh-58px)] place-items-center p-6 text-center">
      <div className="max-w-md rounded-md border border-slate-200 bg-white p-6">
        <Server className="mx-auto mb-3 text-slate-400" size={28} aria-hidden />
        <h2 className="text-base font-semibold">
          {apiState === "error" ? "AWF API unavailable" : "No workspace selected"}
        </h2>
        <p className="mt-2 text-sm text-slate-600">
          {apiState === "error"
            ? "Start the AWF API and confirm AWF_API_BASE_URL plus AWF_API_TOKEN in apps/console/.env.local."
            : "Create or select a workspace to inspect lifecycle, runtime, events, and logs."}
        </p>
      </div>
    </div>
  );
}

function MutedLine({ children }: { children: React.ReactNode }) {
  return <div className="rounded-md border border-dashed border-slate-200 p-4 text-sm text-slate-500">{children}</div>;
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-3 py-2 font-medium">{children}</th>;
}

function Td({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <td className={`px-3 py-2 align-top ${className}`}>{children}</td>;
}

function retryErrorMessage(result: Extract<ApiEnvelope<unknown>, { ok: false }>): string {
  const code = result.errorCode ? `${result.errorCode}: ` : "";
  return `${code}${result.message} Confirm the workspace is still failed or cancelled, resolve any reported lock conflict, then retry.`;
}

function formatScalar(value: number): string {
  if (!Number.isFinite(value)) {
    return "0";
  }
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 });
}

function formatGb(value: number): string {
  return `${formatScalar(value)} GB`;
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) {
    return "0%";
  }
  return `${value.toFixed(1)}%`;
}

async function apiGet<T>(path: string): Promise<ApiEnvelope<T>> {
  try {
    const response = await fetch(path, { cache: "no-store" });
    return await parseApiResponse<T>(response);
  } catch (error) {
    return {
      ok: false,
      status: 0,
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

async function apiPost<T>(path: string): Promise<ApiEnvelope<T>> {
  try {
    const response = await fetch(path, { method: "POST", cache: "no-store" });
    return await parseApiResponse<T>(response);
  } catch (error) {
    return {
      ok: false,
      status: 0,
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

async function parseApiResponse<T>(response: Response): Promise<ApiEnvelope<T>> {
  const text = await response.text();
  const parsed = parseJson(text);
  const body = parsed.ok ? parsed.value : null;
  if (!response.ok) {
    const errorBody = body as
      | { detail?: { error_code?: string; message?: string }; message?: string; error_code?: string }
      | null;
    return {
      ok: false,
      status: response.status,
      message:
        errorBody?.detail?.message ||
        errorBody?.message ||
        text ||
        `Request failed with HTTP ${response.status}.`,
      errorCode: errorBody?.detail?.error_code || errorBody?.error_code,
      detail: body ?? text,
    };
  }
  if (!parsed.ok) {
    return {
      ok: false,
      status: response.status,
      message: text,
      detail: text,
    };
  }
  return { ok: true, data: body as T };
}

type ParsedJson = { ok: true; value: unknown | null } | { ok: false };

function parseJson(text: string): ParsedJson {
  if (!text) {
    return { ok: true, value: null };
  }
  try {
    return { ok: true, value: JSON.parse(text) as unknown };
  } catch {
    return { ok: false };
  }
}

async function readLogTailEntry(
  workspaceId: string,
  stream: WorkspaceLogStream,
): Promise<{ entry: LogEntry; nextOffset: number }> {
  const offset = Math.max(stream.byte_count - 65_536, 0);
  const result = await apiGet<WorkspaceLogRead>(
    `/api/awf/workspaces/${workspaceId}/logs/${encodeURIComponent(
      stream.stream_id,
    )}?offset=${offset}&limit_bytes=65536`,
  );
  if (!result.ok) {
    const now = new Date().toISOString();
    return {
      entry: {
        key: `tail-error:${workspaceId}:${stream.stream_id}:${Date.now()}`,
        workspaceId,
        streamId: stream.stream_id,
        source: stream.source,
        fd: null,
        offset,
        data: `Unable to load log stream: ${result.message}`,
        occurredAt: now,
        order: Date.parse(now),
        kind: "tail",
      },
      nextOffset: offset,
    };
  }

  return {
    entry: {
      key: `tail:${workspaceId}:${stream.stream_id}:${result.data.offset}:${result.data.next_offset}`,
      workspaceId,
      streamId: stream.stream_id,
      source: stream.source,
      fd: null,
      offset: result.data.offset,
      data: result.data.data,
      occurredAt: stream.closed_at ?? stream.opened_at,
      order: Date.parse(stream.closed_at ?? stream.opened_at) || Date.now(),
      kind: "tail",
    },
    nextOffset: result.data.next_offset,
  };
}

function parseFrame(raw: string): AwfStreamFrame | null {
  try {
    return JSON.parse(raw) as AwfStreamFrame;
  } catch {
    return null;
  }
}

function mergeEvent(events: WorkspaceEvent[], event: WorkspaceEvent): WorkspaceEvent[] {
  if (events.some((item) => item.id === event.id)) {
    return events;
  }
  return [event, ...events].slice(0, 100);
}

function workspaceFilterSummary({
  agentFilter,
  repoFilter,
  searchText,
  sortDirection,
  sortKey,
  statusFilter,
}: {
  agentFilter: string;
  repoFilter: string;
  searchText: string;
  sortDirection: SortDirection;
  sortKey: WorkspaceSortKey;
  statusFilter: string;
}): string {
  const parts = [`${sortKey === "updated_at" ? "updated" : "created"} date ${sortDirection}`];
  if (statusFilter !== "all") {
    parts.push(`status ${statusFilter}`);
  }
  if (agentFilter !== "all") {
    parts.push(agentFilter);
  }
  if (searchText.trim()) {
    parts.push(`search "${searchText.trim()}"`);
  }
  if (repoFilter.trim()) {
    parts.push("repo filtered");
  }
  return parts.join(" · ");
}

function compareWorkspaceDates(
  left: WorkspaceOverview,
  right: WorkspaceOverview,
  sortKey: WorkspaceSortKey,
  direction: SortDirection,
): number {
  const leftTime = Date.parse(left[sortKey]);
  const rightTime = Date.parse(right[sortKey]);
  const safeLeft = Number.isNaN(leftTime) ? 0 : leftTime;
  const safeRight = Number.isNaN(rightTime) ? 0 : rightTime;
  const delta = safeLeft - safeRight;
  if (delta === 0) {
    return left.workspace_id.localeCompare(right.workspace_id);
  }
  return direction === "asc" ? delta : -delta;
}

function toLogWorkspaceTarget(workspaceId: string, overview: WorkspaceOverview[]): LogWorkspaceTarget {
  const workspace = overview.find((item) => item.workspace_id === workspaceId);
  if (workspace) {
    return workspace;
  }
  return {
    workspace_id: workspaceId,
    title: workspaceId,
    repo_url: "unknown",
    base_branch: "unknown",
    agent: "codex",
    agent_model: null,
    agent_effort: null,
    agent_model_source: "unavailable",
    agent_effort_source: "unavailable",
    status: "running",
    pr_url: null,
  };
}

function formatTokenCount(value: number | null): string {
  if (value === null || !Number.isFinite(value)) {
    return "—";
  }
  return new Intl.NumberFormat().format(value);
}

function formatCost(value: number | null, currency: string | null): string {
  if (value === null || !Number.isFinite(value) || !currency) {
    return "—";
  }
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    maximumFractionDigits: 4,
  }).format(value);
}

function toggleWorkspaceSelection(current: string[], workspaceId: string, checked: boolean): string[] {
  if (checked) {
    return current.includes(workspaceId) ? current : [...current, workspaceId];
  }
  return current.filter((item) => item !== workspaceId);
}

function toggleStream(current: string[], streamId: string, checked: boolean): string[] {
  if (checked) {
    return current.includes(streamId) ? current : [...current, streamId];
  }
  return current.filter((item) => item !== streamId);
}

function pickWorkspaceLogStreams(streams: WorkspaceLogStream[], current: string[]): string[] {
  const available = new Set(streams.map((stream) => stream.stream_id));
  const retained = current.filter((streamId) => available.has(streamId));
  if (retained.length > 0) {
    return retained;
  }
  const preferred =
    streams.find((stream) => stream.stream_id === "agent.stdout") ??
    streams.find((stream) => stream.byte_count > 0) ??
    streams[0];
  return preferred ? [preferred.stream_id] : [];
}

function compareLogEntries(left: LogEntry, right: LogEntry): number {
  const timeDelta = Date.parse(left.occurredAt) - Date.parse(right.occurredAt);
  if (timeDelta !== 0 && Number.isFinite(timeDelta)) {
    return timeDelta;
  }
  const orderDelta = left.order - right.order;
  if (orderDelta !== 0) {
    return orderDelta;
  }
  const streamDelta = left.streamId.localeCompare(right.streamId);
  if (streamDelta !== 0) {
    return streamDelta;
  }
  return left.offset - right.offset;
}

function trimLogEntries(entries: LogEntry[]): LogEntry[] {
  const kept: LogEntry[] = [];
  let keptChars = 0;
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    const remaining = maxLogChars - keptChars;
    if (remaining <= 0) {
      break;
    }
    if (entry.data.length <= remaining) {
      kept.push(entry);
      keptChars += entry.data.length;
    } else {
      kept.push({ ...entry, data: entry.data.slice(-remaining) });
      break;
    }
  }
  return kept.reverse();
}

function FailureAnalysisPanel({
  summary,
  status,
  error,
}: {
  summary: FailureSummaryResponse | null;
  status: "loading" | "success" | "error" | "unavailable";
  error: string | null;
}) {
  if (status === "unavailable") {
    return (
      <Panel title="Failure Analysis" icon={<AlertCircle size={16} aria-hidden />}>
        <MutedLine>Failure analysis is currently unavailable.</MutedLine>
      </Panel>
    );
  }

  const isLoading = status === "loading";
  const isError = status === "error";

  return (
    <Panel
      title="Failure Analysis"
      icon={<Activity size={16} aria-hidden />}
      action={
        <span className="rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] text-slate-600">
          {isLoading && !summary ? "loading" : isError && !summary ? "error" : `${summary?.total_failures ?? 0} failures in window`}
        </span>
      }
    >
      {isLoading && !summary ? (
        <MutedLine>Failure analysis loading.</MutedLine>
      ) : isError && !summary ? (
        <MutedLine>Unable to load failure analysis: {error}</MutedLine>
      ) : !summary ? (
        <MutedLine>No failure analysis data available.</MutedLine>
      ) : (
        <div className="grid gap-4">
          {error ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              Showing last snapshot. Refresh failed: {error}
            </div>
          ) : null}

          {summary.taxonomy && summary.taxonomy.length > 0 ? (
            <div className="grid gap-2 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-4">
              {summary.taxonomy.map((tax) => (
                <div key={tax.reason} className="min-w-0 rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
                  <div className="truncate text-[10px] font-medium text-slate-500" title={tax.reason}>{tax.reason}</div>
                  <div className="mono mt-1 text-lg font-semibold text-slate-900">{tax.count}</div>
                </div>
              ))}
            </div>
          ) : null}

          {summary.latest_examples && summary.latest_examples.length > 0 ? (
             <div className="grid gap-2">
               <h3 className="text-xs font-semibold text-slate-700">Latest Examples</h3>
               <div className="max-h-[320px] overflow-auto rounded-md border border-slate-200">
                 <table className="w-full min-w-[720px] text-left text-xs">
                   <thead className="sticky top-0 bg-slate-50 text-slate-600 shadow-[0_1px_0_var(--border)]">
                     <tr>
                       <Th>Workspace</Th>
                       <Th>Context</Th>
                       <Th>Reason</Th>
                       <Th>Message</Th>
                       <Th>Time</Th>
                     </tr>
                   </thead>
                   <tbody>
                     {summary.latest_examples.map((example) => (
                       <tr key={`${example.workspace_id}-${example.timestamp}`} className="border-t border-slate-100 bg-white">
                         <Td>
                           <div className="font-medium text-slate-950 truncate max-w-[200px]" title={example.title}>{example.title}</div>
                           <div className="mono text-[11px] text-slate-500 mt-0.5">{compactId(example.workspace_id, 10)}</div>
                         </Td>
                         <Td>
                            <div className="truncate max-w-[150px]" title={example.repo_url}>{example.repo_url}</div>
                            <div className="text-[11px] text-slate-500 mt-0.5 flex items-center gap-1">
                               <Bot size={10} aria-hidden />
                               {example.agent}
                            </div>
                         </Td>
                         <Td>
                           <span className="inline-flex rounded-md border border-red-200 bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-800">
                             {example.failure_reason}
                           </span>
                         </Td>
                         <Td>
                           <div className="truncate max-w-[300px] text-slate-600" title={example.failure_message}>
                             {example.failure_message}
                           </div>
                         </Td>
                         <Td>
                           <div className="flex items-center gap-2">
                             <span className="text-slate-500 whitespace-nowrap">{formatDateTime(example.timestamp)}</span>
                             {example.pr_url ? <SmallExternalAnchor href={example.pr_url} label="PR" /> : null}
                           </div>
                         </Td>
                       </tr>
                     ))}
                   </tbody>
                 </table>
               </div>
             </div>
          ) : null}
        </div>
      )}
    </Panel>
  );
}
