import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  COST_EXCLUSION_NOTE,
  MAX_ALLOCATION_LABEL_LENGTH,
  MAX_CONTAINER_NAME_LENGTH,
  MAX_DATA_QUALITY_NOTES,
  MAX_DECIMAL_STRING_LENGTH,
  MAX_PROVIDER_RESOURCE_UID_LENGTH,
  MAX_RATE_PROVENANCE_LENGTH,
  MAX_RFC3339_TIMESTAMP_LENGTH,
  MAX_SPARKLINE_POINTS,
  MAX_STALE_AFTER_SECONDS,
  MAX_TELEMETRY_SAMPLES,
  buildSparklineGeometry,
  downsampleSeriesForSparkline,
  formatCores,
  parseTelemetryPresentation,
  projectWorkspaceTelemetryView,
  projectWorkspaceTelemetryFreshness,
} from "./console-workspace-telemetry.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE_DIR = join(HERE, "fixtures/console-workspace-telemetry");

function loadFixture(name) {
  return JSON.parse(readFileSync(join(FIXTURE_DIR, `${name}.json`), "utf8"));
}

const SUCCESS = loadFixture("success");
const PARTIAL = loadFixture("partial");
const STALE = loadFixture("stale");
const UNALLOCATED = loadFixture("unallocated");

const FIXED_NOW = Date.parse("2026-09-12T12:01:00+00:00");

test("parseTelemetryPresentation accepts all four verbatim producer fixtures", () => {
  for (const [name, raw] of [
    ["success", SUCCESS],
    ["partial", PARTIAL],
    ["stale", STALE],
    ["unallocated", UNALLOCATED],
  ]) {
    const parsed = parseTelemetryPresentation(raw);
    assert.ok(parsed, `expected ${name} fixture to parse`);
    assert.equal(parsed.state, raw.state);
    assert.equal(parsed.quality, raw.quality);
    assert.equal(parsed.view, raw.view);
    assert.equal(parsed.staleAfterSeconds, 300);
  }
});

test("projectWorkspaceTelemetryView allowlists fields and omits ownership/evidence/notes from display strings", () => {
  const parsed = parseTelemetryPresentation(SUCCESS);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });

  const serialized = JSON.stringify(view);
  assert.equal(serialized.includes("org_example"), false);
  assert.equal(serialized.includes("cell_example"), false);
  assert.equal(serialized.includes("owner_pod_example"), false);
  assert.equal(serialized.includes("job-uid-example"), false);
  assert.equal(serialized.includes("partial_samples"), false);
  assert.equal(serialized.includes("kubernetes.io/container"), false);
  assert.equal(view.exclusionNote, COST_EXCLUSION_NOTE);

  assert.equal(view.cpu.usedCores, 0.25);
  assert.equal(view.memory.usedBytes, 1073741824);
  assert.equal(view.admitted?.cpuRequestCores, 0.5);
  assert.equal(view.admitted?.cpuLimitCores, 1);
  assert.equal(view.admitted?.memoryRequestBytes, 2147483648);
  assert.equal(view.admitted?.memoryLimitBytes, 4294967296);
  assert.equal(view.estimate.estimatedUsd, 0.0123456);
  assert.equal(view.estimate.displayState, "complete");
  assert.equal(view.estimate.currency, "USD");
  assert.equal(view.estimate.rateTableVersion, "gke-autopilot-pod-2026-09-12");
  assert.equal(
    view.estimate.rateSource,
    "https://cloud.google.com/kubernetes-engine/pricing",
  );
  assert.equal(view.isStale, false);
  assert.equal(view.cpu.usedStale, false);
  assert.equal(view.memory.usedStale, false);
  assert.equal(view.sampleTime, "2026-09-12T12:00:00+00:00");
});

test("parseTelemetryPresentation rejects non-objects and malformed envelopes", () => {
  assert.equal(parseTelemetryPresentation(null), null);
  assert.equal(parseTelemetryPresentation(undefined), null);
  assert.equal(parseTelemetryPresentation([]), null);
  assert.equal(parseTelemetryPresentation("x"), null);
  assert.equal(parseTelemetryPresentation({ ...SUCCESS, state: "nope" }), null);
  assert.equal(parseTelemetryPresentation({ ...SUCCESS, view: "2h" }), null);
  assert.equal(parseTelemetryPresentation({ ...SUCCESS, quality: 1 }), null);
  // Null is not recorded; malformed non-null still fails closed.
  assert.equal(parseTelemetryPresentation({ ...SUCCESS, estimate: false }), null);
  const { state: _s, ...missingState } = SUCCESS;
  assert.equal(parseTelemetryPresentation(missingState), null);
});

test("parseTelemetryPresentation rejects mixed-unit sample series", () => {
  const cpuBytes = structuredClone(SUCCESS);
  cpuBytes.cpu_cores_samples[0].unit = "bytes";
  assert.equal(parseTelemetryPresentation(cpuBytes), null);

  const memCores = structuredClone(SUCCESS);
  memCores.memory_bytes_samples[0].unit = "cores";
  assert.equal(parseTelemetryPresentation(memCores), null);
});

test("parseTelemetryPresentation rejects metric_type that does not match its series", () => {
  // Memory meter in the CPU series (even with unit: cores) must fail closed —
  // otherwise values render under the CPU label.
  const memInCpu = structuredClone(SUCCESS);
  memInCpu.cpu_cores_samples[0].metric_type = "kubernetes.io/container/memory/used_bytes";
  assert.equal(parseTelemetryPresentation(memInCpu), null);

  const cpuInMem = structuredClone(SUCCESS);
  cpuInMem.memory_bytes_samples[0].metric_type =
    "kubernetes.io/container/cpu/core_usage_time";
  assert.equal(parseTelemetryPresentation(cpuInMem), null);

  const unknownCpu = structuredClone(SUCCESS);
  unknownCpu.cpu_cores_samples[0].metric_type = "custom.metric/cpu";
  assert.equal(parseTelemetryPresentation(unknownCpu), null);

  const nonString = structuredClone(SUCCESS);
  nonString.memory_bytes_samples[0].metric_type = 1;
  assert.equal(parseTelemetryPresentation(nonString), null);

  const empty = structuredClone(SUCCESS);
  empty.cpu_cores_samples[0].metric_type = "";
  assert.equal(parseTelemetryPresentation(empty), null);

  // Top-level type matches series but evidence.metric_type contradicts — fail closed.
  const evidenceMismatch = structuredClone(SUCCESS);
  evidenceMismatch.cpu_cores_samples[0].evidence = {
    metric_type: "kubernetes.io/container/memory/used_bytes",
  };
  assert.equal(parseTelemetryPresentation(evidenceMismatch), null);

  const evidenceNonString = structuredClone(SUCCESS);
  evidenceNonString.memory_bytes_samples[0].evidence = { metric_type: 1 };
  assert.equal(parseTelemetryPresentation(evidenceNonString), null);
});

test("parseTelemetryPresentation rejects samples with reversed interval windows", () => {
  const reversedCpu = structuredClone(SUCCESS);
  reversedCpu.cpu_cores_samples[0].interval_start = "2026-09-12T12:00:00+00:00";
  reversedCpu.cpu_cores_samples[0].interval_end = "2026-09-12T11:59:00+00:00";
  assert.equal(parseTelemetryPresentation(reversedCpu), null);

  const reversedMem = structuredClone(SUCCESS);
  reversedMem.memory_bytes_samples[0].interval_start = "2026-09-12T12:00:00+00:00";
  reversedMem.memory_bytes_samples[0].interval_end = "2026-09-12T11:59:00+00:00";
  assert.equal(parseTelemetryPresentation(reversedMem), null);

  // Same wall clock via offset, but end is earlier in UTC.
  const reversedOffset = structuredClone(SUCCESS);
  reversedOffset.cpu_cores_samples[0].interval_start = "2026-09-12T12:00:00+00:00";
  reversedOffset.cpu_cores_samples[0].interval_end = "2026-09-12T12:30:00+01:00";
  assert.equal(parseTelemetryPresentation(reversedOffset), null);

  // Sub-millisecond reverse must also fail closed.
  const reversedSubMs = structuredClone(SUCCESS);
  reversedSubMs.cpu_cores_samples[0].interval_start = "2026-09-12T12:00:00.000002Z";
  reversedSubMs.cpu_cores_samples[0].interval_end = "2026-09-12T12:00:00.000001Z";
  assert.equal(parseTelemetryPresentation(reversedSubMs), null);
});

test("parseTelemetryPresentation rejects sample_time outside its measurement interval", () => {
  // Interval is ordered but sample_time is before the window — must fail closed
  // so freshness/partition logic cannot treat an out-of-window instant as current.
  const beforeStart = structuredClone(SUCCESS);
  beforeStart.cpu_cores_samples[0].interval_start = "2026-09-12T12:00:00+00:00";
  beforeStart.cpu_cores_samples[0].interval_end = "2026-09-12T12:01:00+00:00";
  beforeStart.cpu_cores_samples[0].sample_time = "2026-09-12T11:00:00+00:00";
  assert.equal(parseTelemetryPresentation(beforeStart), null);

  const afterEnd = structuredClone(SUCCESS);
  afterEnd.memory_bytes_samples[0].interval_start = "2026-09-12T12:00:00+00:00";
  afterEnd.memory_bytes_samples[0].interval_end = "2026-09-12T12:01:00+00:00";
  afterEnd.memory_bytes_samples[0].sample_time = "2026-09-12T12:02:00+00:00";
  assert.equal(parseTelemetryPresentation(afterEnd), null);

  // Offset spelling that is strictly before start in UTC.
  const beforeViaOffset = structuredClone(SUCCESS);
  beforeViaOffset.cpu_cores_samples[0].interval_start = "2026-09-12T12:00:00+00:00";
  beforeViaOffset.cpu_cores_samples[0].interval_end = "2026-09-12T12:01:00+00:00";
  beforeViaOffset.cpu_cores_samples[0].sample_time = "2026-09-12T12:30:00+01:00";
  assert.equal(parseTelemetryPresentation(beforeViaOffset), null);

  // Sub-ms: sample after end must reject even when ms buckets match.
  const afterSubMs = structuredClone(SUCCESS);
  afterSubMs.cpu_cores_samples[0].interval_start = "2026-09-12T12:00:00.000001Z";
  afterSubMs.cpu_cores_samples[0].interval_end = "2026-09-12T12:00:00.000002Z";
  afterSubMs.cpu_cores_samples[0].sample_time = "2026-09-12T12:00:00.000003Z";
  assert.equal(parseTelemetryPresentation(afterSubMs), null);

  // Sub-ms: sample before start must likewise fail closed.
  const beforeSubMs = structuredClone(SUCCESS);
  beforeSubMs.cpu_cores_samples[0].interval_start = "2026-09-12T12:00:00.000002Z";
  beforeSubMs.cpu_cores_samples[0].interval_end = "2026-09-12T12:00:00.000003Z";
  beforeSubMs.cpu_cores_samples[0].sample_time = "2026-09-12T12:00:00.000001Z";
  assert.equal(parseTelemetryPresentation(beforeSubMs), null);

  // Zero-width window: sample_time must equal the single declared instant.
  const pointMismatch = structuredClone(SUCCESS);
  pointMismatch.cpu_cores_samples[0].interval_start = "2026-09-12T12:00:00+00:00";
  pointMismatch.cpu_cores_samples[0].interval_end = "2026-09-12T12:00:00+00:00";
  pointMismatch.cpu_cores_samples[0].sample_time = "2026-09-12T12:00:01+00:00";
  assert.equal(parseTelemetryPresentation(pointMismatch), null);

  const pointMatch = structuredClone(SUCCESS);
  pointMatch.cpu_cores_samples[0].interval_start = "2026-09-12T12:00:00+00:00";
  pointMatch.cpu_cores_samples[0].interval_end = "2026-09-12T12:00:00+00:00";
  pointMatch.cpu_cores_samples[0].sample_time = "2026-09-12T12:00:00Z";
  assert.ok(parseTelemetryPresentation(pointMatch));

  // Inclusive endpoints remain valid (including alternate RFC3339 spellings).
  const atStart = structuredClone(SUCCESS);
  atStart.cpu_cores_samples[0].interval_start = "2026-09-12T11:59:00+00:00";
  atStart.cpu_cores_samples[0].interval_end = "2026-09-12T12:00:00+00:00";
  atStart.cpu_cores_samples[0].sample_time = "2026-09-12T11:59:00Z";
  assert.ok(parseTelemetryPresentation(atStart));

  const atEnd = structuredClone(SUCCESS);
  atEnd.cpu_cores_samples[0].interval_start = "2026-09-12T11:59:00+00:00";
  atEnd.cpu_cores_samples[0].interval_end = "2026-09-12T12:00:00+00:00";
  atEnd.cpu_cores_samples[0].sample_time = "2026-09-12T13:00:00+01:00";
  assert.ok(parseTelemetryPresentation(atEnd));
});

test("parseTelemetryPresentation rejects unknown sample quality rather than treating it as complete", () => {
  for (const bad of ["typo", "complete", "", 1, null, undefined]) {
    const badCpu = structuredClone(SUCCESS);
    badCpu.cpu_cores_samples[0].quality = bad;
    assert.equal(
      parseTelemetryPresentation(badCpu),
      null,
      `cpu sample quality ${String(bad)}`,
    );
  }
  const badMem = structuredClone(SUCCESS);
  badMem.memory_bytes_samples[0].quality = "unknown";
  assert.equal(parseTelemetryPresentation(badMem), null);

  // Known sample qualities still parse (partial already covered by fixture).
  const okSample = structuredClone(SUCCESS);
  okSample.cpu_cores_samples[0].quality = "ok";
  const okParsed = parseTelemetryPresentation(okSample);
  assert.ok(okParsed);
  assert.equal(
    projectWorkspaceTelemetryView(okParsed, { nowMs: FIXED_NOW }).cpu.usedPartial,
    false,
  );
  const staleSample = structuredClone(SUCCESS);
  staleSample.cpu_cores_samples[0].quality = "stale";
  const staleParsed = parseTelemetryPresentation(staleSample);
  assert.ok(staleParsed);
  // Non-ok allowlisted quality must project as incomplete usage, not complete.
  assert.equal(
    projectWorkspaceTelemetryView(staleParsed, { nowMs: FIXED_NOW }).cpu.usedPartial,
    true,
  );
});

test("stale current meter sample quality marks live view stale even when timestamps are fresh", () => {
  // Envelope/admitted/sample times are within stale_after; only sample quality is stale.
  const staleCpu = structuredClone(SUCCESS);
  staleCpu.cpu_cores_samples[0].quality = "stale";
  const cpuParsed = parseTelemetryPresentation(staleCpu);
  assert.ok(cpuParsed);
  const cpuView = projectWorkspaceTelemetryView(cpuParsed, { nowMs: FIXED_NOW });
  assert.equal(cpuView.state, "success");
  assert.equal(cpuView.quality, "ok");
  assert.equal(cpuView.cpu.usedPartial, true);
  assert.equal(cpuView.cpu.usedStale, true);
  assert.equal(cpuView.memory.usedStale, false);
  assert.equal(cpuView.isStale, true);

  const staleMem = structuredClone(SUCCESS);
  staleMem.memory_bytes_samples[0].quality = "stale";
  const memParsed = parseTelemetryPresentation(staleMem);
  assert.ok(memParsed);
  const memView = projectWorkspaceTelemetryView(memParsed, { nowMs: FIXED_NOW });
  assert.equal(memView.memory.usedPartial, true);
  assert.equal(memView.memory.usedStale, true);
  assert.equal(memView.cpu.usedStale, false);
  assert.equal(memView.isStale, true);

  // Partial sample quality downgrades the meter but is not live-stale by itself.
  const partialCpu = structuredClone(SUCCESS);
  partialCpu.cpu_cores_samples[0].quality = "partial";
  const partialParsed = parseTelemetryPresentation(partialCpu);
  assert.ok(partialParsed);
  const partialView = projectWorkspaceTelemetryView(partialParsed, {
    nowMs: FIXED_NOW,
  });
  assert.equal(partialView.cpu.usedPartial, true);
  assert.equal(partialView.cpu.usedStale, false);
  assert.equal(partialView.isStale, false);
});

test("unallocated fixture preserves null admitted/cost and does not coerce to zero/free", () => {
  const parsed = parseTelemetryPresentation(UNALLOCATED);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.state, "unallocated");
  assert.equal(view.admitted, null);
  assert.equal(view.cpu.usedCores, null);
  assert.equal(view.memory.usedBytes, null);
  assert.equal(view.estimate.estimatedUsd, null);
  assert.equal(view.estimate.displayState, "unallocated");
  assert.equal(view.estimate.rateTableVersion, null);
  assert.notEqual(view.estimate.estimatedUsd, 0);
  assert.equal(view.observedAt, null);
});

test("parseTelemetryPresentation rejects estimate_state that contradicts interval coverage", () => {
  // complete + unpriced coverage would still project displayState "complete".
  const completeWithUnpriced = structuredClone(SUCCESS);
  completeWithUnpriced.estimate = {
    ...structuredClone(SUCCESS.estimate),
    unpriced_interval_seconds: 120,
  };
  assert.equal(parseTelemetryPresentation(completeWithUnpriced), null);

  // unallocated must not retain priced or unpriced interval seconds.
  const unallocatedPriced = structuredClone(UNALLOCATED);
  unallocatedPriced.estimate = {
    ...structuredClone(UNALLOCATED.estimate),
    priced_interval_seconds: 60,
  };
  assert.equal(parseTelemetryPresentation(unallocatedPriced), null);

  const unallocatedUnpriced = structuredClone(UNALLOCATED);
  unallocatedUnpriced.estimate = {
    ...structuredClone(UNALLOCATED.estimate),
    unpriced_interval_seconds: 60,
  };
  assert.equal(parseTelemetryPresentation(unallocatedUnpriced), null);
});

