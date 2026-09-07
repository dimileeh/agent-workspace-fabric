import { capacityUtilizationPct } from "./format.ts";
import type {
  ConsoleBackendKind,
  ConsoleDashboardSummary,
  ResourceSaturationSummary,
} from "./types.ts";

/** Contract: Core local → local scope; hosted → tenant scope. */
function expectedSummaryScope(backendKind: ConsoleBackendKind): "local" | "tenant" {
  return backendKind === "hosted" ? "tenant" : "local";
}

const DASH = "—";

/**
 * OpenAPI `format: date-time` / RFC 3339 profile: full date-time with `T`/`t`
 * and a timezone (`Z`/`z` or ±HH:mm). Rejects Date.parse-permissive forms like
 * `09/07/2026` or date-only `2026-09-07`.
 * Capturing groups let us reject impossible calendar values that Date.parse
 * would normalize (e.g. 2026-02-29 → March 1).
 */
const RFC3339_DATE_TIME =
  /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-](\d{2}):(\d{2}))$/;

/** True only for finite RFC 3339 date-time strings (rejects "", "not-a-date", slash dates, etc.). */
function isFiniteTimestampString(value: string): boolean {
  const match = RFC3339_DATE_TIME.exec(value);
  if (!match) {
    return false;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  // Date.UTC normalizes overflow; require a round-trip to the same components so
  // impossible dates/times (2026-02-29, 25:00:00, month 13) are rejected.
  const dt = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  if (
    dt.getUTCFullYear() !== year ||
    dt.getUTCMonth() !== month - 1 ||
    dt.getUTCDate() !== day ||
    dt.getUTCHours() !== hour ||
    dt.getUTCMinutes() !== minute ||
    dt.getUTCSeconds() !== second
  ) {
    return false;
  }
  const tzDesignator = match[8];
  if (tzDesignator !== "Z" && tzDesignator !== "z") {
    const tzHour = Number(match[9]);
    const tzMinute = Number(match[10]);
    if (tzHour > 23 || tzMinute > 59) {
      return false;
    }
  }
  return Number.isFinite(Date.parse(value));
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
  /** When false (fleet_summary unsupported/omitted), skip summary counters; capacity may still render. */
  includeSummary: boolean;
}): SummaryFleetKpi[] {
  const {
    summary,
    summaryStale,
    saturation,
    saturationStale,
    showCapacity,
    includeSummary,
  } = options;
  const counts = summary?.counts ?? null;
  const windowHint = summary ? `last ${summary.window.since_hours}h` : undefined;
  const capacity = showCapacity && saturation ? capacityUtilizationPct(saturation) : null;

  // Contract: unsupported/omitted fleet_summary must omit the widget, not render dash shells.
  // Available-but-null summary still emits counters as — (loading / first-load failure).
  const kpis: SummaryFleetKpi[] = includeSummary
    ? [
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
      ]
    : [];

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

export function parseDashboardSummary(
  payload: unknown,
  backendKind?: ConsoleBackendKind | null,
): ConsoleDashboardSummary | null {
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
  // When capabilities negotiated a backend_kind, reject scope that would mislabel
  // node-local counts as tenant-wide (or the reverse). Enum-only checks alone
  // accept both values regardless of backend.
  if (backendKind != null && record.scope !== expectedSummaryScope(backendKind)) {
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
  // Contract: window.start = generated_at - since_hours (absolute instant).
  // Reject unrelated but syntactically valid starts so KPI "last Nh" labels
  // cannot describe a different interval from a hosted snapshot.
  const generatedAtMs = Date.parse(record.generated_at as string);
  const expectedStartMs = generatedAtMs - window.since_hours * 3_600_000;
  // Number.isFinite guards overflow of since_hours * ms/hour before equality.
  if (!Number.isFinite(expectedStartMs) || Date.parse(window.start) !== expectedStartMs) {
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
  let anyCountNull = false;
  for (const key of requiredCountKeys) {
    if (!(key in counts)) {
      return null;
    }
    const value = counts[key];
    // Counts are fleet tallies: null (unavailable) or nonnegative integers only.
    if (!isNullableNonNegativeInteger(value)) {
      return null;
    }
    if (value === null) {
      anyCountNull = true;
    }
  }
  // Contract: incomplete/null counters require coverage.status partial|unknown.
  // Reject complete + null so hosted snapshots cannot replace last-good KPIs
  // with dashes under a purported complete result.
  if (anyCountNull && coverage.status === "complete") {
    return null;
  }
  // Immediately after the per-value loop: reject contradictory domain subsets
  // among related non-null counts so malformed hosted snapshots fail closed and
  // the console retains the last-good KPI snapshot.
  const active = counts.active as number | null;
  const executing = counts.executing as number | null;
  const monitoringPr = counts.monitoring_pr as number | null;
  const awaitingHuman = counts.awaiting_human as number | null;
  const awaitingOperator = counts.awaiting_operator as number | null;
  const retrying = counts.retrying as number | null;
  const queued = counts.queued as number | null;
  if (active != null && executing != null && executing > active) {
    return null;
  }
  // monitoring_pr is a non-terminal status bucket ⊆ active.
  if (active != null && monitoringPr != null && monitoringPr > active) {
    return null;
  }
  // queued (requested) is a non-terminal status bucket ⊆ active.
  if (active != null && queued != null && queued > active) {
    return null;
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
  if (
    overlap.awaiting_human_subset_of_monitoring_pr === true &&
    awaitingHuman != null &&
    monitoringPr != null &&
    awaitingHuman > monitoringPr
  ) {
    return null;
  }
  if (overlap.awaiting_operator_in_active_not_executing === true && awaitingOperator != null) {
    if (active != null && awaitingOperator > active) {
      return null;
    }
    if (active != null && executing != null && awaitingOperator + executing > active) {
      return null;
    }
  }
  if (overlap.retrying_in_active_not_executing === true && retrying != null) {
    if (active != null && retrying > active) {
      return null;
    }
    if (active != null && executing != null && retrying + executing > active) {
      return null;
    }
  }
  // Combined disjoint active status buckets: pairwise subset checks miss cases
  // like active=3 with executing=monitoring_pr=awaiting_operator=retrying=1.
  // executing, monitoring_pr, and queued are always distinct statuses;
  // awaiting_operator / retrying join the sum only when their overlap flags
  // declare them in active ∉ executing.
  if (active != null) {
    let disjointActiveSum = 0;
    let partCount = 0;
    if (executing != null) {
      disjointActiveSum += executing;
      partCount += 1;
    }
    if (monitoringPr != null) {
      disjointActiveSum += monitoringPr;
      partCount += 1;
    }
    if (queued != null) {
      disjointActiveSum += queued;
      partCount += 1;
    }
    if (overlap.awaiting_operator_in_active_not_executing === true && awaitingOperator != null) {
      disjointActiveSum += awaitingOperator;
      partCount += 1;
    }
    if (overlap.retrying_in_active_not_executing === true && retrying != null) {
      disjointActiveSum += retrying;
      partCount += 1;
    }
    if (partCount >= 2 && disjointActiveSum > active) {
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
