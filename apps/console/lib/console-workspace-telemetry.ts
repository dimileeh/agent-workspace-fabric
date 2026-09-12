/**
 * Backend-neutral TelemetryPresentation reader + UI projection.
 *
 * Producer fixtures: awf-cloud be0d7a3ac9871ef951860666a236c9c481f45c98
 * (`console_telemetry/contract_fixtures`). Schema under review — fail closed;
 * do not silently normalize types or invent zero/free for missing allocation.
 */

export const TELEMETRY_VIEWS = ["1h", "6h", "24h"] as const;
export type TelemetryViewWindow = (typeof TELEMETRY_VIEWS)[number];

export const TELEMETRY_STATES = ["success", "partial", "stale", "unallocated"] as const;
export type TelemetryPresentationState = (typeof TELEMETRY_STATES)[number];

export const TELEMETRY_QUALITIES = ["ok", "partial", "stale"] as const;
export type TelemetryQuality = (typeof TELEMETRY_QUALITIES)[number];

export const ESTIMATE_STATES = ["complete", "partial", "unallocated"] as const;
export type EstimateState = (typeof ESTIMATE_STATES)[number];

export type CostDisplayState = "complete" | "partial" | "unpriced" | "unallocated";

/** Operator-facing exclusion copy (not producer data_quality_notes). */
export const COST_EXCLUSION_NOTE =
  "Excludes discounts, control-plane/shared infrastructure, network, and persistent storage. Shared Core monitor runtime is unallocated, not free.";

/** Reject absurd magnitudes (CPU cores / USD) rather than accept scientific junk. */
const MAX_DECIMAL_MAGNITUDE = 1e15;
/** Memory / ephemeral bytes upper bound (~1 PiB). */
const MAX_BYTES_MAGNITUDE = 1e18;
/**
 * Hard cap on samples retained per metric array from a telemetry payload.
 * Keeps the trailing (most recent) window when the producer sends more.
 */
export const MAX_TELEMETRY_SAMPLES = 2048;
/** Max SVG points drawn for a telemetry sparkline after downsampling. */
export const MAX_SPARKLINE_POINTS = 64;

const RFC3339_DATE_TIME =
  /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-](\d{2}):(\d{2}))$/;

const DECIMAL_STRING =
  /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

export type ParsedTelemetrySample = {
  containerName: string;
  sampleTime: string;
  intervalStart: string;
  intervalEnd: string;
  unit: "cores" | "bytes";
  value: number;
  quality: TelemetryQuality;
  /** Retained for identity checks; never projected into WorkspaceTelemetryView. */
  providerResourceUid: string | null;
};

export type ParsedAdmittedResources = {
  billable: boolean;
  computeClass: string;
  containerCreating: boolean;
  cpuRequestCores: number | null;
  cpuLimitCores: number | null;
  memoryRequestBytes: number | null;
  memoryLimitBytes: number | null;
  ephemeralStorageRequestBytes: number | null;
  ephemeralStorageLimitBytes: number | null;
  observedAt: string;
  partial: boolean;
  podPhase: string;
  region: string;
};

export type ParsedEstimate = {
  currency: "USD";
  estimateState: EstimateState;
  estimatedUsd: number | null;
  pricedIntervalSeconds: number;
  unpricedIntervalSeconds: number;
  rateTableVersion: string | null;
  rateSource: string | null;
};

export type ParsedTelemetryPresentation = {
  state: TelemetryPresentationState;
  quality: TelemetryQuality;
  view: TelemetryViewWindow;
  staleAfterSeconds: number;
  observedAt: string | null;
  admitted: ParsedAdmittedResources | null;
  cpuSamples: ParsedTelemetrySample[];
  memorySamples: ParsedTelemetrySample[];
  estimate: ParsedEstimate;
};

export type WorkspaceTelemetrySeriesPoint = {
  sampleTime: string;
  value: number;
  quality: TelemetryQuality;
  containerName: string;
};

export type WorkspaceTelemetryView = {
  state: TelemetryPresentationState;
  quality: TelemetryQuality;
  view: TelemetryViewWindow;
  staleAfterSeconds: number;
  observedAt: string | null;
  sampleTime: string | null;
  isStale: boolean;
  admitted: {
    cpuRequestCores: number | null;
    cpuLimitCores: number | null;
    memoryRequestBytes: number | null;
    memoryLimitBytes: number | null;
    computeClass: string | null;
    region: string | null;
    billable: boolean | null;
    partial: boolean;
  } | null;
  cpu: {
    usedCores: number | null;
    usedPartial: boolean;
    series: WorkspaceTelemetrySeriesPoint[];
    containerNamesAtSample: string[] | null;
  };
  memory: {
    usedBytes: number | null;
    usedPartial: boolean;
    series: WorkspaceTelemetrySeriesPoint[];
    containerNamesAtSample: string[] | null;
  };
  estimate: {
    displayState: CostDisplayState;
    currency: "USD";
    estimatedUsd: number | null;
    rateTableVersion: string | null;
    rateSource: string | null;
    pricedIntervalSeconds: number;
    unpricedIntervalSeconds: number;
  };
  exclusionNote: string;
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

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

function isNullableTimestamp(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && isFiniteTimestampString(value));
}

