import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  COST_EXCLUSION_NOTE,
  MAX_SPARKLINE_POINTS,
  MAX_STALE_AFTER_SECONDS,
  MAX_TELEMETRY_SAMPLES,
  buildSparklineGeometry,
  downsampleSeriesForSparkline,
  formatCores,
  parseTelemetryPresentation,
  projectWorkspaceTelemetryView,
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
  assert.equal(parseTelemetryPresentation({ ...SUCCESS, estimate: null }), null);
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
  assert.equal(cpuView.isStale, true);

  const staleMem = structuredClone(SUCCESS);
  staleMem.memory_bytes_samples[0].quality = "stale";
  const memParsed = parseTelemetryPresentation(staleMem);
  assert.ok(memParsed);
  const memView = projectWorkspaceTelemetryView(memParsed, { nowMs: FIXED_NOW });
  assert.equal(memView.memory.usedPartial, true);
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

test("parseTelemetryPresentation rejects stale_after_seconds that overflow thresholdMs", () => {
  // Number.MAX_VALUE is finite and integer-ish, but * 1000 => Infinity and
  // would make age-based freshness always false.
  const maxValue = structuredClone(SUCCESS);
  maxValue.stale_after_seconds = Number.MAX_VALUE;
  assert.equal(parseTelemetryPresentation(maxValue), null);

  const aboveCap = structuredClone(SUCCESS);
  aboveCap.stale_after_seconds = MAX_STALE_AFTER_SECONDS + 1;
  assert.equal(parseTelemetryPresentation(aboveCap), null);

  // Operational 7d cap (not MAX_SAFE_INTEGER/1000).
  assert.equal(MAX_STALE_AFTER_SECONDS, 7 * 24 * 60 * 60);

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
  // Memory latest group is independent; do not invent a pod total across times.
  assert.equal(view.memory.usedBytes, 536870912);
  assert.equal(view.memory.usedPartial, false);
});

test("same-timestamp multi-container samples may sum within that timestamp only", () => {
  const multi = structuredClone(SUCCESS);
  multi.cpu_cores_samples = [
    { ...SUCCESS.cpu_cores_samples[0], container_name: "agent", value: "0.10" },
    { ...SUCCESS.cpu_cores_samples[0], container_name: "sidecar", value: "0.15" },
  ];
  const parsed = parseTelemetryPresentation(multi);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, { nowMs: FIXED_NOW });
  assert.equal(view.cpu.usedCores, 0.25);
  assert.equal(view.cpu.usedPartial, false);
  assert.deepEqual(view.cpu.containerNamesAtSample?.sort(), ["agent", "sidecar"]);
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

test("parseTelemetryPresentation rejects cross-metric sample UID mismatch without presentation UID", () => {
  const cross = structuredClone(SUCCESS);
  delete cross.admitted.provider_resource_uid;
  delete cross.ownership.provider_resource_uid;
  cross.cpu_cores_samples[0].provider_resource_uid = "pod-uid-cpu";
  cross.memory_bytes_samples[0].provider_resource_uid = "pod-uid-memory";
  assert.equal(parseTelemetryPresentation(cross), null);

  const aligned = structuredClone(SUCCESS);
  delete aligned.admitted.provider_resource_uid;
  delete aligned.ownership.provider_resource_uid;
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
  agedMeters.memory_bytes_samples[0].sample_time = "2026-09-12T11:51:00+00:00";
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
  mixed.memory_bytes_samples[0].sample_time = "2026-09-12T12:00:00+00:00";
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
  agedAdmitted.memory_bytes_samples[0].sample_time = "2026-09-12T12:05:00+00:00";
  agedAdmitted.admitted.observed_at = "2026-09-12T11:50:00+00:00";
  const parsed = parseTelemetryPresentation(agedAdmitted);
  assert.ok(parsed);
  const nowMs = Date.parse("2026-09-12T12:05:30+00:00");
  const view = projectWorkspaceTelemetryView(parsed, { nowMs });
  assert.equal(view.isStale, true);
});

test("implausibly future meter sample times fail closed as stale", () => {
  // A future sample_time wins latestTimestamp over legitimate readings; without
  // a closed freshness check, nowMs - ms is negative so isStale stays false
  // indefinitely (malformed producer timestamp or collector clock skew).
  const future = structuredClone(SUCCESS);
  future.cpu_cores_samples = [
    {
      ...SUCCESS.cpu_cores_samples[0],
      container_name: "agent",
      sample_time: "2026-09-12T12:00:00+00:00",
      value: "0.10",
    },
    {
      ...SUCCESS.cpu_cores_samples[0],
      container_name: "sidecar",
      sample_time: "2026-09-13T12:00:00+00:00",
      value: "0.90",
    },
  ];
  future.memory_bytes_samples = [
    {
      ...SUCCESS.memory_bytes_samples[0],
      sample_time: "2026-09-12T12:00:00+00:00",
    },
  ];
  future.observed_at = "2026-09-12T12:00:00+00:00";
  const parsed = parseTelemetryPresentation(future);
  assert.ok(parsed);
  const view = projectWorkspaceTelemetryView(parsed, {
    nowMs: Date.parse("2026-09-12T12:01:00+00:00"),
  });
  assert.equal(view.isStale, true);
  assert.equal(view.sampleTime, "2026-09-13T12:00:00+00:00");
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

function sampleTimestampForIndex(i) {
  const day = String(1 + Math.floor(i / 86400)).padStart(2, "0");
  const tod = i % 86400;
  const hour = String(Math.floor(tod / 3600)).padStart(2, "0");
  const minute = String(Math.floor((tod % 3600) / 60)).padStart(2, "0");
  const second = String(tod % 60).padStart(2, "0");
  return `2026-09-${day}T${hour}:${minute}:${second}+00:00`;
}

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