test("parseTelemetryPresentation rejects sample or interval spans beyond the selected view window", () => {
  // A 1h selector must not plot a series whose samples are more than 1h apart.
  const overSampleSpan = structuredClone(SUCCESS);
  overSampleSpan.view = "1h";
  overSampleSpan.cpu_cores_samples = [
    {
      ...SUCCESS.cpu_cores_samples[0],
      sample_time: "2026-09-12T10:00:00+00:00",
      interval_start: "2026-09-12T10:00:00+00:00",
      interval_end: "2026-09-12T10:00:00+00:00",
      value: "0.10",
    },
    {
      ...SUCCESS.cpu_cores_samples[0],
      container_name: "sidecar",
      sample_time: "2026-09-12T12:00:01+00:00",
      interval_start: "2026-09-12T12:00:01+00:00",
      interval_end: "2026-09-12T12:00:01+00:00",
      value: "0.20",
    },
  ];
  assert.equal(parseTelemetryPresentation(overSampleSpan), null);

  // A single measurement interval longer than the view must also fail closed.
  const overInterval = structuredClone(SUCCESS);
  overInterval.view = "1h";
  overInterval.memory_bytes_samples = [
    {
      ...SUCCESS.memory_bytes_samples[0],
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T10:00:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
    },
  ];
  assert.equal(parseTelemetryPresentation(overInterval), null);

  // Same bound for longer selectors (6h = 21600s).
  const overSixHour = structuredClone(SUCCESS);
  overSixHour.view = "6h";
  overSixHour.estimate = {
    ...structuredClone(SUCCESS.estimate),
    priced_interval_seconds: 21600,
    unpriced_interval_seconds: 0,
  };
  overSixHour.cpu_cores_samples = [
    {
      ...SUCCESS.cpu_cores_samples[0],
      sample_time: "2026-09-12T06:00:00+00:00",
      interval_start: "2026-09-12T06:00:00+00:00",
      interval_end: "2026-09-12T06:00:00+00:00",
    },
    {
      ...SUCCESS.cpu_cores_samples[0],
      container_name: "sidecar",
      sample_time: "2026-09-12T12:00:01+00:00",
      interval_start: "2026-09-12T12:00:01+00:00",
      interval_end: "2026-09-12T12:00:01+00:00",
    },
  ];
  assert.equal(parseTelemetryPresentation(overSixHour), null);

  // Exact view duration remains valid (1h span under view "1h").
  const exactSpan = structuredClone(SUCCESS);
  exactSpan.view = "1h";
  exactSpan.cpu_cores_samples = [
    {
      ...SUCCESS.cpu_cores_samples[0],
      sample_time: "2026-09-12T11:00:00+00:00",
      interval_start: "2026-09-12T11:00:00+00:00",
      interval_end: "2026-09-12T11:00:00+00:00",
      value: "0.10",
    },
    {
      ...SUCCESS.cpu_cores_samples[0],
      container_name: "sidecar",
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T12:00:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
      value: "0.20",
    },
  ];
  exactSpan.memory_bytes_samples = [
    {
      ...SUCCESS.memory_bytes_samples[0],
      sample_time: "2026-09-12T11:00:00+00:00",
      interval_start: "2026-09-12T11:00:00+00:00",
      interval_end: "2026-09-12T11:00:00+00:00",
    },
    {
      ...SUCCESS.memory_bytes_samples[0],
      container_name: "sidecar",
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T12:00:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
    },
  ];
  assert.ok(parseTelemetryPresentation(exactSpan));

  // CPU and memory share one plotted window: each series alone can fit 1h while
  // the combined timestamps span >1h — reject that too.
  const crossSeries = structuredClone(SUCCESS);
  crossSeries.view = "1h";
  crossSeries.cpu_cores_samples = [
    {
      ...SUCCESS.cpu_cores_samples[0],
      sample_time: "2026-09-12T10:00:00+00:00",
      interval_start: "2026-09-12T10:00:00+00:00",
      interval_end: "2026-09-12T10:00:00+00:00",
      value: "0.10",
    },
  ];
  crossSeries.memory_bytes_samples = [
    {
      ...SUCCESS.memory_bytes_samples[0],
      sample_time: "2026-09-12T12:00:01+00:00",
      interval_start: "2026-09-12T12:00:01+00:00",
      interval_end: "2026-09-12T12:00:01+00:00",
    },
  ];
  assert.equal(parseTelemetryPresentation(crossSeries), null);
});

test("parseTelemetryPresentation rejects samples clustered outside the observed_at view window", () => {
  // Span ≤ 1h but entirely before the window ending at observed_at — e.g. a
  // historical 1h tab must not render two-day-old points as that window.
  const offsetCluster = structuredClone(SUCCESS);
  offsetCluster.view = "1h";
  offsetCluster.observed_at = "2026-09-12T12:00:00+00:00";
  offsetCluster.cpu_cores_samples = [
    {
      ...SUCCESS.cpu_cores_samples[0],
      sample_time: "2026-09-10T10:00:00+00:00",
      interval_start: "2026-09-10T10:00:00+00:00",
      interval_end: "2026-09-10T10:00:00+00:00",
      value: "0.10",
    },
    {
      ...SUCCESS.cpu_cores_samples[0],
      container_name: "sidecar",
      sample_time: "2026-09-10T10:45:00+00:00",
      interval_start: "2026-09-10T10:45:00+00:00",
      interval_end: "2026-09-10T10:45:00+00:00",
      value: "0.20",
    },
  ];
  offsetCluster.memory_bytes_samples = [
    {
      ...SUCCESS.memory_bytes_samples[0],
      sample_time: "2026-09-10T10:15:00+00:00",
      interval_start: "2026-09-10T10:15:00+00:00",
      interval_end: "2026-09-10T10:15:00+00:00",
    },
  ];
  assert.equal(parseTelemetryPresentation(offsetCluster), null);

  // Samples after observed_at are also outside the window ending at observed_at.
  const afterObserved = structuredClone(SUCCESS);
  afterObserved.view = "1h";
  afterObserved.observed_at = "2026-09-12T12:00:00+00:00";
  afterObserved.cpu_cores_samples = [
    {
      ...SUCCESS.cpu_cores_samples[0],
      sample_time: "2026-09-12T12:30:00+00:00",
      interval_start: "2026-09-12T12:30:00+00:00",
      interval_end: "2026-09-12T12:30:00+00:00",
      value: "0.10",
    },
  ];
  afterObserved.memory_bytes_samples = [
    {
      ...SUCCESS.memory_bytes_samples[0],
      sample_time: "2026-09-12T12:30:00+00:00",
      interval_start: "2026-09-12T12:30:00+00:00",
      interval_end: "2026-09-12T12:30:00+00:00",
    },
  ];
  assert.equal(parseTelemetryPresentation(afterObserved), null);
});

for (const series of ["cpu_cores_samples", "memory_bytes_samples"]) {
  for (const [label, observed, start, sample, end, accepted] of [
    ["future sample", "12:00:00.000000Z", "12:00:00.000001Z", "12:00:00.000001Z", "12:00:00.000001Z", false],
    ["future interval end", "12:00:00.000000Z", "11:59:00Z", "12:00:00Z", "12:00:00.000001Z", false],
    ["early sample", "12:00:00.000001Z", "11:00:00.000000Z", "11:00:00.000000Z", "11:00:00.000000Z", false],
    ["early interval start", "12:00:00.000001Z", "11:00:00.000000Z", "11:00:00.000001Z", "11:00:00.000001Z", false],
    ["inclusive bounds with offset", "12:00:00.000001Z", "13:00:00.000001000+02:00", "14:00:00.000001000+02:00", "14:00:00.000001000+02:00", true],
    ["inside bounds", "12:00:00.000002Z", "11:00:00.000003Z", "12:00:00.000001Z", "12:00:00.000001Z", true],
  ]) {
    test(`parseTelemetryPresentation checks exact view bounds: ${series} ${label}`, () => {
      const raw = structuredClone(SUCCESS);
      raw.view = "1h";
      raw.observed_at = `2026-09-12T${observed}`;
      raw.cpu_cores_samples = [];
      raw.memory_bytes_samples = [];
      raw[series] = [{
        ...SUCCESS[series][0],
        interval_start: `2026-09-12T${start}`,
        sample_time: `2026-09-12T${sample}`,
        interval_end: `2026-09-12T${end}`,
      }];
      const parsed = parseTelemetryPresentation(raw);
      // Only CPU rate starts may precede the chart left edge.
      if (accepted || (series === "cpu_cores_samples" && label === "early interval start")) {
        assert.ok(parsed);
      } else {
        assert.equal(parsed, null);
      }
    });
  }
}

test("parseTelemetryPresentation rejects estimate coverage beyond the selected view window", () => {
  // A 1h selector must not accept a 2h priced interval as that window's cost.
  const overWindow = structuredClone(SUCCESS);
  overWindow.view = "1h";
  overWindow.estimate = {
    ...structuredClone(SUCCESS.estimate),
    priced_interval_seconds: 7200,
    unpriced_interval_seconds: 0,
  };
  assert.equal(parseTelemetryPresentation(overWindow), null);

  // Combined priced + unpriced coverage also cannot exceed the view duration.
  const overPartial = structuredClone(PARTIAL);
  overPartial.view = "1h";
  overPartial.estimate = {
    ...structuredClone(PARTIAL.estimate),
    priced_interval_seconds: 3000,
    unpriced_interval_seconds: 1200,
  };
  assert.equal(parseTelemetryPresentation(overPartial), null);

  // Same bound applies to longer selectors (6h = 21600s).
  const overSixHour = structuredClone(SUCCESS);
  overSixHour.view = "6h";
  overSixHour.estimate = {
    ...structuredClone(SUCCESS.estimate),
    priced_interval_seconds: 21601,
    unpriced_interval_seconds: 0,
  };
  assert.equal(parseTelemetryPresentation(overSixHour), null);

  // Exact window duration remains valid (success fixture: 1h / 3600s).
  assert.ok(parseTelemetryPresentation(SUCCESS));
});

test("parseTelemetryPresentation requires nonblank rate-table versions for allocated estimates", () => {
  for (const fixture of [SUCCESS, PARTIAL, UNALLOCATED]) {
    for (const version of ["", " \t\n "]) {
      for (const duplicate of [false, true]) {
        const payload = structuredClone(fixture);
        payload.estimate.rate_table_version = version;
        payload.estimate.evidence = duplicate ? { rate_table_version: version } : {};
        const parsed = parseTelemetryPresentation(payload);
        if (fixture === UNALLOCATED) {
          assert.ok(parsed);
          assert.equal(parsed.estimate.rateTableVersion, null);
        } else {
          assert.equal(parsed, null, `${fixture.estimate.estimate_state}: blank rate table`);
        }
      }
    }
  }
});

test("parseTelemetryPresentation rejects malformed or conflicting estimate evidence", () => {
  // Non-string source must fail closed — not silently render as missing provenance.
  const nonStringSource = structuredClone(SUCCESS);
  nonStringSource.estimate = {
    ...structuredClone(SUCCESS.estimate),
    evidence: {
      ...structuredClone(SUCCESS.estimate.evidence),
      source: 42,
    },
  };
  assert.equal(parseTelemetryPresentation(nonStringSource), null);

  const nullSource = structuredClone(SUCCESS);
  nullSource.estimate = {
    ...structuredClone(SUCCESS.estimate),
    evidence: {
      ...structuredClone(SUCCESS.estimate.evidence),
      source: null,
    },
  };
  assert.equal(parseTelemetryPresentation(nullSource), null);

  // Blank / whitespace-only source is also discarded provenance — fail closed.
  const blankSource = structuredClone(SUCCESS);
  blankSource.estimate = {
    ...structuredClone(SUCCESS.estimate),
    evidence: {
      ...structuredClone(SUCCESS.estimate.evidence),
      source: "   ",
    },
  };
  assert.equal(parseTelemetryPresentation(blankSource), null);

  // Evidence rate_table_version that disagrees with the displayed top-level version.
  const conflictingVersion = structuredClone(SUCCESS);
  conflictingVersion.estimate = {
    ...structuredClone(SUCCESS.estimate),
    evidence: {
      ...structuredClone(SUCCESS.estimate.evidence),
      rate_table_version: "other-rate-table",
    },
  };
  assert.equal(parseTelemetryPresentation(conflictingVersion), null);

  // Non-string evidence rate_table_version is also malformed provenance.
  const nonStringVersion = structuredClone(SUCCESS);
  nonStringVersion.estimate = {
    ...structuredClone(SUCCESS.estimate),
    evidence: {
      ...structuredClone(SUCCESS.estimate.evidence),
      rate_table_version: 1,
    },
  };
  assert.equal(parseTelemetryPresentation(nonStringVersion), null);

  // Blank evidence rate_table_version vs non-blank top-level is contradictory.
  const blankEvidenceVersion = structuredClone(SUCCESS);
  blankEvidenceVersion.estimate = {
    ...structuredClone(SUCCESS.estimate),
    evidence: {
      ...structuredClone(SUCCESS.estimate.evidence),
      rate_table_version: "  ",
    },
  };
  assert.equal(parseTelemetryPresentation(blankEvidenceVersion), null);

  // Matching duplicated identities remain valid (success fixture).
  assert.ok(parseTelemetryPresentation(SUCCESS));
  // Unallocated evidence without source / rate_table_version remains valid.
  assert.ok(parseTelemetryPresentation(UNALLOCATED));
});

test("parseTelemetryPresentation rejects estimate_state that contradicts amount fields", () => {
  // complete with a null amount would surface as unpriced despite estimate_state.
  const completeNullAmount = structuredClone(SUCCESS);
  completeNullAmount.estimate = {
    ...structuredClone(SUCCESS.estimate),
    estimated_usd: null,
  };
  assert.equal(parseTelemetryPresentation(completeNullAmount), null);

  // unallocated must not carry a dollar amount.
  const unallocatedWithAmount = structuredClone(UNALLOCATED);
  unallocatedWithAmount.estimate = {
    ...structuredClone(UNALLOCATED.estimate),
    estimated_usd: "0.0100000",
  };
  assert.equal(parseTelemetryPresentation(unallocatedWithAmount), null);

  // partial without unpriced coverage is not a partial estimate.
  const partialFullyPriced = structuredClone(PARTIAL);
  partialFullyPriced.estimate = {
    ...structuredClone(PARTIAL.estimate),
    unpriced_interval_seconds: 0,
  };
  assert.equal(parseTelemetryPresentation(partialFullyPriced), null);
});

test("complete estimates require positive priced coverage, including for zero amounts", () => {
  const raw = structuredClone(SUCCESS);
  raw.estimate.priced_interval_seconds = 0;
  raw.estimate.unpriced_interval_seconds = 0;
  for (const amount of ["0", "0.0040000"]) {
    raw.estimate.estimated_usd = amount;
    assert.equal(parseTelemetryPresentation(raw), null);
  }

  raw.estimate.priced_interval_seconds = 60;
  raw.estimate.estimated_usd = "0";
  const priced = parseTelemetryPresentation(raw);
  assert.ok(priced);
  const view = projectWorkspaceTelemetryView(priced, { nowMs: FIXED_NOW });
  assert.equal(view.estimate.displayState, "complete");
  assert.equal(view.estimate.estimatedUsd, 0);
});

test("partial estimates reject positive priced coverage without an amount", () => {
  const raw = structuredClone(PARTIAL);
  raw.estimate.priced_interval_seconds = 60;
  raw.estimate.unpriced_interval_seconds = 600;
  raw.estimate.estimated_usd = null;
  assert.equal(parseTelemetryPresentation(raw), null);
});

test("partial estimates require priced coverage for a non-null amount, including zero", () => {
  const raw = structuredClone(PARTIAL);
  raw.estimate.priced_interval_seconds = 0;
  raw.estimate.unpriced_interval_seconds = 600;
  for (const amount of ["0", "0.0040000"]) {
    raw.estimate.estimated_usd = amount;
    assert.equal(parseTelemetryPresentation(raw), null);
  }

  raw.estimate.estimated_usd = null;
  const unpriced = parseTelemetryPresentation(raw);
  assert.ok(unpriced);
  const view = projectWorkspaceTelemetryView(unpriced, { nowMs: FIXED_NOW });
  assert.equal(view.estimate.displayState, "unpriced");
  assert.equal(view.estimate.estimatedUsd, null);

  raw.estimate.priced_interval_seconds = 60;
  raw.estimate.estimated_usd = "0";
  const priced = parseTelemetryPresentation(raw);
  assert.ok(priced);
  const pricedView = projectWorkspaceTelemetryView(priced, { nowMs: FIXED_NOW });
  assert.equal(pricedView.estimate.displayState, "partial");
  assert.equal(pricedView.estimate.estimatedUsd, 0);
});

test("parseTelemetryPresentation rejects unallocated state that disagrees with allocation or estimate", () => {
  // Envelope claims unallocated while retaining admitted resources, samples,
  // and a complete dollar estimate — would render a contradictory operator view.
  const contradictory = structuredClone(SUCCESS);
  contradictory.state = "unallocated";
  assert.equal(parseTelemetryPresentation(contradictory), null);

  const withAdmitted = structuredClone(UNALLOCATED);
  withAdmitted.admitted = structuredClone(SUCCESS.admitted);
  assert.equal(parseTelemetryPresentation(withAdmitted), null);

  const withSamples = structuredClone(UNALLOCATED);
  withSamples.cpu_cores_samples = structuredClone(SUCCESS.cpu_cores_samples);
  assert.equal(parseTelemetryPresentation(withSamples), null);

  const withMemSamples = structuredClone(UNALLOCATED);
  withMemSamples.memory_bytes_samples = structuredClone(SUCCESS.memory_bytes_samples);
  assert.equal(parseTelemetryPresentation(withMemSamples), null);

  const withCompleteEstimate = structuredClone(UNALLOCATED);
  withCompleteEstimate.estimate = structuredClone(SUCCESS.estimate);
  assert.equal(parseTelemetryPresentation(withCompleteEstimate), null);

  const withPricedUnallocated = structuredClone(UNALLOCATED);
  withPricedUnallocated.estimate = {
    ...structuredClone(UNALLOCATED.estimate),
    estimated_usd: "1.25",
  };
  assert.equal(parseTelemetryPresentation(withPricedUnallocated), null);

  const estimateUnallocatedOnly = structuredClone(SUCCESS);
  estimateUnallocatedOnly.estimate = structuredClone(UNALLOCATED.estimate);
  assert.equal(parseTelemetryPresentation(estimateUnallocatedOnly), null);

  const nullAdmittedAllocated = structuredClone(SUCCESS);
  nullAdmittedAllocated.admitted = null;
  // Missing admission is partial allocated data, not an unallocated contradiction.
  assert.ok(parseTelemetryPresentation(nullAdmittedAllocated));
  nullAdmittedAllocated.admitted = false;
  assert.equal(parseTelemetryPresentation(nullAdmittedAllocated), null);
});

test("partial fixture keeps missing memory used and partial cost without fabricating values", () => {
  const parsed = parseTelemetryPresentation(PARTIAL);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.state, "partial");
  assert.equal(view.cpu.usedCores, 0.1);
  assert.equal(view.cpu.usedPartial, true);
  assert.equal(view.memory.usedBytes, null);
  assert.equal(view.memory.series.length, 0);
  assert.equal(view.estimate.displayState, "partial");
  assert.equal(view.estimate.estimatedUsd, 0.004);
  assert.equal(view.estimate.unpricedIntervalSeconds, 600);
  assert.equal(view.estimate.pricedIntervalSeconds, 1800);
});

