import assert from "node:assert/strict";
import test from "node:test";

import { fallbackLifecycleStages, fallbackLlmUsage, renderLogEntries } from "./format.ts";

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
