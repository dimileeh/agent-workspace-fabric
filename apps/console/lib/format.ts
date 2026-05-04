import type {
  LlmUsageSummary,
  PricingMetadata,
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

export const lifecycleStages: WorkspaceStatus[] = [
  "requested",
  "provisioning",
  "ready",
  "running",
  "validating",
  "pushing",
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
  const completedThroughIndex = terminal ? Math.max(0, terminalSourceIndex) : activeIndex - 1;

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
    output_tokens: usage?.output_tokens ?? null,
    total_tokens: usage?.total_tokens ?? null,
    cost_estimate: usage?.cost_estimate ?? null,
    currency: usage?.currency ?? null,
    status: usage?.status === "available" ? "available" : "unavailable",
    source: usage?.source ?? "none",
    reason: hasReason ? usage.reason ?? null : "usage_not_reported",
  };
}

export function formatCostWithPricing(
  cost: number | null,
  currency: string | null | undefined,
  pricing: PricingMetadata | null | undefined,
): string {
  if (cost === null || cost === undefined) {
    if (pricing) {
      return "—";
    }
    return "—";
  }
  if (!pricing) {
    return "—";
  }
  if (!pricing.is_current) {
    return "—";
  }
  const c = currency || pricing.currency || "USD";
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: c,
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  }).format(cost);
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
  return trimmed.split("\n").reverse().join("\n");
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

export function statusTone(status: string): "neutral" | "info" | "good" | "warn" | "bad" {
  if (status === "completed" || status === "succeeded" || status === "healthy") {
    return "good";
  }
  if (status === "failed" || status === "destroyed" || status === "error" || status === "dead") {
    return "bad";
  }
  if (status === "cancelled" || status === "destroying" || status === "unhealthy") {
    return "warn";
  }
  if (
    ["running", "validating", "pushing", "monitoring_pr", "provisioning", "ready"].includes(status)
  ) {
    return "info";
  }
  return "neutral";
}

export function toneClass(tone: ReturnType<typeof statusTone>): string {
  switch (tone) {
    case "good":
      return "border-emerald-200 bg-emerald-50 text-emerald-800";
    case "warn":
      return "border-amber-200 bg-amber-50 text-amber-800";
    case "bad":
      return "border-red-200 bg-red-50 text-red-800";
    case "info":
      return "border-blue-200 bg-blue-50 text-blue-800";
    default:
      return "border-slate-200 bg-slate-50 text-slate-700";
  }
}

export function toneFillClass(tone: ReturnType<typeof statusTone>): string {
  switch (tone) {
    case "good":
      return "bg-emerald-500";
    case "warn":
      return "bg-amber-500";
    case "bad":
      return "bg-red-500";
    case "info":
      return "bg-blue-500";
    default:
      return "bg-slate-400";
  }
}
