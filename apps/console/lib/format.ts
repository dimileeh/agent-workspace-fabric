import type {
  LlmUsageSummary,
  PricingMetadata,
  ResourceSaturationSummary,
  WorkspaceLifecycleStage,
  WorkspaceStatus,
} from "@/lib/types";

export type LogSortDirection = "asc" | "desc";

export type RenderableLogEntry = {
  streamId: string;
  fd?: string | null;
  data: string;
  occurredAt: string;
};

export type SelectableLogStream = {
  stream_id: string;
};

export const lifecycleStages: WorkspaceStatus[] = [
  "requested",
  "provisioning",
  "ready",
  "running",
  "validating",
  "pushing",
  // `blocked` is the non-terminal operator-pause state (protected-file violation).
  // It sits just before monitoring_pr — the push→monitor boundary where both the
  // pre-PR and post-PR pauses cluster — so the status filter and fallback progress
  // bar treat it as in-flight, not terminal.
  "blocked",
  "monitoring_pr",
  "completed",
];

export function fallbackLifecycleStages(
  status: WorkspaceStatus,
  terminalSourceStage?: string | null,
): WorkspaceLifecycleStage[] {
  const activeIndex = lifecycleStages.indexOf(status);
  const terminal = status === "failed" || status === "cancelled";
  const terminalSourceIndex = lifecycleStages.indexOf(terminalSourceStage as WorkspaceStatus);
  const completedThroughIndex = terminal ? Math.max(-1, terminalSourceIndex) : activeIndex - 1;

  return lifecycleStages.map((stage, index): WorkspaceLifecycleStage => {
    let stageStatus: WorkspaceLifecycleStage["status"];
    if (terminal) {
      stageStatus = index <= completedThroughIndex ? "completed" : "terminal_skipped";
    } else if (stage === status) {
      stageStatus = "active";
    } else if (index < activeIndex) {
      stageStatus = "completed";
    } else {
      stageStatus = "pending";
    }

    return {
      stage,
      started_at: null,
      ended_at: null,
      duration_seconds: null,
      status: stageStatus,
    };
  });
}

export function fallbackLlmUsage(
  usage?: Partial<LlmUsageSummary> | null,
): LlmUsageSummary {
  const hasReason = usage !== undefined && usage !== null && "reason" in usage;
  return {
    input_tokens: usage?.input_tokens ?? null,
    cached_input_tokens: usage?.cached_input_tokens ?? null,
    output_tokens: usage?.output_tokens ?? null,
    reasoning_output_tokens: usage?.reasoning_output_tokens ?? null,
    total_tokens: usage?.total_tokens ?? null,
    cost_estimate: usage?.cost_estimate ?? null,
    currency: usage?.currency ?? null,
    status: usage?.status === "available" ? "available" : "unavailable",
    source: usage?.source ?? "none",
    reason: hasReason ? usage.reason ?? null : "usage_not_reported",
  };
}

const USAGE_SOURCE_LABELS: Record<string, string> = {
  ccusage: "ccusage",
  operations: "operations",
  none: "none",
};

const USAGE_REASON_LABELS: Record<string, string> = {
  usage_not_reported: "not reported",
  ccusage_source_unsupported: "provider not supported by ccusage",
  ccusage_unavailable: "ccusage not installed",
  ccusage_command_failed: "ccusage command failed",
  ccusage_timeout: "ccusage timed out",
  ccusage_invalid_json: "ccusage output unreadable",
  ccusage_no_records: "no usage recorded yet",
  // compute_cost_estimate reason codes surfaced via usage_payload when AWF
  // pricing can't derive a cost (workspace_observability.py).
  pricing_not_configured: "pricing not configured",
  pricing_stale: "pricing stale",
  pricing_rates_unavailable: "pricing rates unavailable",
  no_token_data: "no token data",
  negative_token_count: "invalid token count",
  unsupported_pricing_unit: "unsupported pricing unit",
};

// Friendly provenance line for the LLM usage block: maps the AWF usage source
// and reason code to human-readable labels without redesigning the dashboard.
export function formatUsageProvenance(
  source: string | null | undefined,
  reason: string | null | undefined,
): string {
  const sourceLabel = (source && USAGE_SOURCE_LABELS[source]) || source || "none";
  if (!reason) {
    return sourceLabel;
  }
  const reasonLabel = USAGE_REASON_LABELS[reason] ?? reason;
  return `${sourceLabel} / ${reasonLabel}`;
}

