import assert from "node:assert/strict";
import test from "node:test";

import {
  fallbackLifecycleStages,
  fallbackLlmUsage,
  formatCostWithPricing,
  pickWorkspaceLogStreams,
  renderLogEntries,
  toneFillClass,
} from "./format.ts";

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

test("fallbackLifecycleStages preserves active non-terminal status", () => {
  const stages = Object.fromEntries(
    fallbackLifecycleStages("validating").map((stage) => [stage.stage, stage.status]),
  );

  assert.equal(stages.running, "completed");
  assert.equal(stages.validating, "active");
  assert.equal(stages.pushing, "pending");
});

test("fallbackLlmUsage protects legacy workspace payloads", () => {
  assert.deepEqual(fallbackLlmUsage(undefined), {
    input_tokens: null,
    output_tokens: null,
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
      output_tokens: 5,
      total_tokens: 15,
      cost_estimate: 0.12,
      currency: "USD",
      status: "available",
      source: "provider",
      reason: null,
    }),
    {
      input_tokens: 10,
      output_tokens: 5,
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

test("toneFillClass maps warning and bad pressure to distinct fills", () => {
  assert.equal(toneFillClass("good"), "bg-emerald-500");
  assert.equal(toneFillClass("warn"), "bg-amber-500");
  assert.equal(toneFillClass("bad"), "bg-red-500");
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
