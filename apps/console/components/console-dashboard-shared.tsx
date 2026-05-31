"use client";

import { ExternalLink,XCircle } from "lucide-react";
import { createContext,useContext } from "react";

import { omitUndefined } from "@/lib/api-payload";
import {
bytes,
statusGlyph,
statusTone,
toneClass,
toneTextClass,
type StatusTone
} from "@/lib/format";
import type { OperatorPreferences,ResolvedOperatorTheme } from "@/lib/operator-preferences";
import {
DEFAULT_OPERATOR_PREFERENCES,
OPERATOR_PREFERENCES_STORAGE_KEY,
decodeOperatorPreferences,
encodeOperatorPreferences,
operatorPreferenceAttributes,
} from "@/lib/operator-preferences";
import type {
ApiEnvelope,
AwfStreamFrame,
CapacityDimension,
Operation,
ResourceSaturationSummary,
Workspace,
WorkspaceEvent,
WorkspaceLogRead,
WorkspaceLogStream,
WorkspaceControlWarning,
WorkspaceOperatorAction,
WorkspaceOverview,
WorkspaceRuntime,
} from "@/lib/types";

export const PanelContext = createContext<"default" | "ghost">("default");

export type CapacityUnit = "cores" | "gb" | "mb" | "slots";

const MIN_POLL_MS = 1000;
const DEFAULT_POLL_MS = 5000;
const parsedPollMs = Number.parseInt(process.env.NEXT_PUBLIC_AWF_CONSOLE_POLL_MS ?? `${DEFAULT_POLL_MS}`, 10);
export const pollMs = Number.isFinite(parsedPollMs) && Number.isInteger(parsedPollMs) && parsedPollMs > 0
  ? Math.max(MIN_POLL_MS, parsedPollMs)
  : DEFAULT_POLL_MS;
export const maxLogChars = 180_000;
export const mergeQueueLimit = 20;

export type WorkspaceSortKey = "created_at" | "updated_at";
export type SortDirection = "asc" | "desc";
export type MergeQueueStatus = "loading" | "success" | "error";

export const emptyCapacityDimension: CapacityDimension = {
  limit: null,
  reserved: 0,
  available: null,
  available_after_next_default: null,
  reason_code: null,
};

export function fallbackCapacityDimension(
  dimension: Partial<CapacityDimension> | null | undefined,
): CapacityDimension {
  return {
    limit: dimension?.limit ?? null,
    reserved: dimension?.reserved ?? 0,
    available: dimension?.available ?? null,
    available_after_next_default: dimension?.available_after_next_default ?? null,
    reason_code: dimension?.reason_code ?? null,
  };
}

