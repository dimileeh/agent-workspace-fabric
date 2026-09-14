/**
 * Backend-neutral TelemetryPresentation reader + UI projection.
 *
 * Producer fixtures: awf-cloud be0d7a3ac9871ef951860666a236c9c481f45c98
 * (`console_telemetry/contract_fixtures`). Schema under review — fail closed;
 * do not silently normalize types or invent zero/free for missing allocation.
 */

import {
  isFiniteTimestampString,
  compareTimestampInstants,
  isSampleTimeWithinMeasurementInterval,
  timestampInstantKey,
  timestampInstantMs,
} from "./console-workspace-telemetry-timestamps.ts";

export { MAX_RFC3339_TIMESTAMP_LENGTH } from "./console-workspace-telemetry-timestamps.ts";

export {
  MAX_SPARKLINE_POINTS,
  buildSparklineGeometry,
  downsampleSeriesForSparkline,
  formatCores,
} from "./console-workspace-telemetry-display.ts";
export type {
  SparklineGeometry,
  SparklineMarker,
  SparklinePath,
} from "./console-workspace-telemetry-display.ts";

export const TELEMETRY_VIEWS = ["1h", "6h", "24h"] as const;
export type TelemetryViewWindow = (typeof TELEMETRY_VIEWS)[number];

/** Wall-clock duration each `view` selector represents (seconds). */
const TELEMETRY_VIEW_DURATION_SECONDS: Record<TelemetryViewWindow, number> = {
  "1h": 3600,
  "6h": 21600,
  "24h": 86400,
};

export const TELEMETRY_STATES = ["success", "partial", "stale", "unallocated"] as const;
export type TelemetryPresentationState = (typeof TELEMETRY_STATES)[number];

export const TELEMETRY_QUALITIES = ["ok", "partial", "stale"] as const;
export type TelemetryQuality = (typeof TELEMETRY_QUALITIES)[number];

/**
 * Fixture-backed metric_type per sample series unit.
 * Enforced so a memory meter cannot land in cpu_cores_samples (or vice versa)
 * and render under the wrong label when unit alone happens to match.
 */
export const TELEMETRY_METRIC_TYPE_BY_UNIT = {
  cores: "kubernetes.io/container/cpu/core_usage_time",
  bytes: "kubernetes.io/container/memory/used_bytes",
} as const;

export const ESTIMATE_STATES = ["complete", "partial", "unpriced", "unallocated"] as const;
export type EstimateState = (typeof ESTIMATE_STATES)[number];

export type EstimateScope = "view" | "resource_attempt";

export type CostDisplayState = "not_recorded" | "complete" | "partial" | "unpriced" | "unallocated";

/** Operator-facing exclusion copy (not producer data_quality_notes). */
export const COST_EXCLUSION_NOTE =
  "Excludes discounts, control-plane/shared infrastructure, network, and persistent storage. Shared Core monitor runtime is unallocated, not free.";

/** Reject absurd magnitudes (CPU cores / USD) rather than accept scientific junk. */
const MAX_DECIMAL_MAGNITUDE = 1e15;
/**
 * Bound scalar strings independently of sample counts to limit UI-thread work.
 * Cores/USD/bytes need ≤16 integer digits plus a short fraction; check length
 * before regex/Number so a single field cannot force unbounded scanning.
 */
export const MAX_DECIMAL_STRING_LENGTH = 64;
/**
 * Bound container_name before retention, identity keys, sorting and partition
 * joins. DNS labels fit in 63 chars; profile service names fit in 64.
 */
export const MAX_CONTAINER_NAME_LENGTH = 64;
/**
 * Bound compute_class, region and pod_phase before snapshot retention and DOM
 * projection; type checks and numeric/sample caps do not limit label sizes.
 */
export const MAX_ALLOCATION_LABEL_LENGTH = 64;
/**
 * Bound rate_table_version, evidence.source and evidence.rate_table_version
 * before trim/retention/DOM projection; fixture IDs and URLs use ~28–52 chars.
 */
