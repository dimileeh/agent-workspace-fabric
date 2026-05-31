"use client";

import {
Activity,
AlertCircle,
ArrowDown,
ArrowUp,
Bot,
Boxes,
CheckCircle2,
ChevronDown,
ChevronUp,
Contrast,
FileText,
HeartPulse,
ListFilter,
Loader2,
Maximize2,
Monitor,
Moon,
Radio,
RefreshCw,
Search,
Sun,
Terminal,
Type,
X
} from "lucide-react";
import {
useEffect,
useId,
useLayoutEffect,
useRef,
useState
} from "react";

import { formatAgentEffort,formatAgentLabel,formatAgentTitle } from "@/lib/agent-format";
import { summarizeVisibleCoordinationWarnings } from "@/lib/coordination-format";
import {
compactId,
fallbackLlmUsage,
formatCostWithPricing,
formatDateTime,
formatUsageProvenance,
lifecycleStages,
pricingAvailabilityReason,
relativeTime,
toneClass
} from "@/lib/format";
import {
requiredNextActionTone,
summarizeRecovery,
summarizeValidationProvenance
} from "@/lib/merge-queue-format";
import type { OperatorPreferences } from "@/lib/operator-preferences";
import { providerReadinessPreflightFacts,providerReadinessPreflightTone } from "@/lib/provider-readiness-format";
import {
formatRecoveryBadge,
formatRecoveryCallout
} from "@/lib/recovery-format";
import type {
MergeQueueItem,
PricingMetadata,
ProviderReadinessPreflight,
Workspace,
WorkspaceOperatorAction,
WorkspaceOverview,
WorkspaceStatus
} from "@/lib/types";
import type { WorkspaceOperatorControl } from "@/lib/workspace-operator-controls";
import { QueueChip,freshnessTone,mergeBlockerTone,satisfiedTierTone } from "./console-dashboard-capacity";
import {
Badge,
ExternalAnchor,
Fact,
OperatorActionState,
Panel,
RetryActionState,
SmallExternalAnchor,
SortDirection,
WorkspaceSortKey,
formatPrLinkLabel,
formatTokenCount,
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
                <button
                  type="button"
                  onClick={() => onSelect(item.workspace_id)}
                  className="grid min-w-0 flex-1 gap-2 text-left"
                >
                  <span
                    className="whitespace-normal break-words text-sm font-semibold text-slate-950"
                    data-testid={`workspace-title-${item.workspace_id}`}
                  >
                    {item.title}
                  </span>
                  <div className="mono break-all text-[11px] text-[var(--muted)]">{item.workspace_id}</div>
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
                </button>
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

export function TaskDetailsModal({
  workspace,
  onClose,
}: {
  workspace: WorkspaceOverview;
  onClose: () => void;
}) {
  const labelId = `task-details-label-${workspace.workspace_id}`;
  const titleId = `task-details-title-${workspace.workspace_id}`;

  useLayoutEffect(() => {
    const scrollY = window.scrollY;
    const previousBodyOverflow = document.body.style.overflow;
    const previousHtmlOverflow = document.documentElement.style.overflow;
    document.body.style.overflow = "hidden";
    document.documentElement.style.overflow = "hidden";
    window.scrollTo(0, scrollY);
    return () => {
      document.body.style.overflow = previousBodyOverflow;
      document.documentElement.style.overflow = previousHtmlOverflow;
      window.scrollTo(0, scrollY);
    };
  }, []);

  return (
    <div
      className="fixed inset-0 z-50 grid overflow-hidden overscroll-contain bg-slate-950/45 p-3 sm:p-6"
      role="dialog"
      aria-modal="true"
      aria-labelledby={`${labelId} ${titleId}`}
    >
      <div className="m-auto grid max-h-[calc(100dvh-1.5rem)] w-full max-w-5xl grid-rows-[auto_minmax(0,1fr)] overflow-hidden rounded-md border border-slate-300 bg-white shadow-xl sm:max-h-[calc(100dvh-3rem)]">
        <header className="flex min-h-14 items-start justify-between gap-3 border-b border-slate-200 px-4 py-3">
          <div className="min-w-0">
            <div id={labelId} className="flex items-center gap-2 text-sm font-semibold text-slate-950">
              <FileText size={16} aria-hidden />
              Task details
            </div>
            <h2 id={titleId} className="mt-1 line-clamp-2 text-base font-semibold text-slate-950">
              {workspace.title}
            </h2>
            <div className="mono mt-1 truncate text-xs text-slate-500">{workspace.workspace_id}</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-slate-300 bg-white text-slate-700 transition hover:bg-slate-50"
            aria-label="Close task details"
          >
            <X size={16} aria-hidden />
          </button>
        </header>
        <div
          className="grid min-h-0 gap-3 overflow-y-auto overscroll-contain p-4"
          data-testid="task-details-scroll"
          tabIndex={0}
        >
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <Fact label="Agent" value={formatAgentLabel(workspace)} />
            <Fact label="Effort" value={formatAgentEffort(workspace)} />
            <Fact label="Base" value={workspace.base_branch} mono />
            <Fact label="Status" value={workspace.status} />
            <Fact label="Created" value={formatDateTime(workspace.created_at)} />
            <Fact label="Updated" value={formatDateTime(workspace.updated_at)} />
            <Fact label="Repository" value={workspace.repo_url} />
            <Fact label="Branch" value={workspace.branch_name ?? "—"} mono />
          </div>
          <CoordinationWarningBlock warnings={workspace.coordination_warnings} status={workspace.status} />
          <section className="grid gap-2 rounded-md border border-slate-200 bg-slate-50 p-3">
            <div className="text-xs font-semibold text-slate-500">Prompt sent to AWF</div>
            <TaskPromptBody prompt={workspace.task_prompt} />
          </section>
        </div>
      </div>
    </div>
  );
}

export function TaskPromptBody({ prompt }: { prompt: string }) {
  const lines = prompt.trim() ? prompt.trim().split(/\r?\n/) : ["No prompt stored for this workspace."];

  return (
    <div className="grid gap-1 text-sm leading-6 text-slate-800">
      {lines.map((line, index) => {
        const trimmed = line.trim();
        const key = `${index}:${line}`;
        if (!trimmed) {
          return <div key={key} className="h-2" />;
        }
        if (trimmed.startsWith("### ")) {
          return (
            <h4 key={key} className="mt-3 text-sm font-semibold text-slate-950">
              {trimmed.slice(4)}
            </h4>
          );
        }
        if (trimmed.startsWith("## ")) {
          return (
            <h3 key={key} className="mt-4 text-base font-semibold text-slate-950">
              {trimmed.slice(3)}
            </h3>
          );
        }
        if (trimmed.startsWith("# ")) {
          return (
            <h3 key={key} className="text-base font-semibold text-slate-950">
              {trimmed.slice(2)}
            </h3>
          );
        }
        if (trimmed.startsWith("- ")) {
          return (
            <div key={key} className="grid grid-cols-[16px_minmax(0,1fr)] gap-2">
              <span className="text-slate-400">•</span>
              <span className="min-w-0 whitespace-pre-wrap break-words">{trimmed.slice(2)}</span>
            </div>
          );
        }
        if (/^\d+\.\s/.test(trimmed)) {
          const [prefix, ...rest] = trimmed.split(/\s+/);
          return (
            <div key={key} className="grid grid-cols-[28px_minmax(0,1fr)] gap-2">
              <span className="mono text-xs text-slate-400">{prefix}</span>
              <span className="min-w-0 whitespace-pre-wrap break-words">{rest.join(" ")}</span>
            </div>
          );
        }
        if (trimmed.startsWith("```")) {
          return (
            <div key={key} className="mono rounded-md bg-slate-900 px-2 py-1 text-xs text-slate-100">
              {trimmed}
            </div>
          );
        }
        return (
          <p key={key} className="whitespace-pre-wrap break-words">
            {line}
          </p>
        );
      })}
    </div>
  );
}

export function WorkspaceSummary({
  overview,
  workspace,
  mergeQueueItem,
  retryState,
  operatorControls,
  operatorActionState,
  onRetry,
  onOperatorAction,
}: {
  overview: WorkspaceOverview;
  workspace: Workspace | null;
  mergeQueueItem: MergeQueueItem | null;
  retryState: RetryActionState;
  operatorControls: WorkspaceOperatorControl[];
  operatorActionState: OperatorActionState;
  onRetry: () => void;
  onOperatorAction: (action: WorkspaceOperatorAction, requestedTier?: number) => void;
}) {
  const canRetry = overview.status === "failed" || overview.status === "cancelled";
  const recovery = workspace?.recovery ?? overview.recovery ?? null;
  const coordinationWarnings =
    workspace?.coordination_warnings ?? overview.coordination_warnings ?? [];

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
          {overview.pr_url ? (
            <ExternalAnchor href={overview.pr_url} label={formatPrLinkLabel(overview.pr_url)} />
          ) : null}
        </div>
      }
    >
      <div className="grid min-w-0 gap-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-lg font-semibold">{overview.title}</h2>
            <p className="mt-1 truncate text-sm text-[var(--muted)]">{overview.repo_url}</p>
          </div>
          <div className="flex flex-col items-end gap-1">
            <div className="flex items-center gap-1">
              <Badge value={overview.status} />
              {overview.status === "running" && overview.subphase ? (
                <span className="inline-flex h-6 items-center rounded-md border border-slate-200 bg-slate-100 px-2 text-[11px] font-medium text-slate-800">
                  ({overview.subphase})
                </span>
              ) : null}
            </div>
            {overview.is_stale_running ? (
              <span className="inline-flex h-6 items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 text-[11px] font-medium text-amber-900">
                <AlertCircle size={12} aria-hidden />
                <span>Stale execution (check logs)</span>
              </span>
            ) : null}
          </div>
        </div>
        <div className="grid min-w-0 gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <Fact label="Workspace" value={overview.workspace_id} mono />
          <Fact label="Agent" value={formatAgentLabel(overview)} />
          <Fact label="Effort" value={formatAgentEffort(overview)} />
          <Fact label="Branch" value={workspace?.branch_name ?? overview.branch_name ?? "—"} mono />
          <Fact label="Base" value={overview.base_branch} mono />
          <Fact label="Phase" value={overview.current_phase} />
          <Fact label="Operation" value={overview.active_operation ?? "none"} />
          <Fact label="Updated" value={formatDateTime(overview.updated_at)} />
        </div>
        <WorkspaceRecoveryBlock item={mergeQueueItem} workspace={workspace} overview={overview} />
        <OperatorControlsBlock
          controls={operatorControls}
          state={operatorActionState}
          onAction={onOperatorAction}
        />
        <UsageSummaryBlock
          usage={workspace?.llm_usage ?? overview.llm_usage}
          pricing={workspace?.pricing ?? overview.pricing ?? null}
        />
        <ProviderReadinessPreflightBlock
          preflight={workspace?.provider_readiness_preflight ?? overview.provider_readiness_preflight ?? null}
        />
        {recovery && overview.status !== "completed" ? (
          <RecoveryCallout recovery={recovery} status={overview.status} />
        ) : null}
        <CoordinationWarningBlock warnings={coordinationWarnings} status={overview.status} />
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

export function WorkspaceRecoveryBlock({
  item,
  workspace,
  overview,
}: {
  item: MergeQueueItem | null;
  workspace: Workspace | null;
  overview: WorkspaceOverview;
}) {
  if (!item) {
    const validation = summarizeValidationProvenance(workspace?.validation_provenance);
    return (
      <div className="grid gap-2 rounded-md border border-slate-200 bg-white p-2 text-xs">
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="font-semibold text-slate-900">Validation freshness</div>
            <div className="mono mt-0.5 truncate text-[11px] text-slate-500">
              {workspace?.branch_name ?? overview.branch_name ?? "no branch"} / {overview.base_branch}
            </div>
          </div>
          <Badge value={overview.status} />
        </div>
        <div className="grid gap-1 sm:grid-cols-2 xl:grid-cols-3">
          <QueueChip label="Required" value={validation.requiredTierLabel} />
          <QueueChip
            label="Satisfied"
            value={validation.latestSatisfiedTierLabel}
            detail={validation.latestSatisfiedTierDetail}
            tone={satisfiedTierTone(validation.latestSatisfiedTierLabel)}
          />
          <QueueChip
            label="Freshness"
            value={validation.freshnessLabel}
            detail={validation.targetRangeLabel}
            tone={freshnessTone(validation.freshnessLabel)}
          />
          <QueueChip label="Reason" value={validation.validationReasonLabel} mono />
          <QueueChip label="Command" value={validation.commandHashLabel} mono />
          <QueueChip label="Profile" value={validation.profileLabel} />
          <QueueChip label="Env" value={validation.environmentLabel} mono />
          <QueueChip label="Base" value={validation.baseShaLabel} mono />
          <QueueChip label="Workspace head" value={validation.workspaceHeadShaLabel} mono />
          <QueueChip
            label="Targets"
            value={validation.targetRangeLabel}
            detail={`${validation.validatedTargetShaLabel} / ${validation.currentTargetShaLabel}`}
            mono
          />
        </div>
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
        <QueueChip label="Workspace head" value={recovery.workspaceHeadShaLabel} mono />
        <QueueChip label="Reason" value={recovery.validationReasonLabel} mono />
        <QueueChip label="Command" value={recovery.commandHashLabel} mono />
        <QueueChip label="Profile" value={recovery.profileLabel} />
        <QueueChip label="Env" value={recovery.environmentLabel} mono />
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

export function OperatorControlsBlock({
  controls,
  state,
  onAction,
}: {
  controls: WorkspaceOperatorControl[];
  state: OperatorActionState;
  onAction: (action: WorkspaceOperatorAction, requestedTier?: number) => void;
}) {
  const visibleControls = controls.filter((control) => control.visible);
  if (visibleControls.length === 0) {
    return null;
  }

  const submittingAction = state.status === "submitting" ? state.action : null;
  const busy = submittingAction !== null;

  return (
    <div className="grid gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold text-slate-900">Operator controls</span>
        {busy ? (
          <span className="mono text-[11px] text-slate-500">{submittingAction} active</span>
        ) : null}
      </div>
      <div className="flex flex-wrap gap-2">
        {visibleControls.map((control) => {
          const submitting = submittingAction === control.action;
          const disabled = busy || !control.enabled;
          const reason = busy && !submitting ? "operation active" : control.reason;
          return (
            <div key={control.action} className="flex min-w-0 items-center gap-1.5">
              <button
                type="button"
                onClick={() => onAction(control.action, control.requestedTier)}
                disabled={disabled}
                title={reason ? `${control.label}: ${reason}` : control.label}
                className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-[11px] font-medium text-slate-800 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <OperatorControlIcon action={control.action} spinning={submitting} />
                {control.label}
              </button>
              {reason ? <span className="max-w-32 truncate text-[11px] text-slate-500">{reason}</span> : null}
            </div>
          );
        })}
      </div>
      {state.status === "success" ? (
        <div className="rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1.5 text-emerald-900">
          <span>{state.message}</span>
          <span className="mono ml-2 text-emerald-800">{compactId(state.operationId, 10)}</span>
        </div>
      ) : null}
      {state.status === "success" && state.warnings.length > 0 ? (
        <div className="grid gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 py-1.5 text-amber-900">
          {state.warnings.map((warning) => (
            <div key={`${warning.warning_code}:${warning.message}`} className="flex min-w-0 items-start gap-1.5">
              <AlertCircle className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
              <span className="min-w-0 break-words">{warning.message || warning.warning_code}</span>
            </div>
          ))}
        </div>
      ) : null}
      {state.status === "error" ? (
        <div className="rounded-md border border-red-200 bg-red-50 px-2 py-1.5 text-red-900">
          {state.message}
        </div>
      ) : null}
    </div>
  );
}

export function OperatorControlIcon({
  action,
  spinning,
}: {
  action: WorkspaceOperatorAction;
  spinning: boolean;
}) {
  if (spinning) {
    return <Loader2 size={13} className="animate-spin" aria-hidden />;
  }
  if (action === "remonitor") {
    return <Radio size={13} aria-hidden />;
  }
  if (action === "refresh") {
    return <RefreshCw size={13} aria-hidden />;
  }
  return <CheckCircle2 size={13} aria-hidden />;
}

export function UsageSummaryBlock({
  usage,
  pricing,
}: {
  usage: Workspace["llm_usage"] | null | undefined;
  pricing: PricingMetadata | null | undefined;
}) {
  const safeUsage = fallbackLlmUsage(usage);
  const pricingReason = pricingAvailabilityReason(pricing);
  const showCost = safeUsage.cost_estimate !== null;

  if (
    safeUsage.status === "unavailable" ||
    (
      safeUsage.input_tokens == null &&
      safeUsage.cached_input_tokens == null &&
      safeUsage.output_tokens == null &&
      safeUsage.reasoning_output_tokens == null &&
      safeUsage.total_tokens == null &&
      safeUsage.cost_estimate == null
    )
  ) {
    return (
      <div data-testid="llm-usage" className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
        <div className="flex items-center justify-between gap-2">
          <span className="font-semibold text-slate-900">LLM usage</span>
          <Badge value="unavailable" />
        </div>
        <div className="mt-2 truncate text-[11px] text-slate-500">
          {formatUsageProvenance(safeUsage.source, safeUsage.reason)}
        </div>
      </div>
    );
  }
  return (
    <div data-testid="llm-usage" className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold text-slate-900">LLM usage</span>
        <div className="flex items-center gap-1.5">
          {pricing ? (
            <span
              className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${pricing.is_current ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-700"}`}
              title={`${pricing.provider} / ${pricing.model} — ${pricing.unit}`}
            >
              {pricing.provider} / {pricing.model}
            </span>
          ) : null}
          <Badge value={safeUsage.status} />
        </div>
      </div>
      <div className="mt-2 grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
        {safeUsage.input_tokens != null && <UsageMetric label="Input" value={formatTokenCount(safeUsage.input_tokens)} />}
        {safeUsage.cached_input_tokens != null && <UsageMetric label="Cached" value={formatTokenCount(safeUsage.cached_input_tokens)} />}
        {safeUsage.output_tokens != null && <UsageMetric label="Output" value={formatTokenCount(safeUsage.output_tokens)} />}
        {safeUsage.reasoning_output_tokens != null && <UsageMetric label="Reasoning" value={formatTokenCount(safeUsage.reasoning_output_tokens)} />}
        {safeUsage.total_tokens != null && <UsageMetric label="Total" value={formatTokenCount(safeUsage.total_tokens)} />}
        <UsageMetric
          label="Cost"
          value={
            showCost
              ? formatCostWithPricing(safeUsage.cost_estimate, safeUsage.currency, pricing, safeUsage.source)
              : "—"
          }
        />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-500">
        {formatUsageProvenance(safeUsage.source, safeUsage.reason)}
        {pricingReason && !showCost ? (
          <>
            <span className="text-slate-300">|</span>
            <span className="text-amber-600">{pricingReason}</span>
          </>
        ) : null}
      </div>
    </div>
  );
}

export function ProviderReadinessPreflightBlock({
  preflight,
}: {
  preflight: ProviderReadinessPreflight | null;
}) {
  if (!preflight) {
    return null;
  }
  const tone = providerReadinessPreflightTone(preflight);
  const facts = providerReadinessPreflightFacts(preflight, {
    formatCheckedAt: relativeTime,
  });
  return (
    <div className={`rounded-md border px-3 py-2 text-xs ${toneClass(tone)}`}>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <span className="font-semibold text-slate-900">Provider preflight</span>
        <span className="mono truncate text-[11px]">{preflight.reason_code}</span>
      </div>
      <div className="mt-2 grid gap-1 sm:grid-cols-2 xl:grid-cols-4">
        {facts.map((fact) => (
          <QueueChip
            key={fact.label}
            label={fact.label}
            value={fact.value}
            detail={fact.detail}
            mono={fact.mono}
            tone={fact.tone}
          />
        ))}
      </div>
      {preflight.override_reason ? (
        <div className="mt-2 truncate text-[11px] text-slate-600">
          override: {preflight.override_reason}
        </div>
      ) : null}
    </div>
  );
}

export function RecoveryCallout({
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

export function CoordinationWarningBlock({
  warnings,
  status,
}: {
  warnings: Workspace["coordination_warnings"] | WorkspaceOverview["coordination_warnings"];
  status: WorkspaceStatus;
}) {
  const summary = summarizeVisibleCoordinationWarnings(warnings, status);
  if (summary.count === 0) {
    return null;
  }
  return (
    <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2 font-semibold">
          <AlertCircle size={14} aria-hidden />
          <span className="truncate">Coordination</span>
        </div>
        <span className="rounded-md border border-amber-300 bg-white/70 px-2 py-0.5 text-[11px] text-amber-900">
          {summary.label}
        </span>
      </div>
      <div className="mt-2 text-xs text-amber-900">{summary.detail}</div>
    </div>
  );
}

export function UsageMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] font-medium text-slate-500">{label}</div>
      <div className="mono truncate text-sm text-slate-950">{value}</div>
    </div>
  );
}
