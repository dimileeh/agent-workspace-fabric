import { capacityUtilizationPct } from "./format.ts";
import type {
  ConsoleDashboardSummary,
  ResourceSaturationSummary,
} from "./types.ts";

const DASH = "—";

export type SummaryFleetKpi = {
  id: string;
  label: string;
  value: string | number;
  tone?: "info" | "warn" | "bad" | "good";
  suffix?: string;
  hint?: string;
  stale?: boolean;
};

function displayCount(value: number | null | undefined): string | number {
  // Null ≠ zero: incomplete/unknown counts render as an em dash.
  if (value == null) {
    return DASH;
  }
  return value;
}

export function fleetKpisFromDashboardSummary(options: {
  summary: ConsoleDashboardSummary | null;
  summaryStale: boolean;
  saturation: ResourceSaturationSummary | null;
  saturationStale: boolean;
  showCapacity: boolean;
}): SummaryFleetKpi[] {
  const { summary, summaryStale, saturation, saturationStale, showCapacity } = options;
  const counts = summary?.counts ?? null;
  const windowHint = summary ? `last ${summary.window.since_hours}h` : undefined;
  const capacity = showCapacity && saturation ? capacityUtilizationPct(saturation) : null;

  const kpis: SummaryFleetKpi[] = [
    {
      id: "active",
      label: "Active",
      value: displayCount(counts?.active),
      stale: summaryStale,
    },
    {
      id: "running",
      label: "Running",
      value: displayCount(counts?.executing),
      tone: counts?.executing ? "info" : undefined,
      stale: summaryStale,
    },
    {
      id: "monitoring_pr",
      label: "Monitoring PR",
      value: displayCount(counts?.monitoring_pr),
      tone: counts?.monitoring_pr ? "info" : undefined,
      stale: summaryStale,
    },
    {
      id: "blocked",
      label: "Awaiting operator",
      value: displayCount(counts?.awaiting_operator),
      tone: counts?.awaiting_operator ? "warn" : undefined,
      stale: summaryStale,
    },
    {
      id: "recovering",
      label: "Auto-retrying",
      value: displayCount(counts?.retrying),
      tone: counts?.retrying ? "info" : undefined,
      stale: summaryStale,
    },
    {
      id: "awaiting_human",
      label: "Awaiting human",
      value: displayCount(counts?.awaiting_human),
      tone: counts?.awaiting_human ? "warn" : undefined,
      stale: summaryStale,
    },
    {
      id: "queued",
      label: "Queued",
      value: displayCount(counts?.queued),
      tone: counts?.queued ? "warn" : undefined,
      hint:
        counts?.queued != null && counts.queued > 0
          ? "awaiting capacity"
          : undefined,
      stale: summaryStale,
    },
    {
      id: "completed",
      label: "Completed",
      value: displayCount(counts?.completed_last_window),
      tone: counts?.completed_last_window ? "good" : undefined,
      hint: counts?.completed_last_window != null ? windowHint : undefined,
      stale: summaryStale,
    },
    {
      id: "cancelled",
      label: "Cancelled",
      value: displayCount(counts?.cancelled_last_window),
      tone: counts?.cancelled_last_window ? "warn" : undefined,
      hint: counts?.cancelled_last_window != null ? windowHint : undefined,
      stale: summaryStale,
    },
    {
      id: "failed",
      label: "Failed",
      value: displayCount(counts?.failed_last_window),
      tone: counts?.failed_last_window ? "bad" : undefined,
      hint: counts?.failed_last_window != null ? windowHint : undefined,
      stale: summaryStale,
    },
  ];

  if (showCapacity) {
    kpis.push({
      id: "capacity",
      label: "Capacity",
      value: capacity ?? DASH,
      suffix: capacity != null ? "%" : undefined,
      tone:
        capacity != null
          ? capacity >= 90
            ? "bad"
            : capacity >= 75
              ? "warn"
              : undefined
          : undefined,
      stale: saturationStale,
    });
  }

  return kpis;
}

export function parseDashboardSummary(payload: unknown): ConsoleDashboardSummary | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const record = payload as ConsoleDashboardSummary;
  if (record.schema_version !== 1 || !record.counts) {
    return null;
  }
  return record;
}
