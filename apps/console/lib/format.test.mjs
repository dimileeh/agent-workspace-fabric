import assert from "node:assert/strict";
import test from "node:test";

import {
  capacityUtilizationPct,
  fallbackLifecycleStages,
  fallbackLlmUsage,
  formatCostWithPricing,
  formatUsageProvenance,
  pickWorkspaceLogStreams,
  renderLogEntries,
  toneFillClass,
} from "./format.ts";

function dim(reserved, limit) {
  return { limit, reserved, available: null, available_after_next_default: null, reason_code: null };
}

function saturationFixture(overrides = {}) {
  return {
    allocated_capacity: {
      steady_cpu: dim(0, 8),
      peak_cpu: dim(0, 8),
      steady_memory_gb: dim(0, 16),
      peak_memory_gb: dim(0, 16),
      disk_mb: dim(0, 10240),
      dind_slots: dim(0, 2),
      pressure_reasons: [],
      ...(overrides.allocated_capacity ?? {}),
    },
    capacity: {
      pressure_reasons: [],
      ...(overrides.capacity ?? {}),
    },
    concurrency: {
      provision: { limit: 2, in_use: 0, queued: 0, available: 2 },
      execution: { limit: 2, in_use: 0, queued: 0, available: 2 },
      ...(overrides.concurrency ?? {}),
    },
    disk: { ok: true, ...(overrides.disk ?? {}) },
    admission: { ok: true, ...(overrides.admission ?? {}) },
  };
}

test("fallbackLifecycleStages marks terminal successors skipped", () => {
  const stages = Object.fromEntries(
    fallbackLifecycleStages("failed", "validating").map((stage) => [stage.stage, stage.status]),
  );

  assert.equal(stages.requested, "completed");
  assert.equal(stages.provisioning, "completed");
  assert.equal(stages.ready, "completed");
  assert.equal(stages.running, "completed");
  assert.equal(stages.validating, "completed");
  assert.equal(stages.pushing, "terminal_skipped");
  assert.equal(stages.monitoring_pr, "terminal_skipped");
  assert.equal(stages.completed, "terminal_skipped");
});

test("fallbackLifecycleStages skips all stages for unknown terminal source", () => {
  const stages = Object.fromEntries(
    fallbackLifecycleStages("failed", "unknown_legacy_stage").map((stage) => [
      stage.stage,
      stage.status,
    ]),
  );

  assert.equal(stages.requested, "terminal_skipped");
  assert.equal(stages.provisioning, "terminal_skipped");
  assert.equal(stages.completed, "terminal_skipped");
});

test("fallbackLifecycleStages preserves active non-terminal status", () => {
  const stages = Object.fromEntries(
    fallbackLifecycleStages("validating").map((stage) => [stage.stage, stage.status]),
  );

  assert.equal(stages.running, "completed");
  assert.equal(stages.validating, "active");
  assert.equal(stages.pushing, "pending");
});

test("formatUsageProvenance maps ccusage source and reason codes to friendly labels", () => {
  assert.equal(formatUsageProvenance("ccusage", null), "ccusage");
  assert.equal(formatUsageProvenance("ccusage", "ccusage_no_records"), "ccusage / no usage recorded yet");
  assert.equal(formatUsageProvenance("ccusage", "ccusage_timeout"), "ccusage / ccusage timed out");
  assert.equal(formatUsageProvenance("operations", null), "operations");
  assert.equal(formatUsageProvenance("none", "usage_not_reported"), "none / not reported");
});

test("formatUsageProvenance maps pricing cost-reason codes to friendly labels", () => {
  assert.equal(
    formatUsageProvenance("ccusage", "pricing_not_configured"),
    "ccusage / pricing not configured",
  );
  assert.equal(formatUsageProvenance("ccusage", "pricing_stale"), "ccusage / pricing stale");
  assert.equal(
    formatUsageProvenance("ccusage", "pricing_rates_unavailable"),
    "ccusage / pricing rates unavailable",
  );
  assert.equal(formatUsageProvenance("ccusage", "no_token_data"), "ccusage / no token data");
  assert.equal(
    formatUsageProvenance("ccusage", "negative_token_count"),
    "ccusage / invalid token count",
  );
  assert.equal(
    formatUsageProvenance("ccusage", "unsupported_pricing_unit"),
    "ccusage / unsupported pricing unit",
  );
});

test("formatUsageProvenance passes through unknown source and reason", () => {
  assert.equal(formatUsageProvenance("custom_src", "weird_reason"), "custom_src / weird_reason");
  assert.equal(formatUsageProvenance(undefined, undefined), "none");
});