export function fallbackResourceSaturation(
  saturation: Partial<ResourceSaturationSummary>,
): ResourceSaturationSummary {
  return {
    generated_at: saturation.generated_at ?? new Date().toISOString(),
    workspace_counts: {
      by_status: saturation.workspace_counts?.by_status ?? {},
      active_total: saturation.workspace_counts?.active_total ?? 0,
      requested: saturation.workspace_counts?.requested ?? 0,
      provisioning: saturation.workspace_counts?.provisioning ?? 0,
      ready: saturation.workspace_counts?.ready ?? 0,
      running: saturation.workspace_counts?.running ?? 0,
      validating: saturation.workspace_counts?.validating ?? 0,
      pushing: saturation.workspace_counts?.pushing ?? 0,
      monitoring_pr: saturation.workspace_counts?.monitoring_pr ?? 0,
      destroying: saturation.workspace_counts?.destroying ?? 0,
      completed: saturation.workspace_counts?.completed ?? 0,
      failed: saturation.workspace_counts?.failed ?? 0,
      cancelled: saturation.workspace_counts?.cancelled ?? 0,
      destroyed: saturation.workspace_counts?.destroyed ?? 0,
    },
    worker: {
      max_concurrent_provisions: saturation.worker?.max_concurrent_provisions ?? 0,
      max_concurrent_executions: saturation.worker?.max_concurrent_executions ?? 0,
    },
    local_capacity: {
      cpu_cores: saturation.local_capacity?.cpu_cores ?? null,
      memory_gb: saturation.local_capacity?.memory_gb ?? null,
      source: saturation.local_capacity?.source ?? null,
      reason_code: saturation.local_capacity?.reason_code ?? null,
      detail: saturation.local_capacity?.detail ?? null,
    },
    resource_defaults: {
      steady_cpu: saturation.resource_defaults?.steady_cpu ?? 0,
      steady_memory_gb: saturation.resource_defaults?.steady_memory_gb ?? 0,
      peak_cpu: saturation.resource_defaults?.peak_cpu ?? 0,
      peak_memory_gb: saturation.resource_defaults?.peak_memory_gb ?? 0,
    },
    reserved_resources: {
      active_workspace_count: saturation.reserved_resources?.active_workspace_count ?? 0,
      steady_cpu: saturation.reserved_resources?.steady_cpu ?? 0,
      steady_memory_gb: saturation.reserved_resources?.steady_memory_gb ?? 0,
      peak_cpu: saturation.reserved_resources?.peak_cpu ?? 0,
      peak_memory_gb: saturation.reserved_resources?.peak_memory_gb ?? 0,
      disk_mb: saturation.reserved_resources?.disk_mb ?? 0,
      dind_slots: saturation.reserved_resources?.dind_slots ?? 0,
    },
    allocated_resources: {
      active_workspace_count: saturation.allocated_resources?.active_workspace_count ?? 0,
      steady_cpu: saturation.allocated_resources?.steady_cpu ?? 0,
      steady_memory_gb: saturation.allocated_resources?.steady_memory_gb ?? 0,
      peak_cpu: saturation.allocated_resources?.peak_cpu ?? 0,
      peak_memory_gb: saturation.allocated_resources?.peak_memory_gb ?? 0,
      disk_mb: saturation.allocated_resources?.disk_mb ?? 0,
      dind_slots: saturation.allocated_resources?.dind_slots ?? 0,
    },
    capacity: {
      steady_cpu: fallbackCapacityDimension(saturation.capacity?.steady_cpu),
      peak_cpu: fallbackCapacityDimension(saturation.capacity?.peak_cpu),
      steady_memory_gb: fallbackCapacityDimension(saturation.capacity?.steady_memory_gb),
      peak_memory_gb: fallbackCapacityDimension(saturation.capacity?.peak_memory_gb),
      disk_mb: fallbackCapacityDimension(saturation.capacity?.disk_mb),
      dind_slots: fallbackCapacityDimension(saturation.capacity?.dind_slots),
      pressure_reasons: saturation.capacity?.pressure_reasons ?? [],
    },
    allocated_capacity: {
      steady_cpu: fallbackCapacityDimension(saturation.allocated_capacity?.steady_cpu),
      peak_cpu: fallbackCapacityDimension(saturation.allocated_capacity?.peak_cpu),
      steady_memory_gb: fallbackCapacityDimension(saturation.allocated_capacity?.steady_memory_gb),
      peak_memory_gb: fallbackCapacityDimension(saturation.allocated_capacity?.peak_memory_gb),
      disk_mb: fallbackCapacityDimension(saturation.allocated_capacity?.disk_mb),
      dind_slots: fallbackCapacityDimension(saturation.allocated_capacity?.dind_slots),
      pressure_reasons: saturation.allocated_capacity?.pressure_reasons ?? [],
    },
    capacity_queue: {
      queued_workspace_count: saturation.capacity_queue?.queued_workspace_count ?? 0,
      oldest_workspace_id: saturation.capacity_queue?.oldest_workspace_id ?? null,
      oldest_wait_seconds: saturation.capacity_queue?.oldest_wait_seconds ?? null,
      planned_resources: {
        steady_cpu: saturation.capacity_queue?.planned_resources?.steady_cpu ?? 0,
        steady_memory_gb: saturation.capacity_queue?.planned_resources?.steady_memory_gb ?? 0,
        peak_cpu: saturation.capacity_queue?.planned_resources?.peak_cpu ?? 0,
        peak_memory_gb: saturation.capacity_queue?.planned_resources?.peak_memory_gb ?? 0,
        disk_mb: saturation.capacity_queue?.planned_resources?.disk_mb ?? 0,
        dind_slots: saturation.capacity_queue?.planned_resources?.dind_slots ?? 0,
      },
      blocked_reason_counts: saturation.capacity_queue?.blocked_reason_counts ?? {},
    },
    concurrency: {
      provision: {
        limit: saturation.concurrency?.provision?.limit ?? 0,
        in_use: saturation.concurrency?.provision?.in_use ?? 0,
        queued: saturation.concurrency?.provision?.queued ?? 0,
        available: saturation.concurrency?.provision?.available ?? 0,
      },
      execution: {
        limit: saturation.concurrency?.execution?.limit ?? 0,
        in_use: saturation.concurrency?.execution?.in_use ?? 0,
        queued: saturation.concurrency?.execution?.queued ?? 0,
        available: saturation.concurrency?.execution?.available ?? 0,
      },
    },
    disk: {
      path: saturation.disk?.path ?? "",
      checked_path: saturation.disk?.checked_path ?? "",
      total_bytes: saturation.disk?.total_bytes ?? 0,
      used_bytes: saturation.disk?.used_bytes ?? 0,
      free_bytes: saturation.disk?.free_bytes ?? 0,
      percent_free: saturation.disk?.percent_free ?? 0,
      threshold_bytes: saturation.disk?.threshold_bytes ?? 0,
      ok: saturation.disk?.ok ?? true,
      status: saturation.disk?.status ?? "unknown",
      reason: saturation.disk?.reason ?? "DISK_UNKNOWN",
      detail: saturation.disk?.detail ?? null,
    },
    admission: {
      ok: saturation.admission?.ok ?? true,
      status: saturation.admission?.status ?? "unknown",
      reason: saturation.admission?.reason ?? "ADMISSION_UNKNOWN",
      detail: saturation.admission?.detail ?? null,
    },
  };
}

