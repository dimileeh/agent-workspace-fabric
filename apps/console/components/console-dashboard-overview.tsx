"use client";

import {
AlertCircle,
AlertTriangle,
ArrowDown,
ArrowUp,
Bot,
Boxes,
ChevronDown,
ChevronUp,
Contrast,
FileText,
GitPullRequest,
HeartPulse,
ListFilter,
ListTree,
Maximize2,
Monitor,
Moon,
Radio,
RefreshCw,
Search,
Server,
Sun,
Terminal,
Type
} from "lucide-react";
import {
type SyntheticEvent,
type UIEvent,
memo,
useCallback,
useEffect,
useId,
useLayoutEffect,
useMemo,
useRef,
useState
} from "react";

import {
formatAgentLabel,
formatAgentTitle,
hasTerminalWorkflowTiming,
resolveWorkflowTiming
} from "@/lib/agent-format";
import {
  displayedTaskKey,
  MAX_FULLSCREEN_LOG_WORKSPACES,
} from "@/lib/console-dashboard-derived";
import { copyTextToClipboard } from "@/lib/clipboard";
import {
  attentionAgeSeconds,
  attentionBadgeLabel,
  attentionSince,
  isAwaitingHuman,
} from "@/lib/attention-format";
import { blockedAgeSeconds, blockedSince } from "@/lib/blocked-format";
import { summarizeVisibleCoordinationWarnings } from "@/lib/coordination-format";
import {
compactDuration,
compactId,
formatDateTime,
lifecycleStages,
recordedDurationLabel,
relativeTime,
toneClass, type StatusTone
} from "@/lib/format";
import type { OperatorPreferences } from "@/lib/operator-preferences";
import {
formatRecoveryBadge
} from "@/lib/recovery-format";
import { formatDashboardCoverageNotice } from "@/lib/console-dashboard-summary";
import type { ConsoleDashboardCountEvidence, WorkspaceOverview } from "@/lib/types";
import {
Badge, KpiStat,
SmallExternalAnchor,
SortDirection,
WorkspaceSortKey,
formatPrLinkLabel,
workspaceFilterSummary
} from "./console-dashboard-shared";

