"use client";

import { ArrowDown, ArrowUp, RefreshCw, Terminal, X } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { formatAgentLabel } from "@/lib/agent-format";
import { resolveWorkspaceStreamSubscription } from "@/lib/console-capabilities";
import { awfPath } from "@/lib/console-urls";
import { pickWorkspaceLogStreams } from "@/lib/format";
import type { ConsoleCapabilities, ListEnvelope, WorkspaceLogStream } from "@/lib/types";
import { LogBrowser } from "./console-dashboard-log-browser";
import {
  type LogEntry,
  type LogStreamActivityMap,
  type LogTailReadResult,
  type LogWorkspaceTarget,
  MutedLine,
  SmallExternalAnchor,
  type SortDirection,
  apiGet,
  compareLogEntries,
  formatPrLinkLabel,
  logStreamActivityFor,
  parseFrame,
  pollMs,
  readLogTailEntry,
  streamAuthProbeDelayMs,
  toggleStream,
  trimLogEntries,
  updateLogStreamActivity,
} from "./console-dashboard-shared";

function fullscreenTailRefreshMessage(message: string | null): string {
  return `Unable to load log stream: ${message ?? "Unable to refresh log tails."}`;
}

function isFullscreenTailAuthFailure(status: number): boolean {
  return status === 401 || status === 403;
}

function isFullscreenStreamAuthDenied(frame: { status?: number }): boolean {
  return frame.status === 401 || frame.status === 403;
}

function formatFullscreenTailRefreshError(
  errors: Record<string, string>,
  selectedStreamIds: readonly string[],
): string | null {
  const messages = [
    ...new Set(
      selectedStreamIds
        .map((streamId) => errors[streamId])
        .filter((message): message is string => Boolean(message)),
    ),
  ];
  return messages.length > 0 ? messages.join("; ") : null;
}