test("parseTelemetryPresentation rejects stale_after_seconds above the 5m live cap", () => {
  // Number.MAX_VALUE is finite and integer-ish, but * 1000 => Infinity and
  // would make age-based freshness always false.
  const maxValue = structuredClone(SUCCESS);
  maxValue.stale_after_seconds = Number.MAX_VALUE;
  assert.equal(parseTelemetryPresentation(maxValue), null);

  // Slice requires live stale status at >5m; a 7d producer value would leave
  // hour-old success fixtures fresh.
  assert.equal(MAX_STALE_AFTER_SECONDS, 300);

  const aboveFiveMinutes = structuredClone(SUCCESS);
  aboveFiveMinutes.stale_after_seconds = 301;
  assert.equal(parseTelemetryPresentation(aboveFiveMinutes), null);

  const sevenDayCap = structuredClone(SUCCESS);
  sevenDayCap.stale_after_seconds = 7 * 24 * 60 * 60;
  assert.equal(parseTelemetryPresentation(sevenDayCap), null);

  const atCap = structuredClone(SUCCESS);
  atCap.stale_after_seconds = MAX_STALE_AFTER_SECONDS;
  assert.ok(parseTelemetryPresentation(atCap));

  const negative = structuredClone(SUCCESS);
  negative.stale_after_seconds = -1;
  assert.equal(parseTelemetryPresentation(negative), null);

  const fractional = structuredClone(SUCCESS);
  fractional.stale_after_seconds = 300.5;
  assert.equal(parseTelemetryPresentation(fractional), null);
});

test("overflowing staleAfterSeconds fail-closes freshness to stale", () => {
  // Defense in depth if a huge threshold bypasses parse (e.g. mutated view model).
  const parsed = parseTelemetryPresentation(SUCCESS);
  assert.ok(parsed);
  parsed.staleAfterSeconds = Number.MAX_VALUE;
  const view = projectWorkspaceTelemetryView(parsed, {
    nowMs: Date.parse("2026-09-12T12:02:00+00:00"),
  });
  assert.equal(view.isStale, true);
});

test("live freshness caps mutated seven-day staleAfterSeconds at five minutes", () => {
  // Review: a 604800 producer threshold must not leave a one-hour-old success
  // fixture fresh; effective live threshold is capped at 300s.
  const parsed = parseTelemetryPresentation(SUCCESS);
  assert.ok(parsed);
  parsed.staleAfterSeconds = 7 * 24 * 60 * 60;
  const oneHourLater = Date.parse("2026-09-12T13:00:00+00:00");
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: oneHourLater });
  assert.equal(view.isStale, true);
});

test("parseTelemetryPresentation rejects nonfinite and oversized numeric strings", () => {
  for (const bad of ["NaN", "Infinity", "-1", "-0.01", "1e309", "1e20", "not-a-number", ""]) {
    const badCpu = structuredClone(SUCCESS);
    badCpu.cpu_cores_samples[0].value = bad;
    assert.equal(parseTelemetryPresentation(badCpu), null, `cpu value ${bad}`);
  }
  for (const bad of ["NaN", "Infinity", "-0.01", "1e20"]) {
    const badEst = structuredClone(SUCCESS);
    badEst.estimate.estimated_usd = bad;
    assert.equal(parseTelemetryPresentation(badEst), null, `estimate ${bad}`);
  }
  // Whitespace-padded decimals are producer contract drift — do not trim/accept.
  for (const bad of [" 0.25", "0.25 ", " 0.25 ", "\t0.25", "0.25\n"]) {
    const badCpu = structuredClone(SUCCESS);
    badCpu.cpu_cores_samples[0].value = bad;
    assert.equal(parseTelemetryPresentation(badCpu), null, `cpu padded ${JSON.stringify(bad)}`);
    const badEst = structuredClone(SUCCESS);
    badEst.estimate.estimated_usd = bad;
    assert.equal(parseTelemetryPresentation(badEst), null, `estimate padded ${JSON.stringify(bad)}`);
    const badAdmitted = structuredClone(SUCCESS);
    badAdmitted.admitted.cpu_request_cores = bad;
    assert.equal(
      parseTelemetryPresentation(badAdmitted),
      null,
      `admitted padded ${JSON.stringify(bad)}`,
    );
  }
  // Lexical nonzero below Number's range underflows to 0; must not become fake zero.
  // Extreme underflows exceed MAX_DECIMAL_STRING_LENGTH and fail closed on the
  // length gate (which also stops unbounded regex/Number work). Shorter
  // lexical nonzeros that still collapse to 0 are covered when representable
  // within the length cap — keep a canonical-zero positive control below.
  const underflow = `0.${"0".repeat(400)}1`;
  assert.equal(Number(underflow), 0);
  assert.ok(underflow.length > MAX_DECIMAL_STRING_LENGTH);
  const underflowCpu = structuredClone(SUCCESS);
  underflowCpu.cpu_cores_samples[0].value = underflow;
  assert.equal(parseTelemetryPresentation(underflowCpu), null, "cpu underflow");
  const underflowMem = structuredClone(SUCCESS);
  // Keep a cores-scale underflow on a cores field; memory bytes require safe integers.
  underflowMem.admitted.cpu_limit_cores = underflow;
  assert.equal(parseTelemetryPresentation(underflowMem), null, "admitted cpu_limit underflow");
  const underflowEst = structuredClone(SUCCESS);
  underflowEst.estimate.estimated_usd = underflow;
  assert.equal(parseTelemetryPresentation(underflowEst), null, "estimate underflow");
  const underflowAdmitted = structuredClone(SUCCESS);
  underflowAdmitted.admitted.cpu_request_cores = underflow;
  assert.equal(parseTelemetryPresentation(underflowAdmitted), null, "admitted underflow");
  // Overlong decimal strings must be rejected before regex/Number scanning.
  const overlong = `${"9".repeat(MAX_DECIMAL_STRING_LENGTH + 1)}`;
  assert.equal(overlong.length, MAX_DECIMAL_STRING_LENGTH + 1);
  const overlongCpu = structuredClone(SUCCESS);
  overlongCpu.cpu_cores_samples[0].value = overlong;
  assert.equal(parseTelemetryPresentation(overlongCpu), null, "cpu overlong decimal");
  const overlongEst = structuredClone(SUCCESS);
  overlongEst.estimate.estimated_usd = overlong;
  assert.equal(parseTelemetryPresentation(overlongEst), null, "estimate overlong decimal");
  const overlongAdmitted = structuredClone(SUCCESS);
  overlongAdmitted.admitted.cpu_request_cores = overlong;
  assert.equal(parseTelemetryPresentation(overlongAdmitted), null, "admitted overlong decimal");
  // Boundary-length valid magnitudes still parse (cap is lexical, not magnitude).
  const atLengthCap = structuredClone(SUCCESS);
  atLengthCap.cpu_cores_samples[0].value = "0." + "1".repeat(MAX_DECIMAL_STRING_LENGTH - 2);
  assert.equal(atLengthCap.cpu_cores_samples[0].value.length, MAX_DECIMAL_STRING_LENGTH);
  assert.notEqual(parseTelemetryPresentation(atLengthCap), null, "length-cap decimal ok");
  // Canonical lexical zeros must still parse as real zero (not rejected as underflow).
  const exactZero = structuredClone(SUCCESS);
  exactZero.cpu_cores_samples[0].value = "0.000";
  exactZero.estimate.estimated_usd = "0";
  assert.notEqual(parseTelemetryPresentation(exactZero), null, "lexical exact zero ok");
  const negMem = structuredClone(SUCCESS);
  negMem.admitted.memory_limit_bytes = -1;
  assert.equal(parseTelemetryPresentation(negMem), null);

  const nonFiniteMem = structuredClone(SUCCESS);
  nonFiniteMem.admitted.memory_request_bytes = Number.POSITIVE_INFINITY;
  assert.equal(parseTelemetryPresentation(nonFiniteMem), null);

  const numericCpuRequest = structuredClone(SUCCESS);
  numericCpuRequest.admitted.cpu_request_cores = 0.5;
  assert.equal(parseTelemetryPresentation(numericCpuRequest), null);

  const stringMem = structuredClone(SUCCESS);
  stringMem.admitted.memory_limit_bytes = "4294967296";
  assert.equal(parseTelemetryPresentation(stringMem), null);
});

test("parseTelemetryPresentation rejects trim-empty container names", () => {
  for (const name of ["", " ", "\t\r\n", "\u00a0\u2003"]) {
    for (const series of [
      ["cpu_cores_samples"],
      ["memory_bytes_samples"],
      ["cpu_cores_samples", "memory_bytes_samples"],
    ]) {
      const raw = structuredClone(SUCCESS);
      for (const key of series) {
        raw[key][0].container_name = name;
      }
      assert.equal(
        parseTelemetryPresentation(raw),
        null,
        `${series.join(" + ")} rejects container name ${JSON.stringify(name)}`,
      );
    }
  }
});

test("parseTelemetryPresentation rejects whitespace-padded duplicate container names", () => {
  for (const name of ["agent ", " agent", "\tagent\r\n", "\u00a0agent\u2003"]) {
    for (const series of [
      ["cpu_cores_samples"],
      ["memory_bytes_samples"],
      ["cpu_cores_samples", "memory_bytes_samples"],
    ]) {
      const raw = structuredClone(SUCCESS);
      for (const key of series) {
        const sample = { ...raw[key][0], container_name: "agent" };
        raw[key] = [sample, { ...sample, container_name: name }];
      }
      assert.equal(
        parseTelemetryPresentation(raw),
        null,
        `${series.join(" + ")} rejects padded duplicate ${JSON.stringify(name)}`,
      );
    }
  }
});

test("parseTelemetryPresentation rejects overlong container_name strings", () => {
  // Sample-count cap does not bound per-name lexical size. Overlong names are
  // copied into identity keys, sets, sorts, and partition joins.
  const overlongName = "c".repeat(MAX_CONTAINER_NAME_LENGTH + 1);
  assert.equal(overlongName.length, MAX_CONTAINER_NAME_LENGTH + 1);

  const overlongCpu = structuredClone(SUCCESS);
  overlongCpu.cpu_cores_samples[0].container_name = overlongName;
  assert.equal(parseTelemetryPresentation(overlongCpu), null, "cpu overlong container_name");

  const overlongMem = structuredClone(SUCCESS);
  overlongMem.memory_bytes_samples[0].container_name = overlongName;
  assert.equal(parseTelemetryPresentation(overlongMem), null, "memory overlong container_name");

  // Boundary-length non-empty names still parse (cap is lexical).
  const atCapName = "c".repeat(MAX_CONTAINER_NAME_LENGTH);
  assert.equal(atCapName.length, MAX_CONTAINER_NAME_LENGTH);
  const atCap = structuredClone(SUCCESS);
  atCap.cpu_cores_samples[0].container_name = atCapName;
  atCap.memory_bytes_samples[0].container_name = atCapName;
  assert.notEqual(parseTelemetryPresentation(atCap), null, "length-cap container_name ok");
});

test("parseTelemetryPresentation rejects overlong provider_resource_uid strings", () => {
  // Sample-count cap does not bound per-UID lexical size. Overlong UIDs are
  // retained and repeatedly compared during identity validation.
  const overlongUid = "u".repeat(MAX_PROVIDER_RESOURCE_UID_LENGTH + 1);
  assert.equal(overlongUid.length, MAX_PROVIDER_RESOURCE_UID_LENGTH + 1);

  const overlongSample = structuredClone(SUCCESS);
  overlongSample.cpu_cores_samples[0].provider_resource_uid = overlongUid;
  assert.equal(
    parseTelemetryPresentation(overlongSample),
    null,
    "sample overlong provider_resource_uid",
  );

  const overlongMem = structuredClone(SUCCESS);
  overlongMem.memory_bytes_samples[0].provider_resource_uid = overlongUid;
  assert.equal(
    parseTelemetryPresentation(overlongMem),
    null,
    "memory overlong provider_resource_uid",
  );

  const overlongAdmitted = structuredClone(SUCCESS);
  overlongAdmitted.admitted.provider_resource_uid = overlongUid;
  assert.equal(
    parseTelemetryPresentation(overlongAdmitted),
    null,
    "admitted overlong provider_resource_uid",
  );

  const overlongOwnership = structuredClone(SUCCESS);
  overlongOwnership.ownership.provider_resource_uid = overlongUid;
  assert.equal(
    parseTelemetryPresentation(overlongOwnership),
    null,
    "ownership overlong provider_resource_uid",
  );

  // Boundary-length non-empty UIDs still parse (cap is lexical).
  const atCapUid = "u".repeat(MAX_PROVIDER_RESOURCE_UID_LENGTH);
  assert.equal(atCapUid.length, MAX_PROVIDER_RESOURCE_UID_LENGTH);
  const atCap = structuredClone(SUCCESS);
  atCap.admitted.provider_resource_uid = atCapUid;
  atCap.admitted.evidence.pod_uid = atCapUid;
  atCap.ownership.provider_resource_uid = atCapUid;
  atCap.cpu_cores_samples[0].provider_resource_uid = atCapUid;
  atCap.memory_bytes_samples[0].provider_resource_uid = atCapUid;
  assert.notEqual(parseTelemetryPresentation(atCap), null, "length-cap provider_resource_uid ok");
});

test("parseTelemetryPresentation rejects overlong rate provenance strings", () => {
  // Numeric/timestamp/allocation caps do not bound rate_table_version or
  // evidence.source. Overlong strings are trimmed, retained, and projected.
  const overlong = "r".repeat(MAX_RATE_PROVENANCE_LENGTH + 1);
  assert.equal(overlong.length, MAX_RATE_PROVENANCE_LENGTH + 1);

  const overlongTopLevel = structuredClone(SUCCESS);
  overlongTopLevel.estimate = {
    ...structuredClone(SUCCESS.estimate),
    rate_table_version: overlong,
    evidence: {
      ...structuredClone(SUCCESS.estimate.evidence),
      rate_table_version: overlong,
    },
  };
  assert.equal(
    parseTelemetryPresentation(overlongTopLevel),
    null,
    "overlong rate_table_version",
  );

  const overlongSource = structuredClone(SUCCESS);
  overlongSource.estimate = {
    ...structuredClone(SUCCESS.estimate),
    evidence: {
      ...structuredClone(SUCCESS.estimate.evidence),
      source: overlong,
    },
  };
  assert.equal(
    parseTelemetryPresentation(overlongSource),
    null,
    "overlong evidence.source",
  );

  // Evidence-only overlong version (top-level still short) fails before compare.
  const overlongEvidenceVersion = structuredClone(SUCCESS);
  overlongEvidenceVersion.estimate = {
    ...structuredClone(SUCCESS.estimate),
    evidence: {
      ...structuredClone(SUCCESS.estimate.evidence),
      rate_table_version: overlong,
    },
  };
  assert.equal(
    parseTelemetryPresentation(overlongEvidenceVersion),
    null,
    "overlong evidence.rate_table_version",
  );

  // Boundary-length matching provenance still parses (cap is lexical).
  const atCap = "r".repeat(MAX_RATE_PROVENANCE_LENGTH);
  assert.equal(atCap.length, MAX_RATE_PROVENANCE_LENGTH);
  const atCapEstimate = structuredClone(SUCCESS);
  atCapEstimate.estimate = {
    ...structuredClone(SUCCESS.estimate),
    rate_table_version: atCap,
    evidence: {
      source: atCap,
      rate_table_version: atCap,
    },
  };
  assert.notEqual(
    parseTelemetryPresentation(atCapEstimate),
    null,
    "length-cap rate provenance ok",
  );
});

for (const field of ["compute_class", "region"]) {
  for (const blank of ["", " ", "\t\n", "\u00a0"]) {
    test(`parseTelemetryPresentation rejects blank ${field}: ${JSON.stringify(blank)}`, () => {
      const raw = structuredClone(SUCCESS);
      raw.admitted[field] = blank;
      assert.equal(parseTelemetryPresentation(raw), null);
    });
  }
}

test("parseTelemetryPresentation preserves region but leaves unknown class unidentified", () => {
  const raw = structuredClone(SUCCESS);
  raw.admitted.compute_class = " Balanced ";
  raw.admitted.region = " us-central1\t";
  const parsed = parseTelemetryPresentation(raw);
  assert.ok(parsed);
  // Do not turn an unknown class spelling into a known pricing identity.
  assert.equal(parsed.admitted.computeClass, null);
  assert.equal(parsed.admitted.partial, true);
  assert.equal(parsed.admitted.region, raw.admitted.region);
});

test("parseTelemetryPresentation rejects overlong allocation presentation strings", () => {
  // Numeric/timestamp/sample/container caps do not bound compute_class, region,
  // or pod_phase. Overlong labels are retained and projected into Fact nodes.
  const overlongLabel = "x".repeat(MAX_ALLOCATION_LABEL_LENGTH + 1);
  assert.equal(overlongLabel.length, MAX_ALLOCATION_LABEL_LENGTH + 1);

  const overlongCompute = structuredClone(SUCCESS);
  overlongCompute.admitted.compute_class = overlongLabel;
  assert.equal(
    parseTelemetryPresentation(overlongCompute),
    null,
    "overlong compute_class",
  );

  const overlongRegion = structuredClone(SUCCESS);
  overlongRegion.admitted.region = overlongLabel;
  assert.equal(parseTelemetryPresentation(overlongRegion), null, "overlong region");

  const overlongPhase = structuredClone(SUCCESS);
  overlongPhase.admitted.pod_phase = overlongLabel;
  assert.equal(parseTelemetryPresentation(overlongPhase), null, "overlong pod_phase");

  // Boundary-length labels still parse (cap is lexical).
  const atCapLabel = "x".repeat(MAX_ALLOCATION_LABEL_LENGTH);
  assert.equal(atCapLabel.length, MAX_ALLOCATION_LABEL_LENGTH);
  const atCap = structuredClone(SUCCESS);
  atCap.admitted.compute_class = atCapLabel;
  atCap.admitted.region = atCapLabel;
  atCap.admitted.pod_phase = atCapLabel;
  assert.notEqual(parseTelemetryPresentation(atCap), null, "length-cap allocation labels ok");
});