export const MAX_RATE_PROVENANCE_LENGTH = 64;
/**
 * Bound provider_resource_uid before retention and repeated identity checks
 * across samples. K8s UUIDs use 36 chars; sample caps alone cannot bound strings.
 */
export const MAX_PROVIDER_RESOURCE_UID_LENGTH = 64;
/**
 * Reject memory/ephemeral byte counts above MAX_SAFE_INTEGER to prevent rounding.
 */
const MAX_BYTES_MAGNITUDE = Number.MAX_SAFE_INTEGER;
/**
 * Hard cap on samples accepted per metric array from a telemetry payload.
 * Larger arrays fail closed before parse so a misbehaving producer cannot
 * force unbounded allocation or an O(n log n) sort in the console.
 */
export const MAX_TELEMETRY_SAMPLES = 2048;
/**
 * Bound data_quality_notes before scanning for strings, even though notes are
 * discarded rather than projected, to prevent unbounded UI-thread work.
 */
export const MAX_DATA_QUALITY_NOTES = 64;
/**
 * Stage3 live freshness cap: sample/envelope/admitted times expire after 5m.
 * Reject larger thresholds that would keep old readings fresh or overflow
 * seconds * 1000 to Infinity.
 */
export const MAX_STALE_AFTER_SECONDS = 5 * 60;

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
  computeClass: string | null;
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
  windowEndAt: string | null;
  estimateScope: EstimateScope;
  estimate: ParsedEstimate | null;
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
  /** Future producer time relative to the projection clock, independent of aging. */
  hasFutureTimestamp: boolean;
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
    /** True when the displayed CPU partition includes a producer-stale sample. */
    usedStale: boolean;
    sampleTime: string | null;
    series: WorkspaceTelemetrySeriesPoint[];
    containerNamesAtSample: string[] | null;
  };
  memory: {
    usedBytes: number | null;
    usedPartial: boolean;
    /** True when the displayed memory partition includes a producer-stale sample. */
    usedStale: boolean;
    sampleTime: string | null;
    series: WorkspaceTelemetrySeriesPoint[];
    containerNamesAtSample: string[] | null;
  };
  estimate: {
    displayState: CostDisplayState;
    scope: EstimateScope;
    currency: "USD";
    estimatedUsd: number | null;
    rateTableVersion: string | null;
    rateSource: string | null;
    pricedIntervalSeconds: number | null;
    unpricedIntervalSeconds: number | null;
  };
  exclusionNote: string;
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

function isNullableTimestamp(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && isFiniteTimestampString(value));
}