export type DetailState = {
  workspace: Workspace | null;
  runtime: WorkspaceRuntime | null;
  events: WorkspaceEvent[];
  operations: Operation[];
  streams: WorkspaceLogStream[];
};

export type LogEntry = {
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

export type LogStreamActivity = {
  byteCount: number;
  lineCount: number;
  changedAt: number;
};

export type LogStreamActivityMap = Record<string, LogStreamActivity>;

export type LogWorkspaceTarget = Pick<
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

export type RetryActionState =
  | { status: "idle" }
  | { status: "submitting" }
  | { status: "success"; newWorkspaceId: string; operationId: string }
  | { status: "error"; message: string };

export type OperatorActionState =
  | { status: "idle" }
  | { status: "submitting"; action: WorkspaceOperatorAction }
  | {
      status: "success";
      action: WorkspaceOperatorAction;
      operationId: string;
      operationStatus: string;
      message: string;
      warnings: WorkspaceControlWarning[];
    }
  | { status: "error"; action: WorkspaceOperatorAction; errorCode: string | null; message: string };

export const emptyDetail: DetailState = {
  workspace: null,
  runtime: null,
  events: [],
  operations: [],
  streams: [],
};


export function Panel({
  title,
  icon,
  action,
  stale = false,
  staleLabel = "stale",
  fill = false,
  className = "",
  children,
}: {
  title: string;
  icon: React.ReactNode;
  action?: React.ReactNode;
  stale?: boolean;
  staleLabel?: string;
  // When true the panel is a flex column so a scrollable child can grow to
  // fill the panel height (used where a grid column is stretched to a sibling).
  fill?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  const variant = useContext(PanelContext);
  const isGhost = variant === "ghost";

  return (
    <section
      className={`min-w-0 w-full max-w-full ${fill ? "flex min-h-0 flex-col " : ""}${isGhost ? "" : "rounded-[var(--radius-panel)] border border-line bg-surface"} ${className}`}
    >
      <div
        className={`flex min-h-11 min-w-0 flex-wrap items-center justify-between gap-3 ${
          isGhost ? "px-1 py-2" : "border-b border-line px-3 py-2"
        }`}
      >
        <h2 className="flex items-center gap-2 text-sm font-semibold text-fg">
          {icon}
          {title}
        </h2>
        <div className="flex min-w-0 items-center gap-2">
          {stale ? (
            <span
              title="Showing the last snapshot — live data may be stale"
              className="inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-attention-border bg-attention-soft px-1.5 py-0.5 text-[10px] font-medium text-attention-text"
            >
              <span aria-hidden>⚠</span>
              {staleLabel}
            </span>
          ) : null}
          {action}
        </div>
      </div>
      {/* Only the body dims when stale (data-awf-stale) so the header's stale
          warning badge keeps full opacity. */}
      <div
        data-awf-stale={stale ? "true" : undefined}
        className={`min-w-0 ${fill ? "flex min-h-0 flex-1 flex-col " : ""}${isGhost ? "py-3" : "p-3"}`}
      >
        {children}
      </div>
    </section>
  );
}

export function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0 rounded-[var(--radius-control)] border border-line bg-surface-2 px-3 py-2">
      <div className="label-caps">{label}</div>
      <div className={`${mono ? "mono" : "tnum"} truncate text-sm text-fg`}>{value}</div>
    </div>
  );
}