test("parseTelemetryPresentation rejects overlong RFC3339 timestamp strings", () => {
  // Decimal length cap does not cover timestamps. A syntactically valid but
  // arbitrarily long fractional-second portion would otherwise pass the regex
  // and Date.parse, then be sliced/padded repeatedly across sample grouping.
  const prefix = "2026-09-12T12:00:00.";
  const suffix = "Z";
  const overlongFracDigits =
    MAX_RFC3339_TIMESTAMP_LENGTH - prefix.length - suffix.length + 1;
  const overlongTs = `${prefix}${"1".repeat(overlongFracDigits)}${suffix}`;
  assert.ok(overlongTs.length > MAX_RFC3339_TIMESTAMP_LENGTH);

  const overlongSample = structuredClone(SUCCESS);
  overlongSample.cpu_cores_samples[0].sample_time = overlongTs;
  overlongSample.cpu_cores_samples[0].interval_start = overlongTs;
  overlongSample.cpu_cores_samples[0].interval_end = overlongTs;
  assert.equal(parseTelemetryPresentation(overlongSample), null, "sample overlong timestamp");

  const overlongObserved = structuredClone(SUCCESS);
  overlongObserved.observed_at = overlongTs;
  assert.equal(parseTelemetryPresentation(overlongObserved), null, "envelope overlong timestamp");

  const overlongAdmitted = structuredClone(SUCCESS);
  overlongAdmitted.admitted.observed_at = overlongTs;
  assert.equal(parseTelemetryPresentation(overlongAdmitted), null, "admitted overlong timestamp");

  // Boundary-length valid timestamps still parse (cap is lexical).
  const atCapFracDigits =
    MAX_RFC3339_TIMESTAMP_LENGTH - prefix.length - suffix.length;
  const atCapTs = `${prefix}${"1".repeat(atCapFracDigits)}${suffix}`;
  assert.equal(atCapTs.length, MAX_RFC3339_TIMESTAMP_LENGTH);
  const atCap = structuredClone(SUCCESS);
  atCap.cpu_cores_samples[0].sample_time = atCapTs;
  atCap.cpu_cores_samples[0].interval_start = atCapTs;
  atCap.cpu_cores_samples[0].interval_end = atCapTs;
  atCap.memory_bytes_samples[0].sample_time = atCapTs;
  atCap.memory_bytes_samples[0].interval_start = atCapTs;
  atCap.memory_bytes_samples[0].interval_end = atCapTs;
  atCap.observed_at = atCapTs;
  atCap.admitted.observed_at = atCapTs;
  assert.notEqual(parseTelemetryPresentation(atCap), null, "length-cap timestamp ok");
});

test("parseTelemetryPresentation rejects byte values outside the safe integer range", () => {
  // Above MAX_SAFE_INTEGER: JS Number rounds, but fail-closed parse must reject.
  const unsafeAdmitted = structuredClone(SUCCESS);
  unsafeAdmitted.admitted.memory_request_bytes = 9007199254740993;
  assert.equal(parseTelemetryPresentation(unsafeAdmitted), null);

  const unsafeLimit = structuredClone(SUCCESS);
  unsafeLimit.admitted.memory_limit_bytes = Number.MAX_SAFE_INTEGER + 1;
  assert.equal(parseTelemetryPresentation(unsafeLimit), null);

  const fractionalBytes = structuredClone(SUCCESS);
  fractionalBytes.admitted.ephemeral_storage_request_bytes = 1024.5;
  assert.equal(parseTelemetryPresentation(fractionalBytes), null);

  const unsafeSample = structuredClone(SUCCESS);
  unsafeSample.memory_bytes_samples[0].value = "9007199254740993";
  assert.equal(parseTelemetryPresentation(unsafeSample), null);

  const fractionalSample = structuredClone(SUCCESS);
  fractionalSample.memory_bytes_samples[0].value = "1024.5";
  assert.equal(parseTelemetryPresentation(fractionalSample), null);

  const maxSafe = structuredClone(SUCCESS);
  maxSafe.admitted.memory_limit_bytes = Number.MAX_SAFE_INTEGER;
  maxSafe.memory_bytes_samples[0].value = String(Number.MAX_SAFE_INTEGER);
  assert.ok(parseTelemetryPresentation(maxSafe));
});

test("projectWorkspaceTelemetryView fails closed when byte aggregates exceed safe integer range", () => {
  // Each sample is individually valid, but their same-timestamp sum is not exact.
  const half = Math.floor(Number.MAX_SAFE_INTEGER / 2) + 1;
  const multi = structuredClone(SUCCESS);
  const base = SUCCESS.memory_bytes_samples[0];
  multi.memory_bytes_samples = [
    {
      ...base,
      container_name: "agent",
      value: String(half),
    },
    {
      ...base,
      container_name: "sidecar",
      value: String(half),
    },
  ];
  const parsed = parseTelemetryPresentation(multi);
  assert.ok(parsed);
  assert.equal(parsed.memorySamples.length, 2);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.memory.usedBytes, null);
  assert.equal(view.memory.usedPartial, true);
  assert.equal(view.memory.series.length, 0);
});

test("container partition does not merge samples across different sample_time moments", () => {
  const multi = structuredClone(SUCCESS);
  multi.cpu_cores_samples = [
    {
      ...SUCCESS.cpu_cores_samples[0],
      container_name: "agent",
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T12:00:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
      value: "0.25",
    },
    {
      ...SUCCESS.cpu_cores_samples[0],
      container_name: "sidecar",
      sample_time: "2026-09-12T11:59:00+00:00",
      interval_start: "2026-09-12T11:59:00+00:00",
      interval_end: "2026-09-12T11:59:00+00:00",
      value: "0.40",
    },
  ];
  // Memory only at the older timestamp — latest CPU timestamp lacks memory.
  multi.memory_bytes_samples = [
    {
      ...SUCCESS.memory_bytes_samples[0],
      container_name: "agent",
      sample_time: "2026-09-12T11:59:00+00:00",
      interval_start: "2026-09-12T11:59:00+00:00",
      interval_end: "2026-09-12T11:59:00+00:00",
      value: "536870912",
    },
  ];
  const parsed = parseTelemetryPresentation(multi);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  // Latest CPU group is agent@12:00 only — do not merge sidecar 0.40 from earlier,
  // and do not present the agent-only subset as a complete pod total vs limits.
  assert.equal(view.cpu.usedCores, null);
  assert.equal(view.cpu.usedPartial, true);
  // Historical sparkline groups must also mark incomplete partitions (not ok).
  assert.deepEqual(
    view.cpu.series.map((p) => ({
      sampleTime: p.sampleTime,
      value: p.value,
      quality: p.quality,
    })),
    [
      { sampleTime: "2026-09-12T11:59:00+00:00", value: 0.4, quality: "partial" },
      { sampleTime: "2026-09-12T12:00:00+00:00", value: 0.25, quality: "partial" },
    ],
  );
  // Memory only saw agent, but CPU observed sidecar elsewhere — do not treat
  // agent-only memory as a complete pod total vs whole-Pod request/limit.
  assert.equal(view.memory.usedBytes, null);
  assert.equal(view.memory.usedPartial, true);
});

test("cross-meter container union marks narrower meter incomplete", () => {
  // CPU observes agent+sidecar at one instant; memory only reports agent.
  // Independent per-meter expected sets would treat memory as complete.
  const multi = structuredClone(SUCCESS);
  const cpuBase = SUCCESS.cpu_cores_samples[0];
  const memBase = SUCCESS.memory_bytes_samples[0];
  multi.cpu_cores_samples = [
    { ...cpuBase, container_name: "agent", value: "0.10" },
    { ...cpuBase, container_name: "sidecar", value: "0.15" },
  ];
  multi.memory_bytes_samples = [
    { ...memBase, container_name: "agent", value: "536870912" },
  ];
  const parsed = parseTelemetryPresentation(multi);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.cpu.usedCores, 0.25);
  assert.equal(view.cpu.usedPartial, false);
  assert.deepEqual(view.cpu.containerNamesAtSample?.sort(), ["agent", "sidecar"]);
  assert.equal(view.memory.usedBytes, null);
  assert.equal(view.memory.usedPartial, true);
  assert.equal(view.memory.series[0]?.quality, "partial");

  // Symmetric: memory has both containers, CPU only agent.
  const symmetric = structuredClone(SUCCESS);
  symmetric.cpu_cores_samples = [
    { ...cpuBase, container_name: "agent", value: "0.10" },
  ];
  symmetric.memory_bytes_samples = [
    { ...memBase, container_name: "agent", value: "268435456" },
    { ...memBase, container_name: "sidecar", value: "268435456" },
  ];
  const symParsed = parseTelemetryPresentation(symmetric);
  assert.ok(symParsed);
  const symView = projectWorkspaceTelemetryView(symParsed, { nowMs: FIXED_NOW });
  assert.equal(symView.memory.usedBytes, 536870912);
  assert.equal(symView.memory.usedPartial, false);
  assert.equal(symView.cpu.usedCores, null);
  assert.equal(symView.cpu.usedPartial, true);
  assert.equal(symView.cpu.series[0]?.quality, "partial");
});

test("same-timestamp multi-container samples may sum within that timestamp only", () => {
  const multi = structuredClone(SUCCESS);
  multi.cpu_cores_samples = [
    { ...SUCCESS.cpu_cores_samples[0], container_name: "agent", value: "0.10" },
    { ...SUCCESS.cpu_cores_samples[0], container_name: "sidecar", value: "0.15" },
  ];
  // Memory must also cover the union or the CPU total is compared alone.
  multi.memory_bytes_samples = [
    { ...SUCCESS.memory_bytes_samples[0], container_name: "agent", value: "268435456" },
    {
      ...SUCCESS.memory_bytes_samples[0],
      container_name: "sidecar",
      value: "268435456",
    },
  ];
  const parsed = parseTelemetryPresentation(multi);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.cpu.usedCores, 0.25);
  assert.equal(view.cpu.usedPartial, false);
  assert.deepEqual(view.cpu.containerNamesAtSample?.sort(), ["agent", "sidecar"]);
  assert.equal(view.memory.usedBytes, 536870912);
  assert.equal(view.memory.usedPartial, false);
});

test("same sample_time with mismatched intervals is partial, not a complete pod total", () => {
  const multi = structuredClone(SUCCESS);
  const base = SUCCESS.cpu_cores_samples[0];
  multi.cpu_cores_samples = [
    {
      ...base,
      container_name: "agent",
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T11:59:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
      value: "0.10",
    },
    {
      ...base,
      container_name: "sidecar",
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T11:58:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
      value: "0.15",
    },
  ];
  const parsed = parseTelemetryPresentation(multi);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  // Different measurement windows must not be compared as a whole-pod total.
  assert.equal(view.cpu.usedCores, null);
  assert.equal(view.cpu.usedPartial, true);
  // Do not emit a sparkline pod-total that sums across mismatched intervals.
  assert.deepEqual(view.cpu.series, []);
});

test("same-instant RFC3339 spellings share interval and partition identity", () => {
  // Producers may serialize the same UTC instant as Z or an equivalent offset.
  // Raw-string equality would treat a complete scrape as mismatched/partial.
  const multi = structuredClone(SUCCESS);
  const base = SUCCESS.cpu_cores_samples[0];
  multi.cpu_cores_samples = [
    {
      ...base,
      container_name: "agent",
      sample_time: "2026-09-12T12:00:00Z",
      interval_start: "2026-09-12T11:59:00Z",
      interval_end: "2026-09-12T12:00:00Z",
      value: "0.10",
    },
    {
      ...base,
      container_name: "sidecar",
      sample_time: "2026-09-12T13:00:00+01:00",
      interval_start: "2026-09-12T12:59:00+01:00",
      interval_end: "2026-09-12T13:00:00+01:00",
      value: "0.15",
    },
  ];
  // Memory uses yet another spelling of the same sample instant as agent.
  multi.memory_bytes_samples = [
    {
      ...SUCCESS.memory_bytes_samples[0],
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T11:59:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
    },
  ];
  const parsed = parseTelemetryPresentation(multi);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.cpu.usedCores, 0.25);
  assert.equal(view.cpu.usedPartial, false);
  assert.deepEqual(view.cpu.containerNamesAtSample?.sort(), ["agent", "sidecar"]);
  assert.equal(view.cpu.series.length, 1);
  assert.equal(view.cpu.series[0].value, 0.25);
  assert.equal(view.cpu.series[0].quality, "ok");
  // CPU Z / memory +00:00 are the same instant — not a mixed Sample label.
  assert.equal(view.sampleTimeMixed, false);
});

test("distinct sub-millisecond sample_times stay separate partitions", () => {
  // Date.parse truncates to ms, so .000001Z and .000002Z share one epoch ms.
  // Identity must retain sub-ms fraction or two same-container readings of 1
  // and 2 become a current usage of 3 and a single history point.
  const multi = structuredClone(SUCCESS);
  // Anchor the view at the newest sample while retaining the identity assertions.
  multi.observed_at = "2026-09-12T12:00:00.000002Z";
  const base = SUCCESS.cpu_cores_samples[0];
  multi.cpu_cores_samples = [
    {
      ...base,
      container_name: "agent",
      sample_time: "2026-09-12T12:00:00.000001Z",
      interval_start: "2026-09-12T11:59:00.000001Z",
      interval_end: "2026-09-12T12:00:00.000001Z",
      value: "1",
    },
    {
      ...base,
      container_name: "agent",
      sample_time: "2026-09-12T12:00:00.000002Z",
      interval_start: "2026-09-12T11:59:00.000002Z",
      interval_end: "2026-09-12T12:00:00.000002Z",
      value: "2",
    },
  ];
  const parsed = parseTelemetryPresentation(multi);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.cpu.usedCores, 2);
  assert.equal(view.cpu.usedPartial, false);
  assert.equal(view.cpu.sampleTime, "2026-09-12T12:00:00.000002Z");
  assert.deepEqual(
    view.cpu.series.map((p) => ({ sampleTime: p.sampleTime, value: p.value })),
    [
      { sampleTime: "2026-09-12T12:00:00.000001Z", value: 1 },
      { sampleTime: "2026-09-12T12:00:00.000002Z", value: 2 },
    ],
  );
});

test("same sub-millisecond instant keeps identity across RFC3339 spellings", () => {
  // Exact instant keys must equate Z / offset / trailing-zero forms of one
  // sub-ms moment (agent-format style) while still not collapsing distinct
  // fractions — otherwise pod totals stay split or inflate incorrectly.
  const multi = structuredClone(SUCCESS);
  // Anchor the view at the newest sample while retaining the identity assertions.
  multi.observed_at = "2026-09-12T12:00:00.000001Z";
  const base = SUCCESS.cpu_cores_samples[0];
  multi.cpu_cores_samples = [
    {
      ...base,
      container_name: "agent",
      sample_time: "2026-09-12T12:00:00.000001Z",
      interval_start: "2026-09-12T11:59:00.000001Z",
      interval_end: "2026-09-12T12:00:00.000001Z",
      value: "0.10",
    },
    {
      ...base,
      container_name: "sidecar",
      sample_time: "2026-09-12T12:00:00.000001000+00:00",
      interval_start: "2026-09-12T11:59:00.000001000+00:00",
      interval_end: "2026-09-12T12:00:00.000001000+00:00",
      value: "0.15",
    },
  ];
  multi.memory_bytes_samples = [
    {
      ...SUCCESS.memory_bytes_samples[0],
      sample_time: "2026-09-12T13:00:00.000001+01:00",
      interval_start: "2026-09-12T12:59:00.000001+01:00",
      interval_end: "2026-09-12T13:00:00.000001+01:00",
    },
  ];
  const parsed = parseTelemetryPresentation(multi);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.cpu.usedCores, 0.25);
  assert.equal(view.cpu.usedPartial, false);
  assert.deepEqual(view.cpu.containerNamesAtSample?.sort(), ["agent", "sidecar"]);
  assert.equal(view.cpu.series.length, 1);
  assert.equal(view.cpu.series[0].value, 0.25);
  assert.equal(view.sampleTimeMixed, false);
});

test("sparkline series uses pod totals per timestamp, not raw per-container points", () => {
  const multi = structuredClone(SUCCESS);
  const base = SUCCESS.cpu_cores_samples[0];
  multi.cpu_cores_samples = [
    {
      ...base,
      container_name: "agent",
      sample_time: "2026-09-12T11:59:00+00:00",
      interval_start: "2026-09-12T11:59:00+00:00",
      interval_end: "2026-09-12T11:59:00+00:00",
      value: "0.10",
    },
    {
      ...base,
      container_name: "sidecar",
      sample_time: "2026-09-12T11:59:00+00:00",
      interval_start: "2026-09-12T11:59:00+00:00",
      interval_end: "2026-09-12T11:59:00+00:00",
      value: "0.15",
    },
    {
      ...base,
      container_name: "agent",
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T12:00:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
      value: "0.20",
    },
    {
      ...base,
      container_name: "sidecar",
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T12:00:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
      value: "0.05",
      quality: "partial",
    },
  ];
  const parsed = parseTelemetryPresentation(multi);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  // Meter and history must agree on pod totals (not four raw container points).
  assert.equal(view.cpu.usedCores, 0.25);
  assert.equal(view.cpu.usedPartial, true);
  assert.equal(view.cpu.series.length, 2);
  assert.deepEqual(
    view.cpu.series.map((p) => ({
      sampleTime: p.sampleTime,
      value: p.value,
      quality: p.quality,
    })),
    [
      { sampleTime: "2026-09-12T11:59:00+00:00", value: 0.25, quality: "ok" },
      { sampleTime: "2026-09-12T12:00:00+00:00", value: 0.25, quality: "partial" },
    ],
  );
  assert.equal(view.cpu.series[1].value, view.cpu.usedCores);
});

test("incomplete historical partitions keep stale over forced partial", () => {
  const multi = structuredClone(SUCCESS);
  const base = SUCCESS.cpu_cores_samples[0];
  multi.cpu_cores_samples = [
    {
      ...base,
      container_name: "sidecar",
      sample_time: "2026-09-12T11:59:00+00:00",
      interval_start: "2026-09-12T11:59:00+00:00",
      interval_end: "2026-09-12T11:59:00+00:00",
      value: "0.40",
      quality: "stale",
    },
    {
      ...base,
      container_name: "agent",
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T12:00:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
      value: "0.25",
    },
  ];
  const parsed = parseTelemetryPresentation(multi);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.deepEqual(
    view.cpu.series.map((p) => ({ sampleTime: p.sampleTime, quality: p.quality })),
    [
      { sampleTime: "2026-09-12T11:59:00+00:00", quality: "stale" },
      { sampleTime: "2026-09-12T12:00:00+00:00", quality: "partial" },
    ],
  );
});

test("parseTelemetryPresentation rejects contradictory allocation evidence Pod identity", () => {
  for (const identitySource of ["admitted", "ownership", "samples"]) {
    const raw = structuredClone(SUCCESS);
    raw.admitted.evidence.pod_uid = "other-pod-uid";
    if (identitySource !== "admitted") delete raw.admitted.provider_resource_uid;
    if (identitySource === "samples") delete raw.ownership.provider_resource_uid;
    assert.equal(parseTelemetryPresentation(raw), null, identitySource);
  }
});

