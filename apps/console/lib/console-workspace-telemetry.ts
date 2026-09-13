/**
 * Backend-neutral TelemetryPresentation reader + UI projection.
 *
 * Producer fixtures: awf-cloud be0d7a3ac9871ef951860666a236c9c481f45c98
 * (`console_telemetry/contract_fixtures`). Schema under review — fail closed;
 * do not silently normalize types or invent zero/free for missing allocation.
 */

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

export const ESTIMATE_STATES = ["complete", "partial", "unallocated"] as const;
export type EstimateState = (typeof ESTIMATE_STATES)[number];

export type CostDisplayState = "complete" | "partial" | "unpriced" | "unallocated";

/** Operator-facing exclusion copy (not producer data_quality_notes). */
export const COST_EXCLUSION_NOTE =
  "Excludes discounts, control-plane/shared infrastructure, network, and persistent storage. Shared Core monitor runtime is unallocated, not free.";

/** Reject absurd magnitudes (CPU cores / USD) rather than accept scientific junk. */
const MAX_DECIMAL_MAGNITUDE = 1e15;
/**
 * Lexical length cap for decimal strings before regex / Number.
 * Legitimate cores/USD/bytes values fit in well under this (≤16 integer digits
 * for MAX_SAFE_INTEGER / 1e15, plus a short fraction). Without the cap, one
 * pathological field can force unbounded scan/parse work on the UI thread;
 * sample-count caps do not protect CPU or cost scalar paths.
 */
export const MAX_DECIMAL_STRING_LENGTH = 64;
/**
 * Lexical length cap for RFC3339 timestamp strings before regex / Date.parse.
 * Legitimate producer timestamps fit well under this (date-time + optional
 * nanosecond fraction + offset ≈ 35 chars). Without the cap, a syntactically
 * valid but arbitrarily long fractional-second portion forces unbounded
 * regex/parse work and later slice/pad/embed work across up to
 * MAX_TELEMETRY_SAMPLES rows — defeating the sample-count and decimal-string
 * bounds on the console thread.
 */
export const MAX_RFC3339_TIMESTAMP_LENGTH = 64;
/**
 * Lexical length cap for container_name before retaining a sample.
 * Legitimate compose/K8s container names fit well under this (DNS labels ≤63;
 * profile service names ≤64). Without the cap, a malformed producer can supply
 * arbitrarily long names that are copied into identity keys, sets, sorts, and
 * partition joins across up to MAX_TELEMETRY_SAMPLES rows — defeating the
 * sample-count bound on the console thread.
 */
export const MAX_CONTAINER_NAME_LENGTH = 64;
/**
 * Lexical length cap for admitted allocation presentation labels
 * (compute_class, region, pod_phase) before retaining the snapshot.
 * Legitimate GKE/autopilot class names, region codes, and pod phases fit well
 * under this. Without the cap, a malformed producer can supply arbitrarily
 * long strings that pass the type-only check and are retained, projected, and
 * rendered into Fact DOM nodes — defeating the numeric, timestamp,
 * sample-count, and container-name bounds on the console thread.
 */
export const MAX_ALLOCATION_LABEL_LENGTH = 64;
/**
 * Lexical length cap for estimate rate provenance strings
 * (rate_table_version, evidence.source, evidence.rate_table_version) before
 * trimming or retaining them.
 * Legitimate rate-table ids and pricing-source URLs fit well under this
 * (fixtures use ~28–52 chars). Without the cap, a malformed producer can
 * supply arbitrarily long strings that pass the type-only check, are scanned
 * by trim(), retained, and projected into Fact DOM nodes — defeating the
 * numeric, timestamp, and allocation-label bounds on the console thread.
 */
export const MAX_RATE_PROVENANCE_LENGTH = 64;
/**
 * Lexical length cap for provider_resource_uid before retaining samples or
 * resolving presentation identity.
 * Legitimate K8s ObjectMeta UIDs are UUIDs (36 chars). Without the cap, a
 * malformed producer can supply arbitrarily long UID strings that pass the
 * type-only check, are retained across up to MAX_TELEMETRY_SAMPLES rows, and
 * are repeatedly compared during identity validation — defeating the
 * sample-count bound on the console thread.
 */
export const MAX_PROVIDER_RESOURCE_UID_LENGTH = 64;
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
/**
 * Hard cap on data_quality_notes accepted for schema compatibility.
 * Notes are discarded (never projected into UI), but the reader still validates
 * each element is a string — reject oversized arrays before that scan so a
 * malformed producer cannot force unbounded work on the console thread.
 */
export const MAX_DATA_QUALITY_NOTES = 64;
/**
 * Live freshness cap for stale_after_seconds (5m). The Stage3 UI contract marks
 * live telemetry stale when sample/envelope/admitted times exceed five minutes;
 * accepting a larger producer threshold would leave hour-old readings fresh.
 * Also rejects absurd finite values (e.g. Number.MAX_VALUE) that would make
 * thresholdMs = seconds * 1000 become Infinity.
 */
