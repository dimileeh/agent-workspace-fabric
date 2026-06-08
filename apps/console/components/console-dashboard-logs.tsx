"use client";

import {
ArrowDown,
ArrowUp,
Maximize2,
RefreshCw,
Terminal,
X
} from "lucide-react";
import {
useCallback,
useEffect,
useId,
useLayoutEffect,
useMemo,
useRef,
useState
} from "react";

import { formatAgentLabel } from "@/lib/agent-format";
import {
bytes,
pickWorkspaceLogStreams,
renderLogEntries
} from "@/lib/format";
import type {
ListEnvelope,
WorkspaceLogStream
} from "@/lib/types";
import {
LogEntry,
LogStreamActivityMap,
LogWorkspaceTarget,
MutedLine,
Panel,
SmallExternalAnchor,
SortDirection,
apiGet,
compareLogEntries,
formatPrLinkLabel,
isLogOutputAtTail,
logStreamActivityFor,
parseFrame,
pollMs,
readLogTailEntry,
scrollLogOutputToTail,
toggleStream,
trimLogEntries,
updateLogStreamActivity
} from "./console-dashboard-shared";

export function LogsPanel({
  streams,
  selectedStreams,
  selectedStreamMetas,
  entries,
  offsets,
  sortDirection,
  tailSignal,
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
  tailSignal: number;
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
        <div className="flex flex-wrap items-center justify-end gap-2">
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
        tailSignal={tailSignal}
        heightClass="h-[420px]"
        onToggleStream={onToggleStream}
        onSelectAll={onSelectAll}
        onClear={onClear}
      />
    </Panel>
  );
}