test("parseTelemetryPresentation validates known allocation evidence UID fields", () => {
  for (const evidence of [[], "invalid", 42]) {
    const raw = structuredClone(SUCCESS);
    raw.admitted.evidence = evidence;
    assert.equal(parseTelemetryPresentation(raw), null);
  }
  for (const field of ["pod_uid", "owner_job_uid"]) {
    for (const invalid of [null, 42, {}, "", " ", "u".repeat(MAX_PROVIDER_RESOURCE_UID_LENGTH + 1)]) {
      const raw = structuredClone(SUCCESS);
      raw.admitted.evidence[field] = invalid;
      assert.equal(parseTelemetryPresentation(raw), null, field);
    }
  }
});

test("parseTelemetryPresentation validates admitted owner Job identity", () => {
  for (const evidence of [undefined, null, {}, SUCCESS.admitted.evidence]) {
    for (const invalid of [null, 42, {}, "", " ", "u".repeat(MAX_PROVIDER_RESOURCE_UID_LENGTH + 1)]) {
      const raw = structuredClone(SUCCESS);
      raw.admitted.evidence = evidence;
      raw.admitted.owner_job_uid = invalid;
      assert.equal(parseTelemetryPresentation(raw), null);
    }
  }
});

test("parseTelemetryPresentation compares duplicated owner Job identities", () => {
  const raw = structuredClone(SUCCESS);
  raw.admitted.owner_job_uid = "different-job-uid";
  assert.equal(parseTelemetryPresentation(raw), null);

  raw.admitted.evidence.owner_job_uid = raw.admitted.owner_job_uid;
  assert.ok(parseTelemetryPresentation(raw), "matching owner Job identities");
  delete raw.admitted.owner_job_uid;
  assert.ok(parseTelemetryPresentation(raw), "evidence-only owner Job identity");
  raw.admitted.owner_job_uid = undefined;
  assert.ok(parseTelemetryPresentation(raw), "undefined optional owner Job identity");
  delete raw.admitted.evidence.owner_job_uid;
  assert.ok(parseTelemetryPresentation(raw), "both owner Job identities absent");
});

test("parseTelemetryPresentation accepts optional and unprojected allocation evidence", () => {
  for (const evidence of [undefined, null, {}, { future_field: { opaque: true } },
    { pod_uid: undefined, owner_job_uid: undefined },
    { owner_job_uid: "u".repeat(MAX_PROVIDER_RESOURCE_UID_LENGTH) }]) {
    const raw = structuredClone(SUCCESS);
    raw.admitted.evidence = evidence;
    if (evidence?.owner_job_uid) raw.admitted.owner_job_uid = evidence.owner_job_uid;
    assert.ok(parseTelemetryPresentation(raw));
  }
  const raw = structuredClone(SUCCESS);
  delete raw.admitted.provider_resource_uid;
  delete raw.ownership.provider_resource_uid;
  assert.ok(parseTelemetryPresentation(raw), "evidence agrees with sample identity");
});

test("parseTelemetryPresentation rejects mismatched sample provider_resource_uid", () => {
  const mismatched = structuredClone(SUCCESS);
  mismatched.cpu_cores_samples[0].provider_resource_uid = "other-pod-uid";
  assert.equal(parseTelemetryPresentation(mismatched), null);

  const mixedSeries = structuredClone(SUCCESS);
  mixedSeries.cpu_cores_samples = [
    { ...SUCCESS.cpu_cores_samples[0], container_name: "agent", value: "0.10" },
    {
      ...SUCCESS.cpu_cores_samples[0],
      container_name: "sidecar",
      value: "0.15",
      provider_resource_uid: "other-pod-uid",
    },
  ];
  assert.equal(parseTelemetryPresentation(mixedSeries), null);
});

test("parseTelemetryPresentation rejects samples without provider_resource_uid", () => {
  const omitted = structuredClone(SUCCESS);
  delete omitted.cpu_cores_samples[0].provider_resource_uid;
  assert.equal(parseTelemetryPresentation(omitted), null);

  const empty = structuredClone(SUCCESS);
  empty.memory_bytes_samples[0].provider_resource_uid = "";
  assert.equal(parseTelemetryPresentation(empty), null);
});

test("parseTelemetryPresentation rejects whitespace-only sample UIDs without presentation UID", () => {
  for (const uid of ["   ", "\t\n", "\u00a0"]) {
    const raw = structuredClone(SUCCESS);
    delete raw.admitted.provider_resource_uid;
    delete raw.ownership.provider_resource_uid;
    delete raw.admitted.evidence.pod_uid;
    for (const sample of [...raw.cpu_cores_samples, ...raw.memory_bytes_samples]) {
      sample.provider_resource_uid = uid;
    }
    assert.equal(parseTelemetryPresentation(raw), null, JSON.stringify(uid));
  }
});

test("parseTelemetryPresentation rejects cross-metric sample UID mismatch without presentation UID", () => {
  const cross = structuredClone(SUCCESS);
  delete cross.admitted.provider_resource_uid;
  delete cross.ownership.provider_resource_uid;
  delete cross.admitted.evidence.pod_uid;
  cross.cpu_cores_samples[0].provider_resource_uid = "pod-uid-cpu";
  cross.memory_bytes_samples[0].provider_resource_uid = "pod-uid-memory";
  assert.equal(parseTelemetryPresentation(cross), null);

  const aligned = structuredClone(SUCCESS);
  delete aligned.admitted.provider_resource_uid;
  delete aligned.ownership.provider_resource_uid;
  delete aligned.admitted.evidence.pod_uid;
  aligned.cpu_cores_samples[0].provider_resource_uid = "pod-uid-shared";
  aligned.memory_bytes_samples[0].provider_resource_uid = "pod-uid-shared";
  assert.ok(parseTelemetryPresentation(aligned));
});

test("parseTelemetryPresentation rejects conflicting admitted vs ownership provider_resource_uid", () => {
  const conflict = structuredClone(SUCCESS);
  conflict.admitted.provider_resource_uid = "pod-uid-admitted";
  conflict.ownership.provider_resource_uid = "pod-uid-ownership";
  // Samples match admitted — must still reject so ownership B cannot display A's samples.
  conflict.cpu_cores_samples[0].provider_resource_uid = "pod-uid-admitted";
  conflict.memory_bytes_samples[0].provider_resource_uid = "pod-uid-admitted";
  assert.equal(parseTelemetryPresentation(conflict), null);

  // Reverse: samples match ownership while admitted names a different pod.
  const conflictOwnership = structuredClone(SUCCESS);
  conflictOwnership.admitted.provider_resource_uid = "pod-uid-admitted";
  conflictOwnership.ownership.provider_resource_uid = "pod-uid-ownership";
  conflictOwnership.cpu_cores_samples[0].provider_resource_uid = "pod-uid-ownership";
  conflictOwnership.memory_bytes_samples[0].provider_resource_uid = "pod-uid-ownership";
  assert.equal(parseTelemetryPresentation(conflictOwnership), null);

  // Malformed identity on either side must fail closed (not silently prefer the other).
  const malformedAdmitted = structuredClone(SUCCESS);
  malformedAdmitted.admitted.provider_resource_uid = "";
  assert.equal(parseTelemetryPresentation(malformedAdmitted), null);
  const malformedOwnership = structuredClone(SUCCESS);
  malformedOwnership.ownership.provider_resource_uid = 42;
  assert.equal(parseTelemetryPresentation(malformedOwnership), null);

  const matching = structuredClone(SUCCESS);
  matching.admitted.provider_resource_uid = "pod-uid-shared";
  matching.admitted.evidence.pod_uid = "pod-uid-shared";
  matching.ownership.provider_resource_uid = "pod-uid-shared";
  matching.cpu_cores_samples[0].provider_resource_uid = "pod-uid-shared";
  matching.memory_bytes_samples[0].provider_resource_uid = "pod-uid-shared";
  assert.ok(parseTelemetryPresentation(matching));
});

test("parseTelemetryPresentation rejects duplicate container samples at the same timestamp", () => {
  const dup = structuredClone(SUCCESS);
  dup.cpu_cores_samples = [
    { ...SUCCESS.cpu_cores_samples[0], container_name: "agent", value: "0.10" },
    { ...SUCCESS.cpu_cores_samples[0], container_name: "agent", value: "0.20" },
  ];
  assert.equal(parseTelemetryPresentation(dup), null);
});

test("parseTelemetryPresentation rejects duplicate container at same instant with alternate RFC3339 spellings", () => {
  // Projection partitions by normalized instant key; identity must use the same
  // key or Z vs +00:00 spellings of one container@instant would parse as
  // distinct rows and inflate the pod total when summed.
  const dup = structuredClone(SUCCESS);
  const base = SUCCESS.cpu_cores_samples[0];
  dup.cpu_cores_samples = [
    {
      ...base,
      container_name: "agent",
      sample_time: "2026-09-12T12:00:00Z",
      interval_start: "2026-09-12T11:59:00Z",
      interval_end: "2026-09-12T12:00:00Z",
      value: "0.10",
    },
    {
      ...base,
      container_name: "agent",
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T11:59:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
      value: "0.20",
    },
  ];
  assert.equal(parseTelemetryPresentation(dup), null);
});

test("projection fails closed on duplicate container@instant even if parse were bypassed", () => {
  // Defense in depth for aggregation: same container with Z vs +00:00 at one
  // epoch must not sum into a complete pod total (Bugbot additional sites).
  const parsed = parseTelemetryPresentation(SUCCESS);
  assert.ok(parsed);
  const presentation = {
    ...parsed,
    cpuSamples: [
      {
        containerName: "agent",
        sampleTime: "2026-09-12T12:00:00Z",
        intervalStart: "2026-09-12T11:59:00Z",
        intervalEnd: "2026-09-12T12:00:00Z",
        unit: "cores",
        value: 0.1,
        quality: "ok",
        providerResourceUid: "pod-uid-example",
      },
      {
        containerName: "agent",
        sampleTime: "2026-09-12T12:00:00+00:00",
        intervalStart: "2026-09-12T11:59:00+00:00",
        intervalEnd: "2026-09-12T12:00:00+00:00",
        unit: "cores",
        value: 0.2,
        quality: "ok",
        providerResourceUid: "pod-uid-example",
      },
    ],
  };
  const view = projectWorkspaceTelemetryView(presentation, { nowMs: FIXED_NOW });
  assert.equal(view.cpu.usedCores, null);
  assert.equal(view.cpu.usedPartial, true);
  assert.deepEqual(view.cpu.series, []);
});

test("projectWorkspaceTelemetryView does not surface provider_resource_uid", () => {
  const parsed = parseTelemetryPresentation(SUCCESS);
  assert.ok(parsed);
  assert.equal(parsed.cpuSamples[0].providerResourceUid, "pod-uid-example");
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(JSON.stringify(view).includes("pod-uid-example"), false);
  assert.equal(JSON.stringify(view).includes("providerResourceUid"), false);
});

test("stale fixture and clock-based freshness", () => {
  const staleParsed = parseTelemetryPresentation(STALE);
  assert.ok(staleParsed);
  const staleView = projectWorkspaceTelemetryView(staleParsed, {
    nowMs: Date.parse("2026-09-12T12:00:00+00:00"),
  });
  assert.equal(staleView.isStale, true);
  assert.equal(staleView.state, "stale");
  assert.equal(staleView.view, "24h");

  const successParsed = parseTelemetryPresentation(SUCCESS);
  assert.ok(successParsed);
  const fresh = projectWorkspaceTelemetryView(successParsed, {
    nowMs: Date.parse("2026-09-12T12:02:00+00:00"),
  });
  assert.equal(fresh.isStale, false);

  const aged = projectWorkspaceTelemetryView(successParsed, {
    nowMs: Date.parse("2026-09-12T12:06:00+00:00"),
  });
  assert.equal(aged.isStale, true);
});

test("fresh envelope with aged meter samples is stale and Sample uses sample times", () => {
  const agedMeters = structuredClone(SUCCESS);
  // Envelope is fresh relative to now; CPU/memory readings are older than stale_after.
  agedMeters.observed_at = "2026-09-12T12:05:00+00:00";
  agedMeters.cpu_cores_samples[0].sample_time = "2026-09-12T11:50:00+00:00";
  agedMeters.cpu_cores_samples[0].interval_start = "2026-09-12T11:50:00+00:00";
  agedMeters.cpu_cores_samples[0].interval_end = "2026-09-12T11:50:00+00:00";
  agedMeters.memory_bytes_samples[0].sample_time = "2026-09-12T11:51:00+00:00";
  agedMeters.memory_bytes_samples[0].interval_start = "2026-09-12T11:51:00+00:00";
  agedMeters.memory_bytes_samples[0].interval_end = "2026-09-12T11:51:00+00:00";
  const parsed = parseTelemetryPresentation(agedMeters);
  assert.ok(parsed);
  const nowMs = Date.parse("2026-09-12T12:05:30+00:00");
  const view = projectWorkspaceTelemetryView(parsed, { nowMs });
  assert.equal(view.isStale, true);
  // Sample label must reflect meter times, not the newer envelope observed_at.
  assert.equal(view.sampleTime, "2026-09-12T11:51:00+00:00");
  assert.notEqual(view.sampleTime, agedMeters.observed_at);
  assert.equal(view.sampleTimeMixed, true);
  assert.equal(view.cpu.sampleTime, "2026-09-12T11:50:00+00:00");
  assert.equal(view.memory.sampleTime, "2026-09-12T11:51:00+00:00");
});

test("differing but fresh CPU/memory sample times mark sampleTimeMixed", () => {
  // Both meters are within stale_after; times differ so a single Sample label
  // must not attribute the older reading to the newer timestamp alone.
  const mixed = structuredClone(SUCCESS);
  mixed.observed_at = "2026-09-12T12:00:00+00:00";
  mixed.cpu_cores_samples[0].sample_time = "2026-09-12T11:58:00+00:00";
  mixed.cpu_cores_samples[0].interval_start = "2026-09-12T11:58:00+00:00";
  mixed.cpu_cores_samples[0].interval_end = "2026-09-12T11:58:00+00:00";
  mixed.memory_bytes_samples[0].sample_time = "2026-09-12T12:00:00+00:00";
  mixed.memory_bytes_samples[0].interval_start = "2026-09-12T12:00:00+00:00";
  mixed.memory_bytes_samples[0].interval_end = "2026-09-12T12:00:00+00:00";
  const parsed = parseTelemetryPresentation(mixed);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, {
    nowMs: Date.parse("2026-09-12T12:01:00+00:00"),
  });
  assert.equal(view.isStale, false);
  assert.equal(view.sampleTimeMixed, true);
  assert.equal(view.cpu.sampleTime, "2026-09-12T11:58:00+00:00");
  assert.equal(view.memory.sampleTime, "2026-09-12T12:00:00+00:00");
  // Newest remains the panel reference time; UI must qualify as mixed.
  assert.equal(view.sampleTime, "2026-09-12T12:00:00+00:00");
});

test("aligned CPU/memory sample times are not sampleTimeMixed", () => {
  const parsed = parseTelemetryPresentation(SUCCESS);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.sampleTimeMixed, false);
  assert.equal(view.cpu.sampleTime, "2026-09-12T12:00:00+00:00");
  assert.equal(view.memory.sampleTime, "2026-09-12T12:00:00+00:00");
});

test("fresh envelope and meters with aged admitted observed_at is stale", () => {
  const agedAdmitted = structuredClone(SUCCESS);
  // Envelope + meters are within stale_after; allocation snapshot is not.
  agedAdmitted.observed_at = "2026-09-12T12:05:00+00:00";
  agedAdmitted.cpu_cores_samples[0].sample_time = "2026-09-12T12:05:00+00:00";
  agedAdmitted.cpu_cores_samples[0].interval_start = "2026-09-12T12:05:00+00:00";
  agedAdmitted.cpu_cores_samples[0].interval_end = "2026-09-12T12:05:00+00:00";
  agedAdmitted.memory_bytes_samples[0].sample_time = "2026-09-12T12:05:00+00:00";
  agedAdmitted.memory_bytes_samples[0].interval_start = "2026-09-12T12:05:00+00:00";
  agedAdmitted.memory_bytes_samples[0].interval_end = "2026-09-12T12:05:00+00:00";
  agedAdmitted.admitted.observed_at = "2026-09-12T11:50:00+00:00";
  const parsed = parseTelemetryPresentation(agedAdmitted);
  assert.ok(parsed);
  const nowMs = Date.parse("2026-09-12T12:05:30+00:00");
  const view = projectWorkspaceTelemetryView(parsed, { nowMs });
  assert.equal(view.isStale, true);
});

test("freshness ignores aged evidence from unsupported widget sections", () => {
  const nowMs = Date.parse("2026-09-12T12:05:30+00:00");

  // Allocation-only: aged meters must not stale a current admitted snapshot.
  const agedMeters = structuredClone(SUCCESS);
  agedMeters.observed_at = "2026-09-12T11:50:00+00:00";
  agedMeters.window_end_at = "2026-09-12T11:50:00+00:00";
  agedMeters.admitted.observed_at = "2026-09-12T12:05:00+00:00";
  agedMeters.cpu_cores_samples[0].sample_time = "2026-09-12T11:50:00+00:00";
  agedMeters.cpu_cores_samples[0].interval_start = "2026-09-12T11:50:00+00:00";
  agedMeters.cpu_cores_samples[0].interval_end = "2026-09-12T11:50:00+00:00";
  agedMeters.memory_bytes_samples[0].sample_time = "2026-09-12T11:50:00+00:00";
  agedMeters.memory_bytes_samples[0].interval_start = "2026-09-12T11:50:00+00:00";
  agedMeters.memory_bytes_samples[0].interval_end = "2026-09-12T11:50:00+00:00";
  const allocParsed = parseTelemetryPresentation(agedMeters);
  assert.ok(allocParsed);
  const allocOnly = projectWorkspaceTelemetryView(allocParsed, {
    nowMs,
    expectTelemetry: false,
    expectAllocation: true,
    expectCost: false,
  });
  assert.equal(allocOnly.isStale, false);
  assert.equal(allocOnly.hasFutureTimestamp, false);
  // Same payload with telemetry expected still ages from meters.
  assert.equal(
    projectWorkspaceTelemetryView(allocParsed, {
      nowMs,
      expectTelemetry: true,
      expectAllocation: true,
    }).isStale,
    true,
  );
  // Clock-tick path must honor the same gates.
  assert.deepEqual(
    projectWorkspaceTelemetryFreshness(allocParsed, allocOnly, nowMs, {
      expectTelemetry: false,
      expectAllocation: true,
    }),
    { isStale: false, hasFutureTimestamp: false },
  );

  // Telemetry-only: aged admitted must not stale current meters/envelope.
  const agedAdmitted = structuredClone(SUCCESS);
  agedAdmitted.observed_at = "2026-09-12T12:05:00+00:00";
  agedAdmitted.cpu_cores_samples[0].sample_time = "2026-09-12T12:05:00+00:00";
  agedAdmitted.cpu_cores_samples[0].interval_start = "2026-09-12T12:05:00+00:00";
  agedAdmitted.cpu_cores_samples[0].interval_end = "2026-09-12T12:05:00+00:00";
  agedAdmitted.memory_bytes_samples[0].sample_time = "2026-09-12T12:05:00+00:00";
  agedAdmitted.memory_bytes_samples[0].interval_start = "2026-09-12T12:05:00+00:00";
  agedAdmitted.memory_bytes_samples[0].interval_end = "2026-09-12T12:05:00+00:00";
  agedAdmitted.admitted.observed_at = "2026-09-12T11:50:00+00:00";
  const telemParsed = parseTelemetryPresentation(agedAdmitted);
  assert.ok(telemParsed);
  const telemOnly = projectWorkspaceTelemetryView(telemParsed, {
    nowMs,
    expectTelemetry: true,
    expectAllocation: false,
    expectCost: false,
  });
  assert.equal(telemOnly.isStale, false);
  assert.equal(telemOnly.hasFutureTimestamp, false);
  assert.equal(
    projectWorkspaceTelemetryView(telemParsed, {
      nowMs,
      expectTelemetry: true,
      expectAllocation: true,
    }).isStale,
    true,
  );
  assert.deepEqual(
    projectWorkspaceTelemetryFreshness(telemParsed, telemOnly, nowMs, {
      expectTelemetry: true,
      expectAllocation: false,
    }),
    { isStale: false, hasFutureTimestamp: false },
  );
});

