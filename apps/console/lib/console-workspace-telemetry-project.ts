/** Project parsed TelemetryPresentation into operator-facing view fields. */

import {
  compareTimestampInstants,
  timestampInstantKey,
} from "./console-workspace-telemetry-timestamps.ts";
import {
  COST_EXCLUSION_NOTE,
  MAX_STALE_AFTER_SECONDS,
} from "./console-workspace-telemetry-types.ts";
import type {
  CostDisplayState,
  ParsedEstimate,
  ParsedTelemetryPresentation,
  ParsedTelemetrySample,
  TelemetryQuality,
  WorkspaceTelemetrySeriesPoint,
  WorkspaceTelemetryView,
} from "./console-workspace-telemetry-types.ts";

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
 * Stale when producer marks state/quality stale (when
 * `honorProducerEnvelopeStale` is true), when a displayed CPU/memory aggregate
 * includes a stale sample, or when the envelope, admitted allocation snapshot,
 * or any meter sample time used for displayed CPU/memory values exceeds the
 * threshold. Fresh envelopes/meters must not mask aged allocation
 * requests/limits, aged resource samples, or producer-stale current readings.
 * Allocation-only callers pass `honorProducerEnvelopeStale: false` so a
 * producer stale envelope driven by aged hidden CPU series cannot stale a
 * current admitted snapshot (see persisted_stale_series).
 */
