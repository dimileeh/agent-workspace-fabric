/** Shared telemetry presentation types and public bounds. */

export const TELEMETRY_VIEWS = ["1h", "6h", "24h"] as const;
export type TelemetryViewWindow = (typeof TELEMETRY_VIEWS)[number];

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