export const MAX_STALE_AFTER_SECONDS = 5 * 60;

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
  // Reject before regex/Date.parse so a single overlong fractional-second
  // field cannot burn UI-thread time scanning an unbounded producer string.
  if (value.length > MAX_RFC3339_TIMESTAMP_LENGTH) {
    return false;
  }
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
    if (item.container_name.trim().length === 0) {
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
    // Reject reversed measurement windows (equal start/end remain valid).
    if (compareTimestampInstants(item.interval_start, item.interval_end) > 0) {
      return null;
    }
    // Out-of-window sample_time would skew partition selection, ordering, and freshness.
    if (
      !isSampleTimeWithinMeasurementInterval(
        item.sample_time,
        item.interval_start,
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
 * True when every sample_time / interval_start / interval_end across the
 * given series falls inside the selected view window ending at `observedAt`
 * (`[observedAt - viewDuration, observedAt]` inclusive). Empty input is
 * vacuously valid. Samples without an envelope `observed_at` cannot be
 * anchored and fail closed. Prevents plotting a multi-hour series under a
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
        sample.intervalStart,
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
    typeof value.compute_class !== "string" ||
    typeof value.container_creating !== "boolean" ||
    typeof value.partial !== "boolean" ||
    typeof value.pod_phase !== "string" ||
    typeof value.region !== "string"
  ) {
    return undefined;
  }
  if (
    value.compute_class.length > MAX_ALLOCATION_LABEL_LENGTH ||
    value.pod_phase.length > MAX_ALLOCATION_LABEL_LENGTH ||
    value.region.length > MAX_ALLOCATION_LABEL_LENGTH
  ) {
    return undefined;
  }
  if (value.compute_class.trim() === "" || value.region.trim() === "") {
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

function parseEstimate(
  value: unknown,
  view: TelemetryViewWindow,
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
  // Interval coverage cannot exceed the selected view window, or a multi-hour
  // charge would display under a shorter selector (e.g. 7200s under "1h").
  const viewDurationSeconds = TELEMETRY_VIEW_DURATION_SECONDS[view];
  if (
    value.priced_interval_seconds + value.unpriced_interval_seconds >
    viewDurationSeconds
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
  } else if (value.estimate_state === "partial") {
    // Partial means some coverage is explicitly unpriced. Any amount, even
    // zero, requires priced coverage; otherwise the amount must be unknown.
    if (
      value.unpriced_interval_seconds === 0 ||
      (value.priced_interval_seconds === 0 && estimatedUsd !== null)
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
    if ("source" in value.evidence && value.evidence.source !== undefined) {
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
  // Sample/interval times must fall inside the selected view window ending at
  // observed_at, or a shorter selector could render offset/multi-hour series.
  const viewDurationSeconds = TELEMETRY_VIEW_DURATION_SECONDS[payload.view];
  const observedAt =
    typeof payload.observed_at === "string" ? payload.observed_at : null;
  if (!samplesFitViewWindow([cpuSamples, memorySamples], viewDurationSeconds, observedAt)) {
    return null;
  }
  const expectedResourceUid = resolvePresentationResourceUid(payload);
  if (expectedResourceUid === undefined) {
    return null;
  }
  if (!assertSampleIdentities([cpuSamples, memorySamples], expectedResourceUid)) {
    return null;
  }
  const estimate = parseEstimate(payload.estimate, payload.view);
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

/**
 * Order two validated RFC3339 instants, including sub-millisecond fraction.
 * An optional integer-ms shift of the right instant preserves its fraction.
 */
function compareTimestampInstants(
  left: string,
  right: string,
  rightOffsetMs = 0,
): number {
  const leftMs = timestampInstantMs(left);
  const rightMs = timestampInstantMs(right) + rightOffsetMs;
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

/**
 * True when sampleTime falls in [intervalStart, intervalEnd] inclusive.
 * Callers must already reject reversed windows (start > end).
 */
function isSampleTimeWithinMeasurementInterval(
  sampleTime: string,
  intervalStart: string,
  intervalEnd: string,
): boolean {
  return (
    compareTimestampInstants(sampleTime, intervalStart) >= 0 &&
    compareTimestampInstants(sampleTime, intervalEnd) <= 0
  );
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

/**
 * Project allowlisted UI fields from a parsed presentation.
 * Does not fetch; `nowMs` is injected for deterministic freshness tests.
 */
export function projectWorkspaceTelemetryView(
  presentation: ParsedTelemetryPresentation,
  options: { nowMs?: number } = {},
): WorkspaceTelemetryView {
  const nowMs = options.nowMs ?? Date.now();
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

  return {
    state: presentation.state,
    quality: presentation.quality,
    view: presentation.view,
    staleAfterSeconds: presentation.staleAfterSeconds,
    observedAt: presentation.observedAt,
    sampleTime,
    sampleTimeMixed,
    hasFutureTimestamp: [
      presentation.observedAt,
      presentation.admitted?.observedAt ?? null,
      ...meterSampleTimes,
    ].some(
      (timestamp) =>
        timestamp !== null &&
        compareTimestampInstants(timestamp, "1970-01-01T00:00:00Z", nowMs) > 0,
    ),
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
