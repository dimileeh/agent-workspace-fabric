import assert from "node:assert/strict";
import test from "node:test";

import {
  capacityUtilizationPct,
  fallbackLifecycleStages,
  fallbackLlmUsage,
  formatCostWithPricing,
  formatUsageProvenance,
  lifecycleStages,
  normalizeLifecycle,
  pickWorkspaceLogStreams,
  renderLogEntries,
  statusGlyph,
  statusTone,
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

test("fallbackLifecycleStages never marks a skipped optional pause completed on a terminal rail", () => {
  // A workspace that failed from a stage past `recovering`/`blocked` (e.g.
  // monitoring_pr) never necessarily entered those optional pauses. The terminal
  // rail must mirror the non-terminal guard and keep an unobserved pause out of
  // `completed`, otherwise it falsely tells the operator the workspace
  // auto-retried (recovering) or paused for them (blocked).
  const stages = Object.fromEntries(
    fallbackLifecycleStages("failed", "monitoring_pr").map((stage) => [stage.stage, stage.status]),
  );

  assert.notEqual(stages.recovering, "completed");
  assert.notEqual(stages.blocked, "completed");
  assert.equal(stages.recovering, "pending");
  assert.equal(stages.blocked, "pending");
  // Non-pause stages up to the terminal source still read completed.
  assert.equal(stages.running, "completed");
  assert.equal(stages.validating, "completed");
  assert.equal(stages.pushing, "completed");
});

test("fallbackLifecycleStages marks an explicitly-entered terminal pause completed", () => {
  // When the terminal transition came *from* the pause itself, it was observed,
  // so the rail should render it completed rather than pending.
  const stages = Object.fromEntries(
    fallbackLifecycleStages("failed", "blocked").map((stage) => [stage.stage, stage.status]),
  );

  assert.equal(stages.blocked, "completed");
  // A different optional pause that was not the terminal source stays pending.
  assert.equal(stages.recovering, "pending");
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

test("fallbackLifecycleStages does not claim execution stages completed for a blocked workspace", () => {
  // `blocked` can be entered from running/validating/pushing but sits at a fixed
  // position in the linear list; without the real lifecycle the fallback must not
  // falsely render a later execution stage (e.g. pushing) as completed.
  const stages = Object.fromEntries(
    fallbackLifecycleStages("blocked").map((stage) => [stage.stage, stage.status]),
  );

  assert.equal(stages.requested, "completed");
  assert.equal(stages.provisioning, "completed");
  assert.equal(stages.ready, "completed");
  assert.equal(stages.running, "pending");
  assert.equal(stages.validating, "pending");
  assert.equal(stages.pushing, "pending");
  assert.equal(stages.blocked, "active");
  assert.equal(stages.monitoring_pr, "pending");
});

test("statusTone marks a blocked workspace as warn (operator attention)", () => {
  assert.equal(statusTone("blocked"), "warn");
});

test("statusGlyph renders the pause glyph for a blocked workspace", () => {
  assert.equal(statusGlyph("blocked"), "⏸");
});

test("lifecycleStages includes blocked as an in-flight stage before monitoring_pr", () => {
  assert.ok(lifecycleStages.includes("blocked"));
  assert.ok(lifecycleStages.indexOf("blocked") < lifecycleStages.indexOf("monitoring_pr"));
  // blocked stays a non-terminal stage, so it must not appear after completed.
  assert.ok(lifecycleStages.indexOf("blocked") < lifecycleStages.indexOf("completed"));
});

// The backend lifecycle omits `blocked`, so a real blocked workspace arrives with no
// active stage. normalizeLifecycle injects the active pause so the rail surfaces it.
function stage(name, status) {
  return { stage: name, started_at: null, ended_at: null, duration_seconds: null, status };
}

test("normalizeLifecycle injects an active blocked stage for a real blocked workspace", () => {
  // Mirrors the backend for a pause from validating: stages it left are completed,
  // downstream pending, and (since blocked isn't a lifecycle stage) nothing active.
  const backend = [
    stage("requested", "completed"),
    stage("provisioning", "completed"),
    stage("ready", "completed"),
    stage("running", "completed"),
    stage("validating", "completed"),
    stage("pushing", "pending"),
    stage("monitoring_pr", "pending"),
    stage("completed", "pending"),
  ];

  const result = normalizeLifecycle("blocked", backend);
  const rendered = result.map((s) => [s.stage, s.status]);

  // blocked is inserted at its canonical position (after pushing, before monitoring_pr)
  // and is the single active stage; the backend stages are untouched.
  assert.deepEqual(rendered, [
    ["requested", "completed"],
    ["provisioning", "completed"],
    ["ready", "completed"],
    ["running", "completed"],
    ["validating", "completed"],
    ["pushing", "pending"],
    ["blocked", "active"],
    ["monitoring_pr", "pending"],
    ["completed", "pending"],
  ]);
  assert.equal(result.filter((s) => s.status === "active").length, 1);
});

test("normalizeLifecycle keeps a post-PR blocked pause right of a completed monitoring_pr", () => {
  // A `blocked` pause entered from `monitoring_pr` (the monitor-repair pause)
  // arrives with `monitoring_pr` already `completed`. The synthetic active
  // `blocked` must land AFTER the completed monitoring_pr, not at its canonical
  // slot to the left of it — otherwise an active step renders left of a
  // completed one, reversing the real transition order.
  const backend = [
    stage("requested", "completed"),
    stage("provisioning", "completed"),
    stage("ready", "completed"),
    stage("running", "completed"),
    stage("validating", "completed"),
    stage("pushing", "completed"),
    stage("monitoring_pr", "completed"),
    stage("completed", "pending"),
  ];

  const result = normalizeLifecycle("blocked", backend);
  const rendered = result.map((s) => [s.stage, s.status]);

  assert.deepEqual(rendered, [
    ["requested", "completed"],
    ["provisioning", "completed"],
    ["ready", "completed"],
    ["running", "completed"],
    ["validating", "completed"],
    ["pushing", "completed"],
    ["monitoring_pr", "completed"],
    ["blocked", "active"],
    ["completed", "pending"],
  ]);
  // No completed stage may appear to the right of the active pause.
  const activeIndex = result.findIndex((s) => s.status === "active");
  assert.ok(
    result.slice(activeIndex + 1).every((s) => s.status !== "completed"),
    "no completed stage may follow the active pause",
  );
  assert.equal(result.filter((s) => s.status === "active").length, 1);
});

test("normalizeLifecycle is a no-op for a non-blocked workspace", () => {
  const backend = [stage("running", "active"), stage("validating", "pending")];
  assert.equal(normalizeLifecycle("running", backend), backend);
});

test("normalizeLifecycle is idempotent when a blocked stage is already present", () => {
  const backend = [stage("validating", "completed"), stage("blocked", "active")];
  assert.equal(normalizeLifecycle("blocked", backend), backend);
});

test("normalizeLifecycle appends blocked when no later stage is present to anchor it", () => {
  const backend = [stage("requested", "completed"), stage("running", "completed")];
  const rendered = normalizeLifecycle("blocked", backend).map((s) => [s.stage, s.status]);
  assert.deepEqual(rendered, [
    ["requested", "completed"],
    ["running", "completed"],
    ["blocked", "active"],
  ]);
});

test("fallbackLifecycleStages marks running completed for a recovering workspace", () => {
  // `recovering` is contractually entered only from `running` (#612 state
  // machine) and sits immediately after it, so the fallback knows `running` was
  // reached: it marks `running` completed and only later execution stages
  // pending — matching the real-data path (`normalizeLifecycle`).
  const stages = Object.fromEntries(
    fallbackLifecycleStages("recovering").map((stage) => [stage.stage, stage.status]),
  );

  assert.equal(stages.requested, "completed");
  assert.equal(stages.provisioning, "completed");
  assert.equal(stages.ready, "completed");
  assert.equal(stages.running, "completed");
  assert.equal(stages.recovering, "active");
  assert.equal(stages.validating, "pending");
  assert.equal(stages.pushing, "pending");
  assert.equal(stages.monitoring_pr, "pending");
});

test("fallbackLifecycleStages never marks a skipped recovering pause completed", () => {
  // `recovering` is an optional pause: a workspace that went running→validating
  // directly never auto-retried. The fallback must NOT render `recovering` as
  // `completed` for such a workspace (it would falsely claim a provider retry
  // happened) — the backend omits the pause entirely, so the rail keeps it
  // `pending`, matching the real-data path. The execution stage it actually
  // reached (`running`) stays completed and the current stage stays active.
  for (const status of ["validating", "pushing", "monitoring_pr"]) {
    const stages = Object.fromEntries(
      fallbackLifecycleStages(status).map((stage) => [stage.stage, stage.status]),
    );
    assert.equal(stages.running, "completed", `running completed for ${status}`);
    assert.equal(stages.recovering, "pending", `recovering not completed for ${status}`);
    assert.equal(stages[status], "active", `${status} active`);
  }
});

test("fallbackLifecycleStages never marks a skipped blocked pause completed", () => {
  // Same guard for `blocked`: a `monitoring_pr` workspace that never paused for
  // the operator must not show `blocked` as completed on the fallback rail.
  const stages = Object.fromEntries(
    fallbackLifecycleStages("monitoring_pr").map((stage) => [stage.stage, stage.status]),
  );
  assert.equal(stages.blocked, "pending");
  assert.equal(stages.monitoring_pr, "active");
});

test("statusTone marks a recovering workspace as info (auto-heal, no action) not warn", () => {
  // recovering is a benign in-flight auto-retry, distinct from blocked's warn.
  assert.equal(statusTone("recovering"), "info");
  assert.notEqual(statusTone("recovering"), "warn");
});

test("statusGlyph renders a distinct refresh glyph for a recovering workspace", () => {
  assert.equal(statusGlyph("recovering"), "↻");
  // Must not reuse blocked's pause glyph — the two pauses read differently.
  assert.notEqual(statusGlyph("recovering"), statusGlyph("blocked"));
});

test("lifecycleStages includes recovering as an in-flight stage after running, before monitoring_pr", () => {
  assert.ok(lifecycleStages.includes("recovering"));
  assert.ok(lifecycleStages.indexOf("recovering") > lifecycleStages.indexOf("running"));
  assert.ok(lifecycleStages.indexOf("recovering") < lifecycleStages.indexOf("monitoring_pr"));
  // recovering stays non-terminal, so it must not appear after completed.
  assert.ok(lifecycleStages.indexOf("recovering") < lifecycleStages.indexOf("completed"));
});

test("normalizeLifecycle injects an active recovering stage for a real recovering workspace", () => {
  // Mirrors the backend for a pause from running: stages it left are completed,
  // downstream pending, and (since recovering isn't a lifecycle stage) nothing active.
  const backend = [
    stage("requested", "completed"),
    stage("provisioning", "completed"),
    stage("ready", "completed"),
    stage("running", "completed"),
    stage("validating", "pending"),
    stage("pushing", "pending"),
    stage("monitoring_pr", "pending"),
    stage("completed", "pending"),
  ];

  const result = normalizeLifecycle("recovering", backend);
  const rendered = result.map((s) => [s.stage, s.status]);

  // recovering is inserted at its canonical position (after running, before validating)
  // and is the single active stage; the backend stages are untouched.
  assert.deepEqual(rendered, [
    ["requested", "completed"],
    ["provisioning", "completed"],
    ["ready", "completed"],
    ["running", "completed"],
    ["recovering", "active"],
    ["validating", "pending"],
    ["pushing", "pending"],
    ["monitoring_pr", "pending"],
    ["completed", "pending"],
  ]);
  assert.equal(result.filter((s) => s.status === "active").length, 1);
});

test("normalizeLifecycle is idempotent when a recovering stage is already present", () => {
  const backend = [stage("running", "completed"), stage("recovering", "active")];
  assert.equal(normalizeLifecycle("recovering", backend), backend);
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

test("capacityUtilizationPct reflects admission saturated as at-least-warn", () => {
  assert.equal(
    capacityUtilizationPct(saturationFixture({ admission: { ok: true, status: "saturated" } })),
    75,
  );
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