test("fallbackLlmUsage protects legacy workspace payloads", () => {
  assert.deepEqual(fallbackLlmUsage(undefined), {
    input_tokens: null,
    cached_input_tokens: null,
    output_tokens: null,
    reasoning_output_tokens: null,
    total_tokens: null,
    cost_estimate: null,
    currency: null,
    status: "unavailable",
    source: "none",
    reason: "usage_not_reported",
  });
});

test("fallbackLlmUsage preserves available provider usage", () => {
  assert.deepEqual(
    fallbackLlmUsage({
      input_tokens: 10,
      cached_input_tokens: 3,
      output_tokens: 5,
      reasoning_output_tokens: 2,
      total_tokens: 15,
      cost_estimate: 0.12,
      currency: "USD",
      status: "available",
      source: "provider",
      reason: null,
    }),
    {
      input_tokens: 10,
      cached_input_tokens: 3,
      output_tokens: 5,
      reasoning_output_tokens: 2,
      total_tokens: 15,
      cost_estimate: 0.12,
      currency: "USD",
      status: "available",
      source: "provider",
      reason: null,
    },
  );
});

test("formatCostWithPricing renders computed usage cost without pricing metadata", () => {
  assert.equal(formatCostWithPricing(0.05, "USD", null), "$0.0500");
});

test("formatCostWithPricing appends stale-pricing suffix when pricing is stale and source is not ccusage", () => {
  const stalePricing = {
    provider: "openai",
    model: "gpt-4o",
    currency: "USD",
    unit: "per_1m_tokens",
    price_per_unit: 5.0,
    timestamp: "2025-01-01T00:00:00Z",
    version: 1,
    is_current: false,
  };
  assert.equal(
    formatCostWithPricing(0.12, "USD", stalePricing),
    "$0.1200 (stale pricing)",
  );
  assert.equal(
    formatCostWithPricing(0.12, "USD", stalePricing, "operations"),
    "$0.1200 (stale pricing)",
  );
  assert.equal(
    formatCostWithPricing(0.12, "USD", stalePricing, null),
    "$0.1200 (stale pricing)",
  );
});

test("formatCostWithPricing suppresses stale-pricing suffix when source is ccusage", () => {
  const stalePricing = {
    provider: "openai",
    model: "gpt-4o",
    currency: "USD",
    unit: "per_1m_tokens",
    price_per_unit: 5.0,
    timestamp: "2025-01-01T00:00:00Z",
    version: 1,
    is_current: false,
  };
  assert.equal(
    formatCostWithPricing(0.12, "USD", stalePricing, "ccusage"),
    "$0.1200",
  );
});

test("toneFillClass maps warning and bad pressure to distinct fills", () => {
  assert.equal(toneFillClass("good"), "tone-fill-good");
  assert.equal(toneFillClass("warn"), "tone-fill-warn");
  assert.equal(toneFillClass("bad"), "tone-fill-bad");
});

test("capacityUtilizationPct takes the worst CPU/memory dimension", () => {
  const saturation = saturationFixture({
    allocated_capacity: {
      steady_cpu: dim(2, 8),
      peak_cpu: dim(6, 8),
      steady_memory_gb: dim(4, 16),
      peak_memory_gb: dim(8, 16),
      disk_mb: dim(0, 10240),
      dind_slots: dim(0, 2),
      pressure_reasons: [],
    },
  });
  assert.equal(capacityUtilizationPct(saturation), 75);
});

test("capacityUtilizationPct flags a saturated DinD-slot constraint while CPU/memory are idle", () => {
  const saturation = saturationFixture({
    allocated_capacity: {
      steady_cpu: dim(0, 8),
      peak_cpu: dim(0, 8),
      steady_memory_gb: dim(0, 16),
      peak_memory_gb: dim(0, 16),
      disk_mb: dim(0, 10240),
      dind_slots: dim(2, 2),
      pressure_reasons: [],
    },
  });
  assert.equal(capacityUtilizationPct(saturation), 100);
});

test("capacityUtilizationPct counts a saturated provision lane", () => {
  const saturation = saturationFixture({
    concurrency: {
      provision: { limit: 2, in_use: 2, queued: 1, available: 0 },
      execution: { limit: 2, in_use: 0, queued: 0, available: 2 },
    },
  });
  assert.equal(capacityUtilizationPct(saturation), 100);
});

test("capacityUtilizationPct falls back to capacity.pressure_reasons when allocated is empty", () => {
  const saturation = saturationFixture({
    allocated_capacity: {
      steady_cpu: dim(1, 8),
      peak_cpu: dim(1, 8),
      steady_memory_gb: dim(0, 16),
      peak_memory_gb: dim(0, 16),
      disk_mb: dim(0, 10240),
      dind_slots: dim(0, 2),
      pressure_reasons: [],
    },
    capacity: { pressure_reasons: ["DIND_CAPACITY_SATURATED"] },
  });
  assert.equal(capacityUtilizationPct(saturation), 100);
});

