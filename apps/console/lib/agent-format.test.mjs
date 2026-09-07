import assert from "node:assert/strict";
import test from "node:test";

import { formatAgentEffort, formatAgentLabel, formatAgentTitle } from "./agent-format.ts";

test("formatAgentLabel includes compact model and effort", () => {
  assert.equal(
    formatAgentLabel({
      agent: "codex",
      agent_model: "gpt-5.5",
      agent_effort: "xhigh",
    }),
    "codex · gpt-5.5 · xhigh",
  );
});

test("formatAgentLabel compacts ollama models and omits missing effort", () => {
  assert.equal(
    formatAgentLabel({
      agent: "opencode",
      agent_model: "ollama/glm-5.1:cloud",
      agent_effort: null,
    }),
    "opencode · glm-5.1:cloud",
  );
});

test("formatAgentTitle omits default model and effort provenance", () => {
  assert.equal(
    formatAgentTitle({
      agent: "codex",
      agent_model: "gpt-5.5",
      agent_effort: "xhigh",
      agent_model_source: "default",
      agent_effort_source: "default",
    }),
    "codex / gpt-5.5 / effort xhigh",
  );
});

test("formatAgentTitle keeps non-default provenance", () => {
  assert.equal(
    formatAgentTitle({
      agent: "codex",
      agent_model: "gpt-5.5",
      agent_effort: "xhigh",
      agent_model_source: "task_policy",
      agent_effort_source: "unavailable",
    }),
    "codex / gpt-5.5 / effort xhigh / model task_policy / effort unavailable",
  );
});

test("formatAgentTitle omits missing legacy provenance fields", () => {
  assert.equal(
    formatAgentTitle({
      agent: "codex",
      agent_model: "gpt-5.5",
      agent_effort: "xhigh",
    }),
    "codex / gpt-5.5 / effort xhigh",
  );
});

test("formatAgentEffort omits missing legacy provenance fields", () => {
  assert.equal(
    formatAgentEffort({
      agent_effort: "xhigh",
    }),
    "xhigh",
  );
});

test("formatAgentLabel names an explicit Cursor Auto routing mode", () => {
  assert.equal(
    formatAgentLabel({
      agent: "cursor",
      agent_model: "auto-smart[optimize_for=intelligence]",
      agent_effort: null,
      cursor_auto_mode: "intelligence",
    }),
    "cursor · Auto Intelligence",
  );
});

test("formatAgentTitle names an explicit Cursor Auto routing mode", () => {
  assert.equal(
    formatAgentTitle({
      agent: "cursor",
      agent_model: "auto-smart[optimize_for=balanced]",
      agent_effort: null,
      cursor_auto_mode: "balance",
      agent_model_source: "task_policy",
      agent_effort_source: "unavailable",
    }),
    "cursor / Auto Balance / model task_policy / effort unavailable",
  );
});

test("never labels default/task_policy/auto as confirmed execution model", async () => {
  const { formatConfirmedExecutionModel, isConfirmedModelSource } = await import("./agent-format.ts");
  assert.equal(isConfirmedModelSource("default"), false);
  assert.equal(isConfirmedModelSource("task_policy"), false);
  assert.equal(isConfirmedModelSource("auto"), false);
  assert.equal(isConfirmedModelSource("inferred"), false);
  assert.equal(isConfirmedModelSource("configured"), false);
  assert.equal(isConfirmedModelSource("execution_evidence"), true);
  assert.equal(isConfirmedModelSource("adapter_report"), true);
  assert.equal(
    formatConfirmedExecutionModel({
      confirmed_execution_model: "gpt-5.5",
      confirmed_execution_model_source: "task_policy",
    }),
    "not recorded",
  );
  assert.equal(
    formatConfirmedExecutionModel({
      confirmed_execution_model: "gpt-5.5",
      confirmed_execution_model_source: "default",
    }),
    "not recorded",
  );
  assert.equal(
    formatConfirmedExecutionModel({
      confirmed_execution_model: "gpt-5.5-2026-08-07",
      confirmed_execution_model_source: "execution_evidence",
    }),
    "gpt-5.5-2026-08-07 (execution_evidence)",
  );
});

test("mergeWorkspacePresentationFields keeps overview metadata when detail omits fields", async () => {
  const { mergeWorkspacePresentationFields, formatConfirmedExecutionModel, formatRequestedModel } =
    await import("./agent-format.ts");
  const overview = {
    requested_model: "gpt-overview",
    requested_model_source: "task_policy",
    requested_effort: "high",
    confirmed_execution_model: "gpt-confirmed",
    confirmed_execution_model_source: "execution_evidence",
  };
  const sparseDetail = {
    agent_model: "legacy-detail",
  };
  const merged = mergeWorkspacePresentationFields(overview, sparseDetail);
  assert.equal(formatRequestedModel(merged), "gpt-overview (task_policy)");
  assert.equal(formatConfirmedExecutionModel(merged), "gpt-confirmed (execution_evidence)");
  assert.equal(
    formatConfirmedExecutionModel(sparseDetail),
    "not recorded",
    "detail-only sparse object would blank confirmed model without merge",
  );
});

test("resolveWorkflowFinishedAt falls back to finished_at", async () => {
  const { resolveWorkflowFinishedAt } = await import("./agent-format.ts");
  assert.equal(
    resolveWorkflowFinishedAt({
      workflow_finished_at: "2026-09-06T17:00:00Z",
      finished_at: "2026-09-06T16:00:00Z",
    }),
    "2026-09-06T17:00:00Z",
  );
  assert.equal(
    resolveWorkflowFinishedAt({
      workflow_finished_at: null,
      finished_at: "2026-09-06T16:30:00Z",
    }),
    "2026-09-06T16:30:00Z",
  );
  assert.equal(
    resolveWorkflowFinishedAt({
      finished_at: "2026-09-06T16:30:00Z",
    }),
    "2026-09-06T16:30:00Z",
  );
  assert.equal(
    resolveWorkflowFinishedAt({
      workflow_finished_at: null,
      finished_at: null,
    }),
    null,
  );
  assert.equal(resolveWorkflowFinishedAt({}), null);
});