export function MultiWorkspaceLogsFullscreen({
  workspaces,
  sortDirection,
  tailSignal,
  allowLogs,
  allowStreamLogs,
  capabilities,
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
  capabilities: ConsoleCapabilities | null;
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
                capabilities={capabilities}
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
  capabilities,
  onRemove,
}: {
  workspace: LogWorkspaceTarget;
  sortDirection: SortDirection;
  tailSignal: number;
  allowLogs: boolean;
  /** Combined listing+stream gate; never pass bare workspace_stream. */
  allowStreamLogs: boolean;
  capabilities: ConsoleCapabilities | null;
  onRemove: () => void;
}) {
  const [streams, setStreams] = useState<WorkspaceLogStream[]>([]);
  const [selectedStreams, setSelectedStreams] = useState<string[]>([]);
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [offsets, setOffsets] = useState<Record<string, number>>({});
  const [streamState, setStreamState] = useState<"idle" | "connecting" | "live" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  // Per-stream tail refresh warnings. A network/5xx read must not replace the
  // last successful snapshot; listing 200 must not clear these either.
  const [tailRefreshErrors, setTailRefreshErrors] = useState<Record<string, string>>({});
  const previousTailSignal = useRef(tailSignal);
  const streamActivityRef = useRef<LogStreamActivityMap>({});
  const selectedStreamsRef = useRef<string[]>([]);
  const previousTailRefreshKey = useRef("");
  // Outstanding loadSelectedTails calls. A static listing refresh retries
  // denied streams, but must not start another reload while one is in flight:
  // a newer generation would discard the slower 200 that clears the latch.
  const tailReloadInFlightCountRef = useRef(0);
  // Listing poll generation. A wall-clock interval can start poll N+1 before
  // poll N returns. Discarding every non-latest 401/403 or non-auth failure
  // starves the column when each response is slower than pollMs: cached
  // private tails and the EventSource stay open, or a continuously failing
  // listing never surfaces an error. The first completed 200 still lands even
  // if a newer poll has started; a later 200 may replace it so activity can
  // observe the next metadata change. A 200 older than the last applied
  // success is discarded so a slow poll cannot restore a stale inventory. A
  // 401/403 is authoritative unless a strictly newer poll has already applied
  // a success. A network/5xx failure is shown unless that newer success has
  // already applied. An older 200 must not clear a newer applied failure.
  // Denial records a revoke watermark covering every poll that has already
  // started so an older or queued 200 cannot restore cleared caches. A poll
  // that starts after that watermark may recover.
  const listingGenerationRef = useRef(0);
  // Highest listing generation that applied a successful 200. An older 401/403
  // must not clear caches that this newer success already owns. An older 200
  // must not overwrite that snapshot either. An older network/5xx failure must
  // not replace the error that success already cleared.
  const appliedListingGenerationRef = useRef(0);
  // Highest listing generation that applied a network/5xx failure. A newer
  // poll merely starting is not recovery. An older 200 must not clear a
  // warning this newer failure already applied, or last-good streams stay up
  // with no error again.
  const appliedListingFailureGenerationRef = useRef(0);
  // Listing network/5xx owns the column error banner until a listing 200 or
  // an authoritative listing 401/403. A later successful tail must not clear
  // it: last-good stream metadata is still on screen, and a hung follow-up
  // listing would otherwise look current.
  const listingOutageMessageRef = useRef<string | null>(null);
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
  // Tail 401/403 while listing stays reachable. Listing success must not clear
  // this latch; only a successful read of each denied stream may recover
  // /stream. A sibling 200, or a 5xx retry of the denied stream, must not.
  // A denial for a stream the operator deselected, or that left the latest
  // listing, no longer blocks recovery of the streams still being read.
  const tailAuthDeniedRef = useRef(false);
  const [tailAuthDenied, setTailAuthDenied] = useState(false);
  // Route-scoped /stream 401/403 while listing and tail stay reachable.
  // Listing or tail 200 must not clear this latch or restore column caches:
  // the stream route can stay denied while those reads remain authorized.
  // A later /stream probe may clear the latch after it connects. The column
  // remounts (and may retry) when the operator reopens it.
  const streamAuthDeniedRef = useRef(false);
  const [streamAuthDenied, setStreamAuthDenied] = useState(false);
  const [streamProbeNonce, setStreamProbeNonce] = useState(0);
  const streamProbeTimerRef = useRef<number | null>(null);
  const tailDeniedStreamIdsRef = useRef<Set<string>>(new Set());
  // Streams whose last selected-tail read failed with network/5xx. The
  // 401/403 denial set stays empty, and a static or closed listing does not
  // change selectedTailRefreshKey, so the refresh-key effect must still retry
  // these on the next listing poll or the stale snapshot warning sticks until
  // Tail all. Kept in sync with tailRefreshErrors; a ref so recording the
  // warning does not immediately re-enter the effect.
  const tailRefreshErrorStreamIdsRef = useRef<Set<string>>(new Set());
  // Overlapping selected-tail reloads stay monotonic so a newer 401/403 cannot
  // lose to an older in-flight 200. A 401/403 is authoritative unless a
  // strictly newer successful tail response has already been applied. Denial
  // records a revoke watermark covering every tail request that has already
  // started so an older or in-flight 200 cannot restore cleared caches. A
  // request that starts after that watermark may recover.
  const tailRequestGenerationRef = useRef(0);
  // Highest tail generation that applied a successful snapshot. An older
  // 401/403 must not clear caches that this newer success already owns.
  const appliedTailGenerationRef = useRef(0);
  // Per-stream generation of that applied success. Overlapping waves can
  // apply a newer 200 for stream A while B fails transiently; only a newer
  // success for that stream may suppress its 401/403 or network/5xx warning.
  // A sibling success must not leave B's cache visible, drop B's outage, or
  // keep /stream open.
  const appliedTailSuccessGenerationRef = useRef<Record<string, number>>({});
  // Highest tail generation covered by an applied 401/403. An older
  // overlapping 200 (started before that denial) must not restore cleared
  // caches. Re-applying a denial already inside this window must not raise
  // the watermark, or a recovery request that started after the original
  // denial would be rejected.
  const revokedTailGenerationRef = useRef(0);
  // Per-stream generation of the newest network/5xx warning copied into
  // tailRefreshErrors. An older failure's queued updater must not overwrite a
  // newer warning, or re-stamp one a later success already cleared.
  const appliedTailFailureGenerationRef = useRef<Record<string, number>>({});
  const eventSourceRef = useRef<EventSource | null>(null);
  const [listingDenied, setListingDenied] = useState(false);
  // Applied listing 200 generation. A static or closed listing does not change
  // selectedTailRefreshKey, and replacing the stream list does not have to
  // change loadSelectedTails identity either. The refresh-key effect still
  // must re-enter so a network/5xx tailRefreshError can retry without Tail all.
  const [appliedListingGeneration, setAppliedListingGeneration] = useState(0);

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
  const tailRefreshError = useMemo(
    () => formatFullscreenTailRefreshError(tailRefreshErrors, selectedStreams),
    [selectedStreams, tailRefreshErrors],
  );
  const tailRefreshStale = Boolean(tailRefreshError) && selectedEntries.length > 0;

  const loadSelectedTails = useCallback(async () => {
    if (!allowLogs) {
      return;
    }
    const selected = streams.filter((stream) => selectedStreams.includes(stream.stream_id));
    if (selected.length === 0) {
      return;
    }
    tailReloadInFlightCountRef.current += 1;
    try {
    const epoch = columnEpochRef.current;
    const generation = ++tailRequestGenerationRef.current;
    // Siblings still unread when a 401/403 settles. Snapshot them with the
    // denial so a later 200 cannot reopen /stream while another selected tail
    // is unauthorized or still hanging.
    const inFlightStreamIds = new Set(selected.map((stream) => stream.stream_id));
    let sawAuthDenial = false;

    const applyTailAuthDenial = (denied: Extract<LogTailReadResult, { ok: false }>) => {
      // Tail 401/403 while listing stays authorized is still revocation for
      // this column's log output. Apply it as soon as this read settles — do
      // not wait for sibling tails, and do not install a status-erased helper
      // entry or leave /stream open for a later live frame to refill caches.
      // Listing denial bumps columnEpochRef and may recover before this wave
      // settles. listingDenied is already false then, and this wave may still
      // be the current generation, so a stale 401 must also be discarded when
      // its captured epoch no longer owns the column. A newer tail reload
      // merely starting is not recovery: discard this denial only when this
      // stream itself has a strictly newer successful snapshot already
      // applied, or when this request started inside an already-applied
      // denial window (raising the watermark then would reject a recovery
      // that started after it). A newer success for a sibling stream is not
      // this stream's recovery.
      if (
        epoch !== columnEpochRef.current ||
        listingDeniedRef.current ||
        generation < (appliedTailSuccessGenerationRef.current[denied.streamId] ?? 0) ||
        (tailAuthDeniedRef.current && generation <= revokedTailGenerationRef.current)
      ) {
        return;
      }
      for (const streamId of inFlightStreamIds) {
        tailDeniedStreamIdsRef.current.add(streamId);
      }
      tailDeniedStreamIdsRef.current.add(denied.streamId);
      if (!tailAuthDeniedRef.current) {
        columnEpochRef.current += 1;
      }
      // Queued cache clears run after this function returns. Listing denial
      // can bump the epoch again and recover before React flushes them, and
      // those updaters must not wipe the recovered snapshot. Compare the
      // epoch this denial stamped, not the pre-bump captured epoch — the
      // first denial always increments, so the captured value would never
      // match and the clear would never apply.
      const denialEpoch = columnEpochRef.current;
      revokedTailGenerationRef.current = Math.max(
        revokedTailGenerationRef.current,
        tailRequestGenerationRef.current,
      );
      tailAuthDeniedRef.current = true;
      sawAuthDenial = true;
      setTailAuthDenied(true);
      appliedTailFailureGenerationRef.current = {};
      tailRefreshErrorStreamIdsRef.current.clear();
      setTailRefreshErrors({});
      setError(denied.message ?? "Unable to load log stream.");
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      setStreamState("idle");
      // A newer reload may already have incremented tailRequestGenerationRef.
      // The clear still applies until that newer request itself lands a
      // success or listing/epoch recovery takes the column.
      const denialStillOwnsColumn = () =>
        denialEpoch === columnEpochRef.current &&
        (appliedTailSuccessGenerationRef.current[denied.streamId] ?? 0) <= generation &&
        tailAuthDeniedRef.current &&
        !listingDeniedRef.current;
      setEntries((current) => (denialStillOwnsColumn() ? [] : current));
      setOffsets((current) => (denialStillOwnsColumn() ? {} : current));
    };

    const applyTailRefreshFailure = (failure: Extract<LogTailReadResult, { ok: false }>) => {
      // Network/5xx must warn as soon as this read settles. Copying failures
      // only after Promise.all never runs while a sibling tail hangs, and
      // apiGet has no timeout, so the column would keep the last snapshot
      // with no stale/error warning. A newer reload merely starting is not
      // recovery — discard this warning only when this stream itself has a
      // strictly newer successful snapshot, a newer failure already owns this
      // stream, or authorization/listing denial has cleared the column. A
      // sibling success advances the global applied generation and must not
      // drop B's outage while B's own read is still hanging.
      if (isFullscreenTailAuthFailure(failure.status)) {
        return;
      }
      if (
        epoch !== columnEpochRef.current ||
        listingDeniedRef.current ||
        tailAuthDeniedRef.current ||
        streamAuthDeniedRef.current ||
        sawAuthDenial ||
        generation < (appliedTailSuccessGenerationRef.current[failure.streamId] ?? 0) ||
        generation <= revokedTailGenerationRef.current
      ) {
        return;
      }
      const prior = appliedTailFailureGenerationRef.current[failure.streamId] ?? 0;
      if (generation < prior) {
        return;
      }
      appliedTailFailureGenerationRef.current[failure.streamId] = generation;
      tailRefreshErrorStreamIdsRef.current.add(failure.streamId);
      const message = fullscreenTailRefreshMessage(failure.message);
      setTailRefreshErrors((current) => {
        if (
          epoch !== columnEpochRef.current ||
          listingDeniedRef.current ||
          tailAuthDeniedRef.current ||
          streamAuthDeniedRef.current ||
          generation < (appliedTailSuccessGenerationRef.current[failure.streamId] ?? 0) ||
          generation <= revokedTailGenerationRef.current ||
          appliedTailFailureGenerationRef.current[failure.streamId] !== generation
        ) {
          return current;
        }
        if (current[failure.streamId] === message) {
          return current;
        }
        return { ...current, [failure.streamId]: message };
      });
    };

    const readSelectedTail = async (stream: (typeof selected)[number]): Promise<LogTailReadResult> => {
      try {
        const result = await readLogTailEntry(
          workspace.workspace_id,
          stream,
          logStreamActivityFor(streamActivityRef.current, workspace.workspace_id, stream),
        );
        inFlightStreamIds.delete(result.ok ? result.entry.streamId : result.streamId);
        if (!result.ok && isFullscreenTailAuthFailure(result.status)) {
          applyTailAuthDenial(result);
        } else if (!result.ok) {
          applyTailRefreshFailure(result);
        }
        return result;
      } catch (cause) {
        inFlightStreamIds.delete(stream.stream_id);
        const failure: Extract<LogTailReadResult, { ok: false }> = {
          ok: false,
          status: 0,
          message: cause instanceof Error ? cause.message : "Unable to refresh log tails.",
          streamId: stream.stream_id,
        };
        applyTailRefreshFailure(failure);
        return failure;
      }
    };

    const results = await Promise.all(selected.map((stream) => readSelectedTail(stream)));
    // Denial already cleared caches and closed /stream. A later sibling 200
    // from this wave must not restore either, even if epoch was already
    // advanced by an earlier denial and would otherwise look current.
    if (sawAuthDenial) {
      return;
    }
    if (
      epoch !== columnEpochRef.current ||
      generation <= revokedTailGenerationRef.current ||
      listingDeniedRef.current ||
      streamAuthDeniedRef.current
    ) {
      return;
    }
    if (
      generation !== tailRequestGenerationRef.current ||
      generation < appliedTailGenerationRef.current
    ) {
      // Sibling success advanced the global watermark. Do not apply this
      // wave's 200s; still record network/5xx for streams that have not recovered.
      for (const result of results) {
        if (!result.ok) {
          applyTailRefreshFailure(result);
        }
      }
      return;
    }
    // Transient network/5xx (and other non-auth) failures: keep the last
    // successful fullscreen snapshot. Stream-metadata polling retriggers these
    // reads, so replacing diagnostics with an error line would hide the
    // snapshot the feed-outage contract requires.
    const successes = results.filter((result): result is Extract<LogTailReadResult, { ok: true }> => result.ok);
    const failures = results.filter((result) => !result.ok);
    for (const success of successes) {
      tailDeniedStreamIdsRef.current.delete(success.entry.streamId);
    }
    // Drop denials the operator deselected or that left this listing. They
    // cannot be read again from here, so leaving them in the set latches
    // tailAuthDenied and keeps /stream closed after the remaining streams
    // succeed. A denial still in this selected∩listed set stays until its
    // own 200 — a sibling 200 or a 5xx retry must not clear it.
    const activeStreamIds = new Set(selected.map((stream) => stream.stream_id));
    for (const streamId of tailDeniedStreamIdsRef.current) {
      if (!activeStreamIds.has(streamId)) {
        tailDeniedStreamIdsRef.current.delete(streamId);
      }
    }
    for (const streamId of tailRefreshErrorStreamIdsRef.current) {
      if (!activeStreamIds.has(streamId)) {
        tailRefreshErrorStreamIdsRef.current.delete(streamId);
      }
    }
    if (tailDeniedStreamIdsRef.current.size > 0) {
      return;
    }
    if (successes.length === 0) {
      if (!tailAuthDeniedRef.current) {
        for (const failure of failures) {
          tailRefreshErrorStreamIdsRef.current.add(failure.streamId);
        }
        setTailRefreshErrors((current) => {
          if (
            epoch !== columnEpochRef.current ||
            generation !== tailRequestGenerationRef.current ||
            listingDeniedRef.current ||
            tailAuthDeniedRef.current ||
            streamAuthDeniedRef.current
          ) {
            return current;
          }
          const next = { ...current };
          for (const failure of failures) {
            next[failure.streamId] = fullscreenTailRefreshMessage(failure.message);
          }
          return next;
        });
      }
      return;
    }
    // A /stream 401/403 can latch after this wave passed the post-await gate.
    // Do not stamp this 200 as recovery or clear the stream-denial banner.
    if (streamAuthDeniedRef.current) {
      return;
    }
    // Every previously denied stream has now returned 200. Listing 200 must
    // not do this; a sibling success alone must not either. Record this
    // generation before the writes so an older in-flight 401/403 cannot
    // clear the snapshot this success just established. Stamp only the
    // streams this wave actually applied — a sibling 200 must not count as
    // recovery for a stream that failed in this same wave.
    appliedTailGenerationRef.current = Math.max(appliedTailGenerationRef.current, generation);
    for (const success of successes) {
      const streamId = success.entry.streamId;
      const prior = appliedTailSuccessGenerationRef.current[streamId] ?? 0;
      if (generation >= prior) {
        appliedTailSuccessGenerationRef.current[streamId] = generation;
      }
    }
    if (tailAuthDeniedRef.current) {
      tailAuthDeniedRef.current = false;
      setTailAuthDenied(false);
      // Denial owned the banner. A listing outage that was suppressed while
      // the latch was set is still unrecovered inventory — surface it instead
      // of leaving the last-good streams unmarked.
      setError(listingOutageMessageRef.current);
    } else if (listingOutageMessageRef.current == null) {
      // Listing network/5xx owns this banner until a listing 200. Tail all
      // (or any later successful tail) must not clear it.
      setError(null);
    }
    // Drop failure stamps for streams this success recovered before the
    // queued write, so an older failure updater cannot re-stamp the warning.
    for (const success of successes) {
      const stamped = appliedTailFailureGenerationRef.current[success.entry.streamId];
      if (stamped != null && stamped <= generation) {
        delete appliedTailFailureGenerationRef.current[success.entry.streamId];
      }
      tailRefreshErrorStreamIdsRef.current.delete(success.entry.streamId);
    }
    for (const failure of failures) {
      tailRefreshErrorStreamIdsRef.current.add(failure.streamId);
    }
    setTailRefreshErrors((current) => {
      if (
        epoch !== columnEpochRef.current ||
        generation !== tailRequestGenerationRef.current ||
        listingDeniedRef.current ||
        tailAuthDeniedRef.current ||
        streamAuthDeniedRef.current
      ) {
        return current;
      }
      const next = { ...current };
      for (const failure of failures) {
        next[failure.streamId] = fullscreenTailRefreshMessage(failure.message);
      }
      for (const success of successes) {
        delete next[success.entry.streamId];
      }
      return next;
    });
    const byStream = new Map(successes.map((result) => [result.entry.streamId, result]));
    setEntries((current) => {
      // Functional updaters can flush after a denial clear; drop the write so
      // previously authorized tails cannot reappear.
      if (
        epoch !== columnEpochRef.current ||
        generation !== tailRequestGenerationRef.current ||
        listingDeniedRef.current ||
        tailAuthDeniedRef.current ||
        streamAuthDeniedRef.current
      ) {
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
        ...successes.map((result) => result.entry),
      ], selectedStreams);
    });
    setOffsets((current) => {
      if (
        epoch !== columnEpochRef.current ||
        generation !== tailRequestGenerationRef.current ||
        listingDeniedRef.current ||
        tailAuthDeniedRef.current ||
        streamAuthDeniedRef.current
      ) {
        return current;
      }
      const next = { ...current };
      for (const result of successes) {
        next[result.entry.streamId] = result.nextOffset;
      }
      return next;
    });
    } finally {
      tailReloadInFlightCountRef.current -= 1;
    }
  }, [allowLogs, selectedStreams, streams, workspace.workspace_id]);

  useEffect(() => {
    selectedStreamsRef.current = selectedStreams;
  }, [selectedStreams]);

  const loadStreams = useCallback(async () => {
    if (!allowLogs) {
      setStreams([]);
      setSelectedStreams([]);
      appliedTailFailureGenerationRef.current = {};
      tailRefreshErrorStreamIdsRef.current.clear();
      setTailRefreshErrors({});
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
      listingOutageMessageRef.current = null;
      setError(message);
      appliedTailFailureGenerationRef.current = {};
      tailRefreshErrorStreamIdsRef.current.clear();
      setTailRefreshErrors({});
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
      // A newer poll may already have started. Suppress this failure only
      // after a newer listing 200 has applied, or a newer failure already
      // owns the warning. Discarding every non-latest network/5xx because
      // generation !== listingGenerationRef.current starves the column when
      // each failure is slower than pollMs: last-good streams stay up with
      // no warning, or the first load stays blank.
      if (
        generation < appliedListingGenerationRef.current ||
        generation < appliedListingFailureGenerationRef.current
      ) {
        return;
      }
      appliedListingFailureGenerationRef.current = generation;
      listingOutageMessageRef.current = result.message;
      // A latched tail 401/403 owns this banner. Listing success refuses to
      // clear it, so a transient listing failure must not replace the
      // authorization reason — a later listing 200 would otherwise leave a
      // recovered 503 on screen indefinitely. Re-check the latch in the
      // updater: a denial can settle after this failure is queued. The
      // outage message stays recorded so a tail success cannot drop it, and
      // so tail-denial recovery can restore it if listing has not.
      setError((current) =>
        generation < appliedListingGenerationRef.current ||
        generation < appliedListingFailureGenerationRef.current ||
        tailAuthDeniedRef.current ||
        streamAuthDeniedRef.current
          ? current
          : result.message,
      );
      return;
    }
    // Older overlapping listing 200: this poll started before the denial that
    // cleared the column, or a newer 200 already applied a later snapshot.
    // Do not restore streams/entries or overwrite the newer inventory. The
    // first completed success still lands (applied generation starts at 0).
    if (generation <= revokedListingGenerationRef.current) {
      return;
    }
    if (generation < appliedListingGenerationRef.current) {
      return;
    }
    // A newer network/5xx already applied the outage warning. This older 200
    // must not clear it or rewind last-good streams. The same watermark is
    // re-checked in listingSuccessStillApplied so a 200 that already passed
    // this return cannot flush after the failure lands.
    if (generation < appliedListingFailureGenerationRef.current) {
      return;
    }
    listingDeniedRef.current = false;
    appliedListingGenerationRef.current = Math.max(
      appliedListingGenerationRef.current,
      generation,
    );
    // Re-enter the tail refresh-key effect even when this 200's metadata
    // fingerprint matches the previous poll. A static/closed stream with a
    // tailRefreshError would otherwise keep the stale snapshot until Tail all.
    setAppliedListingGeneration(appliedListingGenerationRef.current);
    // Listing recovered. Drop the outage even when a tail 401/403 still owns
    // the visible banner, so a later tail success cannot restore a 503 the
    // listing has already replaced.
    listingOutageMessageRef.current = null;
    // Listing 200 does not clear a tail 401/403 latch or its banner.
    // Route-scoped tail permission can stay revoked while the stream list
    // remains authorized; wiping the denial message leaves an empty column.
    setListingDenied(false);
    if (!tailAuthDeniedRef.current && !streamAuthDeniedRef.current) {
      setError((current) =>
        generation < appliedListingFailureGenerationRef.current ||
        tailAuthDeniedRef.current ||
        streamAuthDeniedRef.current
          ? current
          : null,
      );
    }
    const listingItems = result.data.items;
    // Functional writes re-check the applied generation. A newer 200 can land
    // and bump the watermark before React flushes an older success that already
    // passed the early return, and a direct setStreams(items) would still
    // replace the newer inventory with this stale snapshot. A newer network/5xx
    // can also apply after that early return; without the failure watermark
    // the queued write keeps the error but rewinds last-good streams.
    // Activity is last-good state too: committing it before the flush lets the
    // discarded 200 delete timestamps for streams still on screen.
    const pendingStreamActivity = updateLogStreamActivity(
      streamActivityRef.current,
      workspace.workspace_id,
      listingItems,
    );
    let committedListingActivity = false;
    const listingSuccessStillApplied = () => {
      const stillApplied =
        generation === appliedListingGenerationRef.current &&
        generation > revokedListingGenerationRef.current &&
        generation >= appliedListingFailureGenerationRef.current;
      if (stillApplied && !committedListingActivity) {
        streamActivityRef.current = pendingStreamActivity;
        committedListingActivity = true;
      }
      return stillApplied;
    };
    setStreams((current) => (listingSuccessStillApplied() ? listingItems : current));
    setSelectedStreams((current) =>
      listingSuccessStillApplied() ? pickWorkspaceLogStreams(listingItems, current) : current,
    );
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
    // Static listing metadata must not restart tails that already landed.
    // Two failures still retry on the next listing refresh: a 401/403 latch
    // (only that stream's own 200 clears it) and a network/5xx
    // tailRefreshError (the denial set stays empty, so a static or closed
    // stream would otherwise keep the stale snapshot until Tail all).
    // appliedListingGeneration re-enters this effect on each applied listing
    // 200 even when the fingerprint and loadSelectedTails identity are
    // unchanged. Do not start a second reload while one is already in flight
    // — a newer generation would discard the slower success.
    const retryOutstandingTailFailure =
      tailDeniedStreamIdsRef.current.size > 0 ||
      selectedStreamsRef.current.some((streamId) =>
        tailRefreshErrorStreamIdsRef.current.has(streamId),
      );
    if (
      previousTailRefreshKey.current === selectedTailRefreshKey &&
      (!retryOutstandingTailFailure || tailReloadInFlightCountRef.current > 0)
    ) {
      return;
    }
    previousTailRefreshKey.current = selectedTailRefreshKey;
    void loadSelectedTails();
  }, [appliedListingGeneration, loadSelectedTails, selectedTailRefreshKey]);

  useEffect(() => {
    if (previousTailSignal.current === tailSignal) {
      return;
    }
    previousTailSignal.current = tailSignal;
    void loadSelectedTails();
  }, [loadSelectedTails, tailSignal]);

  const clearStreamAuthProbe = () => {
    if (streamProbeTimerRef.current !== null) {
      window.clearTimeout(streamProbeTimerRef.current);
      streamProbeTimerRef.current = null;
    }
  };

  const scheduleStreamAuthProbe = () => {
    if (streamProbeTimerRef.current !== null) {
      return;
    }
    streamProbeTimerRef.current = window.setTimeout(() => {
      streamProbeTimerRef.current = null;
      if (!streamAuthDeniedRef.current) {
        return;
      }
      setStreamProbeNonce((current) => current + 1);
    }, streamAuthProbeDelayMs);
  };

  useEffect(() => {
    return () => {
      clearStreamAuthProbe();
      setStreamProbeNonce(0);
    };
  }, [workspace.workspace_id]);

  useEffect(() => {
    // Listing is required to pick/surface streams; do not open /stream or buffer
    // frames when workspace_logs is unsupported (even if workspace_stream is up).
    // Listing or tail 401/403 while workspace_logs stays advertised must close
    // /stream. Those denials are separate from a route-level /stream 401/403:
    // a later listing or tail 200 must not reopen or restore revoked log text.
    // The stream-route latch can clear only after a later /stream probe connects.
    if (!allowStreamLogs || listingDenied || tailAuthDenied) {
      setStreamState("idle");
      return;
    }
    const probingDeniedStream = streamAuthDenied && streamProbeNonce > 0;
    if (streamAuthDenied && !probingDeniedStream) {
      setStreamState("idle");
      scheduleStreamAuthProbe();
      return;
    }
    // workspace_stream may stay up while workspace_events is unsupported or
    // policy_disabled. Request only the negotiated feeds so Core does not read
    // event history for a log column (same gates as the inspector stream).
    const { allowEvents, channels, tailBytes } = resolveWorkspaceStreamSubscription(capabilities);
    setStreamState("connecting");
    const source = new EventSource(
      awfPath(`workspaces/${workspace.workspace_id}/stream`, {
        channels,
        tail_bytes: tailBytes,
      }),
    );
    eventSourceRef.current = source;
    const openedEpoch = columnEpochRef.current;
    let closedByServer = false;
    let terminalError = false;
    let probeRejected = false;

    const rejectFailedStreamProbe = () => {
      // A delayed probe that receives a non-auth error or closed frame has
      // not proved the route recovered. Close it so a later heartbeat or
      // snapshot cannot accept the probe, and wait for the delayed retry.
      if (!streamAuthDeniedRef.current || openedEpoch !== columnEpochRef.current) {
        return;
      }
      probeRejected = true;
      setStreamState("idle");
      source.close();
      if (eventSourceRef.current === source) {
        eventSourceRef.current = null;
      }
      setStreamProbeNonce(0);
      scheduleStreamAuthProbe();
    };

    const acceptStreamProbe = () => {
      if (!streamAuthDeniedRef.current) {
        return;
      }
      // The probe connected. Keep previously cleared caches empty until new
      // frames arrive; listing or tail 200 must still not restore them.
      streamAuthDeniedRef.current = false;
      setStreamAuthDenied(false);
      setStreamProbeNonce(0);
      // Denial owned the banner. A listing outage recorded while the stream
      // latch was set is still unrecovered inventory — surface it instead of
      // clearing the banner so last-good streams look current. Read the ref
      // when the update flushes: a listing 200 can clear it, or a listing
      // 5xx can record it, between this handshake and the write. Tail
      // recovery already restores the stored outage; only a later listing
      // 200 clears it.
      setError(() => listingOutageMessageRef.current);
    };

    const applyStreamAuthorizationDenial = (message: string) => {
      // Route-level /stream 401/403 is authorization revocation, not an outage.
      // Clear column caches and hold this EventSource closed. Listing and tail
      // can stay authorized; those 200s must not restore revoked log text or
      // reopen /stream. A later probe may clear this latch after it connects.
      if (!streamAuthDeniedRef.current) {
        columnEpochRef.current += 1;
      }
      const denialEpoch = columnEpochRef.current;
      clearStreamAuthProbe();
      setStreamProbeNonce(0);
      streamAuthDeniedRef.current = true;
      setStreamAuthDenied(true);
      scheduleStreamAuthProbe();
      setStreamState("idle");
      setError(message);
      const denialStillOwnsColumn = () =>
        denialEpoch === columnEpochRef.current && streamAuthDeniedRef.current;
      setEntries((current) => (denialStillOwnsColumn() ? [] : current));
      setOffsets((current) => (denialStillOwnsColumn() ? {} : current));
    };

    source.onmessage = (message) => {
      if (
        listingDeniedRef.current ||
        tailAuthDeniedRef.current ||
        openedEpoch !== columnEpochRef.current
      ) {
        return;
      }
      const frame = parseFrame(message.data);
      if (!frame) {
        return;
      }
      if (frame.type === "error" || frame.type === "closed") {
        if (isFullscreenStreamAuthDenied(frame)) {
          applyStreamAuthorizationDenial(
            frame.type === "error" ? frame.message : "Workspace stream authorization denied.",
          );
          source.close();
          if (eventSourceRef.current === source) {
            eventSourceRef.current = null;
          }
          return;
        }
        if (streamAuthDeniedRef.current) {
          rejectFailedStreamProbe();
          return;
        }
      } else if (streamAuthDeniedRef.current && !probeRejected) {
        acceptStreamProbe();
      }
      if (
        probeRejected ||
        streamAuthDeniedRef.current ||
        openedEpoch !== columnEpochRef.current
      ) {
        return;
      }
      if (frame.type === "connected" || frame.type === "heartbeat" || frame.type === "snapshot") {
        setStreamState("live");
        return;
      }
      if (frame.type === "event") {
        // An unsupported workspace_events gate must not treat event history as
        // the live signal. Snapshots and logs stay on this EventSource.
        if (!allowEvents) {
          return;
        }
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
          if (
            listingDeniedRef.current ||
            tailAuthDeniedRef.current ||
            streamAuthDeniedRef.current ||
            openedEpoch !== columnEpochRef.current
          ) {
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
          if (
            listingDeniedRef.current ||
            tailAuthDeniedRef.current ||
            streamAuthDeniedRef.current ||
            openedEpoch !== columnEpochRef.current
          ) {
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
      // A probe that dies without an authorized frame waits for the delayed
      // retry instead of letting EventSource reconnect immediately.
      if (streamAuthDeniedRef.current && openedEpoch === columnEpochRef.current) {
        rejectFailedStreamProbe();
        return;
      }
      if (
        listingDeniedRef.current ||
        tailAuthDeniedRef.current ||
        streamAuthDeniedRef.current ||
        openedEpoch !== columnEpochRef.current
      ) {
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
  }, [allowStreamLogs, capabilities, listingDenied, streamAuthDenied, streamProbeNonce, tailAuthDenied, workspace.workspace_id]);

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
          {tailRefreshStale ? (
            <span
              title="Showing the last snapshot — live data may be stale"
              className="inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-attention-border bg-attention-soft px-1.5 py-0.5 text-[10px] font-medium text-attention-text"
            >
              <span aria-hidden>⚠</span>
              stale
            </span>
          ) : null}
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
        {tailRefreshError ? (
          <div
            role="alert"
            className="mb-2 rounded-md border border-danger-border bg-danger-soft px-2 py-1.5 text-xs text-danger-text"
          >
            <span aria-hidden>⚠</span> {tailRefreshError}
          </div>
        ) : null}
        {!allowLogs ? (
          <MutedLine>Workspace log listing is unavailable.</MutedLine>
        ) : (
          <div data-awf-stale={tailRefreshStale ? "true" : undefined} className="h-full min-h-0">
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
        )}
      </div>
    </section>
  );
}