// Status as color + glyph + label (never color alone). Keeps the `Badge` name
// so existing call sites are unchanged; the rendered status text stays a
// distinct node so text-based selectors still match exactly.
export function Badge({ value }: { value: string }) {
  const tone = statusTone(value);
  return (
    <span
      className={`inline-flex h-6 shrink-0 items-center gap-1 rounded-[var(--radius-control)] border px-2 text-[11px] font-medium ${toneClass(
        tone,
      )}`}
    >
      <span aria-hidden className="text-[10px] leading-none">
        {statusGlyph(value)}
      </span>
      <span>{value}</span>
    </span>
  );
}

export function StatusDot({ status, label }: { status: string; label?: string }) {
  const tone = statusTone(status);
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className={`text-[11px] leading-none ${toneTextClass(tone)}`}>
        {statusGlyph(status)}
      </span>
      {label ? <span className="truncate">{label}</span> : null}
    </span>
  );
}

// Single-glance KPI for the Status layer: a big tabular value with optional
// tone color and a contextual hint.
export function KpiStat({
  label,
  value,
  tone,
  suffix,
  hint,
}: {
  label: string;
  value: string | number;
  tone?: StatusTone;
  suffix?: string;
  hint?: string;
}) {
  return (
    <div className="min-w-0 rounded-[var(--radius-panel)] border border-line bg-surface px-3 py-2">
      <div className="label-caps truncate">{label}</div>
      <div className={`kpi-value mt-0.5 truncate ${tone ? toneTextClass(tone) : "text-fg"}`}>
        {value}
        {suffix ? <span className="ml-0.5 text-sm font-normal text-fg-faint">{suffix}</span> : null}
      </div>
      {hint ? <div className="mt-0.5 truncate text-[11px] text-fg-muted">{hint}</div> : null}
    </div>
  );
}

export function formatPrLinkLabel(href: string, prNumber?: number | null) {
  const number = prNumber ?? extractPrNumberFromHref(href);
  return number ? `PR #${number}` : "PR";
}

