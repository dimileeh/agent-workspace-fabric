"use client";

import { useEffect, useLayoutEffect, useMemo, useRef } from "react";

import { bytes, renderLogEntries } from "@/lib/format";
import type { WorkspaceLogStream } from "@/lib/types";
import {
  type LogEntry,
  MutedLine,
  type SortDirection,
  isLogOutputAtTail,
  scrollLogOutputToTail,
} from "./console-dashboard-shared";

// useLayoutEffect warns during Next.js SSR ("does nothing on the server").
// The log-output effect must measure and adjust scroll position before paint
// to avoid tail-follow flicker, so we keep layout timing on the client and
// fall back to useEffect on the server where there is no layout to measure.
const useIsomorphicLayoutEffect =
  typeof window !== "undefined" ? useLayoutEffect : useEffect;

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