function computeIsStale(
  presentation: ParsedTelemetryPresentation,
  nowMs: number,
  meterSampleTimes: Array<string | null>,
  meterHasStaleSample: boolean,
  honorProducerEnvelopeStale: boolean = true,
): boolean {
  // Section-aware: producer envelope stale is meter-series freshness. Skip it
  // when the caller excluded telemetry so allocation-only views stay current.
  if (
    honorProducerEnvelopeStale &&
    (presentation.state === "stale" || presentation.quality === "stale")
  ) {
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
 * Age projected readings without regrouping or rebuilding telemetry series.
 *
 * `expectTelemetry` / `expectAllocation` mirror negotiated widget gates. Aged
 * timestamps from an unsupported section must not mark the visible panel stale.
 */
export function projectWorkspaceTelemetryFreshness(
  presentation: ParsedTelemetryPresentation,
  readings: {
    cpu: Pick<WorkspaceTelemetryView["cpu"], "sampleTime" | "usedStale">;
    memory: Pick<WorkspaceTelemetryView["memory"], "sampleTime" | "usedStale">;
  },
  nowMs: number,
  options: {
    expectTelemetry?: boolean;
    expectAllocation?: boolean;
  } = {},
): Pick<WorkspaceTelemetryView, "isStale" | "hasFutureTimestamp"> {
  const expectTelemetry = options.expectTelemetry !== false;
  const expectAllocation = options.expectAllocation !== false;
  const meterSampleTimes = expectTelemetry
    ? [readings.cpu.sampleTime, readings.memory.sampleTime]
    : [];
  // Build a sample-free presentation slice so clock ticks never re-read series
  // arrays (freshness ticks use projected meter metadata only). Producer
  // envelope stale is gated inside computeIsStale via honorProducerEnvelopeStale
  // (allocation-only / expectTelemetry=false) rather than rewriting state here.
  const freshnessPresentation: ParsedTelemetryPresentation = {
    state: presentation.state,
    quality: presentation.quality,
    view: presentation.view,
    staleAfterSeconds: presentation.staleAfterSeconds,
    observedAt: expectTelemetry ? presentation.observedAt : null,
    windowEndAt: expectTelemetry ? presentation.windowEndAt : null,
    admitted: expectAllocation ? presentation.admitted : null,
    cpuSamples: [],
    memorySamples: [],
    estimateScope: presentation.estimateScope,
    estimate: presentation.estimate,
    dataQualityNotes: presentation.dataQualityNotes,
  };
  const hasFutureTimestamp = [
    freshnessPresentation.windowEndAt,
    freshnessPresentation.observedAt,
    freshnessPresentation.admitted?.observedAt ?? null,
    ...meterSampleTimes,
  ].some(timestamp => timestamp != null &&
    compareTimestampInstants(timestamp, "1970-01-01T00:00:00Z", nowMs) > 0);
  return {
    hasFutureTimestamp,
    isStale: hasFutureTimestamp || computeIsStale(
      freshnessPresentation,
      nowMs,
      meterSampleTimes,
      expectTelemetry && (readings.cpu.usedStale || readings.memory.usedStale),
      expectTelemetry,
    ),
  };
}

/** Cost-section notes that may explain producer partial when cost is gated off. */
const COST_SECTION_QUALITY_NOTES = new Set([
  "missing_estimate",
  "unpriced_estimate",
  "partial_estimate",
]);
/** Allocation-section notes that may explain producer partial when allocation is gated off. */
const ALLOCATION_SECTION_QUALITY_NOTES = new Set(["missing_admitted"]);

/**
 * True when every retained note is attributable to a gated-off section (or
 * there are no notes). Telemetry quality notes such as `downsampled` block
 * clearing so visible history stays envelope-qualified.
 */
function dataQualityNotesOnlyGatedOff(
  notes: readonly string[],
  gates: { expectAllocation: boolean; expectCost: boolean },
): boolean {
  return notes.every(note => {
    if (COST_SECTION_QUALITY_NOTES.has(note)) {
      return !gates.expectCost;
    }
    if (ALLOCATION_SECTION_QUALITY_NOTES.has(note)) {
      return !gates.expectAllocation;
    }
    return false;
  });
}

/**
 * Project allowlisted UI fields from a parsed presentation.
 * Does not fetch; `nowMs` is injected for deterministic freshness tests.
 *
 * `expectTelemetry` / `expectAllocation` / `expectCost` mirror negotiated
 * widget gates. When a section is unsupported, null/partial admitted or
 * null/partial/unpriced estimate must not infer envelope partial — those
 * fields are hidden, not incomplete — and aged timestamps from that section
 * must not mark the visible panel stale. Producer-marked envelope partial that
 * is attributable only to gated-off allocation/cost evidence is cleared for
 * the visible view when expected telemetry meters are themselves complete and
 * producer notes do not also cite telemetry quality (e.g. downsampled);
 * envelope-only partial with complete nested evidence is kept, and empty or
 * partial meters keep the producer envelope so dashes are not presented as a
 * successful complete reading. Success/ok envelopes with absent enabled meter
 * series are likewise projected partial when telemetry is expected. Producer
 * envelope stale driven by meter aging is likewise cleared when telemetry is
 * unsupported so allocation-only views keep current admitted facts.
 */
export function projectWorkspaceTelemetryView(
  presentation: ParsedTelemetryPresentation,
  options: {
    nowMs?: number;
    expectTelemetry?: boolean;
    expectAllocation?: boolean;
    expectCost?: boolean;
  } = {},
): WorkspaceTelemetryView {
  const nowMs = options.nowMs ?? Date.now();
  const expectTelemetry = options.expectTelemetry !== false;
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

  const allocationIncomplete =
    presentation.admitted === null || Boolean(presentation.admitted?.partial);
  const costIncomplete =
    presentation.estimate === null ||
    presentation.estimate.estimateState === "unpriced" ||
    presentation.estimate.estimateState === "partial";
  // Absent enabled meter series are incomplete evidence (dashes / "No history"),
  // distinct from usedPartial on present samples which keep envelope success.
  const missingEnabledMeterEvidence = expectTelemetry &&
    (cpuAgg.series.length === 0 || memAgg.series.length === 0);
  const missingAllocationEvidence = presentation.state !== "unallocated" &&
    ((expectAllocation && allocationIncomplete) ||
      (expectCost && costIncomplete) ||
      missingEnabledMeterEvidence);
  // Producer may already mark partial for unsupported sections (e.g.
  // missing_estimate). Clear that qualification when incompleteness exists
  // only in gated-off sections so telemetry-only panels stay success/ok —
  // but only when expected meters are themselves complete and notes do not
  // also cite telemetry quality (downsampled / no_samples / …). Empty series
  // leave usedPartial=false, so gating on used!==null (and series length) is
  // required or clearing would present dashes as a successful complete reading.
  const expectedTelemetryComplete = !expectTelemetry ||
    (cpuAgg.used !== null &&
      memAgg.used !== null &&
      cpuAgg.series.length > 0 &&
      memAgg.series.length > 0 &&
      !cpuAgg.usedPartial &&
      !memAgg.usedPartial);
  const notesOnlyGatedOffSections = dataQualityNotesOnlyGatedOff(
    presentation.dataQualityNotes,
    { expectAllocation, expectCost },
  );
  const hiddenSectionOnlyPartial = presentation.state !== "unallocated" &&
    !missingAllocationEvidence &&
    expectedTelemetryComplete &&
    notesOnlyGatedOffSections &&
    ((!expectAllocation && allocationIncomplete) ||
      (!expectCost && costIncomplete));
  // Producer envelope stale is meter-series freshness. Clear it when telemetry
  // is unsupported so allocation-only chrome does not inherit hidden CPU aging
  // (historical mode reads state/quality for producerStale).
  const hiddenTelemetryOnlyStale = !expectTelemetry &&
    (presentation.state === "stale" || presentation.quality === "stale");

  return {
    state: missingAllocationEvidence &&
        (presentation.state === "success" ||
          (hiddenTelemetryOnlyStale && presentation.state === "stale"))
      ? "partial"
      : hiddenSectionOnlyPartial && presentation.state === "partial"
        ? "success"
        : hiddenTelemetryOnlyStale && presentation.state === "stale"
          ? "success"
          : presentation.state,
    quality: missingAllocationEvidence &&
        (presentation.quality === "ok" ||
          (hiddenTelemetryOnlyStale && presentation.quality === "stale"))
      ? "partial"
      : hiddenSectionOnlyPartial && presentation.quality === "partial"
        ? "ok"
        : hiddenTelemetryOnlyStale && presentation.quality === "stale"
          ? "ok"
          : presentation.quality,
    view: presentation.view,
    staleAfterSeconds: presentation.staleAfterSeconds,
    observedAt: presentation.observedAt,
    sampleTime,
    sampleTimeMixed,
    ...projectWorkspaceTelemetryFreshness(
      presentation,
      { cpu: cpuAgg, memory: memAgg },
      nowMs,
      { expectTelemetry, expectAllocation },
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
