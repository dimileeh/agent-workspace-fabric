import assert from "node:assert/strict";
import test from "node:test";

import { fallbackLifecycleStages, fallbackLlmUsage } from "./format.ts";

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
