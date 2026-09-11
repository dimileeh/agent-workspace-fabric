import { capacityUtilizationPct } from "./format.ts";
import type {
  ConsoleBackendKind,
  ConsoleDashboardCountEvidence,
  ConsoleDashboardCounts,
  ConsoleDashboardSummary,
  ResourceSaturationSummary,
} from "./types.ts";

/** Contract: Core local → local scope; hosted → tenant scope. */
function expectedSummaryScope(backendKind: ConsoleBackendKind): "local" | "tenant" {
  return backendKind === "hosted" ? "tenant" : "local";
}

const DASH = "—";
const DASHBOARD_COUNT_KEYS = [
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
type DashboardCountKey = (typeof DASHBOARD_COUNT_KEYS)[number];

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

function hasOnlyKeys(record: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(record).every((key) => keys.includes(key));
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

function countRelationshipsAreValid(counts: Record<DashboardCountKey, number | null>): boolean {
  const {
    active,
    executing,
    monitoring_pr: monitoringPr,
    awaiting_operator: awaitingOperator,
    awaiting_human: awaitingHuman,
    retrying,
    queued,
  } = counts;
  for (const subset of [executing, monitoringPr, queued, awaitingOperator, retrying]) {
    if (active != null && subset != null && subset > active) {
      return false;
    }
  }
  if (awaitingHuman != null && monitoringPr != null && awaitingHuman > monitoringPr) {
    return false;
  }
  for (const disjointCount of [awaitingOperator, retrying]) {
    if (
      active != null &&
      executing != null &&
      disjointCount != null &&
      disjointCount + executing > active
    ) {
      return false;
    }
  }
  if (active != null) {
    const disjointParts = [executing, monitoringPr, queued, awaitingOperator, retrying].filter(
      (value): value is number => value != null,
    );
    if (disjointParts.length >= 2 && disjointParts.reduce((sum, value) => sum + value, 0) > active) {
      return false;
    }
  }
  return true;
}

const CONFIRMED_COUNT_HINT = "confirmed lower bound; project total is incomplete";

function displayCount(
  exact: number | null | undefined,
  confirmed: number | null | undefined,
  baseHint?: string,
): Pick<SummaryFleetKpi, "value" | "suffix" | "hint"> {
  if (exact != null) {
    return { value: exact, hint: baseHint };
  }
  if (confirmed != null) {
    return {
      value: confirmed,
      suffix: " confirmed",
      hint: baseHint ? `${baseHint} · ${CONFIRMED_COUNT_HINT}` : CONFIRMED_COUNT_HINT,
    };
  }
  // Null ≠ zero: incomplete/unknown counts without evidence render as an em dash.
  return { value: DASH };
}

const COVERAGE_NOTE_LABELS: Record<string, string> = {
  queued_count_unavailable: "queued count unavailable",
  no_prior_successful_snapshot: "no prior successful snapshot",
};

function formatCoverageNote(note: string): string {
  const known = COVERAGE_NOTE_LABELS[note];
  if (known) {
    return known;
  }
  const trimmed = note.trim();
  if (!trimmed) {
    return "";
  }
  return trimmed.replaceAll("_", " ");
}

/**
 * Operator-facing incomplete-coverage notice for an HTTP 200 summary.
 * Null when coverage is missing or complete — request errors stay a separate banner.
 */
export function formatDashboardCoverageNotice(
  coverage: { status: string; notes?: readonly string[] | null } | null | undefined,
  countEvidence?: ConsoleDashboardCountEvidence | null,
): string | null {
  if (!coverage || (coverage.status !== "partial" && coverage.status !== "unknown")) {
    return null;
  }
  const notes = (coverage.notes ?? [])
    .map((note) => formatCoverageNote(note))
    .filter((note) => note.length > 0);
  if (countEvidence) {
    notes.unshift(
      `${countEvidence.status_known_workspaces} of ${countEvidence.total_workspaces} workflow statuses known; ${countEvidence.status_unknown_workspaces} unknown`,
    );
  }
  const headline = coverage.status === "partial" ? "partial coverage" : "coverage unknown";
  if (notes.length === 0) {
    return `${headline} — some counts are incomplete`;
  }
  return `${headline} — ${notes.join("; ")}`;
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
  const confirmedCounts = summary?.count_evidence?.confirmed_counts ?? null;
  const windowHint = summary ? `last ${summary.window.since_hours}h` : undefined;
  const capacity = showCapacity && saturation ? capacityUtilizationPct(saturation) : null;
  const displayedNumber = (key: DashboardCountKey): number | null =>
    counts?.[key] ?? confirmedCounts?.[key] ?? null;

  // Contract: unsupported/omitted fleet_summary must omit the widget, not render dash shells.
  // Available-but-null summary still emits counters as — (loading / first-load failure).
  const kpis: SummaryFleetKpi[] = includeSummary
    ? [
        {
          id: "active",
          label: "Active",
          ...displayCount(counts?.active, confirmedCounts?.active),
          stale: summaryStale,
        },
        {
          id: "running",
          label: "Running",
          ...displayCount(counts?.executing, confirmedCounts?.executing),
          tone: displayedNumber("executing") ? "info" : undefined,
          stale: summaryStale,
        },
        {
          id: "monitoring_pr",
          label: "Monitoring PR",
          ...displayCount(counts?.monitoring_pr, confirmedCounts?.monitoring_pr),
          tone: displayedNumber("monitoring_pr") ? "info" : undefined,
          stale: summaryStale,
        },
        {
          id: "blocked",
          label: "Awaiting operator",
          ...displayCount(counts?.awaiting_operator, confirmedCounts?.awaiting_operator),
          tone: displayedNumber("awaiting_operator") ? "warn" : undefined,
          stale: summaryStale,
        },
        {
          id: "recovering",
          label: "Auto-retrying",
          ...displayCount(counts?.retrying, confirmedCounts?.retrying),
          tone: displayedNumber("retrying") ? "info" : undefined,
          stale: summaryStale,
        },
        {
          id: "awaiting_human",
          label: "Awaiting human",
          ...displayCount(counts?.awaiting_human, confirmedCounts?.awaiting_human),
          tone: displayedNumber("awaiting_human") ? "warn" : undefined,
          stale: summaryStale,
        },
        {
          id: "queued",
          label: "Queued",
          ...displayCount(
            counts?.queued,
            confirmedCounts?.queued,
            displayedNumber("queued") ? "awaiting capacity" : undefined,
          ),
          tone: displayedNumber("queued") ? "warn" : undefined,
          stale: summaryStale,
        },
        {
          id: "completed",
          label: "Completed",
          ...displayCount(
            counts?.completed_last_window,
            confirmedCounts?.completed_last_window,
            windowHint,
          ),
          tone: displayedNumber("completed_last_window") ? "good" : undefined,
          stale: summaryStale,
        },
        {
          id: "cancelled",
          label: "Cancelled",
          ...displayCount(
            counts?.cancelled_last_window,
            confirmedCounts?.cancelled_last_window,
            windowHint,
          ),
          tone: displayedNumber("cancelled_last_window") ? "warn" : undefined,
          stale: summaryStale,
        },
        {
          id: "failed",
          label: "Failed",
          ...displayCount(
            counts?.failed_last_window,
            confirmedCounts?.failed_last_window,
            windowHint,
          ),
          tone: displayedNumber("failed_last_window") ? "bad" : undefined,
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
  if (!hasOnlyKeys(record, [
    "schema_version", "scope", "generated_at", "as_of", "last_success_at",
    "window", "coverage", "counts", "count_evidence", "overlap",
  ])) {
    return null;
  }
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
  for (const key of ["generated_at", "as_of"] as const) {
    if (typeof record[key] !== "string" || !isFiniteTimestampString(record[key])) {
      return null;
    }
  }
  // Required key. Null is truthful only until a fully successful snapshot exists;
  // a timestamp retains the last complete build across partial/unknown outages.
  if (!("last_success_at" in record)) {
    return null;
  }
  if (
    record.last_success_at !== null &&
    (typeof record.last_success_at !== "string" ||
      !isFiniteTimestampString(record.last_success_at))
  ) {
    return null;
  }
  if (!record.window || typeof record.window !== "object" || Array.isArray(record.window)) {
    return null;
  }
  const window = record.window as Record<string, unknown>;
  if (!hasOnlyKeys(window, ["anchor", "since_hours", "start"])) {
    return null;
  }
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
  if (!hasOnlyKeys(coverage, ["status", "notes"])) {
    return null;
  }
  if (
    coverage.status !== "complete" &&
    coverage.status !== "partial" &&
    coverage.status !== "unknown"
  ) {
    return null;
  }
  // A complete snapshot is itself a successful build and must name last_success_at.
  if (coverage.status === "complete" && record.last_success_at === null) {
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
  if (!hasOnlyKeys(counts, DASHBOARD_COUNT_KEYS)) {
    return null;
  }
  let anyCountNull = false;
  for (const key of DASHBOARD_COUNT_KEYS) {
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
  if (!record.overlap || typeof record.overlap !== "object" || Array.isArray(record.overlap)) {
    return null;
  }
  const overlap = record.overlap as Record<string, unknown>;
  const overlapKeys = [
    "awaiting_human_subset_of_monitoring_pr",
    "awaiting_operator_in_active_not_executing",
    "retrying_in_active_not_executing",
  ] as const;
  if (!hasOnlyKeys(overlap, overlapKeys)) {
    return null;
  }
  for (const key of overlapKeys) {
    // Fixed v1 count-semantics invariants: literal true only (not provider toggles).
    if (overlap[key] !== true) {
      return null;
    }
  }
  const exactCounts = counts as unknown as ConsoleDashboardCounts;
  if (!countRelationshipsAreValid(exactCounts)) {
    return null;
  }

  if ("count_evidence" in record && record.count_evidence !== null) {
    if (
      !record.count_evidence ||
      typeof record.count_evidence !== "object" ||
      Array.isArray(record.count_evidence)
    ) {
      return null;
    }
    const evidence = record.count_evidence as Record<string, unknown>;
    const evidenceKeys = [
      "total_workspaces",
      "status_known_workspaces",
      "status_unknown_workspaces",
      "confirmed_counts",
    ] as const;
    if (!hasOnlyKeys(evidence, evidenceKeys) || !evidenceKeys.every((key) => key in evidence)) {
      return null;
    }
    if (
      !isNonNegativeInteger(evidence.total_workspaces) ||
      !isNonNegativeInteger(evidence.status_known_workspaces) ||
      !isNonNegativeInteger(evidence.status_unknown_workspaces) ||
      evidence.status_known_workspaces + evidence.status_unknown_workspaces !==
        evidence.total_workspaces
    ) {
      return null;
    }
    if (
      !evidence.confirmed_counts ||
      typeof evidence.confirmed_counts !== "object" ||
      Array.isArray(evidence.confirmed_counts)
    ) {
      return null;
    }
    const confirmed = evidence.confirmed_counts as Record<string, unknown>;
    if (
      !hasOnlyKeys(confirmed, DASHBOARD_COUNT_KEYS) ||
      !DASHBOARD_COUNT_KEYS.every(
        (key) =>
          key in confirmed &&
          isNonNegativeInteger(confirmed[key]) &&
          confirmed[key] <= (evidence.status_known_workspaces as number),
      )
    ) {
      return null;
    }
    const confirmedCounts = confirmed as Record<DashboardCountKey, number>;
    if (
      confirmedCounts.active +
        confirmedCounts.completed_last_window +
        confirmedCounts.cancelled_last_window +
        confirmedCounts.failed_last_window >
      evidence.status_known_workspaces
    ) {
      return null;
    }
    if (!countRelationshipsAreValid(confirmedCounts)) {
      return null;
    }
    for (const key of DASHBOARD_COUNT_KEYS) {
      if (exactCounts[key] != null && exactCounts[key] !== confirmedCounts[key]) {
        return null;
      }
    }
    if (coverage.status === "complete" && evidence.status_unknown_workspaces !== 0) {
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
