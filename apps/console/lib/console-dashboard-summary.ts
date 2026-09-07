import { capacityUtilizationPct } from "./format.ts";
import type {
  ConsoleDashboardSummary,
  ResourceSaturationSummary,
} from "./types.ts";

const DASH = "—";

/** True only when `value` parses to a finite epoch ms (rejects "", "not-a-date", etc.). */
function isFiniteTimestampString(value: string): boolean {
  return value.length > 0 && Number.isFinite(Date.parse(value));
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isNullableNonNegativeInteger(value: unknown): value is number | null {
  return value === null || isNonNegativeInteger(value);
}

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
  const record = payload as Record<string, unknown>;
  if (record.schema_version !== 1) {
    return null;
  }
  if (record.scope !== "local" && record.scope !== "tenant") {
    return null;
  }
  for (const key of ["generated_at", "as_of", "last_success_at"] as const) {
    if (typeof record[key] !== "string" || !isFiniteTimestampString(record[key])) {
      return null;
    }
  }
  if (!record.window || typeof record.window !== "object" || Array.isArray(record.window)) {
    return null;
  }
  const window = record.window as Record<string, unknown>;
  if (
    window.anchor !== "generated_at" ||
    typeof window.since_hours !== "number" ||
    !Number.isInteger(window.since_hours) ||
    window.since_hours < 1 ||
    typeof window.start !== "string" ||
    !isFiniteTimestampString(window.start)
  ) {
    return null;
  }
  if (!record.coverage || typeof record.coverage !== "object" || Array.isArray(record.coverage)) {
    return null;
  }
  const coverage = record.coverage as Record<string, unknown>;
  if (
    coverage.status !== "complete" &&
    coverage.status !== "partial" &&
    coverage.status !== "unknown"
  ) {
    return null;
  }
  // OpenAPI/Python mark notes optional (default []); omit → []. Present but
  // non-array or non-string items are malformed and fail closed.
  let notes: string[];
  if (!("notes" in coverage) || coverage.notes === undefined) {
    notes = [];
  } else if (!Array.isArray(coverage.notes) || !coverage.notes.every((n) => typeof n === "string")) {
    return null;
  } else {
    notes = coverage.notes as string[];
  }
  if (!record.counts || typeof record.counts !== "object" || Array.isArray(record.counts)) {
    return null;
  }
  const counts = record.counts as Record<string, unknown>;
  const requiredCountKeys = [
    "active",
    "executing",
    "monitoring_pr",
    "awaiting_operator",
    "awaiting_human",
    "retrying",
    "queued",
    "completed_last_window",
    "cancelled_last_window",
    "failed_last_window",
  ] as const;
  for (const key of requiredCountKeys) {
    if (!(key in counts)) {
      return null;
    }
    const value = counts[key];
    // Counts are fleet tallies: null (unavailable) or nonnegative integers only.
    if (!isNullableNonNegativeInteger(value)) {
      return null;
    }
  }
  if (!record.overlap || typeof record.overlap !== "object" || Array.isArray(record.overlap)) {
    return null;
  }
  const overlap = record.overlap as Record<string, unknown>;
  for (const key of [
    "awaiting_human_subset_of_monitoring_pr",
    "awaiting_operator_in_active_not_executing",
    "retrying_in_active_not_executing",
  ] as const) {
    if (typeof overlap[key] !== "boolean") {
      return null;
    }
  }
  return {
    ...(payload as ConsoleDashboardSummary),
    coverage: {
      status: coverage.status,
      notes,
    },
  };
}