test("allocation-only ignores producer envelope stale from aged hidden meters", () => {
  // persisted_stale_series: admitted.observed_at is current, but state/quality
  // are producer-stale because the CPU series aged. Filtering meter times alone
  // cannot fix allocation-only freshness; computeIsStale must be section-aware
  // (honorProducerEnvelopeStale=false) so the global stale envelope is ignored.
  const raw = loadFixture("persisted_stale_series");
  const parsed = parseTelemetryPresentation(raw);
  assert.ok(parsed);
  assert.equal(parsed.state, "stale");
  assert.equal(parsed.quality, "stale");
  assert.equal(parsed.admitted?.observedAt, "2026-09-12T12:00:00+00:00");
  const nowMs = Date.parse("2026-09-12T12:01:00+00:00");

  const allocOnly = projectWorkspaceTelemetryView(parsed, {
    nowMs,
    expectTelemetry: false,
    expectAllocation: true,
    expectCost: false,
  });
  assert.equal(allocOnly.isStale, false);
  assert.equal(allocOnly.hasFutureTimestamp, false);
  assert.equal(allocOnly.state, "success");
  assert.equal(allocOnly.quality, "ok");
  // Freshness projection keeps raw producer stale state/quality on the
  // presentation slice; only the section-aware gate must clear isStale.
  assert.equal(parsed.state, "stale");
  assert.equal(parsed.quality, "stale");
  assert.deepEqual(
    projectWorkspaceTelemetryFreshness(parsed, allocOnly, nowMs, {
      expectTelemetry: false,
      expectAllocation: true,
    }),
    { isStale: false, hasFutureTimestamp: false },
  );

  // Telemetry expected: producer envelope stale must still win.
  const full = projectWorkspaceTelemetryView(parsed, {
    nowMs,
    expectTelemetry: true,
    expectAllocation: true,
  });
  assert.equal(full.isStale, true);
  assert.equal(full.state, "stale");
  assert.equal(full.quality, "stale");
});

test("implausibly future meter sample times fail closed as stale", () => {
  // A future sample_time wins latestTimestamp over legitimate readings; without
  // a closed freshness check, nowMs - ms is negative so isStale stays false
  // indefinitely (malformed producer timestamp or collector clock skew).
  // Keep timestamps inside the observed_at-anchored 1h window so parse accepts
  // it; only the freshness check should fail closed on the future reading.
  const future = structuredClone(SUCCESS);
  future.cpu_cores_samples = [
    {
      ...SUCCESS.cpu_cores_samples[0],
      container_name: "agent",
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T12:00:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
      value: "0.10",
    },
    {
      ...SUCCESS.cpu_cores_samples[0],
      container_name: "sidecar",
      sample_time: "2026-09-12T12:30:00+00:00",
      interval_start: "2026-09-12T12:30:00+00:00",
      interval_end: "2026-09-12T12:30:00+00:00",
      value: "0.90",
    },
  ];
  future.memory_bytes_samples = [
    {
      ...SUCCESS.memory_bytes_samples[0],
      sample_time: "2026-09-12T12:00:00+00:00",
      interval_start: "2026-09-12T12:00:00+00:00",
      interval_end: "2026-09-12T12:00:00+00:00",
    },
  ];
  // Envelope must end the selected window at/after the newest meter time.
  future.observed_at = "2026-09-12T12:30:00+00:00";
  const parsed = parseTelemetryPresentation(future);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, {
    nowMs: Date.parse("2026-09-12T12:01:00+00:00"),
  });
  assert.equal(view.isStale, true);
  assert.equal(view.sampleTime, "2026-09-12T12:30:00+00:00");
});

test("view enum accepts only 1h/6h/24h", () => {
  for (const view of ["1h", "6h", "24h"]) {
    assert.ok(parseTelemetryPresentation({ ...SUCCESS, view }));
  }
  for (const view of ["", "12h", "1H", "all"]) {
    assert.equal(parseTelemetryPresentation({ ...SUCCESS, view }), null);
  }
});

test("decimal-string CPU/estimate vs numeric admitted memory bytes are preserved", () => {
  const parsed = parseTelemetryPresentation(SUCCESS);
  assert.ok(parsed);
  assert.equal(typeof parsed.admitted?.cpuRequestCores, "number");
  assert.equal(typeof parsed.admitted?.memoryLimitBytes, "number");
  assert.equal(typeof parsed.estimate.estimatedUsd, "number");
  assert.equal(parsed.cpuSamples[0].value, 0.25);
  assert.equal(parsed.memorySamples[0].value, 1073741824);
});

test("unknown CPU/memory limits stay null rather than zero", () => {
  const unknown = structuredClone(SUCCESS);
  unknown.admitted.cpu_limit_cores = null;
  unknown.admitted.memory_limit_bytes = null;
  const parsed = parseTelemetryPresentation(unknown);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.admitted?.cpuLimitCores, null);
  assert.equal(view.admitted?.memoryLimitBytes, null);
  assert.notEqual(view.admitted?.cpuLimitCores, 0);
  assert.notEqual(view.admitted?.memoryLimitBytes, 0);
});

test("parseTelemetryPresentation rejects admitted requests that exceed their limits", () => {
  const cpuOver = structuredClone(SUCCESS);
  cpuOver.admitted.cpu_request_cores = "2";
  cpuOver.admitted.cpu_limit_cores = "1";
  assert.equal(parseTelemetryPresentation(cpuOver), null, "cpu request > limit");

  const memOver = structuredClone(SUCCESS);
  memOver.admitted.memory_request_bytes = 4294967296;
  memOver.admitted.memory_limit_bytes = 2147483648;
  assert.equal(parseTelemetryPresentation(memOver), null, "memory request > limit");

  const ephemeralOver = structuredClone(SUCCESS);
  ephemeralOver.admitted.ephemeral_storage_request_bytes = 2147483648;
  ephemeralOver.admitted.ephemeral_storage_limit_bytes = 1073741824;
  assert.equal(
    parseTelemetryPresentation(ephemeralOver),
    null,
    "ephemeral request > limit",
  );

  // Equal request/limit remains valid; null limit (unbounded) skips the check.
  const equalCpu = structuredClone(SUCCESS);
  equalCpu.admitted.cpu_request_cores = "1";
  equalCpu.admitted.cpu_limit_cores = "1";
  assert.ok(parseTelemetryPresentation(equalCpu), "cpu request == limit ok");

  const nullLimit = structuredClone(SUCCESS);
  nullLimit.admitted.cpu_limit_cores = null;
  nullLimit.admitted.memory_limit_bytes = null;
  nullLimit.admitted.ephemeral_storage_limit_bytes = null;
  assert.ok(parseTelemetryPresentation(nullLimit), "null limits skip invariant");
});

/** Unique RFC3339 instants packed into the SUCCESS fixture's 1h view window. */
function sampleTimestampForIndex(i) {
  // End at the fixture meter time; 2048 samples at 1s spacing span 2047s < 1h.
  const endMs = Date.parse("2026-09-12T12:00:00+00:00");
  const ms = endMs - (MAX_TELEMETRY_SAMPLES - 1 - i) * 1000;
  return new Date(ms).toISOString().replace(".000Z", "+00:00");
}

test("parseTelemetryPresentation accepts exactly MAX_DATA_QUALITY_NOTES", () => {
  const atCap = structuredClone(SUCCESS);
  atCap.data_quality_notes = Array.from(
    { length: MAX_DATA_QUALITY_NOTES },
    (_, i) => `note_${i}`,
  );
  assert.ok(parseTelemetryPresentation(atCap));
});

test("parseTelemetryPresentation rejects data_quality_notes larger than MAX_DATA_QUALITY_NOTES before scanning", () => {
  const oversized = structuredClone(SUCCESS);
  // Length gate must fail closed without iterating every element.
  oversized.data_quality_notes = Array.from(
    { length: MAX_DATA_QUALITY_NOTES + 1 },
    (_, i) => `note_${i}`,
  );
  assert.equal(parseTelemetryPresentation(oversized), null);
});

test("parseSampleArray accepts exactly MAX_TELEMETRY_SAMPLES", () => {
  const atCap = structuredClone(SUCCESS);
  const template = SUCCESS.cpu_cores_samples[0];
  atCap.cpu_cores_samples = Array.from({ length: MAX_TELEMETRY_SAMPLES }, (_, i) => {
    const ts = sampleTimestampForIndex(i);
    return {
      ...template,
      sample_time: ts,
      interval_start: ts,
      interval_end: ts,
      value: String(i),
    };
  });
  const parsed = parseTelemetryPresentation(atCap);
  assert.ok(parsed);
  assert.equal(parsed.cpuSamples.length, MAX_TELEMETRY_SAMPLES);
  assert.equal(parsed.cpuSamples[0].value, 0);
  assert.equal(parsed.cpuSamples[parsed.cpuSamples.length - 1].value, MAX_TELEMETRY_SAMPLES - 1);
});

test("parseSampleArray rejects arrays larger than MAX_TELEMETRY_SAMPLES before retaining", () => {
  const oversized = structuredClone(SUCCESS);
  const template = SUCCESS.cpu_cores_samples[0];
  // Length gate must fail closed without parsing/sorting the full input.
  oversized.cpu_cores_samples = Array.from(
    { length: MAX_TELEMETRY_SAMPLES + 1 },
    (_, i) => {
      const ts = sampleTimestampForIndex(i);
      return {
        ...template,
        sample_time: ts,
        interval_start: ts,
        interval_end: ts,
        value: String(i),
      };
    },
  );
  assert.equal(parseTelemetryPresentation(oversized), null);

  const oversizedMem = structuredClone(SUCCESS);
  const memTemplate = SUCCESS.memory_bytes_samples[0];
  oversizedMem.memory_bytes_samples = Array.from(
    { length: MAX_TELEMETRY_SAMPLES + 1 },
    (_, i) => {
      const ts = sampleTimestampForIndex(i);
      return {
        ...memTemplate,
        sample_time: ts,
        interval_start: ts,
        interval_end: ts,
        value: String(1024 + i),
      };
    },
  );
  assert.equal(parseTelemetryPresentation(oversizedMem), null);
});
test("downsampleSeriesForSparkline preserves endpoints and bounds length", () => {
  assert.deepEqual(downsampleSeriesForSparkline([]), []);
  assert.deepEqual(downsampleSeriesForSparkline([1, 2, 3]), [1, 2, 3]);
  assert.deepEqual(downsampleSeriesForSparkline([1, 2, 3], 0), []);
  assert.deepEqual(downsampleSeriesForSparkline([1, 2, 3], 1), [3]);

  const long = Array.from({ length: MAX_SPARKLINE_POINTS * 4 }, (_, i) => i);
  const down = downsampleSeriesForSparkline(long);
  assert.ok(down.length <= MAX_SPARKLINE_POINTS);
  assert.equal(down[0], 0);
  assert.equal(down[down.length - 1], long.length - 1);
  assert.equal(new Set(down).size, down.length);
});

test("buildSparklineGeometry retains value extrema when even downsample would drop them", () => {
  // Even sampler for n=65 / max=64 skips index 32 (Math.round(32*64/63)=33).
  const n = MAX_SPARKLINE_POINTS + 1;
  const spikeIdx = 32;
  const last = n - 1;
  const evenSelected = new Set();
  for (let i = 0; i < MAX_SPARKLINE_POINTS; i++) {
    evenSelected.add(Math.round((i * last) / (MAX_SPARKLINE_POINTS - 1)));
  }
  assert.ok(!evenSelected.has(spikeIdx), "precondition: even downsample skips spike index");

  const points = Array.from({ length: n }, (_, i) => ({
    value: i === spikeIdx ? 100 : 1,
  }));
  const naive = downsampleSeriesForSparkline(points);
  assert.ok(
    naive.every((p) => p.value === 1),
    "precondition: naive even downsample drops the sole peak",
  );

  const geom = buildSparklineGeometry(points, 120, 28);
  assert.ok(geom);
  assert.equal(geom.qualification, null);
  assert.ok(
    countSparklineSvgPoints(geom) <= MAX_SPARKLINE_POINTS,
    "extrema retention must stay within the sparkline point cap",
  );
  // Peak at series max maps to y=2.0; without the peak every point is y=26.0 (flat).
  assert.match(
    geom.paths.map((p) => p.d).join(" "),
    /,2\.0\b/,
    "sole interior spike must remain visible after downsampling",
  );
});

test("buildSparklineGeometry retains extrema when non-ok samples saturate the point budget", () => {
  // n=65 all-partial: 63 interior non-ok exceed the 62 interior slots, so the
  // non-ok fill can consume the entire budget before extrema reservation.
  // Even downsample of the 63 interior indices (max 62) skips series index 32.
  const n = MAX_SPARKLINE_POINTS + 1;
  const spikeIdx = 32;
  const nonOkInterior = Array.from({ length: n - 2 }, (_, i) => i + 1);
  const nonOkSelected = new Set(
    downsampleSeriesForSparkline(nonOkInterior, MAX_SPARKLINE_POINTS - 2),
  );
  assert.ok(
    !nonOkSelected.has(spikeIdx),
    "precondition: non-ok interior downsample skips spike index",
  );

  const points = Array.from({ length: n }, (_, i) => ({
    value: i === spikeIdx ? 100 : 1,
    quality: "partial",
  }));

  const geom = buildSparklineGeometry(points, 120, 28);
  assert.ok(geom);
  assert.equal(geom.qualification, "partial");
  assert.ok(
    countSparklineSvgPoints(geom) <= MAX_SPARKLINE_POINTS,
    "extrema retention must stay within the sparkline point cap",
  );
  // Peak at series max maps to y=2.0; without it the dashed path is flat at y=26.0.
  assert.match(
    geom.paths.map((p) => p.d).join(" "),
    /,2\.0\b/,
    "sole interior spike must remain visible when non-ok fill would saturate the budget",
  );
});

test("buildSparklineGeometry draws a marker for one sample and a path for two+", () => {
  assert.equal(buildSparklineGeometry([]), null);

  const single = buildSparklineGeometry([{ value: 0.25 }]);
  assert.ok(single);
  assert.equal(single.paths.length, 0);
  assert.equal(single.qualification, null);
  assert.deepEqual(single.markers, [{ x: 60, y: 14, quality: "ok" }]);

  const multi = buildSparklineGeometry([{ value: 1 }, { value: 3 }, { value: 2 }]);
  assert.ok(multi);
  assert.equal(multi.markers.length, 0);
  assert.equal(multi.qualification, null);
  assert.equal(multi.paths.length, 1);
  assert.equal(multi.paths[0].quality, "ok");
  assert.match(multi.paths[0].d, /^M /);
  assert.match(multi.paths[0].d, / L /);
});

test("buildSparklineGeometry gaps and qualifies non-ok historical points", () => {
  const geom = buildSparklineGeometry([
    { value: 1, quality: "ok" },
    { value: 2, quality: "ok" },
    { value: 3, quality: "partial" },
    { value: 4, quality: "stale" },
    { value: 5, quality: "ok" },
    { value: 6, quality: "ok" },
  ]);
  assert.ok(geom);
  // Two ok runs (2 pts each) + one partial marker + one stale marker — no cross-quality joins.
  assert.equal(geom.paths.length, 2);
  assert.ok(geom.paths.every((p) => p.quality === "ok"));
  assert.deepEqual(
    geom.markers.map((m) => m.quality),
    ["partial", "stale"],
  );
  assert.equal(geom.qualification, "stale");

  const partialOnly = buildSparklineGeometry([
    { value: 1, quality: "partial" },
    { value: 2, quality: "partial" },
  ]);
  assert.ok(partialOnly);
  assert.equal(partialOnly.paths.length, 1);
  assert.equal(partialOnly.paths[0].quality, "partial");
  assert.equal(partialOnly.markers.length, 0);
  assert.equal(partialOnly.qualification, "partial");
});

test("buildSparklineGeometry keeps qualification when downsample would drop non-ok", () => {
  const last = MAX_SPARKLINE_POINTS * 4 - 1;
  const selected = new Set();
  for (let i = 0; i < MAX_SPARKLINE_POINTS; i++) {
    selected.add(Math.round((i * last) / (MAX_SPARKLINE_POINTS - 1)));
  }
  let dropIdx = -1;
  for (let i = 1; i < last; i++) {
    if (!selected.has(i)) {
      dropIdx = i;
      break;
    }
  }
  assert.ok(dropIdx > 0, "need an index even downsample would skip");

  const points = Array.from({ length: last + 1 }, (_, i) => ({
    value: i,
    quality: i === dropIdx ? "partial" : "ok",
  }));

  // Precondition: naive even downsample drops the only non-ok sample.
  const naive = downsampleSeriesForSparkline(points);
  assert.ok(naive.every((p) => (p.quality ?? "ok") === "ok"));

  const geom = buildSparklineGeometry(points);
  assert.ok(geom);
  assert.equal(geom.qualification, "partial");
  assert.ok(
    geom.markers.some((m) => m.quality === "partial") ||
      geom.paths.some((p) => p.quality === "partial"),
    "non-ok sample must remain visible after quality-preserving downsample",
  );
});