/**
 * Strict nonnegative finite decimal string. Rejects NaN/Infinity tokens,
 * scientific notation, negatives, and oversized magnitudes.
 */
function parseDecimalString(
  value: unknown,
  options: { allowNull?: boolean; max?: number } = {},
): number | null | undefined {
  const max = options.max ?? MAX_DECIMAL_MAGNITUDE;
  if (value === null && options.allowNull) {
    return null;
  }
  if (typeof value !== "string") {
    return undefined;
  }
  const trimmed = value.trim();
  if (!DECIMAL_STRING.test(trimmed)) {
    return undefined;
  }
  const n = Number(trimmed);
  if (!Number.isFinite(n) || n < 0 || n > max) {
    return undefined;
  }
  return n;
}

function isNonNegativeFiniteNumber(value: unknown, max = MAX_BYTES_MAGNITUDE): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= max;
}

function isNullableNonNegativeFiniteNumber(
  value: unknown,
  max = MAX_BYTES_MAGNITUDE,
): value is number | null {
  return value === null || isNonNegativeFiniteNumber(value, max);
}

function isOneOf<T extends string>(value: unknown, allowed: readonly T[]): value is T {
  return typeof value === "string" && (allowed as readonly string[]).includes(value);
}

function parseSampleArray(
  value: unknown,
  expectedUnit: "cores" | "bytes",
): ParsedTelemetrySample[] | null {
  if (!Array.isArray(value)) {
    return null;
  }
  const samples: ParsedTelemetrySample[] = [];
  // Bound accepted count early; keep the trailing window (most recent samples).
  const start = Math.max(0, value.length - MAX_TELEMETRY_SAMPLES);
  for (let i = start; i < value.length; i++) {
    const item = value[i];
    if (!isPlainObject(item)) {
      return null;
    }
    if (typeof item.container_name !== "string" || item.container_name.length === 0) {
      return null;
    }
    if (item.unit !== expectedUnit) {
      return null;
    }
    if (
      typeof item.sample_time !== "string" ||
      !isFiniteTimestampString(item.sample_time) ||
      typeof item.interval_start !== "string" ||
      !isFiniteTimestampString(item.interval_start) ||
      typeof item.interval_end !== "string" ||
      !isFiniteTimestampString(item.interval_end)
    ) {
      return null;
    }
    // Fail closed: unknown/typo quality must not parse as complete usage.
    if (!isOneOf(item.quality, TELEMETRY_QUALITIES)) {
      return null;
    }
    // Producer metric_type may say core_usage_time while unit is cores — accept as naming quirk.
    if (typeof item.metric_type !== "string") {
      return null;
    }
    const parsedValue = parseDecimalString(item.value, {
      max: expectedUnit === "bytes" ? MAX_BYTES_MAGNITUDE : MAX_DECIMAL_MAGNITUDE,
    });
    if (parsedValue === undefined || parsedValue === null) {
      return null;
    }
    // Accept known evidence object for schema compatibility; never project it.
    if (item.evidence !== undefined && item.evidence !== null && !isPlainObject(item.evidence)) {
      return null;
    }
    if (
      item.provider_resource_uid !== undefined &&
      typeof item.provider_resource_uid !== "string"
    ) {
      return null;
    }
    samples.push({
      containerName: item.container_name,
      sampleTime: item.sample_time,
      intervalStart: item.interval_start,
      intervalEnd: item.interval_end,
      unit: expectedUnit,
      value: parsedValue,
      quality: item.quality,
      providerResourceUid:
        typeof item.provider_resource_uid === "string" ? item.provider_resource_uid : null,
    });
  }
  return samples;
}

/**
 * Resolve the presentation's resource identity when the producer supplies one.
 * Prefer admitted, then ownership — both are schema-compatible ownership fields.
 */
