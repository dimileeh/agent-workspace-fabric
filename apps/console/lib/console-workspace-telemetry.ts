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
import {
  ESTIMATE_STATES,
  MAX_ALLOCATION_LABEL_LENGTH,
  MAX_CONTAINER_NAME_LENGTH,
  MAX_DATA_QUALITY_NOTE_LENGTH,
  MAX_DATA_QUALITY_NOTES,
  MAX_DECIMAL_STRING_LENGTH,
  MAX_PROVIDER_RESOURCE_UID_LENGTH,
  MAX_RATE_PROVENANCE_LENGTH,
  MAX_STALE_AFTER_SECONDS,
  MAX_TELEMETRY_SAMPLES,
  TELEMETRY_METRIC_TYPE_BY_UNIT,
  TELEMETRY_QUALITIES,
  TELEMETRY_STATES,
  TELEMETRY_VIEWS,
} from "./console-workspace-telemetry-types.ts";
import type {
  EstimateScope,
  ParsedAdmittedResources,
  ParsedEstimate,
  ParsedTelemetryPresentation,
  ParsedTelemetrySample,
  TelemetryViewWindow,
} from "./console-workspace-telemetry-types.ts";

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
export {
  COST_EXCLUSION_NOTE,
  ESTIMATE_STATES,
  MAX_ALLOCATION_LABEL_LENGTH,
  MAX_CONTAINER_NAME_LENGTH,
  MAX_DATA_QUALITY_NOTE_LENGTH,
  MAX_DATA_QUALITY_NOTES,
  MAX_DECIMAL_STRING_LENGTH,
  MAX_PROVIDER_RESOURCE_UID_LENGTH,
  MAX_RATE_PROVENANCE_LENGTH,
  MAX_STALE_AFTER_SECONDS,
  MAX_TELEMETRY_SAMPLES,
  TELEMETRY_METRIC_TYPE_BY_UNIT,
  TELEMETRY_QUALITIES,
  TELEMETRY_STATES,
  TELEMETRY_VIEWS,
} from "./console-workspace-telemetry-types.ts";
export type {
  CostDisplayState,
  EstimateScope,
  EstimateState,
  ParsedAdmittedResources,
  ParsedEstimate,
  ParsedTelemetryPresentation,
  ParsedTelemetrySample,
  TelemetryPresentationState,
  TelemetryQuality,
  TelemetryViewWindow,
  WorkspaceTelemetrySeriesPoint,
  WorkspaceTelemetryView,
} from "./console-workspace-telemetry-types.ts";
export {
  projectWorkspaceTelemetryFreshness,
  projectWorkspaceTelemetryView,
} from "./console-workspace-telemetry-project.ts";

/** Wall-clock duration each `view` selector represents (seconds). */
const TELEMETRY_VIEW_DURATION_SECONDS: Record<TelemetryViewWindow, number> = {
  "1h": 3600,
  "6h": 21600,
  "24h": 86400,
};
/** Reject absurd magnitudes (CPU cores / USD) rather than accept scientific junk. */
const MAX_DECIMAL_MAGNITUDE = 1e15;

/**
 * Reject memory/ephemeral byte counts above MAX_SAFE_INTEGER to prevent rounding.
 */
const MAX_BYTES_MAGNITUDE = Number.MAX_SAFE_INTEGER;

const DECIMAL_STRING =
  /^(?:0|[1-9]\d*)(?:\.\d+)?$/;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

/**
 * Cloud may emit `ownership: null` for no retained resource / shared unallocated.
 * Accept that absence only for the two canonical empty-shell contracts:
 * partial/not-recorded (`estimate: null`) or shared-unallocated
 * (`state` + estimate both unallocated, no numeric cost) — never as a general
 * ownership bypass for parser-valid success/ok empty shells.
 */
export function isLegitimateNullOwnershipNoResource(payload: unknown): boolean {
  if (!isPlainObject(payload) || payload.ownership !== null) {
    return false;
  }
  if (!Array.isArray(payload.cpu_cores_samples) || payload.cpu_cores_samples.length !== 0) {
    return false;
  }
  if (!Array.isArray(payload.memory_bytes_samples) || payload.memory_bytes_samples.length !== 0) {
    return false;
  }
  if (payload.admitted !== null) {
    return false;
  }
  if (payload.estimate === null) {
    return (
      payload.state === "partial" &&
      payload.quality === "partial" &&
      payload.observed_at === null &&
      Array.isArray(payload.data_quality_notes) &&
      payload.data_quality_notes.includes("not_recorded")
    );
  }
  if (!isPlainObject(payload.estimate)) {
    return false;
  }
  return (
    payload.state === "unallocated" &&
    payload.estimate.estimate_state === "unallocated" &&
    payload.estimate.estimated_usd === null
  );
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
  // Accept machine notes for schema compatibility; retain for projection
  // decisions (gated-off vs telemetry quality) but never surface as UI copy.
  let dataQualityNotes: readonly string[] = [];
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
      // Reject overlong notes before retention so array count alone cannot
      // leave unbounded strings in telemetry state / projection hashing.
      if (note.length > MAX_DATA_QUALITY_NOTE_LENGTH) {
        return null;
      }
    }
    dataQualityNotes = payload.data_quality_notes;
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
    dataQualityNotes,
  };
}
