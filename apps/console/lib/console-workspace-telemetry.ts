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
/**
 * Memory / ephemeral bytes upper bound.
 * Capped at Number.MAX_SAFE_INTEGER so admitted/sample byte counts stay exact
 * in JS Number (above this, values round silently and must be rejected).
 */
const MAX_BYTES_MAGNITUDE = Number.MAX_SAFE_INTEGER;
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
 * When requireSafeInteger is set (byte counts), also rejects fractions and
 * values that lose precision under Number.
 */
function parseDecimalString(
  value: unknown,
  options: { allowNull?: boolean; max?: number; requireSafeInteger?: boolean } = {},
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
  if (options.requireSafeInteger && !Number.isSafeInteger(n)) {
    return undefined;
  }
  return n;
}

function isNonNegativeFiniteNumber(value: unknown, max = MAX_BYTES_MAGNITUDE): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= max;
}

/** Admitted byte counts must be exact safe integers (no float rounding). */
function isNonNegativeSafeByteCount(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= 0 &&
    value <= MAX_BYTES_MAGNITUDE
  );
}

function isNullableNonNegativeSafeByteCount(value: unknown): value is number | null {
  return value === null || isNonNegativeSafeByteCount(value);
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
      requireSafeInteger: expectedUnit === "bytes",
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
 * Returns undefined when both are present and disagree (fail closed).
 */
function resolvePresentationResourceUid(
  payload: Record<string, unknown>,
): string | null | undefined {
  let admittedUid: string | null = null;
  let ownershipUid: string | null = null;
  if (isPlainObject(payload.admitted)) {
    const uid = payload.admitted.provider_resource_uid;
    if (typeof uid === "string" && uid.length > 0) {
      admittedUid = uid;
    }
  }
  if (isPlainObject(payload.ownership)) {
    const uid = payload.ownership.provider_resource_uid;
    if (typeof uid === "string" && uid.length > 0) {
      ownershipUid = uid;
    }
  }
  if (admittedUid !== null && ownershipUid !== null && admittedUid !== ownershipUid) {
    return undefined;
  }
  return admittedUid ?? ownershipUid;
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

export type SparklineGeometry = {
  w: number;
  h: number;
  /** Stroked polyline for 2+ samples; null when only a marker is drawn. */
  pathD: string | null;
  /** Centered marker for a single sample (a lone `M` would stroke nothing). */
  marker: { x: number; y: number } | null;
};

/**
 * Map a sorted value series into SVG sparkline geometry.
 * One sample → marker only; two or more → stroked path.
 */
export function buildSparklineGeometry(
  points: readonly { value: number }[],
  width = 120,
  height = 28,
): SparklineGeometry | null {
  if (points.length === 0) {
    return null;
  }
  const series = downsampleSeriesForSparkline(points);
  if (series.length === 1) {
    return {
      w: width,
      h: height,
      pathD: null,
      marker: { x: width / 2, y: height / 2 },
    };
  }
  let min = series[0]!.value;
  let max = series[0]!.value;
  for (let i = 1; i < series.length; i++) {
    const v = series[i]!.value;
    if (v < min) {
      min = v;
    }
    if (v > max) {
      max = v;
    }
  }
  const span = max - min || 1;
  const coords = series.map((p, i) => {
    const x = (i / (series.length - 1)) * width;
    const y = height - ((p.value - min) / span) * (height - 4) - 2;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return {
    w: width,
    h: height,
    pathD: `M ${coords.join(" L ")}`,
    marker: null,
  };
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
    !isNullableNonNegativeSafeByteCount(value.memory_request_bytes) ||
    !isNullableNonNegativeSafeByteCount(value.memory_limit_bytes) ||
    !isNullableNonNegativeSafeByteCount(value.ephemeral_storage_request_bytes) ||
    !isNullableNonNegativeSafeByteCount(value.ephemeral_storage_limit_bytes)
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
  if (expectedResourceUid === undefined) {
    return null;
  }
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
 * Accumulate sample values. For byte meters, each running total must remain a
 * safe integer — individually valid samples near MAX_SAFE_INTEGER can still
 * sum past the exact Number range.
 */
function accumulateSampleTotal(
  samples: ParsedTelemetrySample[],
): number | null {
  let total = 0;
  for (const sample of samples) {
    total += sample.value;
    if (sample.unit === "bytes" && !Number.isSafeInteger(total)) {
      return null;
    }
  }
  return total;
}

/**
 * Build sparkline history as one pod-total point per sample_time.
 * Raw per-container rows must not be plotted as a single line — same-timestamp
 * containers would form a fake trend and disagree with meter totals.
 */
function buildPodTotalSeries(
  samples: ParsedTelemetrySample[],
): WorkspaceTelemetrySeriesPoint[] {
  if (samples.length === 0) {
    return [];
  }
  const byTime = new Map<string, ParsedTelemetrySample[]>();
  for (const sample of samples) {
    const group = byTime.get(sample.sampleTime);
    if (group) {
      group.push(sample);
    } else {
      byTime.set(sample.sampleTime, [sample]);
    }
  }
  const points: WorkspaceTelemetrySeriesPoint[] = [];
  for (const [sampleTime, group] of byTime) {
    const value = accumulateSampleTotal(group);
    // Fail closed: omit points whose byte pod-total is not an exact safe integer.
    if (value === null) {
      continue;
    }
    let quality: TelemetryQuality = "ok";
    const names: string[] = [];
    for (const sample of group) {
      names.push(sample.containerName);
      if (sample.quality !== "ok") {
        // Prefer "stale" over "partial" when both appear; otherwise any non-ok.
        if (sample.quality === "stale" || quality === "ok") {
          quality = sample.quality;
        }
      }
    }
    names.sort();
    points.push({
      sampleTime,
      value,
      quality,
      containerName: names.join(","),
    });
  }
  points.sort((a, b) => Date.parse(a.sampleTime) - Date.parse(b.sampleTime));
  return points;
}

/**
 * Sum samples that share the exact same sample_time string identity.
 * Never merges across different moments. If the latest partition is missing
 * containers that appear elsewhere in the series, treat usage as partial and
 * unavailable (null) rather than presenting the subset as a complete pod total.
 */
function aggregateAtTimestamp(samples: ParsedTelemetrySample[]): {
  used: number | null;
  usedPartial: boolean;
  containerNames: string[] | null;
  sampleTime: string | null;
  series: WorkspaceTelemetrySeriesPoint[];
} {
  const series = buildPodTotalSeries(samples);
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
  let usedPartial = false;
  let incompletePartition = false;
  const containerNames: string[] = [];
  for (const sample of atLatest) {
    // Fail closed: only exact "ok" is complete usage. Parse rejects unknown
    // qualities; projection still treats any non-ok allowlisted quality
    // (partial, stale) as incomplete rather than appearing complete.
    if (sample.quality !== "ok") {
      usedPartial = true;
    }
    containerNames.push(sample.containerName);
  }
  const used = accumulateSampleTotal(atLatest);
  // Individually valid byte samples can still overflow Number's safe range when
  // summed; treat that aggregate as unavailable rather than a rounded total.
  if (used === null) {
    usedPartial = true;
  }
  // Staggered scrapes can leave the newest timestamp with only a subset of
  // containers (e.g. agent@12:00, sidecar@11:59). Do not present that subset
  // sum as a complete pod usage figure against requests/limits — mark partial
  // and leave used unavailable rather than comparing the subset to whole-pod
  // requests/limits.
  const containersAtLatest = new Set(containerNames);
  for (const sample of samples) {
    if (!containersAtLatest.has(sample.containerName)) {
      incompletePartition = true;
      usedPartial = true;
      break;
    }
  }
  return {
    used: incompletePartition || used === null ? null : used,
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

function isTimestampOlderThanStaleThreshold(
  timestamp: string,
  nowMs: number,
  staleAfterSeconds: number,
): boolean {
  const ms = Date.parse(timestamp);
  if (!Number.isFinite(ms)) {
    return false;
  }
  return nowMs - ms > staleAfterSeconds * 1000;
}

/**
 * Stale when producer marks state/quality stale, or when the envelope,
 * admitted allocation snapshot, or any meter sample time used for displayed
 * CPU/memory values exceeds the threshold. Fresh envelopes/meters must not
 * mask aged allocation requests/limits or aged resource samples.
 */
function computeIsStale(
  presentation: ParsedTelemetryPresentation,
  nowMs: number,
  meterSampleTimes: Array<string | null>,
): boolean {
  if (presentation.state === "stale" || presentation.quality === "stale") {
    return true;
  }
  const staleAfter = presentation.staleAfterSeconds;
  if (
    presentation.observedAt !== null &&
    isTimestampOlderThanStaleThreshold(presentation.observedAt, nowMs, staleAfter)
  ) {
    return true;
  }
  if (
    presentation.admitted !== null &&
    isTimestampOlderThanStaleThreshold(
      presentation.admitted.observedAt,
      nowMs,
      staleAfter,
    )
  ) {
    return true;
  }
  for (const sampleTime of meterSampleTimes) {
    if (
      sampleTime !== null &&
      isTimestampOlderThanStaleThreshold(sampleTime, nowMs, staleAfter)
    ) {
      return true;
    }
  }
  return false;
}

function resolveDisplaySampleTime(
  meterSampleTimes: Array<string | null>,
  observedAt: string | null,
): string | null {
  const meterTimes = meterSampleTimes.filter(
    (v): v is string => typeof v === "string",
  );
  if (meterTimes.length > 0) {
    // Prefer meter times so a newer envelope cannot label aged readings.
    return meterTimes.reduce((a, b) => (Date.parse(a) >= Date.parse(b) ? a : b));
  }
  return observedAt;
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
  const meterSampleTimes = [cpuAgg.sampleTime, memAgg.sampleTime];
  const sampleTime = resolveDisplaySampleTime(
    meterSampleTimes,
    presentation.observedAt,
  );

  return {
    state: presentation.state,
    quality: presentation.quality,
    view: presentation.view,
    staleAfterSeconds: presentation.staleAfterSeconds,
    observedAt: presentation.observedAt,
    sampleTime,
    isStale: computeIsStale(presentation, nowMs, meterSampleTimes),
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