function resolvePresentationResourceUid(payload: Record<string, unknown>): string | null {
  if (isPlainObject(payload.admitted)) {
    const admittedUid = payload.admitted.provider_resource_uid;
    if (typeof admittedUid === "string" && admittedUid.length > 0) {
      return admittedUid;
    }
  }
  if (isPlainObject(payload.ownership)) {
    const ownershipUid = payload.ownership.provider_resource_uid;
    if (typeof ownershipUid === "string" && ownershipUid.length > 0) {
      return ownershipUid;
    }
  }
  return null;
}

/**
 * Fail closed on cross-resource samples and duplicate container@time rows.
 * Distinct containers at the same timestamp remain valid (pod partition sum).
 */
function assertSampleIdentities(
  samples: ParsedTelemetrySample[],
  expectedResourceUid: string | null,
): boolean {
  const seenContainerAtTime = new Set<string>();
  let seriesUid: string | null = null;
  for (const sample of samples) {
    const identityKey = `${sample.sampleTime}\0${sample.containerName}`;
    if (seenContainerAtTime.has(identityKey)) {
      return false;
    }
    seenContainerAtTime.add(identityKey);

    if (sample.providerResourceUid === null) {
      continue;
    }
    if (expectedResourceUid !== null && sample.providerResourceUid !== expectedResourceUid) {
      return false;
    }
    if (seriesUid === null) {
      seriesUid = sample.providerResourceUid;
    } else if (sample.providerResourceUid !== seriesUid) {
      return false;
    }
  }
  return true;
}

/**
 * Evenly downsample a sorted series for sparkline rendering.
 * Always preserves first and last points when maxPoints >= 2.
 */
export function downsampleSeriesForSparkline<T>(
  points: readonly T[],
  maxPoints: number = MAX_SPARKLINE_POINTS,
): T[] {
  if (points.length <= maxPoints) {
    return points.slice();
  }
  if (maxPoints <= 0) {
    return [];
  }
  if (maxPoints === 1) {
    return [points[points.length - 1]!];
  }
  const out: T[] = [];
  const last = points.length - 1;
  let prevIdx = -1;
  for (let i = 0; i < maxPoints; i++) {
    const idx = Math.round((i * last) / (maxPoints - 1));
    if (idx === prevIdx) {
      continue;
    }
    out.push(points[idx]!);
    prevIdx = idx;
  }
  return out;
}

function parseAdmitted(value: unknown): ParsedAdmittedResources | null | undefined {
  if (value === null) {
    return null;
  }
  if (!isPlainObject(value)) {
    return undefined;
  }
  const cpuRequestCores = parseDecimalString(value.cpu_request_cores, { allowNull: true });
  const cpuLimitCores = parseDecimalString(value.cpu_limit_cores, { allowNull: true });
  if (cpuRequestCores === undefined || cpuLimitCores === undefined) {
    return undefined;
  }
  if (
    !isNullableNonNegativeFiniteNumber(value.memory_request_bytes) ||
    !isNullableNonNegativeFiniteNumber(value.memory_limit_bytes) ||
    !isNullableNonNegativeFiniteNumber(value.ephemeral_storage_request_bytes) ||
    !isNullableNonNegativeFiniteNumber(value.ephemeral_storage_limit_bytes)
  ) {
    return undefined;
  }
  if (typeof value.observed_at !== "string" || !isFiniteTimestampString(value.observed_at)) {
    return undefined;
  }
  if (
    typeof value.billable !== "boolean" ||
    typeof value.compute_class !== "string" ||
    typeof value.container_creating !== "boolean" ||
    typeof value.partial !== "boolean" ||
    typeof value.pod_phase !== "string" ||
    typeof value.region !== "string"
  ) {
    return undefined;
  }
  // Accept ownership-ish / evidence fields without projecting them.
  if (value.evidence !== undefined && value.evidence !== null && !isPlainObject(value.evidence)) {
    return undefined;
  }
  return {
    billable: value.billable,
    computeClass: value.compute_class,
    containerCreating: value.container_creating,
    cpuRequestCores,
    cpuLimitCores,
    memoryRequestBytes: value.memory_request_bytes,
    memoryLimitBytes: value.memory_limit_bytes,
    ephemeralStorageRequestBytes: value.ephemeral_storage_request_bytes,
    ephemeralStorageLimitBytes: value.ephemeral_storage_limit_bytes,
    observedAt: value.observed_at,
    partial: value.partial,
    podPhase: value.pod_phase,
    region: value.region,
  };
}

