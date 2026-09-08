"use client";

import { ArrowDown, ArrowUp, Maximize2, RefreshCw, Terminal } from "lucide-react";

import type { WorkspaceLogStream } from "@/lib/types";
import { LogBrowser } from "./console-dashboard-log-browser";
import { type LogEntry, Panel, type SortDirection } from "./console-dashboard-shared";

export { MultiWorkspaceLogsFullscreen, WorkspaceLogColumn } from "./console-dashboard-log-fullscreen";

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