test("buildSparklineGeometry preserves sample-grid x when non-ok eviction drops ok anchors", () => {
  const n = MAX_SPARKLINE_POINTS * 2;
  const last = n - 1;
  const width = 120;
  // Two localized partial clusters separated by ok samples. Non-ok count alone
  // exceeds the SVG budget so quality-preserving downsample must drop ok grid
  // anchors between the clusters.
  const c1Start = 8;
  const c1End = 40;
  const c2Start = 88;
  const c2End = 120;
  const nonOkCount = c1End - c1Start + 1 + (c2End - c2Start + 1);
  assert.ok(nonOkCount > MAX_SPARKLINE_POINTS, "non-ok must force ok eviction");

  const points = Array.from({ length: n }, (_, i) => ({
    value: 1,
    quality: (i >= c1Start && i <= c1End) || (i >= c2Start && i <= c2End) ? "partial" : "ok",
  }));

  const geom = buildSparklineGeometry(points, width, 28);
  assert.ok(geom);
  assert.equal(geom.qualification, "partial");

  const partialPaths = geom.paths.filter((p) => p.quality === "partial");
  assert.equal(
    partialPaths.length,
    2,
    "gapped non-ok clusters must stay separate paths after ok-anchor eviction",
  );

  function pathXs(d) {
    return [...d.matchAll(/(\d+(?:\.\d+)?),/g)].map((m) => Number(m[1]));
  }

  const xs1 = pathXs(partialPaths[0].d);
  const xs2 = pathXs(partialPaths[1].d);
  assert.ok(xs1.length >= 2);
  assert.ok(xs2.length >= 2);

  const expected = (idx) => (idx / last) * width;
  assert.ok(Math.min(...xs1) >= expected(c1Start) - 0.1);
  assert.ok(Math.max(...xs1) <= expected(c1End) + 0.1);
  assert.ok(Math.min(...xs2) >= expected(c2Start) - 0.1);
  assert.ok(Math.max(...xs2) <= expected(c2End) + 0.1);
  // Localized clusters must not stretch across the full sparkline width.
  assert.ok(Math.max(...xs1) - Math.min(...xs1) < width * 0.4);
  assert.ok(Math.min(...xs2) > Math.max(...xs1) + width * 0.2);
});

function countSparklineSvgPoints(geom) {
  let n = geom.markers.length;
  for (const path of geom.paths) {
    n += path.d.split(" L ").length;
  }
  return n;
}

test("buildSparklineGeometry caps SVG points when non-ok series exceeds max", () => {
  const n = MAX_SPARKLINE_POINTS * 32; // 2048 accepted samples
  const allPartial = Array.from({ length: n }, (_, i) => ({
    value: i,
    quality: "partial",
  }));
  const partialGeom = buildSparklineGeometry(allPartial);
  assert.ok(partialGeom);
  assert.equal(partialGeom.qualification, "partial");
  assert.ok(
    countSparklineSvgPoints(partialGeom) <= MAX_SPARKLINE_POINTS,
    "same-quality non-ok series must stay within the sparkline point cap",
  );

  const alternating = Array.from({ length: n }, (_, i) => ({
    value: i,
    quality: i % 2 === 0 ? "partial" : "stale",
  }));
  const altGeom = buildSparklineGeometry(alternating);
  assert.ok(altGeom);
  assert.equal(altGeom.qualification, "stale");
  assert.ok(
    countSparklineSvgPoints(altGeom) <= MAX_SPARKLINE_POINTS,
    "alternating non-ok series must not emit unbounded SVG markers",
  );
});

test("formatCores preserves sub-centicore readings as millicores", () => {
  assert.equal(formatCores(0.004), "4 millicores");
  assert.equal(formatCores(0.0045), "4.5 millicores");
  assert.equal(formatCores(0.01), "0.01 cores");
  assert.equal(formatCores(0.25), "0.25 cores");
  assert.equal(formatCores(1), "1 cores");
  assert.equal(formatCores(0), "0 cores");
  assert.equal(formatCores(null), "—");
  assert.equal(formatCores(undefined), "—");
  assert.equal(formatCores(Number.NaN), "—");
});

test("formatCores preserves millicore readings below the toFixed(2) floor", () => {
  // 0.000001 cores → 0.001 millicores; toFixed(2) would collapse to "0 millicores".
  assert.equal(formatCores(0.000001), "0.001 millicores");
  assert.equal(formatCores(0.0000004), "0.0004 millicores");
  assert.equal(formatCores(-0.000001), "-0.001 millicores");
});

test("formatCores does not throw for decimals below the toFixed fixed-point range", () => {
  // fractionDigits = ceil(-log10(abs))+1 exceeds 100 for abs ≲ 1e-102;
  // uncapped toFixed throws RangeError and crashes the resource meter.
  assert.doesNotThrow(() => formatCores(1e-106));
  const text = formatCores(1e-106);
  assert.match(text, /millicores$/);
  assert.ok(!/^0(\.0+)? millicores$/.test(text), `nonzero sample must not collapse: ${text}`);
  assert.doesNotThrow(() => formatCores(-1e-106));
});

for (const source of ["envelope", "allocation", "cpu", "memory"]) {
  test(`projection preserves sub-millisecond clock skew for ${source}`, () => {
    for (const timestamp of [
      "2026-09-12T12:01:00.000001Z",
      "2026-09-12T14:01:00.000001000+02:00",
      "2026-09-12T12:01:00.000000Z",
    ]) {
      const raw = structuredClone(SUCCESS);
      raw.view = "1h";
      if (source === "envelope") raw.observed_at = timestamp;
      const parsed = parseTelemetryPresentation(raw);
      assert.ok(parsed);
      if (source === "allocation") parsed.admitted.observedAt = timestamp;
      if (source === "cpu" || source === "memory") {
        const samples = source === "cpu" ? parsed.cpuSamples : parsed.memorySamples;
        for (const sample of samples) sample.sampleTime = timestamp;
      }
      const future = !timestamp.endsWith(".000000Z");
      for (const nowMs of [FIXED_NOW, FIXED_NOW + 1]) {
        const view = projectWorkspaceTelemetryView(parsed, { nowMs });
        const expected = future && nowMs === FIXED_NOW;
        assert.equal(view.hasFutureTimestamp, expected, `${timestamp} clock-skew flag at ${nowMs}`);
        assert.equal(view.isStale, expected, `${timestamp} freshness at ${nowMs}`);
      }
    }
  });

  test(`projection preserves future-clock-skew signal for ${source}`, () => {
    const parsed = parseTelemetryPresentation(SUCCESS);
    const future = "2026-09-12T12:30:00+00:00";
    if (source === "envelope") parsed.observedAt = future;
    if (source === "allocation") parsed.admitted.observedAt = future;
    if (source === "cpu" || source === "memory") {
      const samples = source === "cpu" ? parsed.cpuSamples : parsed.memorySamples;
      for (const sample of samples) sample.sampleTime = future;
    }
    const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
    assert.equal(view.hasFutureTimestamp, true);
    assert.equal(view.isStale, true);
    // Once the projection clock catches up, only ordinary aging remains.
    const aged = projectWorkspaceTelemetryView(parsed, {
      nowMs: Date.parse(future) + 3600000,
    });
    assert.equal(aged.hasFutureTimestamp, false);
    assert.equal(aged.isStale, true);
  });
}

test("missing telemetry timestamps do not imply clock skew", () => {
  const parsed = parseTelemetryPresentation(UNALLOCATED);
  parsed.observedAt = null;
  assert.equal(projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW }).hasFutureTimestamp, false);
});

test("buildSparklineGeometry positions irregular samples by elapsed time", () => {
  const points = [0, 59, 60].map((minute, i) => ({
    sampleTime: new Date(Date.UTC(2026, 8, 13, 11, minute)).toISOString(),
    value: i,
  }));
  const geom = buildSparklineGeometry(points, 120, 28);
  assert.equal(geom.paths[0].d, "M 0.0,26.0 L 118.0,14.0 L 120.0,2.0");
  const qualified = buildSparklineGeometry(points.map((p, i) => ({
    ...p, quality: i === 1 ? "partial" : "ok",
  })));
  assert.deepEqual(qualified.markers.map((p) => p.x), [0, 118, 120]);
  assert.equal(qualified.paths.length, 0);
});

test("buildSparklineGeometry preserves elapsed positions after downsampling", () => {
  const points = Array.from({ length: 128 }, (_, i) => ({
    sampleTime: new Date(Date.UTC(2026, 8, 13) + i * i * 1000).toISOString(),
    value: i,
  }));
  const geom = buildSparklineGeometry(points, 120, 131);
  const coords = [...geom.paths[0].d.matchAll(/([\d.]+),([\d.]+)/g)];
  assert.ok(coords.length <= MAX_SPARKLINE_POINTS);
  for (const [, x, y] of coords) {
    const originalIndex = 129 - Number(y);
    assert.ok(Math.abs(Number(x) - (originalIndex / 127) ** 2 * 120) <= 0.051);
  }
});

test("buildSparklineGeometry centers coincident timestamps", () => {
  const geom = buildSparklineGeometry([1, 2].map((value) => ({
    sampleTime: "2026-09-13T12:00:00Z", value,
  })));
  assert.equal(geom.paths[0].d, "M 60.0,26.0 L 60.0,2.0");
});

// Paired contract probes: mutations of verbatim canonical evidence, NOT exports
// from the PostgreSQL query. See fixture PROVENANCE.md for the pending sync.
test("paired missing records stay not recorded and allocated partial", () => {
  for (const scenario of ["cold", "checkpoint", "active", "retained"]) {
    const raw = structuredClone(SUCCESS);
    if (scenario !== "retained") raw.estimate = null;
    if (scenario === "cold" || scenario === "retained") raw.admitted = null;
    if (scenario !== "active") {
      raw.cpu_cores_samples = [];
      raw.memory_bytes_samples = [];
      raw.observed_at = null;
    }
    const parsed = parseTelemetryPresentation(raw);
    assert.ok(parsed, scenario);
    const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
    assert.equal(view.state, "partial", scenario);
    assert.equal(view.quality, "partial", scenario);
    if (scenario !== "retained") {
      assert.equal(parsed.estimate, null);
      assert.equal(view.estimate.displayState, "not_recorded");
      for (const field of ["estimatedUsd", "pricedIntervalSeconds", "unpricedIntervalSeconds",
        "rateTableVersion", "rateSource"]) assert.equal(view.estimate[field], null, field);
    } else {
      assert.equal(view.estimate.estimatedUsd, Number(SUCCESS.estimate.estimated_usd));
    }
    assert.equal(view.cpu.usedCores, scenario === "active" ? 0.25 : null);
  }
  const stale = structuredClone(STALE);
  stale.admitted = null;
  stale.estimate = null;
  const view = projectWorkspaceTelemetryView(parseTelemetryPresentation(stale), { nowMs: FIXED_NOW });
  assert.equal(view.state, "stale");
  assert.equal(view.quality, "stale");
  assert.equal(view.isStale, true);
});

test("paired nullable and unknown compute class preserves resources without inventing a class", () => {
  for (const compute_class of [null, "custom-future-class"]) {
    const parsed = parseTelemetryPresentation({
      ...SUCCESS, admitted: { ...SUCCESS.admitted, compute_class },
    });
    assert.ok(parsed);
    const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
    assert.equal(view.admitted.computeClass, null);
    assert.equal(view.admitted.cpuRequestCores, 0.5);
    assert.equal(view.state, "partial");
    assert.equal(view.admitted.partial, true);
  }
  for (const compute_class of [false, {}, 42, "", " ", "x".repeat(65)]) {
    assert.equal(parseTelemetryPresentation({
      ...SUCCESS, admitted: { ...SUCCESS.admitted, compute_class },
    }), null);
  }
});

test("paired recorded unpriced is distinct from missing and unallocated", () => {
  const raw = structuredClone(SUCCESS);
  Object.assign(raw.estimate, {
    estimate_state: "unpriced", estimated_usd: null, priced_interval_seconds: 0,
    unpriced_interval_seconds: 3600,
  });
  const parsed = parseTelemetryPresentation(raw);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.estimate.displayState, "unpriced");
  assert.equal(view.estimate.unpricedIntervalSeconds, 3600);
  assert.equal(view.state, "partial");
  for (const patch of [{ estimated_usd: "0" }, { priced_interval_seconds: 1 }]) {
    assert.equal(parseTelemetryPresentation({ ...raw, estimate: { ...raw.estimate, ...patch } }), null);
  }
  for (const estimate of [undefined, false, {}, 0, "missing"]) {
    assert.equal(parseTelemetryPresentation({ ...SUCCESS, estimate }), null);
  }
  assert.equal(parseTelemetryPresentation({ ...UNALLOCATED, estimate: null }), null);
});

test("inferred qualification ignores gated-off allocation and cost evidence", () => {
  // Complete meters with null admitted/estimate: full-panel still partial, but
  // telemetry-only (allocation/cost unsupported) must keep success/ok.
  const raw = structuredClone(SUCCESS);
  raw.admitted = null;
  raw.estimate = null;
  const parsed = parseTelemetryPresentation(raw);
  assert.ok(parsed);
  const full = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(full.state, "partial");
  assert.equal(full.quality, "partial");
  const telemetryOnly = projectWorkspaceTelemetryView(parsed, {
    nowMs: FIXED_NOW,
    expectAllocation: false,
    expectCost: false,
  });
  assert.equal(telemetryOnly.state, "success");
  assert.equal(telemetryOnly.quality, "ok");
  assert.equal(telemetryOnly.cpu.usedCores, 0.25);
  assert.equal(telemetryOnly.admitted, null);
  assert.equal(telemetryOnly.estimate.displayState, "not_recorded");

  // Allocation expected alone still downgrades on null admitted.
  const allocOnly = projectWorkspaceTelemetryView(parsed, {
    nowMs: FIXED_NOW,
    expectAllocation: true,
    expectCost: false,
  });
  assert.equal(allocOnly.state, "partial");
  assert.equal(allocOnly.quality, "partial");

  // Cost expected alone still downgrades on null/unpriced estimate.
  const costOnly = projectWorkspaceTelemetryView(parsed, {
    nowMs: FIXED_NOW,
    expectAllocation: false,
    expectCost: true,
  });
  assert.equal(costOnly.state, "partial");
  assert.equal(costOnly.quality, "partial");

  const unpriced = structuredClone(SUCCESS);
  Object.assign(unpriced.estimate, {
    estimate_state: "unpriced", estimated_usd: null, priced_interval_seconds: 0,
    unpriced_interval_seconds: 3600,
  });
  const unpricedParsed = parseTelemetryPresentation(unpriced);
  assert.ok(unpricedParsed);
  assert.equal(
    projectWorkspaceTelemetryView(unpricedParsed, {
      nowMs: FIXED_NOW,
      expectAllocation: true,
      expectCost: false,
    }).state,
    "success",
  );
  assert.equal(
    projectWorkspaceTelemetryView(unpricedParsed, {
      nowMs: FIXED_NOW,
      expectAllocation: false,
      expectCost: true,
    }).state,
    "partial",
  );

  // Partial estimate_state is also cost incompleteness: complete meters with a
  // producer envelope partial driven only by a partial estimate must clear when
  // cost is unsupported (same as null/unpriced), and stay partial when cost is
  // expected.
  const partialEstimate = structuredClone(SUCCESS);
  partialEstimate.state = "partial";
  partialEstimate.quality = "partial";
  Object.assign(partialEstimate.estimate, {
    estimate_state: "partial",
    estimated_usd: "0.0040000",
    priced_interval_seconds: 1800,
    unpriced_interval_seconds: 600,
  });
  const partialEstimateParsed = parseTelemetryPresentation(partialEstimate);
  assert.ok(partialEstimateParsed);
  assert.equal(partialEstimateParsed.estimate?.estimateState, "partial");
  const partialEstimateCostOff = projectWorkspaceTelemetryView(partialEstimateParsed, {
    nowMs: FIXED_NOW,
    expectCost: false,
  });
  assert.equal(partialEstimateCostOff.state, "success");
  assert.equal(partialEstimateCostOff.quality, "ok");
  assert.equal(
    projectWorkspaceTelemetryView(partialEstimateParsed, {
      nowMs: FIXED_NOW,
      expectAllocation: false,
      expectCost: false,
    }).state,
    "success",
  );
  assert.equal(
    projectWorkspaceTelemetryView(partialEstimateParsed, {
      nowMs: FIXED_NOW,
      expectAllocation: false,
      expectCost: true,
    }).state,
    "partial",
  );

  // Producer partial caused only by gated-off null allocation/cost must clear
  // for the visible sections (same outcome as inferred success→ok path).
  const producerCostPartial = structuredClone(SUCCESS);
  producerCostPartial.state = "partial";
  producerCostPartial.quality = "partial";
  producerCostPartial.admitted = null;
  producerCostPartial.estimate = null;
  const cleared = parseTelemetryPresentation(producerCostPartial);
  assert.ok(cleared);
  const clearedView = projectWorkspaceTelemetryView(cleared, {
    nowMs: FIXED_NOW,
    expectAllocation: false,
    expectCost: false,
  });
  assert.equal(clearedView.state, "success");
  assert.equal(clearedView.quality, "ok");

  // True envelope-only producer partial (complete nested evidence) is preserved
  // even when allocation/cost widgets are unsupported.
  const envelopeOnly = structuredClone(SUCCESS);
  envelopeOnly.state = "partial";
  envelopeOnly.quality = "partial";
  const envelopeMarked = parseTelemetryPresentation(envelopeOnly);
  assert.ok(envelopeMarked);
  const envelopeView = projectWorkspaceTelemetryView(envelopeMarked, {
    nowMs: FIXED_NOW,
    expectAllocation: false,
    expectCost: false,
  });
  assert.equal(envelopeView.state, "partial");
  assert.equal(envelopeView.quality, "partial");

  // Real producer fixture arrives already partial solely from missing_estimate;
  // cost-unsupported views must not retain that hidden-section qualification.
  const noEstimate = loadFixture("persisted_active_no_estimate");
  const noEstimateParsed = parseTelemetryPresentation(noEstimate);
  assert.ok(noEstimateParsed);
  assert.equal(noEstimateParsed.state, "partial");
  assert.equal(noEstimateParsed.quality, "partial");
  assert.equal(noEstimateParsed.estimate, null);
  assert.equal(noEstimateParsed.admitted?.partial, false);
  const noEstimateNow = Date.parse(noEstimate.window_end_at);
  const costOff = projectWorkspaceTelemetryView(noEstimateParsed, {
    nowMs: noEstimateNow,
    expectCost: false,
  });
  assert.equal(costOff.state, "success");
  assert.equal(costOff.quality, "ok");
  assert.equal(
    projectWorkspaceTelemetryView(noEstimateParsed, {
      nowMs: noEstimateNow,
      expectAllocation: false,
      expectCost: false,
    }).state,
    "success",
  );
  assert.equal(
    projectWorkspaceTelemetryView(noEstimateParsed, { nowMs: noEstimateNow }).state,
    "partial",
  );

});