function parseEstimate(value: unknown): ParsedEstimate | null {
  if (!isPlainObject(value)) {
    return null;
  }
  if (value.currency !== "USD") {
    return null;
  }
  if (!isOneOf(value.estimate_state, ESTIMATE_STATES)) {
    return null;
  }
  const estimatedUsd = parseDecimalString(value.estimated_usd, { allowNull: true });
  if (estimatedUsd === undefined) {
    return null;
  }
  if (
    !isNonNegativeFiniteNumber(value.priced_interval_seconds, MAX_DECIMAL_MAGNITUDE) ||
    !Number.isInteger(value.priced_interval_seconds) ||
    !isNonNegativeFiniteNumber(value.unpriced_interval_seconds, MAX_DECIMAL_MAGNITUDE) ||
    !Number.isInteger(value.unpriced_interval_seconds)
  ) {
    return null;
  }
  if (typeof value.rate_table_version !== "string") {
    return null;
  }
  let rateSource: string | null = null;
  if (value.evidence !== undefined && value.evidence !== null) {
    if (!isPlainObject(value.evidence)) {
      return null;
    }
    if (typeof value.evidence.source === "string") {
      rateSource = value.evidence.source;
    }
  }
  const rateTableVersion =
    value.rate_table_version.trim() === "" ? null : value.rate_table_version;
  return {
    currency: "USD",
    estimateState: value.estimate_state,
    estimatedUsd,
    pricedIntervalSeconds: value.priced_interval_seconds,
    unpricedIntervalSeconds: value.unpriced_interval_seconds,
    rateTableVersion,
    rateSource,
  };
}

/**
 * Fail-closed parse of producer TelemetryPresentation JSON.
 * Accepts known ownership / evidence / data_quality_notes for compatibility
 * but does not return them on the parsed model for UI use.
 */
export function parseTelemetryPresentation(
  payload: unknown,
): ParsedTelemetryPresentation | null {
  if (!isPlainObject(payload)) {
    return null;
  }
  if (!isOneOf(payload.state, TELEMETRY_STATES)) {
    return null;
  }
  if (!isOneOf(payload.quality, TELEMETRY_QUALITIES)) {
    return null;
  }
  if (!isOneOf(payload.view, TELEMETRY_VIEWS)) {
    return null;
  }
  if (
    typeof payload.stale_after_seconds !== "number" ||
    !Number.isFinite(payload.stale_after_seconds) ||
    payload.stale_after_seconds < 0 ||
    !Number.isInteger(payload.stale_after_seconds)
  ) {
    return null;
  }
  if (!isNullableTimestamp(payload.observed_at)) {
    return null;
  }
  // Accept machine notes for schema compatibility; never surface as UI copy.
  if (payload.data_quality_notes !== undefined) {
    if (!Array.isArray(payload.data_quality_notes)) {
      return null;
    }
    for (const note of payload.data_quality_notes) {
      if (typeof note !== "string") {
        return null;
      }
    }
  }
  if (payload.ownership !== undefined && payload.ownership !== null) {
    if (!isPlainObject(payload.ownership)) {
      return null;
    }
  }

  const admitted = parseAdmitted(payload.admitted);
  if (admitted === undefined) {
    return null;
  }
  const cpuSamples = parseSampleArray(payload.cpu_cores_samples, "cores");
  if (cpuSamples === null) {
    return null;
  }
  const memorySamples = parseSampleArray(payload.memory_bytes_samples, "bytes");
  if (memorySamples === null) {
    return null;
  }
  const expectedResourceUid = resolvePresentationResourceUid(payload);
  if (
    !assertSampleIdentities(cpuSamples, expectedResourceUid) ||
    !assertSampleIdentities(memorySamples, expectedResourceUid)
  ) {
    return null;
  }
  const estimate = parseEstimate(payload.estimate);
  if (estimate === null) {
    return null;
  }

  return {
    state: payload.state,
    quality: payload.quality,
    view: payload.view,
    staleAfterSeconds: payload.stale_after_seconds,
    observedAt: payload.observed_at,
    admitted,
    cpuSamples,
    memorySamples,
    estimate,
  };
}

function latestTimestamp(samples: ParsedTelemetrySample[]): string | null {
  let latest: string | null = null;
  let latestMs = Number.NEGATIVE_INFINITY;
  for (const sample of samples) {
    const ms = Date.parse(sample.sampleTime);
    if (ms > latestMs) {
      latestMs = ms;
      latest = sample.sampleTime;
    }
  }
  return latest;
}

/**
 * Sum samples that share the exact same sample_time string identity.
 * Never merges across different moments.
 */