export function extractPrNumberFromHref(href: string): number | null {
  const match = href.match(/\/pull\/(\d+)(?:[/?#]|$)/);
  if (!match) {
    return null;
  }
  const number = Number(match[1]);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

export function ExternalAnchor({ href, label }: { href: string; label: string }) {
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

export function SmallExternalAnchor({ href, label }: { href: string; label: string }) {
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

export function readStoredOperatorPreferences(): OperatorPreferences {
  try {
    return decodeOperatorPreferences(window.localStorage.getItem(OPERATOR_PREFERENCES_STORAGE_KEY));
  } catch {
    return { ...DEFAULT_OPERATOR_PREFERENCES };
  }
}

export function writeStoredOperatorPreferences(preferences: OperatorPreferences) {
  try {
    window.localStorage.setItem(
      OPERATOR_PREFERENCES_STORAGE_KEY,
      encodeOperatorPreferences(preferences),
    );
  } catch {
    // Storage can be unavailable in locked-down browsers; the in-memory setting still applies.
  }
}

export function applyOperatorPreferenceAttributes(
  preferences: OperatorPreferences,
  systemTheme: ResolvedOperatorTheme,
) {
  const root = document.documentElement;
  const attributes = operatorPreferenceAttributes(preferences, systemTheme);
  for (const [name, value] of Object.entries(attributes)) {
    root.setAttribute(name, value);
  }
}

export function openExternalHref(href: string) {
  try {
    const url = new URL(href, window.location.origin);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return;
    }
    window.open(url.toString(), "_blank", "noopener,noreferrer");
  } catch {
    // Ignore invalid URLs.
  }
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="m-4 flex items-start gap-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900">
      <XCircle className="mt-0.5 shrink-0" size={16} aria-hidden />
      <div>{message}</div>
    </div>
  );
}

export function MutedLine({ children }: { children: React.ReactNode }) {
  return <div className="rounded-md border border-dashed border-slate-200 p-4 text-sm text-slate-500">{children}</div>;
}

export function Th({ children }: { children: React.ReactNode }) {
  return <th className="min-w-0 overflow-hidden px-3 py-2 font-medium">{children}</th>;
}

export function Td({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <td className={`min-w-0 overflow-hidden px-3 py-2 align-top ${className}`}>{children}</td>;
}

export function formatScalar(value: number): string {
  if (!Number.isFinite(value)) {
    return "0";
  }
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 });
}

export function formatGb(value: number): string {
  return `${formatScalar(value)} GiB`;
}

export function formatCapacityValue(value: number | null, unit: CapacityUnit): string {
  if (value === null) {
    return "unknown";
  }
  if (unit === "gb") {
    return formatGb(value);
  }
  if (unit === "mb") {
    return bytes(value * 1024 * 1024);
  }
  if (unit === "slots") {
    return `${Math.round(value)} slots`;
  }
  return `${formatScalar(value)} cores`;
}

export function formatPercent(value: number): string {
  if (!Number.isFinite(value)) {
    return "0%";
  }
  return `${value.toFixed(1)}%`;
}

// Single fleet-capacity headline: the worst utilization across peak CPU, peak
// memory, and execution concurrency, clamped to 0-100.
export function capacityUtilizationPct(saturation: ResourceSaturationSummary): number {
  let pct = 0;
  const dims = [saturation.allocated_capacity.peak_cpu, saturation.allocated_capacity.peak_memory_gb];
  for (const dimension of dims) {
    if (dimension.limit && dimension.limit > 0) {
      pct = Math.max(pct, (dimension.reserved / dimension.limit) * 100);
    }
  }
  const execution = saturation.concurrency.execution;
  if (execution.limit > 0) {
    pct = Math.max(pct, (execution.in_use / execution.limit) * 100);
  }
  return Math.min(100, Math.max(0, Math.round(pct)));
}

export async function apiGet<T>(path: string): Promise<ApiEnvelope<T>> {
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

export async function apiPost<T>(path: string, body?: unknown): Promise<ApiEnvelope<T>> {
  try {
    const init: RequestInit = { method: "POST", cache: "no-store" };
    if (body !== undefined) {
      init.headers = { "content-type": "application/json" };
      init.body = JSON.stringify(omitUndefined(body));
    }
    const response = await fetch(path, init);
    return await parseApiResponse<T>(response);
  } catch (error) {
    return {
      ok: false,
      status: 0,
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

export function operatorActionPath(action: WorkspaceOperatorAction, workspaceId: string): string {
  return `/api/operator/workspaces/${encodeURIComponent(workspaceId)}/${action}`;
}

export function operatorActionReason(action: WorkspaceOperatorAction): string {
  switch (action) {
    case "remonitor":
      return "operator console remonitor";
    case "refresh":
      return "operator console refresh";
    case "revalidate":
      return "operator console revalidate";
    default:
      return "operator console";
  }
}

export function operatorIdempotencyKey(action: WorkspaceOperatorAction, workspaceId: string): string {
  const suffix =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `console:${action}:${workspaceId}:${suffix}`;
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

export type ParsedJson = { ok: true; value: unknown | null } | { ok: false };

export function parseJson(text: string): ParsedJson {
  if (!text) {
    return { ok: true, value: null };
  }
  try {
    return { ok: true, value: JSON.parse(text) as unknown };
  } catch {
    return { ok: false };
  }
}

export async function readLogTailEntry(
  workspaceId: string,
  stream: WorkspaceLogStream,
  activity = logStreamFallbackActivity(stream),
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
      occurredAt: new Date(activity).toISOString(),
      order: activity,
      kind: "tail",
    },
    nextOffset: result.data.next_offset,
  };
}

export function logStreamActivityKey(workspaceId: string, streamId: string): string {
  return `${workspaceId}:${streamId}`;
}

export function updateLogStreamActivity(
  current: LogStreamActivityMap,
  workspaceId: string,
  streams: readonly WorkspaceLogStream[],
  now = Date.now(),
): LogStreamActivityMap {
  const next = { ...current };
  const seen = new Set<string>();

  for (const stream of streams) {
    const key = logStreamActivityKey(workspaceId, stream.stream_id);
    seen.add(key);
    const previous = current[key];
    const fallback = logStreamFallbackActivity(stream, now);
    const changed =
      !previous || previous.byteCount !== stream.byte_count || previous.lineCount !== stream.line_count;
    next[key] = {
      byteCount: stream.byte_count,
      lineCount: stream.line_count,
      changedAt: !previous ? fallback : changed ? (stream.closed_at ? fallback : now) : previous.changedAt,
    };
  }

  const workspacePrefix = `${workspaceId}:`;
  for (const key of Object.keys(next)) {
    if (key.startsWith(workspacePrefix) && !seen.has(key)) {
      delete next[key];
    }
  }
  return next;
}

export function logStreamActivityFor(
  activity: LogStreamActivityMap,
  workspaceId: string,
  stream: WorkspaceLogStream,
): number {
  return activity[logStreamActivityKey(workspaceId, stream.stream_id)]?.changedAt ?? logStreamFallbackActivity(stream);
}

export function logStreamFallbackActivity(stream: WorkspaceLogStream, fallback = Date.now()): number {
  const parsed = Date.parse(stream.closed_at ?? stream.opened_at);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function isLogOutputAtTail(node: HTMLElement, direction: SortDirection): boolean {
  const thresholdPx = 8;
  if (direction === "desc") {
    return node.scrollTop <= thresholdPx;
  }
  return node.scrollHeight - node.clientHeight - node.scrollTop <= thresholdPx;
}

export function scrollLogOutputToTail(node: HTMLElement, direction: SortDirection): void {
  node.scrollTop = direction === "desc" ? 0 : node.scrollHeight;
}

export function parseFrame(raw: string): AwfStreamFrame | null {
  try {
    return JSON.parse(raw) as AwfStreamFrame;
  } catch {
    return null;
  }
}

export function mergeEvent(events: WorkspaceEvent[], event: WorkspaceEvent): WorkspaceEvent[] {
  if (events.some((item) => item.id === event.id)) {
    return events;
  }
  return [event, ...events].slice(0, 100);
}

export function workspaceFilterSummary({
  agentFilters,
  modelFilters,
  repoFilter,
  searchText,
  sortDirection,
  sortKey,
  statusFilters,
}: {
  agentFilters: string[];
  modelFilters: string[];
  repoFilter: string;
  searchText: string;
  sortDirection: SortDirection;
  sortKey: WorkspaceSortKey;
  statusFilters: string[];
}): string {
  const parts = [`${sortKey === "updated_at" ? "updated" : "created"} date ${sortDirection}`];
  if (statusFilters.length > 0) {
    parts.push(`status ${formatFilterSelection(statusFilters, "statuses")}`);
  }
  if (agentFilters.length > 0) {
    parts.push(`agent ${formatFilterSelection(agentFilters, "agents")}`);
  }
  if (modelFilters.length > 0) {
    parts.push(`model ${formatFilterSelection(modelFilters, "models")}`);
  }
  if (searchText.trim()) {
    parts.push(`search "${searchText.trim()}"`);
  }
  if (repoFilter.trim()) {
    parts.push("repo filtered");
  }
  return parts.join(" · ");
}

export function formatFilterSelection(values: string[], pluralLabel: string): string {
  if (values.length <= 2) {
    return values.join(", ");
  }
  return `${values.length} ${pluralLabel}`;
}

export function compareWorkspaceDates(
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

export function toLogWorkspaceTarget(workspaceId: string, overview: WorkspaceOverview[]): LogWorkspaceTarget {
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

export function formatTokenCount(value: number | null): string {
  if (value === null || !Number.isFinite(value)) {
    return "—";
  }
  return new Intl.NumberFormat().format(value);
}

export function toggleWorkspaceSelection(current: string[], workspaceId: string, checked: boolean): string[] {
  if (checked) {
    return current.includes(workspaceId) ? current : [...current, workspaceId];
  }
  return current.filter((item) => item !== workspaceId);
}

export function toggleStream(current: string[], streamId: string, checked: boolean): string[] {
  if (checked) {
    return current.includes(streamId) ? current : [...current, streamId];
  }
  return current.filter((item) => item !== streamId);
}

export function compareLogEntries(left: LogEntry, right: LogEntry): number {
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

function trimStreamEntries(entries: readonly LogEntry[], capacity: number): { entries: LogEntry[]; used: number } {
  const kept: LogEntry[] = [];
  if (capacity <= 0) {
    return { entries: kept, used: 0 };
  }
  let keptChars = 0;
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    const remaining = capacity - keptChars;
    if (remaining <= 0) {
      break;
    }
    if (entry.data.length <= remaining) {
      kept.push(entry);
      keptChars += entry.data.length;
      continue;
    }
    kept.push({ ...entry, data: entry.data.slice(-remaining) });
    keptChars += remaining;
    break;
  }
  return { entries: kept.reverse(), used: keptChars };
}

export function trimLogEntries(
  entries: LogEntry[],
  selectedStreamIds: readonly string[] = [],
): LogEntry[] {
  const ordered = [...entries].sort(compareLogEntries);
  if (selectedStreamIds.length === 0) {
    const trimmed = trimStreamEntries(ordered, maxLogChars);
    return trimmed.entries;
  }

  const selectedStreams = new Set(selectedStreamIds);
  const selected = ordered.filter((entry) => selectedStreams.has(entry.streamId));
  const unselected = ordered.filter((entry) => !selectedStreams.has(entry.streamId));

  const selectedTrimmed = trimStreamEntries(selected, maxLogChars);
  const unselectedTrimmed = trimStreamEntries(unselected, Math.max(0, maxLogChars - selectedTrimmed.used));

  return [...selectedTrimmed.entries, ...unselectedTrimmed.entries].sort(compareLogEntries);
}