test("capacityUtilizationPct treats blocked disk/admission as exhausted", () => {
  assert.equal(capacityUtilizationPct(saturationFixture({ disk: { ok: false } })), 100);
  assert.equal(capacityUtilizationPct(saturationFixture({ admission: { ok: false } })), 100);
});

test("capacityUtilizationPct returns null when no limit is comparable", () => {
  const saturation = saturationFixture({
    allocated_capacity: {
      steady_cpu: dim(0, 0),
      peak_cpu: dim(0, null),
      steady_memory_gb: dim(0, 0),
      peak_memory_gb: dim(0, null),
      disk_mb: dim(0, 0),
      dind_slots: dim(0, 0),
      pressure_reasons: [],
    },
    concurrency: {
      provision: { limit: 0, in_use: 0, queued: 0, available: 0 },
      execution: { limit: 0, in_use: 0, queued: 0, available: 0 },
    },
  });
  assert.equal(capacityUtilizationPct(saturation), null);
});

test("capacityUtilizationPct honors a *_SATURATED pressure reason with no comparable limit", () => {
  const saturation = saturationFixture({
    allocated_capacity: {
      steady_cpu: dim(0, 8),
      peak_cpu: dim(0, 8),
      steady_memory_gb: dim(0, 16),
      peak_memory_gb: dim(0, 16),
      disk_mb: dim(0, 0),
      dind_slots: dim(0, 0),
      pressure_reasons: ["DIND_CAPACITY_SATURATED"],
    },
  });
  assert.equal(capacityUtilizationPct(saturation), 100);
});

test("renderLogEntries preserves message order inside chunks in asc mode", () => {
  const rendered = renderLogEntries(
    [
      {
        streamId: "agent.stdout",
        fd: null,
        data: "first\nsecond\nthird\n",
        occurredAt: "stamp",
      },
    ],
    "asc",
  );

  assert.equal(rendered, "[stamp] agent.stdout\nfirst\nsecond\nthird");
});

test("renderLogEntries reverses message order inside chunks in desc mode", () => {
  const rendered = renderLogEntries(
    [
      {
        streamId: "agent.stdout",
        fd: null,
        data: "first\nsecond\nthird\n",
        occurredAt: "stamp",
      },
    ],
    "desc",
  );

  assert.equal(rendered, "[stamp] agent.stdout\nthird\nsecond\nfirst");
});

test("renderLogEntries reverses large chunks without split-based line materialization", () => {
  const lines = Array.from({ length: 1_000 }, (_, index) => `line ${index}`);
  const originalSplit = String.prototype.split;
  String.prototype.split = function splitGuard(separator, limit) {
    if (separator === "\n" && String(this).startsWith("line 0\nline 1\n")) {
      throw new Error("log reversal should not split large chunks");
    }
    return Reflect.apply(originalSplit, this, [separator, limit]);
  };

  try {
    const rendered = renderLogEntries(
      [
        {
          streamId: "agent.stdout",
          fd: null,
          data: `${lines.join("\n")}\n`,
          occurredAt: "stamp",
        },
      ],
      "desc",
    );

    assert.equal(rendered, `[stamp] agent.stdout\n${lines.toReversed().join("\n")}`);
  } finally {
    String.prototype.split = originalSplit;
  }
});

test("renderLogEntries leaves chunk ordering to the caller", () => {
  const rendered = renderLogEntries(
    [
      {
        streamId: "agent.stdout",
        fd: null,
        data: "older",
        occurredAt: "old",
      },
      {
        streamId: "agent.stdout",
        fd: null,
        data: "newer",
        occurredAt: "new",
      },
    ],
    "desc",
  );

  assert.equal(rendered, "[old] agent.stdout\nolder\n\n[new] agent.stdout\nnewer");
});

test("pickWorkspaceLogStreams defaults to all streams", () => {
  assert.deepEqual(
    pickWorkspaceLogStreams(
      [
        { stream_id: "validation.01_setup.stdout" },
        { stream_id: "agent.stdout" },
        { stream_id: "agent.stderr" },
      ],
      [],
    ),
    ["validation.01_setup.stdout", "agent.stdout", "agent.stderr"],
  );
});

test("pickWorkspaceLogStreams preserves an existing valid stream selection", () => {
  assert.deepEqual(
    pickWorkspaceLogStreams(
      [
        { stream_id: "validation.01_setup.stdout" },
        { stream_id: "agent.stdout" },
        { stream_id: "agent.stderr" },
      ],
      ["agent.stdout", "deleted.stream"],
    ),
    ["agent.stdout"],
  );
});