export function TopBar({
  apiState,
  streamState,
  lastRefresh,
  selectedId,
  preferences,
  onPreferencesChange,
  isPending,
  onRefresh,
}: {
  apiState: "checking" | "ok" | "error";
  streamState: "idle" | "connecting" | "live" | "error";
  lastRefresh: Date | null;
  selectedId: string | null;
  preferences: OperatorPreferences;
  onPreferencesChange: (next: Partial<OperatorPreferences>) => void;
  isPending: boolean;
  onRefresh: () => void;
}) {
  return (
    <header className="flex min-h-14 flex-wrap items-center justify-between gap-3 bg-white px-4 py-2">
      <div className="flex min-w-0 items-center gap-3">
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
      <div className="flex w-full min-w-0 flex-wrap items-center justify-start gap-2 text-xs sm:w-auto sm:flex-1 sm:justify-end">
        <PreferenceControls preferences={preferences} onChange={onPreferencesChange} />
        <StatePill icon={<HeartPulse size={13} />} label="API" state={apiState} />
        <StatePill icon={<Radio size={13} />} label="Stream" state={streamState} />
        <span className="inline-flex h-8 w-[24ch] items-center justify-center rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1 text-center font-mono text-[11px] tabular-nums text-slate-600">
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

export function PreferenceControls({
  preferences,
  onChange,
}: {
  preferences: OperatorPreferences;
  onChange: (next: Partial<OperatorPreferences>) => void;
}) {
  return (
    <div
      className="flex flex-wrap items-center gap-1 rounded-md border border-slate-200 bg-slate-50 p-1"
      aria-label="Display preferences"
    >
      <PreferenceButton
        label="Use light theme"
        pressed={preferences.theme === "light"}
        onClick={() => onChange({ theme: "light" })}
      >
        <Sun size={14} aria-hidden />
      </PreferenceButton>
      <PreferenceButton
        label="Use dark theme"
        pressed={preferences.theme === "dark"}
        onClick={() => onChange({ theme: "dark" })}
      >
        <Moon size={14} aria-hidden />
      </PreferenceButton>
      <PreferenceButton
        label="Use system theme"
        pressed={preferences.theme === "system"}
        onClick={() => onChange({ theme: "system" })}
      >
        <Monitor size={14} aria-hidden />
      </PreferenceButton>
      <PreferenceButton
        label="Enable high contrast"
        pressed={preferences.contrast === "high"}
        onClick={() =>
          onChange({
            contrast: preferences.contrast === "high" ? "normal" : "high",
          })
        }
      >
        <Contrast size={14} aria-hidden />
      </PreferenceButton>
      <PreferenceButton
        label="Use larger font size"
        pressed={preferences.fontSize === "large"}
        onClick={() =>
          onChange({
            fontSize: preferences.fontSize === "large" ? "standard" : "large",
          })
        }
      >
        <Type size={14} aria-hidden />
      </PreferenceButton>
    </div>
  );
}

export function PreferenceButton({
  label,
  pressed,
  onClick,
  children,
}: {
  label: string;
  pressed: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={pressed}
      title={label}
      onClick={onClick}
      className={`inline-flex h-8 w-8 items-center justify-center rounded-md border text-slate-700 transition hover:bg-white ${
        pressed
          ? "border-blue-400 bg-blue-50 text-blue-800 shadow-[inset_0_0_0_1px_var(--accent)]"
          : "border-transparent bg-transparent"
      }`}
    >
      {children}
    </button>
  );
}

export function StatePill({
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
    <span className={`inline-flex min-h-8 items-center gap-1.5 rounded-md border px-2.5 py-1 ${toneClass(tone)}`}>
      {icon}
      {label}: {state}
    </span>
  );
}

export type FleetKpi = {
  id: string;
  label: string;
  value: string | number;
  tone?: StatusTone;
  suffix?: string;
  hint?: string;
  stale?: boolean;
};

// Status layer (ISA-101 three-layer model): a single-glance fleet HUD answering
// "is the fleet ok?" — the 5-7 KPIs an operator scans first, most critical first.
// KPIs dim per source (saturation vs reliability summary) so only the actually
// stale values fade, while the warning above stays at full opacity.
export function FleetHealthStrip({
  kpis,
  error,
  lastSuccessAt,
  coverageStatus,
  coverageNotes,
  countEvidence,
}: {
  kpis: FleetKpi[];
  error?: string | null;
  lastSuccessAt?: string | null;
  coverageStatus?: "complete" | "partial" | "unknown" | null;
  coverageNotes?: readonly string[] | null;
  countEvidence?: ConsoleDashboardCountEvidence | null;
}) {
  const anyStale = kpis.some((kpi) => kpi.stale);
  // HTTP 200 can still be incomplete. Do not treat partial/unknown as a request
  // error — that banner is cleared on success — but surface coverage so operators
  // can tell a degraded snapshot from a complete one (CONSOLE_BACKEND_CONTRACT).
  const coverageNotice = formatDashboardCoverageNotice(
    coverageStatus ? { status: coverageStatus, notes: coverageNotes ?? [] } : null,
    countEvidence,
  );
  return (
    <div className="border-b border-line bg-canvas px-4 py-3" aria-label="Fleet health">
      {error ? (
        <div
          className="mb-2 inline-flex max-w-full flex-wrap items-center gap-1 rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-2 py-0.5 text-[11px] font-medium text-danger-text"
          role="alert"
          data-testid="dashboard-summary-error"
        >
          <span aria-hidden>⚠</span>
          <span>{error}</span>
          {lastSuccessAt ? (
            <span className="text-danger-text/80">· last success {lastSuccessAt}</span>
          ) : null}
        </div>
      ) : null}
      {coverageNotice ? (
        <div
          className="mb-2 inline-flex max-w-full flex-wrap items-center gap-1 rounded-[var(--radius-control)] border border-attention-border bg-attention-soft px-2 py-0.5 text-[11px] font-medium text-attention-text"
          role="status"
          data-testid="dashboard-summary-coverage"
        >
          <span aria-hidden>⚠</span>
          <span>{coverageNotice}</span>
          {!error && lastSuccessAt ? (
            <span className="text-attention-text/80">· last complete {lastSuccessAt}</span>
          ) : null}
        </div>
      ) : null}
      {anyStale ? (
        <div className="mb-2 inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-attention-border bg-attention-soft px-2 py-0.5 text-[11px] font-medium text-attention-text">
          <span aria-hidden>⚠</span>
          some values show the last snapshot — live data may be stale
        </div>
      ) : null}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-9">
        {kpis.map((kpi) => (
          <KpiStat
            key={kpi.id}
            label={kpi.label}
            value={kpi.value}
            tone={kpi.tone}
            suffix={kpi.suffix}
            hint={kpi.hint}
            stale={kpi.stale}
          />
        ))}
      </div>
    </div>
  );
}

const NAV_ITEMS: {
  id: string;
  label: string;
  icon: typeof ListTree;
  key: "workspaces" | "capacity" | "mergeQueue" | "failures";
}[] = [
  { id: "awf-workspaces", label: "Workspaces", icon: ListTree, key: "workspaces" },
  { id: "awf-capacity", label: "Capacity", icon: Server, key: "capacity" },
  { id: "awf-merge-queue", label: "Merge queue", icon: GitPullRequest, key: "mergeQueue" },
  { id: "awf-failures", label: "Failures", icon: AlertTriangle, key: "failures" },
];

// Section jump-nav. On wide screens the panels sit side by side and need no
// navigation; on narrow screens they stack into one tall column, so this sticky
// bar (narrow-only) lets operators jump straight to a section.
// Only offer links whose matching section id is mounted (capability-gated).
export function SectionNav({
  showCapacity,
  showMergeQueue,
  showFailures,
}: {
  showCapacity: boolean;
  showMergeQueue: boolean;
  showFailures: boolean;
}) {
  const visible = {
    workspaces: true,
    capacity: showCapacity,
    mergeQueue: showMergeQueue,
    failures: showFailures,
  };
  const go = (id: string) => document.getElementById(id)?.scrollIntoView({ block: "start" });
  return (
    <nav
      aria-label="Jump to section"
      className="sticky top-0 z-30 flex gap-2 overflow-x-auto border-b border-line bg-canvas px-4 py-2 xl:hidden"
    >
      {NAV_ITEMS.filter((item) => visible[item.key]).map((item) => {
        const Icon = item.icon;
        return (
          <button
            key={item.id}
            type="button"
            onClick={() => go(item.id)}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 py-1 text-[11px] font-medium text-fg-muted transition hover:bg-surface-2 hover:text-fg"
          >
            <Icon size={13} aria-hidden />
            {item.label}
          </button>
        );
      })}
    </nav>
  );
}

export function WorkspaceFilters({
  statusFilters,
  agentFilters,
  modelFilters,
  availableModels,
  availableAgents,
  repoFilter,
  searchText,
  sortKey,
  sortDirection,
  onStatusFilters,
  onAgentFilters,
  onModelFilters,
  onRepoFilter,
  onSearchText,
  onSortKey,
  onSortDirection,
  expanded,
  onToggleExpanded,
}: {
  statusFilters: string[];
  agentFilters: string[];
  modelFilters: string[];
  availableModels: string[];
  availableAgents?: string[];
  repoFilter: string;
  searchText: string;
  sortKey: WorkspaceSortKey;
  sortDirection: SortDirection;
  onStatusFilters: (value: string[]) => void;
  onAgentFilters: (value: string[]) => void;
  onModelFilters: (value: string[]) => void;
  onRepoFilter: (value: string) => void;
  onSearchText: (value: string) => void;
  onSortKey: (value: WorkspaceSortKey) => void;
  onSortDirection: (value: SortDirection) => void;
  expanded: boolean;
  onToggleExpanded: () => void;
}) {
  const statusOptions = Array.from(
    new Set([...lifecycleStages, "failed", "cancelled", "destroying", "destroyed"]),
  );
  const defaultAgents = [
    "codex",
    "claude_code",
    "cursor",
    "antigravity",
    "opencode",
    "grok",
  ];
  const agentOptions = Array.from(
    new Set([...defaultAgents, ...agentFilters, ...(availableAgents ?? [])]),
  ).filter(Boolean);
  const modelOptions = Array.from(new Set([...modelFilters, ...availableModels])).filter(Boolean);
  const activeFilters = workspaceFilterSummary({
    agentFilters,
    modelFilters,
    repoFilter,
    searchText,
    sortDirection,
    sortKey,
    statusFilters,
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
        <div className="grid gap-2">
          <div className="grid grid-cols-2 gap-2">
            <MultiChoiceFilter
              label="Status"
              values={statusFilters}
              onChange={onStatusFilters}
              options={statusOptions}
            />
            <MultiChoiceFilter
              label="Agent"
              values={agentFilters}
              onChange={onAgentFilters}
              options={agentOptions}
            />
          </div>
          <MultiChoiceFilter
            label="Model"
            values={modelFilters}
            onChange={onModelFilters}
            options={modelOptions}
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

export function MultiChoiceFilter({
  label,
  values,
  options,
  onChange,
}: {
  label: string;
  values: string[];
  options: string[];
  onChange: (values: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLFieldSetElement>(null);
  const labelId = useId();
  const buttonId = useId();
  const selected = new Set(values);
  const summary = formatMultiChoiceSummary(values);
  const toggle = (option: string) => {
    if (selected.has(option)) {
      onChange(values.filter((value) => value !== option));
      return;
    }
    onChange([...values, option]);
  };

  useEffect(() => {
    if (!open) {
      return;
    }
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && rootRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <fieldset
      ref={rootRef}
      className="relative grid min-w-0 gap-1 text-[11px] font-medium text-slate-600"
    >
      <legend id={labelId}>{label}</legend>
      <button
        id={buttonId}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-labelledby={`${labelId} ${buttonId}`}
        onClick={() => setOpen((value) => !value)}
        className="inline-flex h-8 min-w-0 items-center justify-between gap-2 rounded-md border border-slate-300 bg-white px-2 text-sm font-normal text-slate-900 transition hover:bg-slate-50"
      >
        <span className="min-w-0 truncate">{summary}</span>
        <ChevronDown
          size={13}
          aria-hidden
          className={`shrink-0 text-slate-500 transition ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open ? (
        <div
          role="menu"
          aria-label={`${label} options`}
          className="absolute top-full right-0 left-0 z-50 mt-1 grid max-h-64 min-w-48 gap-1 overflow-auto rounded-md border border-slate-200 bg-white p-1.5 text-xs shadow-lg"
        >
          <label className="flex h-7 min-w-0 cursor-pointer items-center gap-2 rounded px-2 text-slate-700 hover:bg-slate-50">
            <input
              type="checkbox"
              checked={values.length === 0}
              onChange={() => onChange([])}
              className="h-3.5 w-3.5 shrink-0 rounded border-slate-300"
            />
            <span className="min-w-0 truncate">all</span>
          </label>
          {options.map((option) => (
            <label
              key={option}
              title={option}
              className="flex h-7 min-w-0 cursor-pointer items-center gap-2 rounded px-2 text-slate-700 hover:bg-slate-50"
            >
              <input
                type="checkbox"
                checked={selected.has(option)}
                onChange={() => toggle(option)}
                className="h-3.5 w-3.5 shrink-0 rounded border-slate-300"
              />
              <span className="min-w-0 truncate">{option}</span>
            </label>
          ))}
        </div>
      ) : null}
    </fieldset>
  );
}

export function formatMultiChoiceSummary(values: string[]): string {
  if (values.length === 0) {
    return "all";
  }
  if (values.length <= 2) {
    return values.join(", ");
  }
  return `${values.length} selected`;
}

export function WorkspaceSelectionToolbar({
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
      <span className="text-slate-500">
        {selectedCount} selected for logs
        {selectedCount > MAX_FULLSCREEN_LOG_WORKSPACES
          ? `; first ${MAX_FULLSCREEN_LOG_WORKSPACES} will open`
          : ""}
      </span>
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

export const WORKSPACE_RENDER_WINDOW_SIZE = 100;
export const WORKSPACE_RENDER_OVERSCAN_ROWS = 2;
export const WORKSPACE_HISTORY_SCROLL_THRESHOLD_PX = 240;
const WORKSPACE_RENDER_ROW_HEIGHT_ESTIMATE_PX = 240;
const WORKSPACE_LIST_TOP_EDGE_TOLERANCE_PX = 0.5;

function workspaceRowAtOffset(rowOffsets: readonly number[], offset: number): number {
  let low = 0;
  let high = Math.max(0, rowOffsets.length - 2);
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (rowOffsets[middle + 1] <= offset) {
      low = middle + 1;
    } else {
      high = middle;
    }
  }
  return low;
}

function workspaceRowsBeforeOffset(rowOffsets: number[], offset: number): number {
  let low = 0;
  let high = rowOffsets.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (rowOffsets[middle] < offset) {
      low = middle + 1;
    } else {
      high = middle;
    }
  }
  return low;
}

type WorkspaceCardProps = {
  item: WorkspaceOverview;
  selected: boolean;
  selectedForLogs: boolean;
  showWorkspaceLogs: boolean;
  copied: boolean;
  copyToastVisible: boolean;
  onSelect: (workspaceId: string) => void;
  onToggleWorkspaceSelection: (workspaceId: string, checked: boolean) => void;
  onOpenDetails: (workspaceId: string) => void;
  onOpenLogs: (workspaceId: string) => void;
  onCopy: (event: SyntheticEvent<HTMLElement>, workspaceId: string) => void;
};

const WorkspaceCard = memo(function WorkspaceCard({
  item,
  selected,
  selectedForLogs,
  showWorkspaceLogs,
  copied,
  copyToastVisible,
  onSelect,
  onToggleWorkspaceSelection,
  onOpenDetails,
  onOpenLogs,
  onCopy,
}: WorkspaceCardProps) {
  const recoveryBadge = formatRecoveryBadge(item.recovery, item.status);
  const coordinationSummary = summarizeVisibleCoordinationWarnings(item.coordination_warnings, item.status);
  const blockedFor = item.status === "blocked" ? blockedAgeSeconds(blockedSince(item)) : null;
  const awaitingHumanFor = isAwaitingHuman(item)
    ? attentionAgeSeconds(attentionSince(item))
    : null;
  const taskKey = displayedTaskKey(item);
  const terminal = hasTerminalWorkflowTiming(item);
  const terminalTiming = terminal ? resolveWorkflowTiming(item) : null;
  return (
    <div
      data-testid={`workspace-card-${item.workspace_id}`}
      className={`grid min-w-0 gap-2 border-b border-slate-100 px-3 py-3 transition hover:bg-slate-50 ${
        selected ? "bg-blue-50" : "bg-white"
      }`}
    >
            <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
              <div className="flex min-w-0 items-start gap-2">
                {showWorkspaceLogs ? (
                  <input
                    type="checkbox"
                    checked={selectedForLogs}
                    onChange={(event) => onToggleWorkspaceSelection(item.workspace_id, event.target.checked)}
                    aria-label={`Select ${item.title} for fullscreen logs`}
                    className="mt-0.5 h-4 w-4 shrink-0 rounded border-slate-300"
                  />
                ) : null}
                <div className="relative grid min-w-0 flex-1 gap-2 text-left">
                  <button
                    type="button"
                    onClick={() => onSelect(item.workspace_id)}
                    aria-label={`Open workspace details for ${item.title}`}
                    className="absolute inset-0 z-0 rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]"
                  />
                  <div className="pointer-events-none relative z-10 grid min-w-0 gap-2">
                    <span
                      className="whitespace-normal break-words text-sm font-semibold text-slate-950"
                      data-testid={`workspace-title-${item.workspace_id}`}
                    >
                      {item.title}
                    </span>
                    {taskKey ? (
                      <span
                        className="mono text-[11px] text-slate-500"
                        data-testid={`workspace-task-key-${item.workspace_id}`}
                      >
                        {taskKey}
                      </span>
                    ) : null}
                    <span className="relative inline-flex min-w-0 items-center gap-1.5">
                      <button
                        type="button"
                        onClick={(event) => onCopy(event, item.workspace_id)}
                        className="workspace-id-copy pointer-events-auto focus:outline-none focus-visible:rounded-sm focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]"
                        aria-label={`Copy workspace id ${item.workspace_id}`}
                        title="Copy workspace id"
                      >
                        {item.workspace_id}
                      </button>
                      {copied ? (
                        <span
                          aria-live="polite"
                          className={`pointer-events-none absolute left-full top-1/2 ml-2 -translate-y-1/2 rounded-md border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-800 shadow-sm transition duration-300 ${
                            copyToastVisible ? "opacity-100" : "opacity-0"
                          }`}
                        >
                          copied
                        </span>
                      ) : null}
                    </span>
                    <div
                      className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500"
                      data-testid={`workspace-timing-${item.workspace_id}`}
                    >
                      <span>Created {formatDateTime(item.created_at)}</span>
                      {terminal ? (
                        <>
                          <span data-testid={`workspace-finished-${item.workspace_id}`}>
                            Finished{" "}
                            {terminalTiming?.finishedAt
                              ? formatDateTime(terminalTiming.finishedAt)
                              : "not recorded"}
                          </span>
                          <span data-testid={`workspace-duration-${item.workspace_id}`}>
                            Duration{" "}
                            {recordedDurationLabel(terminalTiming?.durationSeconds) ?? "not recorded"}
                          </span>
                        </>
                      ) : (
                        <span data-testid={`workspace-last-activity-${item.workspace_id}`}>
                          Last activity{" "}
                          {item.last_activity_at
                            ? formatDateTime(item.last_activity_at)
                            : "not recorded"}
                        </span>
                      )}
                    </div>
                    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-600">
                      <Bot size={13} aria-hidden className="shrink-0" />
                      <span className="break-words" title={formatAgentTitle(item)}>
                        {formatAgentLabel(item)}
                      </span>
                      <span className="text-slate-300">/</span>
                      <span className="min-w-0 break-words">{item.base_branch}</span>
                    </div>
                    <div
                      className="break-words text-xs text-[var(--muted)]"
                      data-testid={`workspace-repo-${item.workspace_id}`}
                    >
                      {item.repo_url}
                    </div>
                  </div>
                </div>
              </div>
              <div className="flex w-28 shrink-0 flex-col items-end gap-1 sm:w-32">
                <div className="flex max-w-full items-center gap-1">
                  <Badge value={item.status} />
                  {item.status === "running" && item.subphase ? (
                    <span className="inline-flex h-6 max-w-16 items-center rounded-md border border-slate-200 bg-slate-100 px-2 text-[11px] font-medium text-slate-800 sm:max-w-20">
                      <span className="truncate">({item.subphase})</span>
                    </span>
                  ) : null}
                </div>
                {item.is_stale_running ? (
                  <span className="inline-flex h-6 max-w-full items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 text-[11px] font-medium text-amber-900">
                    <AlertCircle size={12} aria-hidden />
                    <span className="truncate">Stale running</span>
                  </span>
                ) : null}
                {item.status === "blocked" ? (
                  <span
                    data-testid={`workspace-blocked-age-${item.workspace_id}`}
                    className="inline-flex h-6 max-w-full items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 text-[11px] font-medium text-amber-900"
                  >
                    <AlertCircle size={12} aria-hidden />
                    <span className="truncate">Blocked for {compactDuration(blockedFor)}</span>
                  </span>
                ) : null}
                {isAwaitingHuman(item) ? (
                  <span
                    data-testid={`workspace-awaiting-human-${item.workspace_id}`}
                    title={item.awaiting_human_reason ?? "Awaiting human"}
                    className="inline-flex h-6 max-w-full items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 text-[11px] font-medium text-amber-900"
                  >
                    <AlertCircle size={12} aria-hidden />
                    <span className="truncate">
                      {attentionBadgeLabel(
                        awaitingHumanFor != null ? compactDuration(awaitingHumanFor) : null,
                      )}
                    </span>
                  </span>
                ) : null}
                {recoveryBadge ? (
                  <span className="inline-flex h-6 max-w-full items-center rounded-md border border-amber-200 bg-amber-50 px-2 text-[11px] font-medium text-amber-900">
                    <span className="truncate">{recoveryBadge}</span>
                  </span>
                ) : null}
                {coordinationSummary.count > 0 ? (
                  <span
                    title={coordinationSummary.detail}
                    className="inline-flex h-6 max-w-full items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 text-[11px] font-medium text-amber-900"
                  >
                    <AlertCircle size={12} aria-hidden />
                    <span className="truncate">{coordinationSummary.label}</span>
                  </span>
                ) : null}
                {item.pr_url ? (
                  <SmallExternalAnchor href={item.pr_url} label={formatPrLinkLabel(item.pr_url)} />
                ) : null}
              </div>
            </div>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => onOpenDetails(item.workspace_id)}
                className="inline-flex h-7 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-[11px] text-slate-800 transition hover:bg-slate-50"
              >
                <FileText size={12} aria-hidden />
                Details
              </button>
              {showWorkspaceLogs ? (
                <button
                  type="button"
                  onClick={() => onOpenLogs(item.workspace_id)}
                  className="inline-flex h-7 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-[11px] text-slate-800 transition hover:bg-slate-50"
                >
                  <Terminal size={12} aria-hidden />
                  Logs
                </button>
              ) : null}
            </div>
          </div>
  );
});

export function WorkspaceList({
  items,
  selectedId,
  showWorkspaceLogs = true,
  selectedWorkspaceIds,
  onSelect,
  onToggleWorkspaceSelection,
  onOpenDetails,
  onOpenLogs,
  hasMore,
  loadingMore,
  historyComplete,
  historyError,
  loadedCount,
  onLoadMore,
}: {
  items: WorkspaceOverview[];
  selectedId: string | null;
  showWorkspaceLogs?: boolean;
  selectedWorkspaceIds: string[];
  onSelect: (workspaceId: string) => void;
  onToggleWorkspaceSelection: (workspaceId: string, checked: boolean) => void;
  onOpenDetails: (workspaceId: string) => void;
  onOpenLogs: (workspaceId: string) => void;
  hasMore: boolean;
  loadingMore: boolean;
  historyComplete: boolean;
  historyError: boolean;
  loadedCount: number;
  onLoadMore: () => Promise<void>;
}) {
  const [windowStart, setWindowStart] = useState(0);
  const [pageStart, setPageStart] = useState(0);
  const [virtualRowHeight, setVirtualRowHeight] = useState(
    WORKSPACE_RENDER_ROW_HEIGHT_ESTIMATE_PX,
  );
  const [measuredRowHeights, setMeasuredRowHeights] = useState<ReadonlyMap<string, number>>(
    () => new Map(),
  );
  const [copiedWorkspaceId, setCopiedWorkspaceId] = useState<string | null>(null);
  const [copyToastVisible, setCopyToastVisible] = useState(false);
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const renderedWindowRef = useRef<HTMLDivElement | null>(null);
  const measuredWindowWidthRef = useRef<number | null>(null);
  const measuredRootFontSizeRef = useRef<number | null>(null);
  const pendingScrollAnchorRef = useRef<{
    workspaceId: string;
    offsetRatio: number;
    sourceRowOffsets: readonly number[];
  } | null>(null);
  const preserveScrollTopRef = useRef<number | null>(null);
  const suppressScrollLoadRef = useRef(false);
  const suppressScrollFrameRef = useRef<number | null>(null);
  const historyLoadPendingRef = useRef(false);
  const nearBottomTriggerScrollTopRef = useRef<number | null>(null);
  const suppressNextButtonLoadRef = useRef(false);
  const copyFadeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const copyClearTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const nearBottomTriggeredRef = useRef(false);
  const previousSelectedIdRef = useRef<string | null>(null);
  const selectedWasLoadedRef = useRef(false);
  const selectionOwnsScrollAnchorRef = useRef(false);
  const committedListGeometryRef = useRef<{
    workspaceIds: readonly string[];
    rowOffsets: readonly number[];
    selectedId: string | null;
  } | null>(null);
  const maxPageStart = Math.max(
    0,
    Math.floor((items.length - 1) / WORKSPACE_RENDER_WINDOW_SIZE) *
      WORKSPACE_RENDER_WINDOW_SIZE,
  );
  const maxWindowStart = maxPageStart;
  const windowEnd = Math.min(items.length, windowStart + WORKSPACE_RENDER_WINDOW_SIZE);
  const pageEnd = Math.min(items.length, pageStart + WORKSPACE_RENDER_WINDOW_SIZE);
  const rowOffsets = useMemo(() => {
    const offsets = [0];
    for (const item of items) {
      offsets.push(
        offsets[offsets.length - 1] +
          (measuredRowHeights.get(item.workspace_id) ?? virtualRowHeight),
      );
    }
    return offsets;
  }, [items, measuredRowHeights, virtualRowHeight]);

  const scrollWithoutLoading = useCallback((top: number) => {
    const scrollContainer = scrollContainerRef.current;
    if (!scrollContainer) return;
    suppressScrollLoadRef.current = true;
    if (suppressScrollFrameRef.current !== null) {
      cancelAnimationFrame(suppressScrollFrameRef.current);
    }
    scrollContainer.scrollTo({ top });
    suppressScrollFrameRef.current = requestAnimationFrame(() => {
      suppressScrollLoadRef.current = false;
      suppressScrollFrameRef.current = null;
    });
  }, []);

  useLayoutEffect(() => {
    const previous = committedListGeometryRef.current;
    const workspaceIds = items.map((item) => item.workspace_id);
    committedListGeometryRef.current = { workspaceIds, rowOffsets, selectedId };
    if (!previous || previous.selectedId !== selectedId) return;
    const membershipChanged =
      previous.workspaceIds.length !== workspaceIds.length ||
      previous.workspaceIds.some((workspaceId, index) => workspaceId !== workspaceIds[index]);
    const scrollContainer = scrollContainerRef.current;
    if (!membershipChanged || !scrollContainer || previous.workspaceIds.length === 0) return;
    if (scrollContainer.scrollTop <= WORKSPACE_LIST_TOP_EDGE_TOLERANCE_PX) {
      setPageStart(0);
      setWindowStart(0);
      scrollWithoutLoading(0);
      return;
    }

    const previousAnchorIndex = workspaceRowAtOffset(
      previous.rowOffsets,
      scrollContainer.scrollTop,
    );
    const workspaceId = previous.workspaceIds[previousAnchorIndex];
    let anchorIndex = workspaceIds.indexOf(workspaceId);
    let fallbackViewportDelta: number | null = null;
    if (anchorIndex < 0) {
      const survivingWorkspaceIds = new Set(workspaceIds);
      const fallbackWorkspaceId =
        previous.workspaceIds
          .slice(previousAnchorIndex + 1)
          .find((candidate) => survivingWorkspaceIds.has(candidate)) ??
        previous.workspaceIds
          .slice(0, previousAnchorIndex)
          .reverse()
          .find((candidate) => survivingWorkspaceIds.has(candidate));
      if (!fallbackWorkspaceId) return;
      anchorIndex = workspaceIds.indexOf(fallbackWorkspaceId);
      const previousFallbackIndex = previous.workspaceIds.indexOf(fallbackWorkspaceId);
      fallbackViewportDelta =
        previous.rowOffsets[previousFallbackIndex] - scrollContainer.scrollTop;
    }
    const previousAnchorHeight =
      previous.rowOffsets[previousAnchorIndex + 1] -
      previous.rowOffsets[previousAnchorIndex];
    const offsetRatio = previousAnchorHeight > 0
      ? (scrollContainer.scrollTop - previous.rowOffsets[previousAnchorIndex]) /
        previousAnchorHeight
      : 0;
    const anchorHeight = rowOffsets[anchorIndex + 1] - rowOffsets[anchorIndex];
    const anchorWindowStart = Math.min(
      Math.floor(anchorIndex / WORKSPACE_RENDER_WINDOW_SIZE) *
        WORKSPACE_RENDER_WINDOW_SIZE,
      maxWindowStart,
    );
    setPageStart(anchorWindowStart);
    setWindowStart(anchorWindowStart);
    scrollWithoutLoading(
      fallbackViewportDelta === null
        ? rowOffsets[anchorIndex] + offsetRatio * anchorHeight
        : rowOffsets[anchorIndex] - fallbackViewportDelta,
    );
  }, [items, maxWindowStart, rowOffsets, scrollWithoutLoading, selectedId]);

  useEffect(() => {
    const selectedIndex = selectedId
      ? items.findIndex((item) => item.workspace_id === selectedId)
      : -1;
    const selectionChanged = selectedId !== previousSelectedIdRef.current;
    const selectedBecameLoaded =
      selectedIndex >= 0 &&
      selectedId === previousSelectedIdRef.current &&
      !selectedWasLoadedRef.current;
    const shouldFollowSelection =
      selectedIndex >= 0 &&
      (selectedId !== previousSelectedIdRef.current || selectedBecameLoaded);
    if (selectionChanged || selectedIndex < 0) {
      selectionOwnsScrollAnchorRef.current = false;
    }
    previousSelectedIdRef.current = selectedId;
    selectedWasLoadedRef.current = selectedIndex >= 0;
    const scrollContainer = scrollContainerRef.current;
    const selectedRowStart = rowOffsets[selectedIndex] ?? 0;
    const selectedRowEnd = rowOffsets[selectedIndex + 1] ?? selectedRowStart;
    const selectedIsVisible = (() => {
      if (!shouldFollowSelection || !scrollContainer) return false;
      const controlsHeight = scrollContainer.querySelector<HTMLElement>(":scope > .sticky")
        ?.offsetHeight ?? 0;
      const viewportStart = scrollContainer.scrollTop;
      const viewportEnd = viewportStart + scrollContainer.clientHeight - controlsHeight;
      return selectedRowEnd > viewportStart && selectedRowStart < viewportEnd;
    })();
    if (shouldFollowSelection && scrollContainer) {
      const selectionOwnsScrollAnchor = !selectedIsVisible ||
        Math.abs(scrollContainer.scrollTop - selectedRowStart) <= WORKSPACE_LIST_TOP_EDGE_TOLERANCE_PX;
      selectionOwnsScrollAnchorRef.current = selectionOwnsScrollAnchor;
      if (selectionOwnsScrollAnchor) {
        preserveScrollTopRef.current = null;
      }
    }
    const selectedWindowStart = shouldFollowSelection && !selectedIsVisible
      ? Math.floor(selectedIndex / WORKSPACE_RENDER_WINDOW_SIZE) *
        WORKSPACE_RENDER_WINDOW_SIZE
      : null;
    setPageStart((current) =>
      selectedWindowStart ?? Math.min(current, maxPageStart),
    );
    setWindowStart((current) =>
      selectedWindowStart !== null
        ? Math.min(selectedWindowStart, maxWindowStart)
        : Math.min(current, maxWindowStart),
    );
    if (selectedWindowStart !== null && scrollContainer) {
      scrollWithoutLoading(selectedRowStart);
    }
  }, [items, maxPageStart, maxWindowStart, rowOffsets, scrollWithoutLoading, selectedId]);

  useLayoutEffect(() => {
    const renderedWindow = renderedWindowRef.current;
    const renderedCount = windowEnd - windowStart;
    if (!renderedWindow || renderedCount <= 0) return;

    const measureRows = () => {
      const measuredWindowWidth = renderedWindow.getBoundingClientRect().width;
      const measuredRootFontSize = Number.parseFloat(
        getComputedStyle(document.documentElement).fontSize,
      );
      const layoutScaleChanged =
        (measuredWindowWidthRef.current !== null &&
          Math.abs(measuredWindowWidthRef.current - measuredWindowWidth) >= 0.5) ||
        (measuredRootFontSizeRef.current !== null &&
          Math.abs(measuredRootFontSizeRef.current - measuredRootFontSize) >= 0.5);
      const measuredHeights = layoutScaleChanged
        ? new Map<string, number>()
        : new Map(measuredRowHeights);
      let heightsChanged = layoutScaleChanged;
      let measuredHeightTotal = 0;
      Array.from(renderedWindow.children).forEach((child, childIndex) => {
        const item = items[windowStart + childIndex];
        const measuredHeight = child.getBoundingClientRect().height;
        if (!item || measuredHeight <= 0) return;
        measuredHeightTotal += measuredHeight;
        if (Math.abs((measuredHeights.get(item.workspace_id) ?? 0) - measuredHeight) >= 0.5) {
          heightsChanged = true;
          measuredHeights.set(item.workspace_id, measuredHeight);
        }
      });
      const measuredRowHeight = measuredHeightTotal / renderedCount;
      const estimateChanged = measuredRowHeight > 0 &&
        Math.abs(measuredRowHeight - virtualRowHeight) >= 0.5;
      if (!heightsChanged && !estimateChanged) return;

      const scrollContainer = scrollContainerRef.current;
      if (scrollContainer && items.length > 0) {
        const anchorIndex = workspaceRowAtOffset(rowOffsets, scrollContainer.scrollTop);
        const anchorHeight = rowOffsets[anchorIndex + 1] - rowOffsets[anchorIndex];
        pendingScrollAnchorRef.current = {
          workspaceId: items[anchorIndex].workspace_id,
          offsetRatio: anchorHeight > 0
            ? (scrollContainer.scrollTop - rowOffsets[anchorIndex]) / anchorHeight
            : 0,
          sourceRowOffsets: rowOffsets,
        };
      }
      measuredWindowWidthRef.current = measuredWindowWidth;
      measuredRootFontSizeRef.current = measuredRootFontSize;
      setMeasuredRowHeights(measuredHeights);
      if (estimateChanged) {
        setVirtualRowHeight(measuredRowHeight);
      }
    };

    measureRows();
    const resizeObserver = new ResizeObserver(measureRows);
    Array.from(renderedWindow.children).forEach((child) => resizeObserver.observe(child));
    return () => resizeObserver.disconnect();
  }, [items, measuredRowHeights, rowOffsets, virtualRowHeight, windowEnd, windowStart]);

  useLayoutEffect(() => {
    const pendingAnchor = pendingScrollAnchorRef.current;
    if (pendingAnchor?.sourceRowOffsets === rowOffsets) return;
    pendingScrollAnchorRef.current = null;
    const anchorIndex = pendingAnchor
      ? items.findIndex((item) => item.workspace_id === pendingAnchor.workspaceId)
      : -1;
    const anchorHeight = anchorIndex >= 0
      ? rowOffsets[anchorIndex + 1] - rowOffsets[anchorIndex]
      : 0;
    const anchoredScrollTop = pendingAnchor && anchorIndex >= 0
      ? rowOffsets[anchorIndex] + pendingAnchor.offsetRatio * anchorHeight
      : null;
    const scrollTop = anchoredScrollTop ??
      preserveScrollTopRef.current;
    preserveScrollTopRef.current = null;
    if (scrollTop !== null) {
      const scrollContainer = scrollContainerRef.current;
      const atTop =
        scrollContainer !== null &&
        scrollContainer.scrollTop <= WORKSPACE_LIST_TOP_EDGE_TOLERANCE_PX;
      scrollWithoutLoading(atTop ? 0 : scrollTop);
    }
  }, [items, rowOffsets, scrollWithoutLoading]);

  useEffect(() => () => {
    if (suppressScrollFrameRef.current !== null) {
      cancelAnimationFrame(suppressScrollFrameRef.current);
    }
    if (copyFadeTimeoutRef.current !== null) clearTimeout(copyFadeTimeoutRef.current);
    if (copyClearTimeoutRef.current !== null) clearTimeout(copyClearTimeoutRef.current);
  }, []);

  const copyWorkspaceId = useCallback(async (
    event: SyntheticEvent<HTMLElement>,
    workspaceId: string,
  ) => {
    event.preventDefault();
    event.stopPropagation();
    if (copyFadeTimeoutRef.current !== null) clearTimeout(copyFadeTimeoutRef.current);
    if (copyClearTimeoutRef.current !== null) clearTimeout(copyClearTimeoutRef.current);
    const copied = await copyTextToClipboard(workspaceId);
    if (!copied) {
      setCopiedWorkspaceId(null);
      setCopyToastVisible(false);
      return;
    }
    setCopiedWorkspaceId(workspaceId);
    setCopyToastVisible(true);
    copyFadeTimeoutRef.current = setTimeout(() => setCopyToastVisible(false), 1000);
    copyClearTimeoutRef.current = setTimeout(() => {
      setCopiedWorkspaceId((current) => (current === workspaceId ? null : current));
    }, 1400);
  }, []);

  const requestHistoryPage = useCallback(() => {
    if (historyLoadPendingRef.current) return false;
    historyLoadPendingRef.current = true;
    void onLoadMore().finally(() => {
      historyLoadPendingRef.current = false;
      suppressNextButtonLoadRef.current = false;
    });
    return true;
  }, [onLoadMore]);

  const updateWindowAndLoadNearBottom = useCallback((event: UIEvent<HTMLDivElement>) => {
    const element = event.currentTarget;
    if (items.length === 0) return;
    const controlsHeight = element.querySelector<HTMLElement>(":scope > .sticky")?.offsetHeight ?? 0;
    const firstVisibleRow = Math.max(
      0,
      Math.min(items.length - 1, workspaceRowAtOffset(rowOffsets, element.scrollTop)),
    );
    const lastVisibleRow = Math.max(
      firstVisibleRow + 1,
      Math.min(
        items.length,
        workspaceRowsBeforeOffset(
          rowOffsets,
          element.scrollTop + element.clientHeight - controlsHeight,
        ),
      ),
    );
    const visiblePageStart = Math.min(
      maxPageStart,
      Math.floor(firstVisibleRow / WORKSPACE_RENDER_WINDOW_SIZE) *
        WORKSPACE_RENDER_WINDOW_SIZE,
    );
    const overscanStart = Math.max(0, firstVisibleRow - WORKSPACE_RENDER_OVERSCAN_ROWS);
    const overscanEnd = Math.min(
      items.length,
      lastVisibleRow + WORKSPACE_RENDER_OVERSCAN_ROWS,
    );
    let visibleWindowStart = visiblePageStart;
    if (overscanStart < visibleWindowStart) {
      visibleWindowStart = overscanStart;
    }
    if (overscanEnd > visibleWindowStart + WORKSPACE_RENDER_WINDOW_SIZE) {
      visibleWindowStart = overscanEnd - WORKSPACE_RENDER_WINDOW_SIZE;
    }
    visibleWindowStart = Math.max(0, Math.min(visibleWindowStart, maxWindowStart));
    setPageStart((current) => current === visiblePageStart ? current : visiblePageStart);
    setWindowStart((current) =>
      current === visibleWindowStart ? current : visibleWindowStart,
    );

    if (suppressScrollLoadRef.current) return;
    selectionOwnsScrollAnchorRef.current = false;
    if (
      preserveScrollTopRef.current !== null &&
      Math.abs(element.scrollTop - preserveScrollTopRef.current) >
        WORKSPACE_LIST_TOP_EDGE_TOLERANCE_PX
    ) {
      preserveScrollTopRef.current = null;
    }
    const remaining = element.scrollHeight - element.scrollTop - element.clientHeight;
    if (remaining > WORKSPACE_HISTORY_SCROLL_THRESHOLD_PX) {
      nearBottomTriggeredRef.current = false;
      nearBottomTriggerScrollTopRef.current = null;
      suppressNextButtonLoadRef.current = false;
      return;
    }
    const advancedSinceTrigger =
      nearBottomTriggerScrollTopRef.current !== null &&
      element.scrollTop > nearBottomTriggerScrollTopRef.current + 1;
    if (
      hasMore &&
      !loadingMore &&
      !historyLoadPendingRef.current &&
      (!nearBottomTriggeredRef.current || advancedSinceTrigger) &&
      remaining <= WORKSPACE_HISTORY_SCROLL_THRESHOLD_PX
    ) {
      preserveScrollTopRef.current = element.scrollTop;
      if (requestHistoryPage()) {
        nearBottomTriggeredRef.current = true;
        nearBottomTriggerScrollTopRef.current = element.scrollTop;
        // A locator or browser may scroll this button into view immediately
        // before dispatching its click. That click belongs to this same load.
        suppressNextButtonLoadRef.current = true;
      }
    }
  }, [hasMore, items, loadingMore, maxPageStart, maxWindowStart, requestHistoryPage, rowOffsets]);

  const showWindow = useCallback((nextStart: number) => {
    const boundedStart = Math.max(0, Math.min(nextStart, maxPageStart));
    selectionOwnsScrollAnchorRef.current = false;
    preserveScrollTopRef.current = null;
    setPageStart(boundedStart);
    setWindowStart(Math.min(boundedStart, maxWindowStart));
    scrollWithoutLoading(rowOffsets[boundedStart] ?? 0);
  }, [maxPageStart, maxWindowStart, rowOffsets, scrollWithoutLoading]);

  const loadMoreFromButton = useCallback(() => {
    // Consume a click paired with an in-flight scroll-to-button load.
    if (suppressNextButtonLoadRef.current && !historyError) {
      suppressNextButtonLoadRef.current = false;
      return;
    }
    suppressNextButtonLoadRef.current = false;
    preserveScrollTopRef.current = scrollContainerRef.current?.scrollTop ?? null;
    if (requestHistoryPage()) {
      nearBottomTriggeredRef.current = true;
      nearBottomTriggerScrollTopRef.current = scrollContainerRef.current?.scrollTop ?? null;
    }
  }, [historyError, requestHistoryPage]);

  const historyFooter = (
    <div
      className="grid gap-2 border-t border-slate-200 bg-slate-50 px-3 py-3 text-[11px] text-slate-600"
      data-testid="workspace-history-scope"
    >
      <span aria-live="polite">
        {hasMore || loadingMore || historyError
          ? `${loadedCount} loaded. More matching workspaces are available. Search and client-side filters cover loaded workspaces only.`
          : historyComplete
            ? `All ${loadedCount} matching workspaces loaded.`
            : `${loadedCount} workspaces loaded; history scope is incomplete.`}
      </span>
      {hasMore || loadingMore || historyError ? (
        <button
          type="button"
          disabled={loadingMore}
          onClick={loadMoreFromButton}
          className="inline-flex h-8 w-full items-center justify-center rounded-md border border-slate-300 bg-white px-3 text-xs text-slate-800 transition hover:bg-slate-50 disabled:opacity-50"
        >
          {loadingMore
            ? "Loading more workspaces…"
            : historyError
              ? "Retry loading older workspaces"
              : "Load more workspaces"}
        </button>
      ) : null}
    </div>
  );

  if (items.length === 0) {
    return (
      <div
        className="max-h-[calc(100vh-205px)] overflow-y-auto overflow-x-hidden [overflow-anchor:none]"
        data-testid="workspace-list-scroll"
        onScroll={updateWindowAndLoadNearBottom}
        ref={scrollContainerRef}
      >
        <div className="grid min-h-64 place-items-center p-6 text-center text-sm text-[var(--muted)]">
          <ListFilter className="mx-auto mb-3 text-slate-400" size={24} aria-hidden />
          No loaded workspaces match the current filters.
        </div>
        {historyFooter}
      </div>
    );
  }

  const selectedSet = new Set(selectedWorkspaceIds);
  const topSpacerHeight = rowOffsets[windowStart];
  const bottomSpacerHeight = rowOffsets[items.length] - rowOffsets[windowEnd];
  return (
    <div
      className="max-h-[calc(100vh-205px)] overflow-y-auto overflow-x-hidden [overflow-anchor:none]"
      data-testid="workspace-list-scroll"
      onScroll={updateWindowAndLoadNearBottom}
      ref={scrollContainerRef}
    >
      {items.length > WORKSPACE_RENDER_WINDOW_SIZE ? (
        <div className="sticky top-0 z-20 flex items-center justify-between gap-2 border-b border-slate-200 bg-white px-3 py-2 text-[11px] text-slate-600">
          <span>{pageStart + 1}–{pageEnd} of {items.length} loaded</span>
          <div className="flex gap-1">
            <button
              type="button"
              aria-label="Previous workspace results"
              disabled={pageStart === 0}
              onClick={() => showWindow(pageStart - WORKSPACE_RENDER_WINDOW_SIZE)}
              className="rounded border border-slate-300 px-2 py-1 disabled:opacity-40"
            >
              Previous
            </button>
            <button
              type="button"
              aria-label="Next workspace results"
              disabled={pageEnd >= items.length}
              onClick={() => showWindow(pageStart + WORKSPACE_RENDER_WINDOW_SIZE)}
              className="rounded border border-slate-300 px-2 py-1 disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </div>
      ) : null}
      {topSpacerHeight > 0 ? <div aria-hidden style={{ height: topSpacerHeight }} /> : null}
      <div ref={renderedWindowRef}>
        {items.slice(windowStart, windowEnd).map((item) => (
          <WorkspaceCard
            key={item.workspace_id}
            item={item}
            selected={selectedId === item.workspace_id}
            selectedForLogs={selectedSet.has(item.workspace_id)}
            showWorkspaceLogs={showWorkspaceLogs}
            copied={copiedWorkspaceId === item.workspace_id}
            copyToastVisible={copyToastVisible}
            onSelect={onSelect}
            onToggleWorkspaceSelection={onToggleWorkspaceSelection}
            onOpenDetails={onOpenDetails}
            onOpenLogs={onOpenLogs}
            onCopy={copyWorkspaceId}
          />
        ))}
      </div>
      {bottomSpacerHeight > 0 ? <div aria-hidden style={{ height: bottomSpacerHeight }} /> : null}
      {historyFooter}
    </div>
  );
}
