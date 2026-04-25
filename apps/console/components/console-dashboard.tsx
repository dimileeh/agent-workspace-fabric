"use client";

import {
  Activity,
  AlertCircle,
  ArrowDown,
  ArrowUp,
  Bot,
  Boxes,
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
  compactId,
  formatDateTime,
  lifecycleStages,
  relativeTime,
  statusTone,
  toneClass,
} from "@/lib/format";
import type {
  ApiEnvelope,
  AwfStreamFrame,
  ListEnvelope,
  Operation,
  RuntimeService,
  Workspace,
  WorkspaceEvent,
  WorkspaceLogRead,
  WorkspaceLogStream,
  WorkspaceOverview,
  WorkspaceRuntime,
  WorkspaceStatus,
} from "@/lib/types";

const pollMs = Number(process.env.NEXT_PUBLIC_AWF_CONSOLE_POLL_MS || "5000");
const maxLogChars = 180_000;

type WorkspaceSortKey = "created_at" | "updated_at";
type SortDirection = "asc" | "desc";

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
  "workspace_id" | "title" | "repo_url" | "base_branch" | "agent" | "status" | "pr_url"
>;

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
  const [sortKey, setSortKey] = useState<WorkspaceSortKey>("created_at");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
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
    setOverview(result.data.items);
    setLastRefresh(new Date());
    setSelectedId((current) =>
      current && result.data.items.some((item) => item.workspace_id === current)
        ? current
        : result.data.items[0]?.workspace_id ?? null,
    );
  }, [overviewPath]);

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
      workspace: workspace.ok ? workspace.data : null,
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
    setDetail(emptyDetail);
    setSelectedStreams([]);
    setLogEntries([]);
    setStreamOffsets({});
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
        setDetail((current) => ({ ...current, workspace: frame.workspace }));
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
        setStreamState("error");
        setError(frame.message);
      }
    };

    source.onerror = () => {
      setStreamState("error");
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
            item.status,
          ]
            .join(" ")
            .toLowerCase()
            .includes(needle),
        )
      : overview;
    return [...filtered].sort((left, right) => compareWorkspaceDates(left, right, sortKey, sortDirection));
  }, [overview, searchText, sortDirection, sortKey]);

  const selectedOverview = overview.find((item) => item.workspace_id === selectedId) ?? null;
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
        onRefresh={() => startTransition(() => void loadOverview())}
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
          {selectedId && selectedOverview ? (
            <div className="grid gap-4 p-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(420px,0.9fr)]">
              <div className="grid min-w-0 gap-4">
                <WorkspaceSummary overview={selectedOverview} workspace={detail.workspace} />
                <LifecycleRail status={selectedOverview.status} />
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
}) {
  return (
    <div className="grid gap-3 border-b border-[var(--border)] p-3">
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
            <option value="created_at">created date</option>
            <option value="updated_at">updated date</option>
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
      {items.map((item) => (
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
                  <span>{item.agent}</span>
                  <span className="text-slate-300">/</span>
                  <span className="truncate">{item.base_branch}</span>
                </div>
                <div className="truncate text-xs text-[var(--muted)]">{item.repo_url}</div>
              </button>
            </div>
            <div className="flex shrink-0 flex-col items-end gap-1">
              <Badge value={item.status} />
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
      ))}
    </div>
  );
}

function WorkspaceSummary({
  overview,
  workspace,
}: {
  overview: WorkspaceOverview;
  workspace: Workspace | null;
}) {
  return (
    <Panel
      title="Workspace"
      icon={<Activity size={16} aria-hidden />}
      action={overview.pr_url ? <ExternalAnchor href={overview.pr_url} label="PR" /> : null}
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
          <Fact label="Agent" value={overview.agent} />
          <Fact label="Branch" value={workspace?.branch_name ?? overview.branch_name ?? "—"} mono />
          <Fact label="Base" value={overview.base_branch} mono />
          <Fact label="Phase" value={overview.current_phase} />
          <Fact label="Operation" value={overview.active_operation ?? "none"} />
          <Fact label="Updated" value={formatDateTime(overview.updated_at)} />
        </div>
        {overview.failure_reason || overview.failure_message ? (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
            <div className="font-semibold">{overview.failure_reason ?? "failure"}</div>
            <div className="mt-1 text-red-800">{overview.failure_message ?? "No details."}</div>
          </div>
        ) : null}
      </div>
    </Panel>
  );
}

