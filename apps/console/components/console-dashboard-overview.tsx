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
useEffect,
useId,
useRef,
useState
} from "react";

import { formatAgentLabel,formatAgentTitle } from "@/lib/agent-format";
import { copyTextToClipboard } from "@/lib/clipboard";
import { blockedAgeSeconds, blockedSince } from "@/lib/blocked-format";
import { summarizeVisibleCoordinationWarnings } from "@/lib/coordination-format";
import {
compactDuration,
compactId,
formatDateTime,
lifecycleStages,
relativeTime,
toneClass,
type StatusTone
} from "@/lib/format";
import type { OperatorPreferences } from "@/lib/operator-preferences";
import {
formatRecoveryBadge
} from "@/lib/recovery-format";
import type {
WorkspaceOverview
} from "@/lib/types";
import {
Badge,
KpiStat,
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
export function FleetHealthStrip({ kpis }: { kpis: FleetKpi[] }) {
  const anyStale = kpis.some((kpi) => kpi.stale);
  return (
    <div className="border-b border-line bg-canvas px-4 py-3" aria-label="Fleet health">
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

const NAV_ITEMS: { id: string; label: string; icon: typeof ListTree }[] = [
  { id: "awf-workspaces", label: "Workspaces", icon: ListTree },
  { id: "awf-capacity", label: "Capacity", icon: Server },
  { id: "awf-merge-queue", label: "Merge queue", icon: GitPullRequest },
  { id: "awf-failures", label: "Failures", icon: AlertTriangle },
];

// Section jump-nav. On wide screens the panels sit side by side and need no
// navigation; on narrow screens they stack into one tall column, so this sticky
// bar (narrow-only) lets operators jump straight to a section.
export function SectionNav() {
  const go = (id: string) => document.getElementById(id)?.scrollIntoView({ block: "start" });
  return (
    <nav
      aria-label="Jump to section"
      className="sticky top-0 z-30 flex gap-2 overflow-x-auto border-b border-line bg-canvas px-4 py-2 xl:hidden"
    >
      {NAV_ITEMS.map((item) => {
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
  const agentOptions = ["codex", "claude_code", "gemini", "opencode"];
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

export function WorkspaceList({
  items,
  selectedId,
  selectedWorkspaceIds,
  onSelect,
  onToggleWorkspaceSelection,
  onOpenDetails,
  onOpenLogs,
}: {
  items: WorkspaceOverview[];
  selectedId: string | null;
  selectedWorkspaceIds: string[];
  onSelect: (workspaceId: string) => void;
  onToggleWorkspaceSelection: (workspaceId: string, checked: boolean) => void;
  onOpenDetails: (workspaceId: string) => void;
  onOpenLogs: (workspaceId: string) => void;
}) {
  const [copiedWorkspaceId, setCopiedWorkspaceId] = useState<string | null>(null);
  const [copyToastVisible, setCopyToastVisible] = useState(false);
  const copyFadeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const copyClearTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (copyFadeTimeoutRef.current !== null) {
        clearTimeout(copyFadeTimeoutRef.current);
      }
      if (copyClearTimeoutRef.current !== null) {
        clearTimeout(copyClearTimeoutRef.current);
      }
    };
  }, []);

  const copyWorkspaceId = async (event: SyntheticEvent<HTMLElement>, workspaceId: string) => {
    event.preventDefault();
    event.stopPropagation();
    // Clear any pending timers upfront so every copy attempt starts from a clean
    // state — otherwise stale timers from a prior successful copy could fire during
    // the await or after a failed attempt and unexpectedly mutate the toast state.
    if (copyFadeTimeoutRef.current !== null) {
      clearTimeout(copyFadeTimeoutRef.current);
      copyFadeTimeoutRef.current = null;
    }
    if (copyClearTimeoutRef.current !== null) {
      clearTimeout(copyClearTimeoutRef.current);
      copyClearTimeoutRef.current = null;
    }
    // Use the fallback-aware helper: navigator.clipboard is unavailable over plain
    // HTTP (e.g. a Tailscale address), so it falls back to execCommand there.
    const copied = await copyTextToClipboard(workspaceId);
    if (!copied) {
      setCopiedWorkspaceId(null);
      setCopyToastVisible(false);
      return;
    }
    setCopiedWorkspaceId(workspaceId);
    setCopyToastVisible(true);
    copyFadeTimeoutRef.current = setTimeout(() => {
      setCopyToastVisible(false);
    }, 1000);
    copyClearTimeoutRef.current = setTimeout(() => {
      setCopiedWorkspaceId((current) => (current === workspaceId ? null : current));
    }, 1400);
  };

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
        const recoveryBadge = formatRecoveryBadge(item.recovery, item.status);
        const coordinationSummary = summarizeVisibleCoordinationWarnings(item.coordination_warnings, item.status);
        // Blocked-age proxy for the list: the overview response carries no
        // block_state, so derive "blocked for N" from the blocked transition
        // event (or updated_at). The inspector shows the precise blocked_at.
        const blockedFor =
          item.status === "blocked" ? blockedAgeSeconds(blockedSince(item)) : null;
        return (
          <div
            key={item.workspace_id}
            data-testid={`workspace-card-${item.workspace_id}`}
            className={`grid min-w-0 gap-2 border-b border-slate-100 px-3 py-3 transition hover:bg-slate-50 ${
              selectedId === item.workspace_id ? "bg-blue-50" : "bg-white"
            }`}
          >
            <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
              <div className="flex min-w-0 items-start gap-2">
                <input
                  type="checkbox"
                  checked={selectedSet.has(item.workspace_id)}
                  onChange={(event) => onToggleWorkspaceSelection(item.workspace_id, event.target.checked)}
                  aria-label={`Select ${item.title} for fullscreen logs`}
                  className="mt-0.5 h-4 w-4 shrink-0 rounded border-slate-300"
                />
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
                    <span className="relative inline-flex min-w-0 items-center gap-1.5">
                      <button
                        type="button"
                        onClick={(event) => void copyWorkspaceId(event, item.workspace_id)}
                        className="workspace-id-copy pointer-events-auto focus:outline-none focus-visible:rounded-sm focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]"
                        aria-label={`Copy workspace id ${item.workspace_id}`}
                        title="Copy workspace id"
                      >
                        {item.workspace_id}
                      </button>
                      {copiedWorkspaceId === item.workspace_id ? (
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
                    <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
                      <span>created {formatDateTime(item.created_at)}</span>
                      <span>updated {formatDateTime(item.updated_at)}</span>
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
