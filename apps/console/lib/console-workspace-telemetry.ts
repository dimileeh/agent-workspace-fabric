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
 * Hard cap on samples accepted per metric array from a telemetry payload.
 * Larger arrays fail closed before parse so a misbehaving producer cannot
 * force unbounded allocation or an O(n log n) sort in the console.
 */
export const MAX_TELEMETRY_SAMPLES = 2048;
/** Max SVG points drawn for a telemetry sparkline after downsampling. */
export const MAX_SPARKLINE_POINTS = 64;
/**
 * Operational cap for stale_after_seconds (7d). Rejects absurd finite values
 * (e.g. Number.MAX_VALUE) that would make thresholdMs = seconds * 1000 become
 * Infinity and treat arbitrarily old telemetry as fresh. Also stays far below
 * Number.MAX_SAFE_INTEGER / 1000 so the ms conversion remains a safe integer.
 */
export const MAX_STALE_AFTER_SECONDS = 7 * 24 * 60 * 60;

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
  /** Newest meter sample time (or envelope observedAt when no meters). */
  sampleTime: string | null;
  /**
   * True when CPU and memory aggregates have distinct sample times.
   * The panel must not attribute both meters to a single Sample timestamp.
   */
  sampleTimeMixed: boolean;
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
    sampleTime: string | null;
    series: WorkspaceTelemetrySeriesPoint[];
    containerNamesAtSample: string[] | null;
  };
  memory: {
    usedBytes: number | null;
    usedPartial: boolean;
    sampleTime: string | null;
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
  // Reject before parsing so the cap bounds CPU/memory, not only retained output.
  if (value.length > MAX_TELEMETRY_SAMPLES) {
    return null;
  }
  const samples: ParsedTelemetrySample[] = [];
  for (let i = 0; i < value.length; i++) {
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
 * Read provider_resource_uid from an admitted/ownership object.
 * null = field absent; undefined = present but not a non-empty string (fail closed).
 */
function readPresentationResourceUidField(
  container: Record<string, unknown> | null,
): string | null | undefined {
  if (container === null) {
    return null;
  }
  if (!("provider_resource_uid" in container) || container.provider_resource_uid === undefined) {
    return null;
  }
  const uid = container.provider_resource_uid;
  if (typeof uid !== "string" || uid.length === 0) {
    return undefined;
  }
  return uid;
}

/**
 * Resolve the presentation's resource identity when the producer supplies one.
 * Prefer admitted, then ownership — both are schema-compatible ownership fields.
 * Returns undefined when either field is malformed, or both are present and disagree
 * (fail closed so Pod A samples cannot render under Pod B ownership).
 */
function resolvePresentationResourceUid(
  payload: Record<string, unknown>,
): string | null | undefined {
  const admittedUid = readPresentationResourceUidField(
    isPlainObject(payload.admitted) ? payload.admitted : null,
  );
  const ownershipUid = readPresentationResourceUidField(
    isPlainObject(payload.ownership) ? payload.ownership : null,
  );
  if (admittedUid === undefined || ownershipUid === undefined) {
    return undefined;
  }
  if (admittedUid !== null && ownershipUid !== null && admittedUid !== ownershipUid) {
    return undefined;
  }
  return admittedUid ?? ownershipUid;
}

/**
 * Fail closed on cross-resource samples and duplicate container@time rows.
 * Distinct containers at the same timestamp remain valid (pod partition sum).
 * Duplicate identity uses normalized instant keys (not raw RFC3339 spelling) so
 * Z / offset forms of the same instant cannot slip through parse and inflate
 * pod totals, while distinct sub-ms instants stay distinct.
 * Every retained sample must carry a non-empty UID matching one presentation-wide
 * identity (admitted/ownership when present, else the shared sample series UID).
 */
function assertSampleIdentities(
  sampleGroups: readonly ParsedTelemetrySample[][],
  expectedResourceUid: string | null,
): boolean {
  let seriesUid: string | null = expectedResourceUid;
  for (const samples of sampleGroups) {
    const seenContainerAtTime = new Set<string>();
    for (const sample of samples) {
      const identityKey = `${timestampInstantKey(sample.sampleTime)}\0${sample.containerName}`;
      if (seenContainerAtTime.has(identityKey)) {
        return false;
      }
      seenContainerAtTime.add(identityKey);

      const uid = sample.providerResourceUid;
      if (uid === null || uid.length === 0) {
        return false;
      }
      if (seriesUid === null) {
        seriesUid = uid;
      } else if (uid !== seriesUid) {
        return false;
      }
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

type SparklineDownsamplePoint = {
  value: number;
  quality: TelemetryQuality;
  /** Index in the pre-downsample series; anchors SVG x to the sample grid. */
  originalIndex: number;
};

/**
 * Downsample sparkline samples for SVG while preferring non-ok representation.
 * Plain even sampling can drop partial/stale samples and erase quality gaps;
 * when non-ok + endpoints exceed maxPoints, keep a bounded representative
 * subset of non-ok indices (never more than maxPoints total).
 * Returned points carry originalIndex so geometry can keep the time grid after
 * ok anchors are evicted.
 */
function downsampleSparklinePreservingQuality(
  points: readonly SparklineInputPoint[],
  maxPoints: number = MAX_SPARKLINE_POINTS,
): SparklineDownsamplePoint[] {
  const asDownsamplePoint = (idx: number): SparklineDownsamplePoint => ({
    value: points[idx]!.value,
    quality: sparklinePointQuality(points[idx]!),
    originalIndex: idx,
  });

  if (points.length <= maxPoints) {
    return points.map((_, idx) => asDownsamplePoint(idx));
  }
  if (maxPoints <= 0) {
    return [];
  }
  if (maxPoints === 1) {
    return [asDownsamplePoint(points.length - 1)];
  }

  const last = points.length - 1;
  const keep = new Set<number>([0, last]);

  const nonOkInterior: number[] = [];
  for (let i = 1; i < last; i++) {
    if (sparklinePointQuality(points[i]!) !== "ok") {
      nonOkInterior.push(i);
    }
  }

  const interiorBudget = maxPoints - keep.size;
  if (nonOkInterior.length <= interiorBudget) {
    for (const idx of nonOkInterior) {
      keep.add(idx);
    }
  } else if (interiorBudget > 0) {
    for (const idx of downsampleSeriesForSparkline(nonOkInterior, interiorBudget)) {
      keep.add(idx);
    }
  }

  if (keep.size < maxPoints) {
    const evenIdx = downsampleSeriesForSparkline(
      Array.from({ length: points.length }, (_, i) => i),
      maxPoints,
    );
    for (const idx of evenIdx) {
      if (keep.size >= maxPoints) {
        break;
      }
      keep.add(idx);
    }
  }

  return [...keep].sort((a, b) => a - b).map(asDownsamplePoint);
}

export type SparklinePath = {
  d: string;
  quality: TelemetryQuality;
};

export type SparklineMarker = {
  x: number;
  y: number;
  quality: TelemetryQuality;
};

export type SparklineGeometry = {
  w: number;
  h: number;
  /** Contiguous same-quality runs with 2+ samples (gaps between quality changes). */
  paths: SparklinePath[];
  /** Single-sample runs (including a lone series point). */
  markers: SparklineMarker[];
  /** Worst non-ok quality present, or null when every point is ok. */
  qualification: "partial" | "stale" | null;
};

type SparklineInputPoint = {
  value: number;
  quality?: TelemetryQuality;
};

function sparklinePointQuality(point: SparklineInputPoint): TelemetryQuality {
  return point.quality ?? "ok";
}

function worstSparklineQualification(
  qualities: readonly TelemetryQuality[],
): "partial" | "stale" | null {
  let worst: "partial" | "stale" | null = null;
  for (const quality of qualities) {
    if (quality === "stale") {
      return "stale";
    }
    if (quality === "partial") {
      worst = "partial";
    }
  }
  return worst;
}

/** True when any original sample strictly between fromIdx and toIdx differs in quality. */
function hasOriginalQualityBreak(
  points: readonly SparklineInputPoint[],
  fromIdx: number,
  toIdx: number,
  runQuality: TelemetryQuality,
): boolean {
  for (let j = fromIdx + 1; j < toIdx; j++) {
    if (sparklinePointQuality(points[j]!) !== runQuality) {
      return true;
    }
  }
  return false;
}

function pathDFromCoords(coords: readonly string[]): string {
  return `M ${coords.join(" L ")}`;
}

/**
 * Map a sorted value series into SVG sparkline geometry.
 * Contiguous same-quality runs stay connected; quality transitions leave gaps.
 * One-sample runs become markers. Non-ok history is never a single unqualified path.
 * Qualification uses the full series; SVG downsampling prefers non-ok samples
 * within the point budget. X uses each sample's original series index so
 * ok-anchor eviction cannot warp the time grid or join previously gapped
 * non-ok runs into one path.
 */
export function buildSparklineGeometry(
  points: readonly SparklineInputPoint[],
  width = 120,
  height = 28,
): SparklineGeometry | null {
  if (points.length === 0) {
    return null;
  }
  const qualification = worstSparklineQualification(points.map(sparklinePointQuality));
  const series = downsampleSparklinePreservingQuality(points);
  const lastOrig = points.length - 1;

  if (series.length === 1) {
    return {
      w: width,
      h: height,
      paths: [],
      markers: [{ x: width / 2, y: height / 2, quality: series[0]!.quality }],
      qualification,
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
  const coords = series.map((p) => {
    const x = (p.originalIndex / lastOrig) * width;
    const y = height - ((p.value - min) / span) * (height - 4) - 2;
    return {
      x,
      y,
      text: `${x.toFixed(1)},${y.toFixed(1)}`,
      quality: p.quality,
      originalIndex: p.originalIndex,
    };
  });

  const paths: SparklinePath[] = [];
  const markers: SparklineMarker[] = [];
  let runStart = 0;
  for (let i = 1; i <= coords.length; i++) {
    const qualityBreak = i === coords.length || coords[i]!.quality !== coords[runStart]!.quality;
    // Index gaps from downsampling must not join distinct original quality runs
    // (e.g. two partial clusters separated by ok samples).
    const originalRunBreak =
      !qualityBreak &&
      hasOriginalQualityBreak(
        points,
        coords[i - 1]!.originalIndex,
        coords[i]!.originalIndex,
        coords[runStart]!.quality,
      );
    if (!qualityBreak && !originalRunBreak) {
      continue;
    }
    const run = coords.slice(runStart, i);
    const quality = run[0]!.quality;
    if (run.length === 1) {
      markers.push({ x: run[0]!.x, y: run[0]!.y, quality });
    } else {
      paths.push({
        d: pathDFromCoords(run.map((c) => c.text)),
        quality,
      });
    }
    runStart = i;
  }

  return {
    w: width,
    h: height,
    paths,
    markers,
    qualification,
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
    !isNonNegativeFiniteNumber(payload.stale_after_seconds, MAX_STALE_AFTER_SECONDS) ||
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
  if (!assertSampleIdentities([cpuSamples, memorySamples], expectedResourceUid)) {
    return null;
  }
  const estimate = parseEstimate(payload.estimate);
  if (estimate === null) {
    return null;
  }

  // Envelope state must agree with allocation and estimate. An "unallocated"
  // notice with admitted resources, usage samples, or a dollar cost is a
  // contradictory operator view — fail closed rather than render it.
  const envelopeUnallocated = payload.state === "unallocated";
  if (envelopeUnallocated !== (estimate.estimateState === "unallocated")) {
    return null;
  }
  if (envelopeUnallocated) {
    if (admitted !== null) {
      return null;
    }
    if (cpuSamples.length > 0 || memorySamples.length > 0) {
      return null;
    }
    if (estimate.estimatedUsd !== null) {
      return null;
    }
  } else if (admitted === null) {
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

/** Epoch ms for a parsed RFC3339 sample timestamp (already validated upstream). */
function timestampInstantMs(value: string): number {
  return Date.parse(value);
}

/**
 * Fractional digits beyond milliseconds (Date.parse precision). Trailing zeros
 * are stripped so `.000001` and `.000001000` share identity.
 */
function submillisecondFraction(value: string): string {
  const fractionalSeconds = RFC3339_DATE_TIME.exec(value)?.[7] ?? "";
  return fractionalSeconds.slice(4).replace(/0+$/, "");
}

/**
 * Exact instant identity for partitioning: epoch ms + sub-ms fraction.
 * Equates Z / offset spellings of the same UTC moment; keeps distinct
 * sub-millisecond instants that Date.parse would otherwise collapse.
 */
function timestampInstantKey(value: string): string {
  return `${timestampInstantMs(value)}\0${submillisecondFraction(value)}`;
}

/** Order two validated RFC3339 instants, including sub-millisecond fraction. */
function compareTimestampInstants(left: string, right: string): number {
  const leftMs = timestampInstantMs(left);
  const rightMs = timestampInstantMs(right);
  if (leftMs !== rightMs) {
    return leftMs < rightMs ? -1 : 1;
  }
  const leftFrac = submillisecondFraction(left);
  const rightFrac = submillisecondFraction(right);
  const precision = Math.max(leftFrac.length, rightFrac.length);
  const normalizedLeft = leftFrac.padEnd(precision, "0");
  const normalizedRight = rightFrac.padEnd(precision, "0");
  return normalizedLeft === normalizedRight
    ? 0
    : normalizedLeft < normalizedRight
      ? -1
      : 1;
}

function latestTimestamp(samples: ParsedTelemetrySample[]): string | null {
  let latest: string | null = null;
  for (const sample of samples) {
    if (
      latest === null ||
      compareTimestampInstants(sample.sampleTime, latest) > 0
    ) {
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

/** True when a same-instant partition lists the same container more than once. */
function hasDuplicateContainers(
  samples: readonly ParsedTelemetrySample[],
): boolean {
  const seen = new Set<string>();
  for (const sample of samples) {
    if (seen.has(sample.containerName)) {
      return true;
    }
    seen.add(sample.containerName);
  }
  return false;
}

/**
 * True when every sample shares the same interval_start/interval_end instant
 * (normalized key, including sub-ms). Alternate RFC3339 spellings of the same
 * moment must match; samples that only share sample_time may still cover
 * different measurement windows and must not be treated as one complete pod
 * reading.
 */
function samplesShareIntervalTuple(samples: ParsedTelemetrySample[]): boolean {
  if (samples.length <= 1) {
    return true;
  }
  const first = samples[0];
  const startKey = timestampInstantKey(first.intervalStart);
  const endKey = timestampInstantKey(first.intervalEnd);
  for (let i = 1; i < samples.length; i++) {
    const sample = samples[i];
    if (
      timestampInstantKey(sample.intervalStart) !== startKey ||
      timestampInstantKey(sample.intervalEnd) !== endKey
    ) {
      return false;
    }
  }
  return true;
}

/**
 * Build sparkline history as one pod-total point per sample_time instant.
 * Raw per-container rows must not be plotted as a single line — same-timestamp
 * containers would form a fake trend and disagree with meter totals.
 * Staggered scrapes that leave a timestamp missing containers seen elsewhere
 * in the series are marked partial (same completeness rule as the latest meter).
 * Same-timestamp samples with mismatched interval windows are omitted — they are
 * not one measurement window, so their sum must not appear as a pod total.
 */
function buildPodTotalSeries(
  samples: ParsedTelemetrySample[],
): WorkspaceTelemetrySeriesPoint[] {
  if (samples.length === 0) {
    return [];
  }
  const seriesContainers = new Set<string>();
  // Group by exact instant so Z / offset spellings share a partition while
  // distinct sub-ms readings stay separate.
  const byTime = new Map<string, ParsedTelemetrySample[]>();
  for (const sample of samples) {
    seriesContainers.add(sample.containerName);
    const key = timestampInstantKey(sample.sampleTime);
    const group = byTime.get(key);
    if (group) {
      group.push(sample);
    } else {
      byTime.set(key, [sample]);
    }
  }
  const points: WorkspaceTelemetrySeriesPoint[] = [];
  for (const group of byTime.values()) {
    // Aggregate only samples that share the full interval tuple. Differing
    // interval_start/interval_end at the same sample_time are different
    // measurement windows — do not sum them into a pod-total point.
    if (!samplesShareIntervalTuple(group)) {
      continue;
    }
    // Fail closed: duplicate container@instant must not become a pod total
    // (parse rejects these; projection still guards if identity is bypassed).
    if (hasDuplicateContainers(group)) {
      continue;
    }
    const value = accumulateSampleTotal(group);
    // Fail closed: omit points whose byte pod-total is not an exact safe integer.
    if (value === null) {
      continue;
    }
    let quality: TelemetryQuality = "ok";
    const names: string[] = [];
    const groupContainers = new Set<string>();
    for (const sample of group) {
      names.push(sample.containerName);
      groupContainers.add(sample.containerName);
      if (sample.quality !== "ok") {
        // Prefer "stale" over "partial" when both appear; otherwise any non-ok.
        if (sample.quality === "stale" || quality === "ok") {
          quality = sample.quality;
        }
      }
    }
    // Incomplete partition vs containers seen in the series: do not present as ok.
    if (quality !== "stale") {
      for (const name of seriesContainers) {
        if (!groupContainers.has(name)) {
          quality = "partial";
          break;
        }
      }
    }
    names.sort();
    points.push({
      sampleTime: group[0].sampleTime,
      value,
      quality,
      containerName: names.join(","),
    });
  }
  points.sort((a, b) => compareTimestampInstants(a.sampleTime, b.sampleTime));
  return points;
}

/**
 * Sum samples that share the same sample_time instant (normalized key).
 * Never merges across different moments. If the latest partition is missing
 * containers that appear elsewhere in the series, or containers disagree on
 * interval windows, treat usage as partial and unavailable (null) rather than
 * presenting the subset / mismatched windows as a complete pod total.
 */
function aggregateAtTimestamp(samples: ParsedTelemetrySample[]): {
  used: number | null;
  usedPartial: boolean;
  /** True when the displayed (latest) partition includes a producer-stale sample. */
  usedStale: boolean;
  containerNames: string[] | null;
  sampleTime: string | null;
  series: WorkspaceTelemetrySeriesPoint[];
} {
  const series = buildPodTotalSeries(samples);
  if (samples.length === 0) {
    return {
      used: null,
      usedPartial: false,
      usedStale: false,
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
      usedStale: false,
      containerNames: null,
      sampleTime: null,
      series,
    };
  }
  const latestKey = timestampInstantKey(sampleTime);
  const atLatest = samples.filter(
    (s) => timestampInstantKey(s.sampleTime) === latestKey,
  );
  let usedPartial = false;
  let usedStale = false;
  let incompletePartition = false;
  const containerNames: string[] = [];
  for (const sample of atLatest) {
    // Fail closed: only exact "ok" is complete usage. Parse rejects unknown
    // qualities; projection still treats any non-ok allowlisted quality
    // (partial, stale) as incomplete rather than appearing complete.
    if (sample.quality !== "ok") {
      usedPartial = true;
      if (sample.quality === "stale") {
        usedStale = true;
      }
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
  // Same sample_time with different interval windows is not one pod reading.
  if (!samplesShareIntervalTuple(atLatest)) {
    incompletePartition = true;
    usedPartial = true;
  }
  // Duplicate container in the latest partition would inflate the pod sum.
  if (hasDuplicateContainers(atLatest)) {
    incompletePartition = true;
    usedPartial = true;
  }
  return {
    used: incompletePartition || used === null ? null : used,
    usedPartial,
    usedStale,
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
  // Fail closed: a future timestamp yields a negative age and would otherwise
  // win latestTimestamp while staying isStale:false indefinitely (malformed
  // producer time or severe collector clock skew).
  if (ms > nowMs) {
    return true;
  }
  const thresholdMs = staleAfterSeconds * 1000;
  // Fail closed: an overflowing threshold can never be exceeded, so aged
  // telemetry would otherwise appear fresh forever.
  if (!Number.isFinite(thresholdMs)) {
    return true;
  }
  return nowMs - ms > thresholdMs;
}

/**
 * Stale when producer marks state/quality stale, when a displayed CPU/memory
 * aggregate includes a stale sample, or when the envelope, admitted allocation
 * snapshot, or any meter sample time used for displayed CPU/memory values
 * exceeds the threshold. Fresh envelopes/meters must not mask aged allocation
 * requests/limits, aged resource samples, or producer-stale current readings.
 */
function computeIsStale(
  presentation: ParsedTelemetryPresentation,
  nowMs: number,
  meterSampleTimes: Array<string | null>,
  meterHasStaleSample: boolean,
): boolean {
  if (presentation.state === "stale" || presentation.quality === "stale") {
    return true;
  }
  if (meterHasStaleSample) {
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
    return meterTimes.reduce((a, b) =>
      compareTimestampInstants(a, b) >= 0 ? a : b,
    );
  }
  return observedAt;
}

function meterSampleTimesAreMixed(
  meterSampleTimes: Array<string | null>,
): boolean {
  const distinct = new Set<string>();
  for (const value of meterSampleTimes) {
    if (typeof value === "string") {
      distinct.add(timestampInstantKey(value));
    }
  }
  return distinct.size > 1;
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
  const sampleTimeMixed = meterSampleTimesAreMixed(meterSampleTimes);

  return {
    state: presentation.state,
    quality: presentation.quality,
    view: presentation.view,
    staleAfterSeconds: presentation.staleAfterSeconds,
    observedAt: presentation.observedAt,
    sampleTime,
    sampleTimeMixed,
    isStale: computeIsStale(
      presentation,
      nowMs,
      meterSampleTimes,
      cpuAgg.usedStale || memAgg.usedStale,
    ),
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
      sampleTime: cpuAgg.sampleTime,
      series: cpuAgg.series,
      containerNamesAtSample: cpuAgg.containerNames,
    },
    memory: {
      usedBytes: memAgg.used,
      usedPartial: memAgg.usedPartial,
      sampleTime: memAgg.sampleTime,
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

/**
 * Format CPU cores for the resource meter.
 * Sub-centicore samples (e.g. 0.004) must not collapse to "0 cores" via toFixed(2).
 * Sub-0.005 millicore samples must not collapse to "0 millicores" either.
 */
export function formatCores(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  const abs = Math.abs(value);
  if (abs > 0 && abs < 0.01) {
    return `${formatScaledDecimal(value * 1000)} millicores`;
  }
  return `${formatScaledDecimal(value)} cores`;
}

/** Compact decimal text; never round a nonzero finite value to a literal "0". */
function formatScaledDecimal(value: number): string {
  if (Number.isInteger(value)) {
    return String(value);
  }
  const fixed2 = value.toFixed(2).replace(/\.?0+$/, "");
  if (fixed2 !== "" && Number(fixed2) !== 0) {
    return fixed2;
  }
  // toFixed(2) collapsed a nonzero reading (e.g. 0.001 → "0.00").
  // Keep enough fractional digits that the display stays nonzero.
  // toFixed rejects digits > 100; values below that fixed-point range
  // need an exponential fallback so the meter does not throw.
  const abs = Math.abs(value);
  const fractionDigits = Math.max(2, Math.ceil(-Math.log10(abs)) + 1);
  if (fractionDigits > 100) {
    return value.toExponential(2).replace(/\.?0+e/, "e");
  }
  return value.toFixed(fractionDigits).replace(/\.?0+$/, "");
}
