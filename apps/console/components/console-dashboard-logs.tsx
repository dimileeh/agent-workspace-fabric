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
import { awfPath } from "@/lib/console-urls";
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

// useLayoutEffect warns during Next.js SSR ("does nothing on the server").
// The log-output effect must measure and adjust scroll position before paint
// to avoid tail-follow flicker, so we keep layout timing on the client and
// fall back to useEffect on the server where there is no layout to measure.
const useIsomorphicLayoutEffect =
  typeof window !== "undefined" ? useLayoutEffect : useEffect;

export function LogsPanel({
  streams,
  selectedStreams,
  selectedStreamMetas,
  entries,
  offsets,
  sortDirection,
  tailSignal,
  refreshError = null,
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
  refreshError?: string | null;
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
      stale={Boolean(refreshError) && entries.length > 0}
      action={
        <div className="flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            onClick={onToggleSortDirection}
            className="inline-flex h-8 items-center gap-1.5 rounded-md border border-line bg-surface px-2.5 text-xs text-fg transition hover:bg-surface-2"
            title={sortDirection === "desc" ? "Descending" : "Ascending"}
          >
            {sortDirection === "desc" ? <ArrowDown size={13} aria-hidden /> : <ArrowUp size={13} aria-hidden />}
            {sortDirection}
          </button>
          <button
            type="button"
            onClick={onOpenFullscreen}
            disabled={streams.length === 0}
            className="inline-flex h-8 items-center gap-2 rounded-md border border-line bg-surface px-3 text-xs text-fg transition hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Maximize2 size={13} aria-hidden />
            Fullscreen
          </button>
          <button
            type="button"
            onClick={onReload}
            disabled={selectedStreams.length === 0}
            className="inline-flex h-8 items-center gap-2 rounded-md border border-line bg-surface px-3 text-xs text-fg transition hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <RefreshCw size={13} aria-hidden />
            Tail
          </button>
        </div>
      }
    >
      {refreshError ? (
        <div
          role="alert"
          className="mb-3 rounded-md border border-danger-border bg-danger-soft px-2 py-1.5 text-xs text-danger-text"
        >
          <span aria-hidden>⚠</span> {refreshError}
        </div>
      ) : null}
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
  allowLogs,
  allowStreamLogs,
  onTailAll,
  onToggleSortDirection,
  onRemoveWorkspace,
  onClose,
}: {
  workspaces: LogWorkspaceTarget[];
  sortDirection: SortDirection;
  tailSignal: number;
  allowLogs: boolean;
  /** Combined listing+stream gate; never pass bare workspace_stream. */
  allowStreamLogs: boolean;
  onTailAll: () => void;
  onToggleSortDirection: () => void;
  onRemoveWorkspace: (workspaceId: string) => void;
  onClose: () => void;
}) {
  const dialogTitleId = useId();
  const dialogDescriptionId = useId();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  // Keep the latest onClose without re-running the focus-trap effect (which
  // would steal focus back to the dialog on every parent re-render).
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const dialog = dialogRef.current;
    // Capture the trigger so focus can be restored when the dialog closes.
    const previouslyFocused = document.activeElement as HTMLElement | null;
    dialog?.focus();

    const focusableSelector =
      'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
    const getFocusable = () =>
      dialog ? Array.from(dialog.querySelectorAll<HTMLElement>(focusableSelector)) : [];

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab" || !dialog) {
        return;
      }
      const focusable = getFocusable();
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey) {
        if (active === first || active === dialog || !dialog.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last || active === dialog || !dialog.contains(active)) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      previouslyFocused?.focus?.();
    };
    // Mount once: focus capture/restoration and the listener must not reset on
    // every render. onClose is read live via onCloseRef.
  }, []);

  const gridStyle = {
    gridTemplateColumns: `repeat(${workspaces.length}, minmax(280px, 1fr))`,
    minWidth: workspaces.length > 5 ? `${workspaces.length * 320}px` : "100%",
  };

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={dialogTitleId}
      aria-describedby={dialogDescriptionId}
      tabIndex={-1}
      className="fixed inset-0 z-50 bg-slate-950/40 p-3 md:p-4 outline-none"
    >
      <section className="flex h-full min-h-0 flex-col overflow-hidden rounded-md border border-line bg-surface shadow-2xl">
        <div className="flex min-h-12 flex-wrap items-center justify-between gap-3 border-b border-line px-3">
          <div className="min-w-0">
            <h2 id={dialogTitleId} className="flex items-center gap-2 text-sm font-semibold">
              <Terminal size={16} aria-hidden />
              Logs
            </h2>
            <p id={dialogDescriptionId} className="text-[11px] text-fg-muted">
              {workspaces.length} workspace columns
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onToggleSortDirection}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-line bg-surface px-2.5 text-xs text-fg transition hover:bg-surface-2"
              title={sortDirection === "desc" ? "Descending" : "Ascending"}
            >
              {sortDirection === "desc" ? <ArrowDown size={13} aria-hidden /> : <ArrowUp size={13} aria-hidden />}
              {sortDirection}
            </button>
            <button
              type="button"
              onClick={onTailAll}
              className="inline-flex h-8 items-center gap-2 rounded-md border border-line bg-surface px-3 text-xs text-fg transition hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <RefreshCw size={13} aria-hidden />
              Tail all
            </button>
            <button
              type="button"
              onClick={onClose}
              className="inline-flex h-8 items-center gap-2 rounded-md border border-line bg-surface px-3 text-xs text-fg transition hover:bg-surface-2"
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
                allowLogs={allowLogs}
                allowStreamLogs={allowStreamLogs}
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
  allowLogs,
  allowStreamLogs,
  onRemove,
}: {
  workspace: LogWorkspaceTarget;
  sortDirection: SortDirection;
  tailSignal: number;
  allowLogs: boolean;
  /** Combined listing+stream gate; never pass bare workspace_stream. */
  allowStreamLogs: boolean;
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
  // Listing poll generation. A wall-clock interval can start poll N+1 before
  // poll N returns. Discarding every non-latest 401/403 starves the column
  // when each denial is slower than pollMs: cached private tails and the
  // EventSource stay open. Ordinary overlapping successes still apply so the
  // first listing snapshot can land and activity can observe the next metadata
  // change. A 401/403 is authoritative unless a strictly newer poll has
  // already applied a success. Denial records a revoke watermark covering
  // every poll that has already started so an older or queued 200 cannot
  // restore cleared caches. A poll that starts after that watermark may recover.
  const listingGenerationRef = useRef(0);
  // Highest listing generation that applied a successful 200. An older 401/403
  // must not clear caches that this newer success already owns.
  const appliedListingGenerationRef = useRef(0);
  // Highest listing generation covered by an applied 401/403. An older
  // overlapping 200 (started before that denial) must not restore cleared
  // caches, even if it later observes a matching epoch. A later poll has a
  // higher generation and may recover if authorization returns. Re-applying a
  // denial already inside this window must not raise the watermark.
  const revokedListingGenerationRef = useRef(0);
  // Bumped on authorization denial so in-flight listing/tail reads cannot write
  // back previously authorized entries after the column caches are cleared.
  const columnEpochRef = useRef(0);
  const listingDeniedRef = useRef(false);
  const eventSourceRef = useRef<EventSource | null>(null);
  const [listingDenied, setListingDenied] = useState(false);

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
    if (!allowLogs) {
      return;
    }
    const selected = streams.filter((stream) => selectedStreams.includes(stream.stream_id));
    if (selected.length === 0) {
      return;
    }
    const epoch = columnEpochRef.current;
    let results: Awaited<ReturnType<typeof readLogTailEntry>>[];
    try {
      results = await Promise.all(
        selected.map((stream) =>
          readLogTailEntry(
            workspace.workspace_id,
            stream,
            logStreamActivityFor(streamActivityRef.current, workspace.workspace_id, stream),
          ),
        ),
      );
    } catch (cause) {
      if (epoch !== columnEpochRef.current || listingDeniedRef.current) {
        return;
      }
      setError(cause instanceof Error ? cause.message : "Unable to refresh log tails.");
      return;
    }
    if (epoch !== columnEpochRef.current || listingDeniedRef.current) {
      return;
    }
    setError(null);
    const byStream = new Map(results.map((result) => [result.entry.streamId, result]));
    setEntries((current) => {
      // Functional updaters can flush after a denial clear; drop the write so
      // previously authorized tails cannot reappear.
      if (epoch !== columnEpochRef.current || listingDeniedRef.current) {
        return current;
      }
      return trimLogEntries([
        ...current.filter((entry) => {
          const result = byStream.get(entry.streamId);
          if (!result) {
            return true;
          }
          return entry.kind === "live" && entry.offset >= result.nextOffset;
        }),
        ...results.map((result) => result.entry),
      ], selectedStreams);
    });
    setOffsets((current) => {
      if (epoch !== columnEpochRef.current || listingDeniedRef.current) {
        return current;
      }
      const next = { ...current };
      for (const result of results) {
        next[result.entry.streamId] = result.nextOffset;
      }
      return next;
    });
  }, [allowLogs, selectedStreams, streams, workspace.workspace_id]);

  useEffect(() => {
    selectedStreamsRef.current = selectedStreams;
  }, [selectedStreams]);

  const loadStreams = useCallback(async () => {
    if (!allowLogs) {
      setStreams([]);
      setSelectedStreams([]);
      return;
    }
    const epoch = columnEpochRef.current;
    const generation = ++listingGenerationRef.current;
    const result = await apiGet<ListEnvelope<WorkspaceLogStream>>(
      awfPath(`workspaces/${workspace.workspace_id}/logs`),
    );
    const applyAuthoritativeListingDenial = (deniedGeneration: number, message: string) => {
      // A newer listing success already owns the column. A late 401/403 from
      // an older poll must not clear it.
      if (deniedGeneration < appliedListingGenerationRef.current) {
        return;
      }
      // This poll started inside an already-applied denial window. Raising the
      // watermark here would reject a recovery poll that started after the
      // original denial.
      if (
        listingDeniedRef.current &&
        deniedGeneration <= revokedListingGenerationRef.current
      ) {
        return;
      }
      setError(message);
      columnEpochRef.current += 1;
      revokedListingGenerationRef.current = Math.max(
        revokedListingGenerationRef.current,
        listingGenerationRef.current,
      );
      listingDeniedRef.current = true;
      selectedStreamsRef.current = [];
      streamActivityRef.current = {};
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      setStreams([]);
      setSelectedStreams([]);
      setEntries([]);
      setOffsets({});
      setStreamState("idle");
      setListingDenied(true);
    };

    if (epoch !== columnEpochRef.current) {
      return;
    }
    if (!result.ok) {
      // Feed-level 401/403 is auth revocation for this column, not a transient
      // outage: drop last-good streams/entries and close the live EventSource
      // even while workspace_logs remains advertised (CONSOLE_BACKEND_CONTRACT).
      // Apply it even if a newer poll has started but not yet applied a success.
      // Waiting for that newer poll starves the column when every denial is
      // slower than pollMs (generation !== listingGenerationRef.current forever).
      if (result.status === 401 || result.status === 403) {
        applyAuthoritativeListingDenial(generation, result.message);
        return;
      }
      // Ignore a superseded non-auth failure; a newer poll owns the column.
      if (generation !== listingGenerationRef.current) {
        return;
      }
      setError(result.message);
      return;
    }
    // Older overlapping listing 200: this poll started before the denial that
    // cleared the column. Do not restore streams/entries or reopen the stream.
    if (generation <= revokedListingGenerationRef.current) {
      return;
    }
    listingDeniedRef.current = false;
    appliedListingGenerationRef.current = Math.max(
      appliedListingGenerationRef.current,
      generation,
    );
    setListingDenied(false);
    setError(null);
    streamActivityRef.current = updateLogStreamActivity(
      streamActivityRef.current,
      workspace.workspace_id,
      result.data.items,
    );
    setStreams(result.data.items);
    setSelectedStreams((current) => pickWorkspaceLogStreams(result.data.items, current));
  }, [allowLogs, workspace.workspace_id]);

  useEffect(() => {
    void loadStreams();
    if (!allowLogs) {
      return;
    }
    const interval = window.setInterval(() => void loadStreams(), pollMs);
    return () => window.clearInterval(interval);
  }, [allowLogs, loadStreams]);

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
    // Listing is required to pick/surface streams; do not open /stream or buffer
    // frames when workspace_logs is unsupported (even if workspace_stream is up).
    if (!allowStreamLogs || listingDenied) {
      setStreamState("idle");
      return;
    }
    setStreamState("connecting");
    const source = new EventSource(
      awfPath(`workspaces/${workspace.workspace_id}/stream`, {
        channels: "events,agent,validation,services",
        tail_bytes: 65536,
      }),
    );
    eventSourceRef.current = source;
    const openedEpoch = columnEpochRef.current;
    let closedByServer = false;
    let terminalError = false;

    source.onmessage = (message) => {
      if (listingDeniedRef.current || openedEpoch !== columnEpochRef.current) {
        return;
      }
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
        setEntries((current) => {
          if (listingDeniedRef.current || openedEpoch !== columnEpochRef.current) {
            return current;
          }
          return trimLogEntries(
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
          );
        });
        setOffsets((current) => {
          if (listingDeniedRef.current || openedEpoch !== columnEpochRef.current) {
            return current;
          }
          return {
            ...current,
            [frame.stream_id]: Math.max(
              current[frame.stream_id] ?? 0,
              frame.next_offset ?? frame.offset,
            ),
          };
        });
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
      // close() from an authorization denial fires error; do not flip the
      // cleared column back to connecting or surface a live stream.
      if (listingDeniedRef.current || openedEpoch !== columnEpochRef.current) {
        setStreamState("idle");
        return;
      }
      if (terminalError) {
        setStreamState("error");
        return;
      }
      setStreamState(closedByServer || source.readyState === EventSource.CLOSED ? "idle" : "connecting");
    };

    return () => {
      source.close();
      if (eventSourceRef.current === source) {
        eventSourceRef.current = null;
      }
    };
  }, [allowStreamLogs, listingDenied, workspace.workspace_id]);

  return (
    <section className="flex min-h-0 flex-col overflow-hidden rounded-md border border-line bg-surface">
      <div className="flex min-h-14 items-start justify-between gap-2 border-b border-line px-3 py-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-fg-strong">{workspace.title}</h3>
          <p className="mono truncate text-[11px] text-fg-muted">{workspace.workspace_id}</p>
          <p className="truncate text-[11px] text-fg-muted">
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
            className="inline-flex h-6 items-center rounded-md border border-line bg-surface px-2 text-[11px] text-fg transition hover:bg-surface-2"
          >
            <X size={11} aria-hidden />
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 p-2">
        {error ? (
          <div className="mb-2 rounded-md border border-danger-border bg-danger-soft px-2 py-1.5 text-xs text-danger-text">
            {error}
          </div>
        ) : null}
        {!allowLogs ? (
          <MutedLine>Workspace log listing is unavailable.</MutedLine>
        ) : (
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
        )}
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
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-fg-muted">
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
    <div className="rounded-md border border-line bg-surface-2">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-2 py-1.5">
        <span className="text-[11px] font-medium text-fg-muted">Streams</span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onSelectAll}
            className="h-6 rounded-md border border-line bg-surface px-2 text-[11px] text-fg hover:bg-surface-2"
          >
            All
          </button>
          <button
            type="button"
            onClick={onClear}
            className="h-6 rounded-md border border-line bg-surface px-2 text-[11px] text-fg hover:bg-surface-2"
          >
            Clear
          </button>
        </div>
      </div>
      <div className="grid max-h-32 gap-1 overflow-auto p-2 md:grid-cols-2">
        {streams.map((stream) => (
          <label
            key={stream.stream_id}
            className="flex min-w-0 items-start gap-2 rounded-md border border-line bg-surface px-2 py-1.5 text-xs"
          >
            <input
              type="checkbox"
              checked={selectedSet.has(stream.stream_id)}
              onChange={(event) => onToggleStream(stream.stream_id, event.target.checked)}
              className="mt-0.5 h-3.5 w-3.5 shrink-0"
            />
            <span className="min-w-0">
              <span className="mono block truncate text-fg-strong">{stream.stream_id}</span>
              <span className="block truncate text-[11px] text-fg-muted">
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

  useIsomorphicLayoutEffect(() => {
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
      className={`mono min-h-0 overflow-auto whitespace-pre-wrap rounded-md bg-[var(--terminal)] p-3 text-[11px] leading-relaxed break-words text-[var(--terminal-foreground)] ${heightClass}`}
    >
      {value || "No log data loaded."}
    </pre>
  );
}
