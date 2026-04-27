import assert from "node:assert/strict";
import test from "node:test";

import { fallbackLifecycleStages } from "./format.ts";

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