export function MultiWorkspaceLogsFullscreen({
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
  const dialogTitleId = useId();
  const dialogDescriptionId = useId();

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
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={dialogTitleId}
      aria-describedby={dialogDescriptionId}
      className="fixed inset-0 z-50 bg-slate-950/40 p-3 md:p-4"
    >
      <section className="flex h-full min-h-0 flex-col overflow-hidden rounded-md border border-slate-300 bg-white shadow-2xl">
        <div className="flex min-h-12 flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-3">
          <div className="min-w-0">
            <h2 id={dialogTitleId} className="flex items-center gap-2 text-sm font-semibold">
              <Terminal size={16} aria-hidden />
              Logs
            </h2>
            <p id={dialogDescriptionId} className="text-[11px] text-slate-500">
              {workspaces.length} workspace columns
            </p>
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

export function WorkspaceLogColumn({
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
  const previousTailSignal = useRef(tailSignal);
  const streamActivityRef = useRef<LogStreamActivityMap>({});
  const selectedStreamsRef = useRef<string[]>([]);
  const previousTailRefreshKey = useRef("");

  const selectedStreamMetas = useMemo(
    () => streams.filter((stream) => selectedStreams.includes(stream.stream_id)),
    [selectedStreams, streams],
  );
  const selectedTailRefreshKey = useMemo(
    () =>
      selectedStreamMetas
        .map((stream) =>
          [
            stream.stream_id,
            stream.byte_count,
            stream.line_count,
            stream.opened_at,
            stream.closed_at ?? "",
          ].join(":"),
        )
        .join("|"),
    [selectedStreamMetas],
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
      selected.map((stream) =>
        readLogTailEntry(
          workspace.workspace_id,
          stream,
          logStreamActivityFor(streamActivityRef.current, workspace.workspace_id, stream),
        ),
      ),
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
      ], selectedStreams),
    );
    setOffsets((current) => {
      const next = { ...current };
      for (const result of results) {
        next[result.entry.streamId] = result.nextOffset;
      }
      return next;
    });
  }, [selectedStreams, streams, workspace.workspace_id]);

  useEffect(() => {
    selectedStreamsRef.current = selectedStreams;
  }, [selectedStreams]);

  const loadStreams = useCallback(async () => {
    const result = await apiGet<ListEnvelope<WorkspaceLogStream>>(
      `/api/awf/workspaces/${workspace.workspace_id}/logs`,
    );
    if (!result.ok) {
      setError(result.message);
      return;
    }
    setError(null);
    streamActivityRef.current = updateLogStreamActivity(
      streamActivityRef.current,
      workspace.workspace_id,
      result.data.items,
    );
    setStreams(result.data.items);
    setSelectedStreams((current) => pickWorkspaceLogStreams(result.data.items, current));
  }, [workspace.workspace_id]);

  useEffect(() => {
    void loadStreams();
    const interval = window.setInterval(() => void loadStreams(), pollMs);
    return () => window.clearInterval(interval);
  }, [loadStreams]);

  useEffect(() => {
    if (!selectedTailRefreshKey) {
      previousTailRefreshKey.current = "";
      return;
    }
    if (previousTailRefreshKey.current === selectedTailRefreshKey) {
      return;
    }
    previousTailRefreshKey.current = selectedTailRefreshKey;
    void loadSelectedTails();
  }, [loadSelectedTails, selectedTailRefreshKey]);

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
          trimLogEntries(
            [
            ...current.filter(
              (item) =>
                item.streamId !== frame.stream_id ||
                item.kind !== "tail" ||
                entry.kind !== "tail",
            ),
            entry,
            ],
            selectedStreamsRef.current,
          ),
        );
        setOffsets((current) => ({
          ...current,
          [frame.stream_id]: Math.max(
            current[frame.stream_id] ?? 0,
            frame.next_offset ?? frame.offset,
          ),
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
          {workspace.pr_url ? (
            <SmallExternalAnchor href={workspace.pr_url} label={formatPrLinkLabel(workspace.pr_url, workspace.pr_number)} />
          ) : null}
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
          tailSignal={tailSignal}
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

export function LogBrowser({
  streams,
  selectedStreams,
  selectedStreamMetas,
  entries,
  offsets,
  sortDirection,
  tailSignal,
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
  tailSignal: number;
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
      <LogOutput value={renderedLog} heightClass={heightClass} sortDirection={sortDirection} tailSignal={tailSignal} />
    </div>
  );
}

export function LogStreamPicker({
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

export function LogOutput({
  value,
  heightClass,
  sortDirection,
  tailSignal,
}: {
  value: string;
  heightClass: string;
  sortDirection: SortDirection;
  tailSignal: number;
}) {
  const ref = useRef<HTMLPreElement | null>(null);
  const preserveRef = useRef({ scrollHeight: 0, scrollTop: 0 });
  const followTailRef = useRef(true);
  const previousSortRef = useRef(sortDirection);
  const previousTailSignalRef = useRef(tailSignal);

  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) {
      return;
    }

    const previous = preserveRef.current;
    const sortChanged = previousSortRef.current !== sortDirection;
    const tailRequested = previousTailSignalRef.current !== tailSignal;

    if (sortChanged || tailRequested || followTailRef.current) {
      scrollLogOutputToTail(node, sortDirection);
    } else if (sortDirection === "desc") {
      const heightDelta = node.scrollHeight - previous.scrollHeight;
      node.scrollTop = previous.scrollTop + Math.max(heightDelta, 0);
    } else {
      node.scrollTop = previous.scrollTop;
    }

    followTailRef.current = isLogOutputAtTail(node, sortDirection);
    preserveRef.current = {
      scrollHeight: node.scrollHeight,
      scrollTop: node.scrollTop,
    };
    previousSortRef.current = sortDirection;
    previousTailSignalRef.current = tailSignal;
  }, [sortDirection, tailSignal, value]);

  return (
    <pre
      ref={ref}
      data-testid="log-output"
      onScroll={(event) => {
        const node = event.currentTarget;
        followTailRef.current = isLogOutputAtTail(node, sortDirection);
        preserveRef.current = {
          scrollHeight: node.scrollHeight,
          scrollTop: node.scrollTop,
        };
      }}
      className={`mono min-h-0 overflow-auto whitespace-pre-wrap rounded-md bg-[var(--terminal)] p-3 text-[11px] leading-relaxed break-words text-slate-100 ${heightClass}`}
    >
      {value || "No log data loaded."}
    </pre>
  );
}