function aggregateAtTimestamp(samples: ParsedTelemetrySample[]): {
  used: number | null;
  usedPartial: boolean;
  containerNames: string[] | null;
  sampleTime: string | null;
  series: WorkspaceTelemetrySeriesPoint[];
} {
  const series: WorkspaceTelemetrySeriesPoint[] = samples.map((s) => ({
    sampleTime: s.sampleTime,
    value: s.value,
    quality: s.quality,
    containerName: s.containerName,
  }));
  if (samples.length === 0) {
    return {
      used: null,
      usedPartial: false,
      containerNames: null,
      sampleTime: null,
      series,
    };
  }
  const sampleTime = latestTimestamp(samples);
  if (sampleTime === null) {
    return {
      used: null,
      usedPartial: false,
      containerNames: null,
      sampleTime: null,
      series,
    };
  }
  const atLatest = samples.filter((s) => s.sampleTime === sampleTime);
  let used = 0;
  let usedPartial = false;
  const containerNames: string[] = [];
  for (const sample of atLatest) {
    used += sample.value;
    // Fail closed: only exact "ok" is complete usage. Parse rejects unknown
    // qualities; projection still treats any non-ok allowlisted quality
    // (partial, stale) as incomplete rather than appearing complete.
    if (sample.quality !== "ok") {
      usedPartial = true;
    }
    containerNames.push(sample.containerName);
  }
  return {
    used,
    usedPartial,
    containerNames,
    sampleTime,
    series,
  };
}

function resolveCostDisplayState(estimate: ParsedEstimate): CostDisplayState {
  if (estimate.estimateState === "unallocated") {
    return "unallocated";
  }
  if (estimate.estimatedUsd === null) {
    return "unpriced";
  }
  if (estimate.estimateState === "partial") {
    return "partial";
  }
  return "complete";
}

function computeIsStale(
  presentation: ParsedTelemetryPresentation,
  nowMs: number,
): boolean {
  if (presentation.state === "stale" || presentation.quality === "stale") {
    return true;
  }
  if (presentation.observedAt === null) {
    return false;
  }
  const observedMs = Date.parse(presentation.observedAt);
  if (!Number.isFinite(observedMs)) {
    return false;
  }
  return nowMs - observedMs > presentation.staleAfterSeconds * 1000;
}

/**
 * Project allowlisted UI fields from a parsed presentation.
 * Does not fetch; `nowMs` is injected for deterministic freshness tests.
 */
export function projectWorkspaceTelemetryView(
  presentation: ParsedTelemetryPresentation,
  options: { nowMs?: number } = {},
): WorkspaceTelemetryView {
  const nowMs = options.nowMs ?? Date.now();
  const cpuAgg = aggregateAtTimestamp(presentation.cpuSamples);
  const memAgg = aggregateAtTimestamp(presentation.memorySamples);

  let sampleTime: string | null = null;
  const candidates = [cpuAgg.sampleTime, memAgg.sampleTime, presentation.observedAt].filter(
    (v): v is string => typeof v === "string",
  );
  if (candidates.length > 0) {
    sampleTime = candidates.reduce((a, b) => (Date.parse(a) >= Date.parse(b) ? a : b));
  }

  return {
    state: presentation.state,
    quality: presentation.quality,
    view: presentation.view,
    staleAfterSeconds: presentation.staleAfterSeconds,
    observedAt: presentation.observedAt,
    sampleTime,
    isStale: computeIsStale(presentation, nowMs),
    admitted:
      presentation.admitted === null
        ? null
        : {
            cpuRequestCores: presentation.admitted.cpuRequestCores,
            cpuLimitCores: presentation.admitted.cpuLimitCores,
            memoryRequestBytes: presentation.admitted.memoryRequestBytes,
            memoryLimitBytes: presentation.admitted.memoryLimitBytes,
            computeClass: presentation.admitted.computeClass,
            region: presentation.admitted.region,
            billable: presentation.admitted.billable,
            partial: presentation.admitted.partial,
          },
    cpu: {
      usedCores: cpuAgg.used,
      usedPartial: cpuAgg.usedPartial,
      series: cpuAgg.series,
      containerNamesAtSample: cpuAgg.containerNames,
    },
    memory: {
      usedBytes: memAgg.used,
      usedPartial: memAgg.usedPartial,
      series: memAgg.series,
      containerNamesAtSample: memAgg.containerNames,
    },
    estimate: {
      displayState: resolveCostDisplayState(presentation.estimate),
      currency: "USD",
      estimatedUsd: presentation.estimate.estimatedUsd,
      rateTableVersion: presentation.estimate.rateTableVersion,
      rateSource: presentation.estimate.rateSource,
      pricedIntervalSeconds: presentation.estimate.pricedIntervalSeconds,
      unpricedIntervalSeconds: presentation.estimate.unpricedIntervalSeconds,
    },
    exclusionNote: COST_EXCLUSION_NOTE,
  };
}