test("empty expected meters keep producer partial under gated-off allocation/cost", () => {
  // Regression for PRRT_kwDOSJAM6s6iFLfj: gated-off null/unpriced sections must
  // not clear the envelope when telemetry is expected but samples are absent
  // (usedPartial stays false on empty series).
  for (const name of ["persisted_cold_absent", "persisted_checkpoint_no_samples"]) {
    const emptyMeters = loadFixture(name);
    const emptyParsed = parseTelemetryPresentation(emptyMeters);
    assert.ok(emptyParsed);
    assert.equal(emptyParsed.state, "partial");
    assert.equal(emptyParsed.quality, "partial");
    assert.equal(emptyParsed.cpuSamples.length, 0);
    assert.equal(emptyParsed.memorySamples.length, 0);
    const emptyNow = Date.parse(emptyMeters.window_end_at);
    const telemetryOnlyEmpty = projectWorkspaceTelemetryView(emptyParsed, {
      nowMs: emptyNow,
      expectAllocation: false,
      expectCost: false,
    });
    assert.equal(telemetryOnlyEmpty.state, "partial");
    assert.equal(telemetryOnlyEmpty.quality, "partial");
    assert.equal(telemetryOnlyEmpty.cpu.usedCores, null);
    assert.equal(telemetryOnlyEmpty.cpu.usedPartial, false);
    assert.equal(telemetryOnlyEmpty.memory.usedBytes, null);
    assert.equal(telemetryOnlyEmpty.memory.usedPartial, false);
    // Cost unsupported alone still must not promote empty meters to success/ok.
    const costOffEmpty = projectWorkspaceTelemetryView(emptyParsed, {
      nowMs: emptyNow,
      expectCost: false,
    });
    assert.equal(costOffEmpty.state, "partial");
    assert.equal(costOffEmpty.quality, "partial");
  }
});

test("success envelope with empty enabled meters projects partial", () => {
  // Regression for PRRT_kwDOSJAM6s6iF4lq: success/ok with complete allocation
  // and cost but absent meter series must not present dashes as a complete
  // reading when telemetry is expected.
  for (const empty of ["both", "cpu", "memory"]) {
    const raw = structuredClone(SUCCESS);
    if (empty === "both" || empty === "cpu") raw.cpu_cores_samples = [];
    if (empty === "both" || empty === "memory") raw.memory_bytes_samples = [];
    const parsed = parseTelemetryPresentation(raw);
    assert.ok(parsed, empty);
    assert.equal(parsed.state, "success", empty);
    assert.equal(parsed.quality, "ok", empty);
    const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
    assert.equal(view.state, "partial", empty);
    assert.equal(view.quality, "partial", empty);
    if (empty === "both" || empty === "cpu") {
      assert.equal(view.cpu.usedCores, null, empty);
      assert.equal(view.cpu.series.length, 0, empty);
    }
    if (empty === "both" || empty === "memory") {
      assert.equal(view.memory.usedBytes, null, empty);
      assert.equal(view.memory.series.length, 0, empty);
    }
  }

  // Telemetry-unsupported views must not infer partial from hidden empty meters.
  const bothEmpty = structuredClone(SUCCESS);
  bothEmpty.cpu_cores_samples = [];
  bothEmpty.memory_bytes_samples = [];
  const telemetryOff = projectWorkspaceTelemetryView(
    parseTelemetryPresentation(bothEmpty),
    { nowMs: FIXED_NOW, expectTelemetry: false },
  );
  assert.equal(telemetryOff.state, "success");
  assert.equal(telemetryOff.quality, "ok");
});

test("paired memory gauges normalize only internal starts and keep complete container partitions", () => {
  const raw = structuredClone(SUCCESS);
  for (const family of ["cpu_cores_samples", "memory_bytes_samples"]) {
    raw[family].push({ ...raw[family][0], container_name: "sidecar" });
  }
  raw.memory_bytes_samples[0].interval_start = null;
  const parsed = parseTelemetryPresentation(raw);
  assert.ok(parsed);
  assert.equal(parsed.memorySamples[0].intervalStart, raw.memory_bytes_samples[0].sample_time);
  assert.equal(raw.memory_bytes_samples[0].interval_start, null);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.memory.usedBytes, 2147483648);
  assert.equal(view.memory.usedPartial, false);
  raw.memory_bytes_samples[1].interval_start = null;
  assert.ok(parseTelemetryPresentation(raw));
  for (const bad of [undefined, false, "", "invalid"]) {
    raw.memory_bytes_samples[0].interval_start = bad;
    assert.equal(parseTelemetryPresentation(raw), null);
  }
  raw.memory_bytes_samples[0].interval_start = null;
  raw.memory_bytes_samples[0].interval_end = "2026-09-12T12:00:01Z";
  raw.window_end_at = "2026-09-12T12:00:01Z";
  assert.equal(parseTelemetryPresentation(raw), null);
  for (const bad of [null, undefined, "invalid", "2026-09-12T12:00:01Z"]) {
    const cpu = structuredClone(SUCCESS);
    cpu.cpu_cores_samples[0].interval_start = bad;
    assert.equal(parseTelemetryPresentation(cpu), null);
  }
});

test("paired chart clock allows CPU lag without refreshing oldest-series freshness", () => {
  const raw = structuredClone(SUCCESS);
  raw.window_end_at = raw.observed_at;
  raw.observed_at = "2026-09-12T11:59:00Z";
  Object.assign(raw.cpu_cores_samples[0], {
    sample_time: raw.observed_at, interval_end: raw.observed_at,
    interval_start: "2026-09-12T11:58:00Z",
  });
  raw.memory_bytes_samples[0].interval_start = null;
  const parsed = parseTelemetryPresentation(raw);
  assert.ok(parsed);
  assert.equal(parsed.windowEndAt, raw.window_end_at);
  const fresh = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(fresh.sampleTimeMixed, true);
  assert.equal(fresh.observedAt, raw.observed_at);
  assert.equal(fresh.isStale, false);
  const aged = projectWorkspaceTelemetryView(parsed, { nowMs: Date.parse("2026-09-12T12:04:01Z") });
  assert.equal(aged.isStale, true);
  delete raw.window_end_at;
  assert.equal(parseTelemetryPresentation(raw), null, "legacy still anchors on observed_at");
});

test("paired explicit window bounds preserve precision, CPU start exception and legacy fallback", () => {
  for (const [view, hours] of [["1h", 1], ["6h", 6], ["24h", 24]]) {
    const raw = structuredClone(SUCCESS);
    raw.view = view;
    raw.window_end_at = "2026-09-12T12:00:00.000001Z";
    const left = new Date(Date.parse("2026-09-12T12:00:00Z") - hours * 3600000).toISOString().replace(".000Z", ".000001Z");
    Object.assign(raw.cpu_cores_samples[0], {
      sample_time: left, interval_end: left,
      interval_start: new Date(Date.parse(left) - 60000).toISOString(),
    });
    assert.ok(parseTelemetryPresentation(raw), view);
    raw.cpu_cores_samples[0].sample_time = left.replace("000001Z", "000000Z");
    assert.equal(parseTelemetryPresentation(raw), null, "sub-ms before left");
    raw.cpu_cores_samples[0].sample_time = left;
    raw.cpu_cores_samples[0].interval_end = "2026-09-12T12:00:00.000002Z";
    assert.equal(parseTelemetryPresentation(raw), null, "interval end after right");
  }
  for (const window_end_at of [null, undefined, false, 12, "bad",
    "2026-09-12T12:00:00." + "0".repeat(65) + "Z",
    "2026-09-12T11:59:59.999999Z"]) {
    assert.equal(parseTelemetryPresentation({ ...SUCCESS, window_end_at }), null);
  }
  assert.ok(parseTelemetryPresentation({ ...SUCCESS, window_end_at: "2026-09-12T14:00:00+02:00" }));
  const raw = { ...SUCCESS, window_end_at: "2026-09-12T12:02:00Z" };
  const view = projectWorkspaceTelemetryView(parseTelemetryPresentation(raw), { nowMs: FIXED_NOW });
  assert.equal(view.hasFutureTimestamp, true);
  assert.equal(view.isStale, true);
  assert.ok(parseTelemetryPresentation({ ...raw, observed_at: null }));
  assert.equal(parseTelemetryPresentation({ ...SUCCESS, observed_at: null }), null);
  assert.ok(parseTelemetryPresentation({
    ...raw, observed_at: "2026-09-10T12:00:00Z",
  }), "old freshness may predate chart");
});

test("paired allocation-attempt estimates retain lifetime amounts under every chart selector", () => {
  for (const view of ["1h", "6h", "24h"]) {
    for (const duration of [3600, 7200]) {
      const raw = structuredClone(SUCCESS);
      raw.view = view;
      raw.estimate_scope = "resource_attempt";
      raw.estimate.priced_interval_seconds = duration;
      raw.admitted = null; // retained cleaned terminal summary
      const parsed = parseTelemetryPresentation(raw);
      assert.ok(parsed);
      const projection = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
      assert.equal(projection.estimate.scope, "resource_attempt");
      assert.equal(projection.estimate.pricedIntervalSeconds, duration);
      assert.equal(projection.estimate.estimatedUsd, Number(raw.estimate.estimated_usd));
    }
  }
  for (const estimate_scope of ["view", undefined]) {
    const raw = structuredClone(SUCCESS);
    if (estimate_scope) raw.estimate_scope = estimate_scope;
    assert.equal(parseTelemetryPresentation(raw).estimateScope, "view");
    raw.estimate.priced_interval_seconds = 7200;
    assert.equal(parseTelemetryPresentation(raw), null);
  }
  for (const estimate_scope of [null, undefined, false, "workspace", ""]) {
    assert.equal(parseTelemetryPresentation({ ...SUCCESS, estimate_scope }), null);
  }
  for (const scope of ["view", "resource_attempt"]) {
    for (const patch of [
      { priced_interval_seconds: Infinity }, { priced_interval_seconds: 1e15 + 1 },
      { priced_interval_seconds: 1.5 }, { unpriced_interval_seconds: -1 },
      { currency: "EUR" }, { estimated_usd: "NaN" }, { estimated_usd: null },
    ]) {
      assert.equal(parseTelemetryPresentation({
        ...SUCCESS, estimate_scope: scope, estimate: { ...SUCCESS.estimate, ...patch },
      }), null);
    }
  }
});

test("paired family cap accepts whole-container full-range partitions and rejects 2049/2880", () => {
  const raw = structuredClone(SUCCESS);
  raw.view = "24h";
  raw.window_end_at = raw.observed_at;
  for (const family of ["cpu_cores_samples", "memory_bytes_samples"]) {
    const base = raw[family][0];
    raw[family] = Array.from({ length: 1024 }, (_, index) => {
      const time = new Date(Date.parse(raw.observed_at) - Math.round((1023 - index) * 86400000 / 1023)).toISOString();
      return ["agent", "sidecar"].map(container_name => ({
        ...base, container_name, sample_time: time, interval_start: family === "memory_bytes_samples" ? null : time,
        interval_end: time,
      }));
    }).flat();
  }
  const parsed = parseTelemetryPresentation(raw);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.cpu.series.length, 1024);
  assert.equal(view.cpu.series[0].sampleTime, "2026-09-11T12:00:00.000Z");
  assert.equal(view.cpu.series.at(-1).sampleTime, "2026-09-12T12:00:00.000Z");
  assert.equal(view.cpu.usedCores, 0.5);
  assert.equal(view.memory.usedBytes, 2147483648);
  for (const family of ["cpu_cores_samples", "memory_bytes_samples"]) {
    for (const count of [2049, 2880]) {
      assert.equal(parseTelemetryPresentation({
        ...raw, [family]: Array.from({ length: count }, (_, i) => raw[family][i % 2048]),
      }), null);
    }
  }
  raw.memory_bytes_samples.pop();
  const incomplete = projectWorkspaceTelemetryView(parseTelemetryPresentation(raw), { nowMs: FIXED_NOW });
  assert.equal(incomplete.memory.usedBytes, null);
  assert.equal(incomplete.memory.usedPartial, true);
  raw.memory_bytes_samples = [];
  assert.equal(projectWorkspaceTelemetryView(parseTelemetryPresentation(raw), { nowMs: FIXED_NOW }).memory.usedBytes, null);
});

test("paired nullable evidence retains resource identity and malformed non-null guards", () => {
  const raw = { ...structuredClone(SUCCESS), admitted: null, estimate: null };
  raw.cpu_cores_samples[0].provider_resource_uid = "other-resource";
  assert.equal(parseTelemetryPresentation(raw), null);
  raw.cpu_cores_samples[0].provider_resource_uid = SUCCESS.ownership.provider_resource_uid;
  assert.ok(parseTelemetryPresentation(raw));
  for (const admitted of [false, 0, "", {}, undefined]) {
    assert.equal(parseTelemetryPresentation({ ...raw, admitted }), null);
  }
  for (const field of ["cpu_request_cores", "memory_request_bytes", "observed_at", "billable"]) {
    assert.equal(parseTelemetryPresentation({
      ...raw, admitted: { ...SUCCESS.admitted, [field]: "malformed" },
    }), null, field);
  }
});

test("paired attempt duration sum stays exact at existing scalar limits", () => {
  const raw = {
    ...structuredClone(PARTIAL), estimate_scope: "resource_attempt",
    estimate: {
      ...PARTIAL.estimate, priced_interval_seconds: 1e15, unpriced_interval_seconds: 1e15,
    },
  };
  const parsed = parseTelemetryPresentation(raw);
  assert.ok(parsed);
  assert.equal(parsed.estimate.pricedIntervalSeconds + parsed.estimate.unpricedIntervalSeconds, 2e15);
  assert.equal(Number.isSafeInteger(parsed.estimate.pricedIntervalSeconds + parsed.estimate.unpricedIntervalSeconds), true);
});

test("PR643 persisted unpriced null/absent source remains unknown", () => {
  for (const omitted of [false, true]) {
    const raw = loadFixture("persisted_unpriced_allocation");
    if (omitted) delete raw.estimate.evidence.source;
    const parsed = parseTelemetryPresentation(raw);
    assert.ok(parsed);
    const projected = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
    assert.equal(projected.estimate.displayState, "unpriced");
    assert.equal(projected.estimate.estimatedUsd, null);
    assert.equal(projected.estimate.rateSource, null);
    assert.equal(projected.estimate.pricedIntervalSeconds, 0);
    assert.equal(projected.estimate.unpricedIntervalSeconds, 3600);
  }
});

test("PR643 unpriced allowance preserves malformed provenance and pricing guards", () => {
  for (const source of ["", " ", 1, false, [], {}, "x".repeat(65)]) {
    const raw = loadFixture("persisted_unpriced_allocation");
    raw.estimate.evidence.source = source;
    assert.equal(parseTelemetryPresentation(raw), null);
  }
  for (const mutate of [
    r => { r.estimate.evidence.rate_table_version = "conflicting"; },
    r => { r.ownership.provider_resource_uid = "other"; },
    r => { r.estimate.estimated_usd = "0"; },
    r => { r.estimate.priced_interval_seconds = 1; },
    r => { r.estimate.estimate_state = "complete"; r.estimate.estimated_usd = "1"; r.estimate.priced_interval_seconds = 3600; r.estimate.unpriced_interval_seconds = 0; },
  ]) {
    const raw = loadFixture("persisted_unpriced_allocation");
    mutate(raw);
    assert.equal(parseTelemetryPresentation(raw), null);
  }
});

test("all 13 exact persisted exports project with bounded resource-attempt semantics", () => {
  const names = ["active_no_estimate", "checkpoint_no_samples", "cold_absent", "cpu_lagging_memory", "day_two_containers", "estimate_no_checkpoint", "missing_family", "retained_cleaned_terminal", "shared_unallocated", "stale_series", "terminal_1h", "terminal_2h_under_1h", "unpriced_allocation"];
  for (const name of names) {
    const raw = loadFixture(`persisted_${name}`);
    const parsed = parseTelemetryPresentation(raw);
    assert.ok(parsed, name);
    const view = projectWorkspaceTelemetryView(parsed, { nowMs: Date.parse(raw.window_end_at) });
    assert.equal(view.estimate.scope, "resource_attempt", name);
    assert.equal(view.estimate.estimatedUsd, raw.estimate?.estimated_usd == null ? null : Number(raw.estimate.estimated_usd));
    assert.equal(view.admitted === null, raw.admitted === null);
    if (name === "day_two_containers") {
      assert.equal(raw.cpu_cores_samples.length, 2048);
      assert.equal(raw.memory_bytes_samples.length, 2048);
      assert.equal(new Set(raw.cpu_cores_samples.map(s => s.container_name)).size, 2);
      assert.ok(view.cpu.series.length > 1);
      assert.equal(view.view, "24h");
    }
    if (name === "stale_series") assert.equal(view.isStale, true);
  }
});

test("persisted exports match unchanged producer SHA-256 provenance", async () => {
  const { createHash } = await import("node:crypto");
  const provenance = loadFixture("persisted_provenance");
  assert.equal(Object.keys(provenance.sha256).length, 13);
  for (const [name, digest] of Object.entries(provenance.sha256)) {
    assert.equal(createHash("sha256").update(readFileSync(join(FIXTURE_DIR, name))).digest("hex"), digest, name);
  }
});

test("freshness ticks use projected meter metadata without reading sample arrays", () => {
  for (const raw of [SUCCESS, PARTIAL, STALE, UNALLOCATED]) {
    const parsed = parseTelemetryPresentation(raw);
    const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
    const times = [FIXED_NOW - 120_000, FIXED_NOW, FIXED_NOW + 300_001];
    const expected = times.map(nowMs => {
      const projected = projectWorkspaceTelemetryView(parsed, { nowMs });
      return { isStale: projected.isStale, hasFutureTimestamp: projected.hasFutureTimestamp };
    });
    for (const key of ["cpuSamples", "memorySamples"]) {
      Object.defineProperty(parsed, key, { get() { assert.fail("freshness must not read raw series"); } });
    }
    for (const [index, nowMs] of times.entries()) {
      assert.deepEqual(projectWorkspaceTelemetryFreshness(parsed, view, nowMs), expected[index]);
    }
  }
});


test("cached readings cross future and stale boundaries without a new response", () => {
  const parsed = parseTelemetryPresentation(SUCCESS);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  const sampledAt = Date.parse(view.cpu.sampleTime);
  for (const [offset, isStale, hasFutureTimestamp] of [
    [-1, true, true],
    [0, false, false],
    [300_000, false, false],
    [300_001, true, false],
  ]) {
    assert.deepEqual(projectWorkspaceTelemetryFreshness(parsed, view, sampledAt + offset), {
      isStale, hasFutureTimestamp,
    });
  }
});