export function formatCostWithPricing(
  cost: number | null,
  currency: string | null | undefined,
  pricing: PricingMetadata | null | undefined,
  source?: string | null,
): string {
  if (cost === null || cost === undefined) {
    return "—";
  }
  const c = currency || pricing?.currency || "USD";
  let formatted: string;
  try {
    formatted = new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: c,
      minimumFractionDigits: 4,
      maximumFractionDigits: 4,
    }).format(cost);
  } catch {
    formatted = new Intl.NumberFormat(undefined, {
      minimumFractionDigits: 4,
      maximumFractionDigits: 4,
    }).format(cost);
  }
  if (pricing && !pricing.is_current && source !== "ccusage") {
    return `${formatted} (stale pricing)`;
  }
  return formatted;
}

export function pricingAvailabilityReason(
  pricing: PricingMetadata | null | undefined,
): string | null {
  if (!pricing) {
    return "pricing not configured";
  }
  if (!pricing.is_current) {
    return "pricing stale";
  }
  return null;
}

export function compactId(value: string | null | undefined, head = 8): string {
  if (!value) {
    return "—";
  }
  if (value.length <= head + 4) {
    return value;
  }
  return `${value.slice(0, head)}…${value.slice(-4)}`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  const diff = date.getTime() - Date.now();
  const abs = Math.abs(diff);
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["day", 86_400_000],
    ["hour", 3_600_000],
    ["minute", 60_000],
    ["second", 1_000],
  ];
  const [unit, size] = units.find(([, unitSize]) => abs >= unitSize) ?? ["second", 1_000];
  return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(
    Math.round(diff / size),
    unit,
  );
}