function LifecycleRail({ status }: { status: WorkspaceStatus }) {
  const terminal = status === "failed" || status === "cancelled";
  const activeIndex = lifecycleStages.indexOf(status);
  return (
    <Panel title="Lifecycle" icon={<GitPullRequest size={16} aria-hidden />}>
      <div className="grid gap-2 md:grid-cols-4 xl:grid-cols-8">
        {lifecycleStages.map((stage, index) => {
          const reached = !terminal && activeIndex >= index;
          const active = status === stage;
          return (
            <div
              key={stage}
              className={`min-h-16 rounded-md border p-2 ${
                active
                  ? "border-blue-300 bg-blue-50"
                  : reached
                    ? "border-emerald-200 bg-emerald-50"
                    : "border-slate-200 bg-white"
              }`}
            >
              <div className="flex items-center gap-1.5">
                {reached ? (
                  <CheckCircle2 size={14} className="text-emerald-700" aria-hidden />
                ) : active ? (
                  <CircleDot size={14} className="text-blue-700" aria-hidden />
                ) : (
                  <Clock3 size={14} className="text-slate-400" aria-hidden />
                )}
                <span className="text-xs font-medium">{stage}</span>
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
          {sortedEvents.map((event) => (
            <div key={event.id} className="grid gap-1 border-b border-slate-100 py-2 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium">{event.event_type}</span>
                <span className="text-slate-500">{formatDateTime(event.occurred_at)}</span>
              </div>
              <div className="flex flex-wrap gap-2 text-slate-600">
                <span>{event.old_state ?? "—"} → {event.new_state ?? "—"}</span>
                {event.reason_code ? <span className="mono">{event.reason_code}</span> : null}
              </div>
            </div>
          ))}
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
        setStreamState("error");
        setError(frame.message);
      }
    };

    source.onerror = () => {
      setStreamState("error");
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
            {workspace.agent} / {workspace.status} / stream {streamState}
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
  const renderedLog = useMemo(() => renderLogEntries(entries), [entries]);
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

async function apiGet<T>(path: string): Promise<ApiEnvelope<T>> {
  try {
    const response = await fetch(path, { cache: "no-store" });
    const text = await response.text();
    const body = parseJsonOrNull(text);
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
    if (body === null && text) {
      return {
        ok: false,
        status: response.status,
        message: text,
        detail: text,
      };
    }
    return { ok: true, data: body as T };
  } catch (error) {
    return {
      ok: false,
      status: 0,
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

function parseJsonOrNull(text: string): unknown | null {
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
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

function parseFrame(raw: string): AwfStreamFrame | ({ type: "connected"; workspace_id: string } & Record<string, unknown>) | null {
  try {
    return JSON.parse(raw) as AwfStreamFrame | ({ type: "connected"; workspace_id: string } & Record<string, unknown>);
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
    status: "running",
    pr_url: null,
  };
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

function renderLogEntries(entries: LogEntry[]): string {
  return entries.map(renderLogEntry).join("\n\n");
}

function renderLogEntry(entry: LogEntry): string {
  const stamp = formatLogStamp(entry.occurredAt);
  const stream = entry.fd ? `${entry.streamId} ${entry.fd}` : entry.streamId;
  const header = `[${stamp}] ${stream}`;
  const data = entry.data.endsWith("\n") ? entry.data.slice(0, -1) : entry.data;
  return data ? `${header}\n${data}` : header;
}

function formatLogStamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}