/**
 * Strict nonnegative finite decimal string. Rejects NaN/Infinity tokens,
 * scientific notation, negatives, oversized magnitudes, and lexical nonzeros
 * that underflow to Number 0 (below the representable range).
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
  // Reject before regex/Number so a single overlong field cannot burn
  // memory or UI-thread time scanning an unbounded producer string.
  if (value.length > MAX_DECIMAL_STRING_LENGTH) {
    return undefined;
  }
  // Validate the original string — never trim. Whitespace-padded values
  // (" 0.25 ") are producer serialization drift and must fail closed.
  if (!DECIMAL_STRING.test(value)) {
    return undefined;
  }
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0 || n > max) {
    return undefined;
  }
  // Lexical nonzero that underflows to 0 (below Number.MIN_VALUE) must not be
  // accepted as literal zero — CPU/USD would otherwise project fake-zero usage.
  // Only canonical lexical zeros ("0", "0.0", …) may parse as numeric 0.
  if (n === 0 && !/^0(?:\.0+)?$/.test(value)) {
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

/** True when metric_type is the fixture type for this series unit (exact tuple). */
function isSeriesMetricType(
  metricType: unknown,
  expectedUnit: "cores" | "bytes",
): metricType is (typeof TELEMETRY_METRIC_TYPE_BY_UNIT)[typeof expectedUnit] {
  return metricType === TELEMETRY_METRIC_TYPE_BY_UNIT[expectedUnit];
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
    // Reject before retaining so identity keys / partition joins cannot allocate
    // unbounded strings across up to MAX_TELEMETRY_SAMPLES rows.
    if (item.container_name.length > MAX_CONTAINER_NAME_LENGTH) {
      return null;
    }
    if (item.container_name !== item.container_name.trim()) {
      return null;
    }
    if (item.unit !== expectedUnit) {
      return null;
    }
    // Cloud memory gauges have no measurement start; normalize only internally.
    const intervalStart =
      expectedUnit === "bytes" && item.interval_start === null
        ? item.sample_time
        : item.interval_start;
    if (
      typeof item.sample_time !== "string" ||
      !isFiniteTimestampString(item.sample_time) ||
      typeof intervalStart !== "string" ||
      !isFiniteTimestampString(intervalStart) ||
      typeof item.interval_end !== "string" ||
      !isFiniteTimestampString(item.interval_end)
    ) {
      return null;
    }
    if (
      expectedUnit === "bytes" && item.interval_start === null &&
      compareTimestampInstants(item.sample_time, item.interval_end) !== 0
    ) {
      return null;
    }
    // Reject reversed measurement windows (equal start/end remain valid).
    if (compareTimestampInstants(intervalStart, item.interval_end) > 0) {
      return null;
    }
    // Out-of-window sample_time would skew partition selection, ordering, and freshness.
    if (
      !isSampleTimeWithinMeasurementInterval(
        item.sample_time,
        intervalStart,
        item.interval_end,
      )
    ) {
      return null;
    }
    // Fail closed: unknown/typo quality must not parse as complete usage.
    if (!isOneOf(item.quality, TELEMETRY_QUALITIES)) {
      return null;
    }
    // Fail closed: metric_type must match the series unit so memory meters
    // cannot land in cpu_cores_samples (or vice versa) and render mislabeled.
    // CPU fixtures use core_usage_time with unit cores (producer naming quirk).
    if (!isSeriesMetricType(item.metric_type, expectedUnit)) {
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
    // When evidence declares metric_type, it must match the same series tuple.
    if (item.evidence !== undefined && item.evidence !== null) {
      if (!isPlainObject(item.evidence)) {
        return null;
      }
      if (
        item.evidence.metric_type !== undefined &&
        !isSeriesMetricType(item.evidence.metric_type, expectedUnit)
      ) {
        return null;
      }
    }
    if (
      item.provider_resource_uid !== undefined &&
      typeof item.provider_resource_uid !== "string"
    ) {
      return null;
    }
    // Reject before retaining so identity comparisons cannot allocate/compare
    // unbounded strings across up to MAX_TELEMETRY_SAMPLES rows.
    if (
      typeof item.provider_resource_uid === "string" &&
      item.provider_resource_uid.length > MAX_PROVIDER_RESOURCE_UID_LENGTH
    ) {
      return null;
    }
    samples.push({
      containerName: item.container_name,
      sampleTime: item.sample_time,
      intervalStart,
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
 * True when every sample_time / interval_end across the
 * given series falls inside the selected view window ending at the chart anchor
 * (`[observedAt - viewDuration, observedAt]` inclusive). Empty input is
 * vacuously valid. Samples without a chart anchor fail closed. CPU rate
 * starts can precede the left edge; legacy memory intervals keep their guard.
 * Prevents plotting a multi-hour series under a
 * shorter view selector, and prevents clustered-but-offset timestamps (e.g.
 * a 1h cluster two days before observed_at) from rendering as that window.
 * CPU and memory are checked together because both series share one plotted
 * window.
 */
function samplesFitViewWindow(
  seriesList: readonly ParsedTelemetrySample[][],
  viewDurationSeconds: number,
  observedAt: string | null,
): boolean {
  let earliest: string | null = null;
  let latest: string | null = null;
  for (const samples of seriesList) {
    for (const sample of samples) {
      for (const timestamp of [
        sample.sampleTime,
        ...(sample.unit === "bytes" ? [sample.intervalStart] : []),
        sample.intervalEnd,
      ]) {
        if (earliest === null || compareTimestampInstants(timestamp, earliest) < 0) {
          earliest = timestamp;
        }
        if (latest === null || compareTimestampInstants(timestamp, latest) > 0) {
          latest = timestamp;
        }
      }
    }
  }
  if (earliest === null || latest === null) {
    return true;
  }
  if (observedAt === null) {
    return false;
  }
  const observedAtMs = timestampInstantMs(observedAt);
  if (!Number.isFinite(observedAtMs)) {
    return false;
  }
  return (
    compareTimestampInstants(earliest, observedAt, -viewDurationSeconds * 1000) >= 0 &&
    compareTimestampInstants(latest, observedAt) <= 0
  );
}

/**
 * Read a resource UID from an admitted/ownership/evidence object.
 * null = field absent; undefined = present but not a non-empty string (fail closed).
 */
function readPresentationResourceUidField(
  container: Record<string, unknown> | null,
  field = "provider_resource_uid",
): string | null | undefined {
  if (container === null) {
    return null;
  }
  if (!(field in container) || container[field] === undefined) {
    return null;
  }
  const uid = container[field];
  if (typeof uid !== "string" || uid.length === 0) {
    return undefined;
  }
  // Fail closed before identity resolve compares/retains unbounded UIDs.
  if (uid.length > MAX_PROVIDER_RESOURCE_UID_LENGTH) {
    return undefined;
  }
  return uid;
}

/**
 * Resolve the presentation's resource identity when the producer supplies one.
 * Include allocation evidence alongside admitted and ownership identities.
 * Returns undefined when any field is malformed, or present identities disagree
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
  const evidenceUid = readPresentationResourceUidField(
    isPlainObject(payload.admitted) && isPlainObject(payload.admitted.evidence)
      ? payload.admitted.evidence
      : null,
    "pod_uid",
  );
  let resourceUid: string | null = null;
  for (const uid of [admittedUid, ownershipUid, evidenceUid]) {
    if (uid === undefined) {
      return undefined;
    }
    if (uid !== null) {
      if (resourceUid !== null && uid !== resourceUid) {
        return undefined;
      }
      resourceUid = uid;
    }
  }
  return resourceUid;
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
      if (uid === null || uid.trim().length === 0) {
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
 * Kubernetes request-vs-limit: when both sides are present, request must not
 * exceed limit. A null limit means unbounded; a null request is unconstrained.
 */
function requestWithinLimit(request: number | null, limit: number | null): boolean {
  if (request === null || limit === null) {
    return true;
  }
  return request <= limit;
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
  if (!requestWithinLimit(cpuRequestCores, cpuLimitCores)) {
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
  if (
    !requestWithinLimit(value.memory_request_bytes, value.memory_limit_bytes) ||
    !requestWithinLimit(
      value.ephemeral_storage_request_bytes,
      value.ephemeral_storage_limit_bytes,
    )
  ) {
    return undefined;
  }
  if (typeof value.observed_at !== "string" || !isFiniteTimestampString(value.observed_at)) {
    return undefined;
  }
  if (
    typeof value.billable !== "boolean" ||
    (value.compute_class !== null && typeof value.compute_class !== "string") ||
    typeof value.container_creating !== "boolean" ||
    typeof value.partial !== "boolean" ||
    typeof value.pod_phase !== "string" ||
    typeof value.region !== "string"
  ) {
    return undefined;
  }
  if (
    (value.compute_class !== null && value.compute_class.length > MAX_ALLOCATION_LABEL_LENGTH) ||
    value.pod_phase.length > MAX_ALLOCATION_LABEL_LENGTH ||
    value.region.length > MAX_ALLOCATION_LABEL_LENGTH
  ) {
    return undefined;
  }
  if ((value.compute_class !== null && value.compute_class.trim() === "") || value.region.trim() === "") {
    return undefined;
  }
  // Validate known allocation provenance without projecting it.
  const ownerJobUid = readPresentationResourceUidField(value, "owner_job_uid");
  if (ownerJobUid === undefined || (ownerJobUid !== null && ownerJobUid.trim() === "")) {
    return undefined;
  }
  if (value.evidence !== undefined && value.evidence !== null) {
    if (!isPlainObject(value.evidence)) {
      return undefined;
    }
    for (const field of ["pod_uid", "owner_job_uid"]) {
      const uid = readPresentationResourceUidField(value.evidence, field);
      if (uid === undefined || (uid !== null && uid.trim() === "")) {
        return undefined;
      }
      if (field === "owner_job_uid" && ownerJobUid !== null && uid !== null && uid !== ownerJobUid) {
        return undefined;
      }
    }
  }
  // Unknown bounded metadata is not an invented compute identity.
  const computeClass = isOneOf(value.compute_class, [
    "autopilot", "autopilot-spot", "general-purpose", "balanced",
    "scale-out", "scale-out-arm", "scale-out-x86",
  ]) ? value.compute_class : null;
  return {
    billable: value.billable,
    computeClass,
    containerCreating: value.container_creating,
    cpuRequestCores,
    cpuLimitCores,
    memoryRequestBytes: value.memory_request_bytes,
    memoryLimitBytes: value.memory_limit_bytes,
    ephemeralStorageRequestBytes: value.ephemeral_storage_request_bytes,
    ephemeralStorageLimitBytes: value.ephemeral_storage_limit_bytes,
    observedAt: value.observed_at,
    partial: value.partial || computeClass === null,
    podPhase: value.pod_phase,
    region: value.region,
  };
}

function parseEstimate(
  value: unknown,
  view: TelemetryViewWindow,
  scope: EstimateScope,
): ParsedEstimate | null {
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
  // Each integer duration is bounded by 1e15 above, so their combined duration
  // is finite and exact (<= 2e15 < Number.MAX_SAFE_INTEGER), even for attempts.
  // Legacy/view estimates cover the chart; retained attempt estimates do not.
  const viewDurationSeconds = TELEMETRY_VIEW_DURATION_SECONDS[view];
  if (
    scope === "view" &&
    value.priced_interval_seconds + value.unpriced_interval_seconds > viewDurationSeconds
  ) {
    return null;
  }
  // Fail closed on estimate_state vs amount/interval contradictions so
  // resolveCostDisplayState cannot present contradictory pricing as complete.
  if (value.estimate_state === "complete") {
    if (
      value.priced_interval_seconds === 0 ||
      value.unpriced_interval_seconds !== 0 ||
      estimatedUsd === null
    ) {
      return null;
    }
  } else if (value.estimate_state === "unallocated") {
    if (
      value.priced_interval_seconds !== 0 ||
      value.unpriced_interval_seconds !== 0 ||
      estimatedUsd !== null
    ) {
      return null;
    }
  } else if (value.estimate_state === "unpriced") {
    if (value.priced_interval_seconds !== 0 || estimatedUsd !== null) {
      return null;
    }
  } else if (value.estimate_state === "partial") {
    // Partial means some coverage is explicitly unpriced. Any amount, even
    // zero, requires priced coverage; priced coverage requires a known amount.
    if (
      value.unpriced_interval_seconds === 0 ||
      (value.priced_interval_seconds === 0 && estimatedUsd !== null) ||
      (value.priced_interval_seconds > 0 && estimatedUsd === null)
    ) {
      return null;
    }
  }
  if (typeof value.rate_table_version !== "string") {
    return null;
  }
  // Bound before trim/retain so a pathological version cannot force unbounded
  // scan work or an overlong Fact projection.
  if (value.rate_table_version.length > MAX_RATE_PROVENANCE_LENGTH) {
    return null;
  }
  let rateSource: string | null = null;
  if (value.evidence !== undefined && value.evidence !== null) {
    if (!isPlainObject(value.evidence)) {
      return null;
    }
    // Known evidence fields must keep their types; do not silently drop a
    // malformed or blank source while still displaying the estimate as complete.
    if (value.evidence.source === null && value.estimate_state !== "unpriced") return null;
    if (value.evidence.source !== undefined && value.evidence.source !== null) {
      if (
        typeof value.evidence.source !== "string" ||
        value.evidence.source.length > MAX_RATE_PROVENANCE_LENGTH ||
        value.evidence.source.trim() === ""
      ) {
        return null;
      }
      rateSource = value.evidence.source;
    }
    // Duplicated rate-table identity must agree with the displayed top-level
    // version — contradictory provenance fails closed.
    if (
      "rate_table_version" in value.evidence &&
      value.evidence.rate_table_version !== undefined
    ) {
      if (typeof value.evidence.rate_table_version !== "string") {
        return null;
      }
      if (value.evidence.rate_table_version.length > MAX_RATE_PROVENANCE_LENGTH) {
        return null;
      }
      // Compare the same empty→null normalization used for display so a blank
      // evidence version cannot disagree with a blank top-level identity, and a
      // non-blank pair must match exactly.
      const evidenceVersion =
        value.evidence.rate_table_version.trim() === ""
          ? null
          : value.evidence.rate_table_version;
      const topLevelVersion =
        value.rate_table_version.trim() === "" ? null : value.rate_table_version;
      if (evidenceVersion !== topLevelVersion) {
        return null;
      }
    }
  }
  const rateTableVersion =
    value.rate_table_version.trim() === "" ? null : value.rate_table_version;
  if (rateTableVersion === null && value.estimate_state !== "unallocated") {
    return null;
  }
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
  const windowEndAt = "window_end_at" in payload ? payload.window_end_at : payload.observed_at;
  if (!isNullableTimestamp(windowEndAt) ||
      ("window_end_at" in payload && windowEndAt === null)) {
    return null;
  }
  if (payload.observed_at !== null && windowEndAt !== null &&
      compareTimestampInstants(payload.observed_at, windowEndAt) > 0) {
    return null;
  }
  const estimateScope = "estimate_scope" in payload ? payload.estimate_scope : "view";
  if (!isOneOf(estimateScope, ["view", "resource_attempt"])) {
    return null;
  }
  // Accept machine notes for schema compatibility; never surface as UI copy.
  if (payload.data_quality_notes !== undefined) {
    if (!Array.isArray(payload.data_quality_notes)) {
      return null;
    }
    // Reject before scanning so the cap bounds CPU, not only retained output.
    if (payload.data_quality_notes.length > MAX_DATA_QUALITY_NOTES) {
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
  // Select by sample/end time. A CPU rate may start before the left chart edge.
  const viewDurationSeconds = TELEMETRY_VIEW_DURATION_SECONDS[payload.view];
  if (!samplesFitViewWindow([cpuSamples, memorySamples], viewDurationSeconds, windowEndAt)) {
    return null;
  }
  const expectedResourceUid = resolvePresentationResourceUid(payload);
  if (expectedResourceUid === undefined) {
    return null;
  }
  if (!assertSampleIdentities([cpuSamples, memorySamples], expectedResourceUid)) {
    return null;
  }
  const estimate = payload.estimate === null ? null : parseEstimate(payload.estimate, payload.view, estimateScope);
  if (estimate === null && payload.estimate !== null) {
    return null;
  }

  // Envelope state must agree with allocation and estimate. An "unallocated"
  // notice with admitted resources, usage samples, or a dollar cost is a
  // contradictory operator view — fail closed rather than render it.
  const envelopeUnallocated = payload.state === "unallocated";
  if (envelopeUnallocated !== (estimate?.estimateState === "unallocated")) {
    return null;
  }
  if (envelopeUnallocated) {
    if (admitted !== null) {
      return null;
    }
    if (cpuSamples.length > 0 || memorySamples.length > 0) {
      return null;
    }
    if (estimate?.estimatedUsd !== null) {
      return null;
    }
  }

  return {
    state: payload.state,
    quality: payload.quality,
    view: payload.view,
    staleAfterSeconds: payload.stale_after_seconds,
    observedAt: payload.observed_at,
    windowEndAt,
    estimateScope,
    admitted,
    cpuSamples,
    memorySamples,
    estimate,
  };
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

/** Container names observed across one or more sample arrays. */
function collectContainerNames(
  ...sampleLists: readonly (readonly ParsedTelemetrySample[])[]
): Set<string> {
  const names = new Set<string>();
  for (const samples of sampleLists) {
    for (const sample of samples) {
      names.add(sample.containerName);
    }
  }
  return names;
}

/**
 * Build sparkline history as one pod-total point per sample_time instant.
 * Raw per-container rows must not be plotted as a single line — same-timestamp
 * containers would form a fake trend and disagree with meter totals.
 * Staggered scrapes that leave a timestamp missing containers seen elsewhere
 * in the series (or on the sibling meter via `expectedContainers`) are marked
 * partial (same completeness rule as the latest meter).
 * Same-timestamp samples with mismatched interval windows are omitted — they are
 * not one measurement window, so their sum must not appear as a pod total.
 */
function buildPodTotalSeries(
  samples: ParsedTelemetrySample[],
  expectedContainers?: ReadonlySet<string>,
): WorkspaceTelemetrySeriesPoint[] {
  if (samples.length === 0) {
    return [];
  }
  const seriesContainers =
    expectedContainers ?? collectContainerNames(samples);
  // Group by exact instant so Z / offset spellings share a partition while
  // distinct sub-ms readings stay separate.
  const byTime = new Map<string, ParsedTelemetrySample[]>();
  for (const sample of samples) {
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
 * containers that appear elsewhere in the series (or on the sibling meter via
 * `expectedContainers`), or containers disagree on interval windows, treat
 * usage as partial and unavailable (null) rather than presenting the subset /
 * mismatched windows as a complete pod total.
 */
function aggregateAtTimestamp(
  samples: ParsedTelemetrySample[],
  expectedContainers?: ReadonlySet<string>,
): {
  used: number | null;
  usedPartial: boolean;
  /** True when the displayed (latest) partition includes a producer-stale sample. */
  usedStale: boolean;
  containerNames: string[] | null;
  sampleTime: string | null;
  series: WorkspaceTelemetrySeriesPoint[];
} {
  const series = buildPodTotalSeries(samples, expectedContainers);
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
  // requests/limits. When `expectedContainers` is supplied (cross-meter union),
  // also require containers observed only on the sibling meter.
  const containersAtLatest = new Set(containerNames);
  const requiredContainers =
    expectedContainers ?? collectContainerNames(samples);
  for (const name of requiredContainers) {
    if (!containersAtLatest.has(name)) {
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

function resolveCostDisplayState(estimate: ParsedEstimate | null): CostDisplayState {
  if (estimate === null) {
    return "not_recorded";
  }
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
  // Shift the epoch by nowMs to compare against the clock without discarding
  // producer sub-millisecond digits.
  if (compareTimestampInstants(timestamp, "1970-01-01T00:00:00Z", nowMs) > 0) {
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
  // Live contract: effective freshness threshold is at most five minutes.
  // Cap finite producer values above the live max (e.g. mutated 7d bypass);
  // leave values whose ms conversion overflows so thresholdMs fail-closes.
  let staleAfter = presentation.staleAfterSeconds;
  if (
    Number.isFinite(staleAfter) &&
    Number.isFinite(staleAfter * 1000) &&
    staleAfter > MAX_STALE_AFTER_SECONDS
  ) {
    staleAfter = MAX_STALE_AFTER_SECONDS;
  }
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

/** Age projected readings without regrouping or rebuilding telemetry series. */
export function projectWorkspaceTelemetryFreshness(
  presentation: ParsedTelemetryPresentation,
  readings: {
    cpu: Pick<WorkspaceTelemetryView["cpu"], "sampleTime" | "usedStale">;
    memory: Pick<WorkspaceTelemetryView["memory"], "sampleTime" | "usedStale">;
  },
  nowMs: number,
): Pick<WorkspaceTelemetryView, "isStale" | "hasFutureTimestamp"> {
  const meterSampleTimes = [readings.cpu.sampleTime, readings.memory.sampleTime];
  const hasFutureTimestamp = [
    presentation.windowEndAt,
    presentation.observedAt,
    presentation.admitted?.observedAt ?? null,
    ...meterSampleTimes,
  ].some(timestamp => timestamp != null &&
    compareTimestampInstants(timestamp, "1970-01-01T00:00:00Z", nowMs) > 0);
  return {
    hasFutureTimestamp,
    isStale: hasFutureTimestamp || computeIsStale(
      presentation,
      nowMs,
      meterSampleTimes,
      readings.cpu.usedStale || readings.memory.usedStale,
    ),
  };
}

/**
 * Project allowlisted UI fields from a parsed presentation.
 * Does not fetch; `nowMs` is injected for deterministic freshness tests.
 *
 * `expectAllocation` / `expectCost` mirror negotiated widget gates. When a
 * section is unsupported, null/partial admitted or null/unpriced estimate must
 * not infer envelope partial — those fields are hidden, not incomplete.
 */
export function projectWorkspaceTelemetryView(
  presentation: ParsedTelemetryPresentation,
  options: {
    nowMs?: number;
    expectAllocation?: boolean;
    expectCost?: boolean;
  } = {},
): WorkspaceTelemetryView {
  const nowMs = options.nowMs ?? Date.now();
  const expectAllocation = options.expectAllocation !== false;
  const expectCost = options.expectCost !== false;
  // Completeness is pod-scoped: a container seen on either meter must be present
  // on both before either reading is treated as a whole-Pod total vs limits.
  const expectedContainers = collectContainerNames(
    presentation.cpuSamples,
    presentation.memorySamples,
  );
  const cpuAgg = aggregateAtTimestamp(
    presentation.cpuSamples,
    expectedContainers,
  );
  const memAgg = aggregateAtTimestamp(
    presentation.memorySamples,
    expectedContainers,
  );
  const meterSampleTimes = [cpuAgg.sampleTime, memAgg.sampleTime];
  const sampleTime = resolveDisplaySampleTime(
    meterSampleTimes,
    presentation.observedAt,
  );
  const sampleTimeMixed = meterSampleTimesAreMixed(meterSampleTimes);

  const missingAllocationEvidence = presentation.state !== "unallocated" &&
    ((expectAllocation &&
      (presentation.admitted === null || presentation.admitted.partial)) ||
      (expectCost &&
        (presentation.estimate === null ||
          presentation.estimate.estimateState === "unpriced")));

  return {
    state: missingAllocationEvidence && presentation.state === "success" ? "partial" : presentation.state,
    quality: missingAllocationEvidence && presentation.quality === "ok" ? "partial" : presentation.quality,
    view: presentation.view,
    staleAfterSeconds: presentation.staleAfterSeconds,
    observedAt: presentation.observedAt,
    sampleTime,
    sampleTimeMixed,
    ...projectWorkspaceTelemetryFreshness(presentation, { cpu: cpuAgg, memory: memAgg }, nowMs),
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
      usedStale: cpuAgg.usedStale,
      sampleTime: cpuAgg.sampleTime,
      series: cpuAgg.series,
      containerNamesAtSample: cpuAgg.containerNames,
    },
    memory: {
      usedBytes: memAgg.used,
      usedPartial: memAgg.usedPartial,
      usedStale: memAgg.usedStale,
      sampleTime: memAgg.sampleTime,
      series: memAgg.series,
      containerNamesAtSample: memAgg.containerNames,
    },
    estimate: {
      displayState: resolveCostDisplayState(presentation.estimate),
      scope: presentation.estimateScope,
      currency: "USD",
      estimatedUsd: presentation.estimate?.estimatedUsd ?? null,
      rateTableVersion: presentation.estimate?.rateTableVersion ?? null,
      rateSource: presentation.estimate?.rateSource ?? null,
      pricedIntervalSeconds: presentation.estimate?.pricedIntervalSeconds ?? null,
      unpricedIntervalSeconds: presentation.estimate?.unpricedIntervalSeconds ?? null,
    },
    exclusionNote: COST_EXCLUSION_NOTE,
  };
}