export function compactDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return "—";
  }
  const totalSeconds = Math.max(0, Math.round(seconds));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainingSeconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${remainingSeconds}s`;
  }
  return `${remainingSeconds}s`;
}

// Single fleet-capacity headline: the worst utilization across every allocated
// capacity dimension (CPU, memory, disk, DinD slots) and both concurrency lanes
// (provision, execution), clamped to 0-100. Any dimension flagged saturated via
// a *_SATURATED pressure reason counts as fully utilized, so the strip cannot
// read healthy while a hard constraint the capacity panel already shows as
// saturated has no headroom left. Returns null when no limit is comparable, so
// the headline can show "unknown" rather than a misleading 0%.
export function capacityUtilizationPct(saturation: ResourceSaturationSummary): number | null {
  let pct = 0;
  let known = false;
  const allocated = saturation.allocated_capacity;
  const dimensions = [
    allocated.steady_cpu,
    allocated.peak_cpu,
    allocated.steady_memory_gb,
    allocated.peak_memory_gb,
    allocated.disk_mb,
    allocated.dind_slots,
  ];
  for (const dimension of dimensions) {
    if (dimension && dimension.limit && dimension.limit > 0) {
      known = true;
      pct = Math.max(pct, (dimension.reserved / dimension.limit) * 100);
    }
  }
  for (const lane of [saturation.concurrency.provision, saturation.concurrency.execution]) {
    if (lane && lane.limit > 0) {
      known = true;
      pct = Math.max(pct, (lane.in_use / lane.limit) * 100);
    }
  }
  // Mirror the capacity panel: fall back to capacity.pressure_reasons when the
  // allocated list is empty.
  const pressureReasons =
    allocated.pressure_reasons && allocated.pressure_reasons.length > 0
      ? allocated.pressure_reasons
      : (saturation.capacity?.pressure_reasons ?? []);
  if (pressureReasons.some((reason) => reason.endsWith("_SATURATED"))) {
    known = true;
    pct = Math.max(pct, 100);
  }
  // The scheduler is actively refusing new work (disk threshold breached or
  // admission blocked, e.g. INSUFFICIENT_DISK) → capacity is effectively
  // exhausted regardless of how much workspace disk is reserved.
  if (saturation.disk?.ok === false || saturation.admission?.ok === false) {
    known = true;
    pct = Math.max(pct, 100);
  }
  // Admission flagged saturated (not yet blocking) is a warning the capacity
  // panel already surfaces; reflect it as at-least-warn pressure (>=75%) so the
  // headline can't read healthy while admission is visibly saturated.
  if (saturation.admission?.status === "saturated") {
    known = true;
    pct = Math.max(pct, 75);
  }
  if (!known) {
    return null;
  }
  return Math.min(100, Math.max(0, Math.round(pct)));
}

export function bytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB"];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

export function renderLogEntries(
  entries: RenderableLogEntry[],
  direction: LogSortDirection,
): string {
  return entries.map((entry) => renderLogEntry(entry, direction)).join("\n\n");
}

export function pickWorkspaceLogStreams(
  streams: readonly SelectableLogStream[],
  current: readonly string[],
): string[] {
  const available = new Set(streams.map((stream) => stream.stream_id));
  const retained = current.filter((streamId) => available.has(streamId));
  if (retained.length > 0) {
    return retained;
  }
  return streams.map((stream) => stream.stream_id);
}

export function renderLogEntry(
  entry: RenderableLogEntry,
  direction: LogSortDirection,
): string {
  const stamp = formatLogStamp(entry.occurredAt);
  const stream = entry.fd ? `${entry.streamId} ${entry.fd}` : entry.streamId;
  const header = `[${stamp}] ${stream}`;
  const data = orderLogData(entry.data, direction);
  return data ? `${header}\n${data}` : header;
}

function orderLogData(data: string, direction: LogSortDirection): string {
  const trimmed = data.endsWith("\n") ? data.slice(0, -1) : data;
  if (direction === "asc" || !trimmed) {
    return trimmed;
  }
  return reverseLogLines(trimmed);
}

function reverseLogLines(data: string): string {
  let reversed = "";
  let wroteLine = false;
  let end = data.length;

  while (true) {
    const lineStart = end > 0 ? data.lastIndexOf("\n", end - 1) : -1;
    if (wroteLine) {
      reversed += "\n";
    }
    reversed += data.slice(lineStart + 1, end);
    wroteLine = true;

    if (lineStart === -1) {
      return reversed;
    }
    end = lineStart;
  }
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

export type StatusTone = "neutral" | "info" | "good" | "warn" | "bad";

export function statusTone(status: string): StatusTone {
  if (status === "completed" || status === "succeeded" || status === "healthy") {
    return "good";
  }
  if (status === "failed" || status === "destroyed" || status === "error" || status === "dead") {
    return "bad";
  }
  // `blocked` is operator-attention (awaiting a guide decision), distinct from the
  // `info` in-flight states and the `bad` failure states.
  if (
    status === "cancelled" ||
    status === "destroying" ||
    status === "unhealthy" ||
    status === "blocked"
  ) {
    return "warn";
  }
  if (
    ["running", "validating", "pushing", "monitoring_pr", "provisioning", "ready"].includes(status)
  ) {
    return "info";
  }
  return "neutral";
}

// Status colors are explicit CSS classes (see globals.css `.tone-*`), not
// Tailwind utilities — Tailwind does not reliably scan class names composed in
// this file, so utility strings here would silently fail to generate.
export function toneClass(tone: StatusTone): string {
  return `tone-${tone}`;
}

export function toneFillClass(tone: StatusTone): string {
  return `tone-fill-${tone}`;
}

export function toneTextClass(tone: StatusTone): string {
  return `tone-text-${tone}`;
}

// Glyph paired with every status badge/dot so state is never conveyed by color
// alone (ISA-101 / WCAG). Shape + label + color together.
export function statusGlyph(status: string): string {
  switch (status) {
    case "completed":
    case "succeeded":
    case "healthy":
      return "✓";
    case "failed":
    case "error":
    case "dead":
    case "destroyed":
      return "✕";
    case "cancelled":
      return "⊘";
    case "blocked":
      // Pause glyph — a distinct shape (used nowhere else) so the operator-pause
      // state reads as "halted, awaiting you" without relying on color alone.
      return "⏸";
    case "destroying":
      return "◌";
    case "monitoring_pr":
      return "◆";
    case "running":
    case "validating":
    case "pushing":
      return "●";
    case "requested":
    case "provisioning":
    case "ready":
      return "◷";
    default:
      return "•";
  }
}
