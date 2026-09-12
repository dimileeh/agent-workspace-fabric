import assert from "node:assert/strict";
import test from "node:test";

import {
  formatAgentEffort,
  formatAgentIdentityLabel,
  formatAgentLabel,
  formatAgentTitle,
} from "./agent-format.ts";

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

test("formatAgentIdentityLabel omits requested effort from the inspector agent identity", () => {
  assert.equal(
    formatAgentIdentityLabel({
      agent: "codex",
      agent_model: "gpt-5.5",
      agent_effort: "xhigh",
    }),
    "codex · gpt-5.5",
  );
  assert.equal(
    formatAgentIdentityLabel({
      agent: "cursor",
      agent_model: "auto-smart[optimize_for=intelligence]",
      agent_effort: "high",
      cursor_auto_mode: "intelligence",
    }),
    "cursor · Auto Intelligence",
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

test("never labels non-confirming provenance as confirmed execution model", async () => {
  const { formatConfirmedExecutionModel, isConfirmedModelSource } = await import("./agent-format.ts");
  assert.equal(isConfirmedModelSource("default"), false);
  assert.equal(isConfirmedModelSource("task_policy"), false);
  assert.equal(isConfirmedModelSource("auto"), false);
  assert.equal(isConfirmedModelSource("inferred"), false);
  assert.equal(isConfirmedModelSource("configured"), false);
  assert.equal(isConfirmedModelSource("unavailable"), false);
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
      confirmed_execution_model: "gpt-stale",
      confirmed_execution_model_source: "unavailable",
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

test("accepts contract-valid confirmed sources outside the former allowlist", async () => {
  const { formatConfirmedExecutionModel, isConfirmedModelSource } = await import("./agent-format.ts");
  assert.equal(isConfirmedModelSource("provider_report"), true);
  assert.equal(isConfirmedModelSource("runtime_evidence"), true);
  assert.equal(isConfirmedModelSource("cursor_cli_usage"), true);
  assert.equal(isConfirmedModelSource(""), false);
  assert.equal(isConfirmedModelSource("   "), false);
  assert.equal(isConfirmedModelSource("AUTO"), false);
  assert.equal(
    formatConfirmedExecutionModel({
      confirmed_execution_model: "gpt-5.5",
      confirmed_execution_model_source: "cursor_cli_usage",
    }),
    "gpt-5.5 (cursor_cli_usage)",
  );
  assert.equal(
    formatConfirmedExecutionModel({
      confirmed_execution_model: "gpt-5.5",
      confirmed_execution_model_source: "auto",
    }),
    "not recorded",
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

test("mergeWorkspacePresentationFields keeps confirmed model and source atomic", async () => {
  const { mergeWorkspacePresentationFields, formatConfirmedExecutionModel } = await import(
    "./agent-format.ts"
  );
  const overview = {
    confirmed_execution_model: "gpt-overview-confirmed",
    confirmed_execution_model_source: "execution_evidence",
  };

  const modelOnlyDetail = mergeWorkspacePresentationFields(overview, {
    confirmed_execution_model: "gpt-detail-confirmed",
  });
  assert.equal(
    formatConfirmedExecutionModel(modelOnlyDetail),
    "gpt-overview-confirmed (execution_evidence)",
    "partial detail must not attach a new model to overview provenance",
  );
  assert.equal(modelOnlyDetail.confirmed_execution_model, "gpt-overview-confirmed");
  assert.equal(modelOnlyDetail.confirmed_execution_model_source, "execution_evidence");

  const sourceOnlyDetail = mergeWorkspacePresentationFields(overview, {
    confirmed_execution_model_source: "adapter_report",
  });
  assert.equal(
    formatConfirmedExecutionModel(sourceOnlyDetail),
    "gpt-overview-confirmed (execution_evidence)",
    "partial detail must not attach a new source to the overview model",
  );
  assert.equal(sourceOnlyDetail.confirmed_execution_model, "gpt-overview-confirmed");
  assert.equal(sourceOnlyDetail.confirmed_execution_model_source, "execution_evidence");

  const completeDetail = mergeWorkspacePresentationFields(overview, {
    confirmed_execution_model: "gpt-detail-confirmed",
    confirmed_execution_model_source: "adapter_report",
  });
  assert.equal(
    formatConfirmedExecutionModel(completeDetail),
    "gpt-detail-confirmed (adapter_report)",
  );
});

test("formatRequestedModel does not attach legacy source to an explicit request", async () => {
  const { formatRequestedModel, formatRequestedEffort } = await import("./agent-format.ts");
  assert.equal(
    formatRequestedModel({
      requested_model: "gpt-explicit",
      agent_model: "gpt-legacy",
      agent_model_source: "task_policy",
    }),
    "gpt-explicit",
    "explicit request without requested_model_source must not inherit agent_model_source",
  );
  assert.equal(
    formatRequestedEffort({
      requested_effort: "xhigh",
      agent_effort: "high",
      agent_effort_source: "task_policy",
    }),
    "xhigh",
    "explicit effort without requested_effort_source must not inherit agent_effort_source",
  );
  assert.equal(
    formatRequestedModel({
      agent_model: "gpt-legacy",
      agent_model_source: "task_policy",
    }),
    "gpt-legacy (task_policy)",
    "legacy value may still use legacy provenance",
  );
  assert.equal(
    formatRequestedEffort({
      agent_effort: "high",
      agent_effort_source: "task_policy",
    }),
    "high (task_policy)",
    "legacy effort may still use legacy provenance",
  );
});

test("mergeWorkspacePresentationFields keeps requested value and source atomic", async () => {
  const {
    mergeWorkspacePresentationFields,
    formatRequestedModel,
    formatRequestedEffort,
  } = await import("./agent-format.ts");
  const overview = {
    requested_model: "gpt-overview",
    requested_model_source: "task_policy",
    requested_effort: "high",
    requested_effort_source: "task_policy",
  };

  const modelOnlyDetail = mergeWorkspacePresentationFields(overview, {
    requested_model: "gpt-detail",
  });
  assert.equal(
    formatRequestedModel(modelOnlyDetail),
    "gpt-detail",
    "detail model without source must not inherit overview provenance",
  );
  assert.equal(modelOnlyDetail.requested_model, "gpt-detail");
  assert.equal(modelOnlyDetail.requested_model_source, undefined);

  const modelSourceOnlyDetail = mergeWorkspacePresentationFields(overview, {
    requested_model_source: "workspace_override",
  });
  assert.equal(
    formatRequestedModel(modelSourceOnlyDetail),
    "gpt-overview (task_policy)",
    "detail source without model must not attach to overview model",
  );
  assert.equal(modelSourceOnlyDetail.requested_model, "gpt-overview");
  assert.equal(modelSourceOnlyDetail.requested_model_source, "task_policy");

  const effortOnlyDetail = mergeWorkspacePresentationFields(overview, {
    requested_effort: "xhigh",
  });
  assert.equal(
    formatRequestedEffort(effortOnlyDetail),
    "xhigh",
    "detail effort without source must not inherit overview provenance",
  );
  assert.equal(effortOnlyDetail.requested_effort, "xhigh");
  assert.equal(effortOnlyDetail.requested_effort_source, undefined);

  const effortSourceOnlyDetail = mergeWorkspacePresentationFields(overview, {
    requested_effort_source: "workspace_override",
  });
  assert.equal(
    formatRequestedEffort(effortSourceOnlyDetail),
    "high (task_policy)",
    "detail effort source without value must not attach to overview effort",
  );
  assert.equal(effortSourceOnlyDetail.requested_effort, "high");
  assert.equal(effortSourceOnlyDetail.requested_effort_source, "task_policy");

  const completeDetail = mergeWorkspacePresentationFields(overview, {
    requested_model: "gpt-detail",
    requested_model_source: "workspace_override",
    requested_effort: "xhigh",
    requested_effort_source: "workspace_override",
  });
  assert.equal(formatRequestedModel(completeDetail), "gpt-detail (workspace_override)");
  assert.equal(formatRequestedEffort(completeDetail), "xhigh (workspace_override)");
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
      workflow_finished_at: "not-a-timestamp",
      finished_at: "2026-09-06T16:30:00Z",
    }),
    "2026-09-06T16:30:00Z",
  );
  assert.equal(
    resolveWorkflowFinishedAt({
      workflow_finished_at: "not-a-timestamp",
      finished_at: "also-not-a-timestamp",
    }),
    null,
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

test("distinctFinishedAt omits finished_at already shown as Workflow finished", async () => {
  const { distinctFinishedAt } = await import("./agent-format.ts");
  assert.equal(
    distinctFinishedAt({
      workflow_finished_at: null,
      finished_at: "2026-09-06T16:30:00Z",
    }),
    null,
    "cloud rows that only send finished_at must not render it twice",
  );
  assert.equal(
    distinctFinishedAt({
      finished_at: "2026-09-06T16:30:00Z",
    }),
    null,
  );
  assert.equal(
    distinctFinishedAt({
      workflow_finished_at: "2026-09-06T17:00:00Z",
      finished_at: "2026-09-06T17:00:00Z",
    }),
    null,
  );
  assert.equal(
    distinctFinishedAt({
      workflow_finished_at: "2026-09-06T17:00:00Z",
      finished_at: "2026-09-06T17:00:00.000Z",
    }),
    null,
    "equivalent ISO forms of the same instant must not render twice",
  );
  assert.equal(
    distinctFinishedAt({
      workflow_finished_at: "2026-09-06T17:00:00Z",
      finished_at: "not-a-timestamp",
    }),
    null,
    "malformed finished_at must not render beside a valid workflow finish",
  );
  assert.equal(
    distinctFinishedAt({
      workflow_finished_at: null,
      finished_at: "not-a-timestamp",
    }),
    null,
    "malformed finished_at must not bypass a missing workflow finish",
  );
  assert.equal(
    distinctFinishedAt({
      workflow_finished_at: "also-not-a-timestamp",
      finished_at: "not-a-timestamp",
    }),
    null,
    "malformed finished_at must not bypass a rejected workflow finish",
  );
  assert.equal(
    distinctFinishedAt({
      workflow_finished_at: "2026-09-06T17:00:00Z",
      finished_at: "2026-09-06T16:00:00Z",
    }),
    "2026-09-06T16:00:00Z",
  );
  assert.equal(
    distinctFinishedAt({
      workflow_finished_at: "not-a-timestamp",
      finished_at: "2026-09-06T16:00:00Z",
    }),
    null,
    "a valid finished_at fallback must not render under both finish labels",
  );
  assert.equal(
    distinctFinishedAt({
      workflow_finished_at: "2026-09-06T17:00:00Z",
      finished_at: null,
    }),
    null,
  );
  assert.equal(distinctFinishedAt({}), null);
});

test("resolveWorkflowTiming preserves explicit duration without a terminal finish", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const item = {
    status: "completed",
    recovery: null,
    workflow_finished_at: null,
    finished_at: null,
    lifecycle: [],
  };

  assert.deepEqual(resolveWorkflowTiming({ ...item, duration_seconds: 125 }), {
    finishedAt: null,
    durationSeconds: 125,
  });
  assert.deepEqual(resolveWorkflowTiming({ ...item, duration_seconds: -1 }), {
    finishedAt: null,
    durationSeconds: null,
  });
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      workflow_finished_at: "not-a-timestamp",
      duration_seconds: 125,
    }),
    { finishedAt: null, durationSeconds: 125 },
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      workflow_finished_at: "not-a-timestamp",
      duration_seconds: -1,
    }),
    { finishedAt: null, durationSeconds: null },
  );
});

test("resolveWorkflowTiming preserves duration when an unused finished_at differs", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const item = {
    status: "completed",
    recovery: null,
    workflow_finished_at: "2026-09-06T12:12:00Z",
    duration_seconds: 600,
    lifecycle: [],
  };

  assert.deepEqual(
    resolveWorkflowTiming({ ...item, finished_at: "2026-09-06T12:10:00Z" }),
    { finishedAt: "2026-09-06T12:12:00Z", durationSeconds: 600 },
  );
  assert.deepEqual(
    resolveWorkflowTiming({ ...item, finished_at: "not-a-timestamp" }),
    { finishedAt: "2026-09-06T12:12:00Z", durationSeconds: 600 },
  );
});

test("resolveWorkflowTiming uses the retained terminal event after recovery", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const recovery = {
    started_at: "2026-09-06T12:10:00Z",
  };
  const terminalEvent = {
    id: "event_terminal",
    event_type: "workspace.state_changed",
    old_state: "validating",
    new_state: "completed",
    occurred_at: "2026-09-06T12:20:00Z",
  };
  const item = {
    status: "completed",
    recovery,
    workflow_finished_at: null,
    finished_at: null,
    duration_seconds: null,
    lifecycle: [],
    latest_state_change: terminalEvent,
    latest_workflow_terminal_state_change: terminalEvent,
  };

  assert.deepEqual(resolveWorkflowTiming(item), {
    finishedAt: "2026-09-06T12:20:00Z",
    durationSeconds: null,
  });
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      recovery: { started_at: terminalEvent.occurred_at },
    }),
    { finishedAt: "2026-09-06T12:20:00Z", durationSeconds: null },
    "backend event ordering must retain a terminal transition at the recovery timestamp",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      status: "destroyed",
      recovery: { started_at: terminalEvent.occurred_at },
      latest_state_change: {
        id: "event_destroyed",
        event_type: "workspace.state_changed",
        old_state: "destroying",
        new_state: "destroyed",
        occurred_at: "2026-09-06T12:30:00Z",
      },
      latest_workflow_terminal_state_change: {
        ...terminalEvent,
        old_state: "running",
        new_state: "failed",
      },
    }),
    { finishedAt: null, durationSeconds: null },
    "timestamp equality alone must not reuse a pre-recovery terminal event",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      status: "destroyed",
      recovery: {
        started_at: terminalEvent.occurred_at,
        started_event_order: 40,
      },
      latest_state_change: {
        id: "event_destroyed",
        event_type: "workspace.state_changed",
        old_state: "destroying",
        new_state: "destroyed",
        occurred_at: "2026-09-06T12:30:00Z",
        event_order: 43,
      },
      latest_workflow_terminal_state_change: {
        ...terminalEvent,
        old_state: "monitoring_pr",
        new_state: "completed",
        event_order: 41,
      },
    }),
    { finishedAt: "2026-09-06T12:20:00Z", durationSeconds: null },
    "event order must preserve a tied recovered finish after cleanup replaces the latest state change",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      status: "destroyed",
      recovery: {
        started_at: terminalEvent.occurred_at,
        started_event_order: 40,
      },
      latest_state_change: {
        id: "event_destroyed",
        event_type: "workspace.state_changed",
        old_state: "destroying",
        new_state: "destroyed",
        occurred_at: "2026-09-06T12:30:00Z",
        event_order: 43,
      },
      latest_workflow_terminal_state_change: {
        ...terminalEvent,
        old_state: "running",
        new_state: "failed",
        event_order: 39,
      },
    }),
    { finishedAt: null, durationSeconds: null },
    "a tied terminal event ordered before recovery must remain rejected after cleanup",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      status: "failed",
      latest_state_change: {
        event_type: "workspace.state_changed",
        old_state: "destroying",
        new_state: "failed",
        occurred_at: "2026-09-06T12:30:00Z",
      },
    }),
    { finishedAt: "2026-09-06T12:20:00Z", durationSeconds: null },
    "cleanup failure must preserve the recovered completed workflow finish",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      status: "destroyed",
      recovery: { started_at: "2026-09-06T12:21:00Z" },
      latest_state_change: {
        event_type: "workspace.state_changed",
        old_state: "destroying",
        new_state: "destroyed",
        occurred_at: "2026-09-06T12:30:00Z",
      },
      latest_workflow_terminal_state_change: {
        ...terminalEvent,
        old_state: "running",
        new_state: "failed",
      },
    }),
    { finishedAt: null, durationSeconds: null },
    "destroy after remonitor must not reuse the pre-remonitor failed event",
  );

  for (const [label, overrides] of [
    [
      "non-state event",
      {
        latest_workflow_terminal_state_change: {
          ...terminalEvent,
          event_type: "workspace.test_marker",
        },
      },
    ],
    [
      "mismatched terminal status",
      {
        latest_workflow_terminal_state_change: {
          ...terminalEvent,
          new_state: "failed",
        },
      },
    ],
    [
      "destroy transition instead of workflow terminal event",
      {
        status: "destroyed",
        latest_workflow_terminal_state_change: {
          ...terminalEvent,
          old_state: "destroying",
          new_state: "destroyed",
        },
      },
    ],
    [
      "malformed event timestamp",
      {
        latest_workflow_terminal_state_change: {
          ...terminalEvent,
          occurred_at: "not-a-timestamp",
        },
      },
    ],
    [
      "event predating recovery",
      {
        latest_workflow_terminal_state_change: {
          ...terminalEvent,
          occurred_at: "2026-09-06T12:09:59Z",
        },
      },
    ],
    ["malformed recovery timestamp", { recovery: { started_at: "not-a-timestamp" } }],
  ]) {
    assert.deepEqual(
      resolveWorkflowTiming({ ...item, ...overrides }),
      { finishedAt: null, durationSeconds: null },
      label,
    );
  }
});

test("resolveWorkflowTiming compares explicit and lifecycle finishes at full recorded precision", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const lifecycle = [
    {
      stage: "requested",
      started_at: "2026-09-06T12:00:00.500600Z",
      ended_at: "2026-09-06T12:00:01.500600Z",
      duration_seconds: 1,
      status: "completed",
    },
    {
      stage: "completed",
      started_at: "2026-09-06T12:00:01.500600Z",
      ended_at: "2026-09-06T12:00:01.500600Z",
      duration_seconds: 0,
      status: "completed",
    },
  ];

  assert.deepEqual(
    resolveWorkflowTiming({
      status: "completed",
      recovery: null,
      workflow_finished_at: "2026-09-06T12:00:01.500400Z",
      finished_at: null,
      duration_seconds: null,
      lifecycle,
    }),
    {
      finishedAt: "2026-09-06T12:00:01.500400Z",
      durationSeconds: null,
    },
    "a millisecond-truncated match must not attach a contradictory lifecycle duration",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      status: "completed",
      recovery: null,
      workflow_finished_at: "2026-09-06T12:00:01.5006000+00:00",
      finished_at: null,
      duration_seconds: null,
      lifecycle,
    }),
    {
      finishedAt: "2026-09-06T12:00:01.5006000+00:00",
      durationSeconds: 1,
    },
    "equivalent timezone and fractional forms must retain lifecycle duration",
  );
});

test("resolveWorkflowTiming skips malformed explicit finish candidates", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");

  assert.deepEqual(
    resolveWorkflowTiming({
      status: "completed",
      recovery: null,
      workflow_finished_at: "not-a-timestamp",
      finished_at: "2026-09-06T12:12:00Z",
      duration_seconds: 600,
      lifecycle: [],
    }),
    { finishedAt: "2026-09-06T12:12:00Z", durationSeconds: 600 },
    "a malformed workflow_finished_at must not hide a valid finished_at",
  );

  assert.deepEqual(
    resolveWorkflowTiming({
      status: "completed",
      recovery: null,
      workflow_finished_at: "not-a-timestamp",
      finished_at: "also-not-a-timestamp",
      duration_seconds: null,
      lifecycle: [
        {
          stage: "requested",
          started_at: "2026-09-06T12:00:00Z",
          ended_at: "2026-09-06T12:00:00Z",
          duration_seconds: 0,
          status: "completed",
        },
        {
          stage: "running",
          started_at: "2026-09-06T12:00:00Z",
          ended_at: "2026-09-06T12:10:00Z",
          duration_seconds: 600,
          status: "completed",
        },
        {
          stage: "completed",
          started_at: "2026-09-06T12:10:00Z",
          ended_at: "2026-09-06T12:10:00Z",
          duration_seconds: 0,
          status: "completed",
        },
      ],
    }),
    { finishedAt: "2026-09-06T12:10:00Z", durationSeconds: 600 },
    "malformed explicit fields must not hide trustworthy lifecycle timing",
  );
});

test("resolveWorkflowTiming rejects non-RFC and impossible recorded timestamps", async () => {
  const { resolveWorkflowFinishedAt, resolveWorkflowTiming } =
    await import("./agent-format.ts");

  for (const invalidTimestamp of [
    "09/07/2026",
    "2026-02-30T12:00:00Z",
    "2026-09-07T12:00:00+24:00",
    "2026-09-07T12:00:00+00:60",
  ]) {
    assert.equal(
      resolveWorkflowFinishedAt({ workflow_finished_at: invalidTimestamp }),
      null,
      `${invalidTimestamp} must not be accepted as an explicit finish`,
    );
  }
  assert.equal(
    resolveWorkflowFinishedAt({
      workflow_finished_at: "2026-09-07t12:00:00z",
    }),
    "2026-09-07t12:00:00z",
  );
  assert.equal(
    resolveWorkflowFinishedAt({
      workflow_finished_at: "2026-09-07T12:00:00+02:30",
    }),
    "2026-09-07T12:00:00+02:30",
  );

  assert.deepEqual(
    resolveWorkflowTiming({
      status: "completed",
      recovery: null,
      workflow_finished_at: null,
      finished_at: null,
      duration_seconds: null,
      lifecycle: [
        {
          stage: "requested",
          started_at: "2026-02-30T11:00:00Z",
          ended_at: "2026-02-30T12:00:00Z",
          duration_seconds: 3600,
          status: "completed",
        },
        {
          stage: "completed",
          started_at: "2026-02-30T12:00:00Z",
          ended_at: "2026-02-30T12:00:00Z",
          duration_seconds: 0,
          status: "completed",
        },
      ],
    }),
    { finishedAt: null, durationSeconds: null },
    "an impossible lifecycle boundary must not fabricate a finish or duration",
  );
});

test("resolveWorkflowTiming rejects lifecycle stages that end before they start", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const item = {
    status: "failed",
    recovery: null,
    workflow_finished_at: null,
    finished_at: null,
    duration_seconds: null,
    lifecycle: [
      {
        stage: "requested",
        started_at: "2026-09-06T12:00:00Z",
        ended_at: "2026-09-06T12:01:00Z",
        duration_seconds: 60,
        status: "completed",
      },
      {
        stage: "running",
        started_at: "2026-09-06T12:05:00Z",
        ended_at: "2026-09-06T12:04:00Z",
        duration_seconds: 60,
        status: "completed",
      },
    ],
    last_event: {
      event_type: "workspace.state_changed",
      old_state: "running",
      new_state: "failed",
      occurred_at: "2026-09-06T12:04:00Z",
    },
  };

  assert.deepEqual(resolveWorkflowTiming(item), {
    finishedAt: null,
    durationSeconds: null,
  });
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      lifecycle: [
        {
          ...item.lifecycle[0],
          started_at: "2026-09-06T12:00:00.000900Z",
          ended_at: "2026-09-06T12:00:00.000800Z",
          duration_seconds: 0,
        },
      ],
      last_event: {
        ...item.last_event,
        old_state: "requested",
        occurred_at: "2026-09-06T12:00:00.000800Z",
      },
    }),
    { finishedAt: null, durationSeconds: null },
    "a reversed sub-millisecond stage must not become the terminal finish",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      lifecycle: [
        {
          ...item.lifecycle[0],
          started_at: "2026-09-06T12:02:00Z",
          ended_at: "2026-09-06T12:01:00Z",
        },
        {
          ...item.lifecycle[1],
          ended_at: "2026-09-06T12:06:00Z",
        },
      ],
      last_event: {
        ...item.last_event,
        occurred_at: "2026-09-06T12:06:00Z",
      },
    }),
    { finishedAt: null, durationSeconds: null },
    "an inverted earlier stage must invalidate an otherwise trustworthy terminal boundary",
  );
});

test("resolveWorkflowTiming rejects lifecycle timing that contradicts stage status", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const completedStage = {
    stage: "completed",
    started_at: "2026-09-06T12:10:00Z",
    ended_at: "2026-09-06T12:10:00Z",
    duration_seconds: 0,
    status: "completed",
  };
  const contradictoryStages = [
    {
      stage: "requested",
      started_at: "2026-09-06T12:00:00Z",
      ended_at: "2026-09-06T12:10:00Z",
      duration_seconds: 600,
      status: "pending",
    },
    {
      stage: "requested",
      started_at: "2026-09-06T12:00:00Z",
      ended_at: "2026-09-06T12:10:00Z",
      duration_seconds: 600,
      status: "terminal_skipped",
    },
    {
      stage: "requested",
      started_at: "2026-09-06T12:00:00Z",
      ended_at: "2026-09-06T12:10:00Z",
      duration_seconds: 600,
      status: "active",
    },
  ];

  for (const contradictoryStage of contradictoryStages) {
    assert.deepEqual(
      resolveWorkflowTiming({
        status: "completed",
        recovery: null,
        workflow_finished_at: null,
        finished_at: null,
        duration_seconds: null,
        lifecycle: [contradictoryStage, completedStage],
      }),
      { finishedAt: null, durationSeconds: null },
      `${contradictoryStage.status} timing must invalidate lifecycle fallback`,
    );
  }

  assert.deepEqual(
    resolveWorkflowTiming({
      status: "failed",
      recovery: null,
      workflow_finished_at: null,
      finished_at: null,
      duration_seconds: null,
      lifecycle: [
        {
          stage: "requested",
          started_at: "2026-09-06T12:00:00Z",
          ended_at: "2026-09-06T12:01:00Z",
          duration_seconds: 60,
          status: "completed",
        },
        {
          stage: "running",
          started_at: "2026-09-06T12:01:00Z",
          ended_at: null,
          duration_seconds: null,
          status: "active",
        },
        {
          stage: "validating",
          started_at: "2026-09-06T12:05:00Z",
          ended_at: "2026-09-06T12:10:00Z",
          duration_seconds: 300,
          status: "completed",
        },
      ],
      last_event: {
        event_type: "workspace.state_changed",
        old_state: "validating",
        new_state: "failed",
        occurred_at: "2026-09-06T12:10:00Z",
      },
    }),
    { finishedAt: null, durationSeconds: null },
    "an unended active stage must invalidate a later corroborated terminal boundary",
  );
});

test("resolveWorkflowTiming rejects stage durations that contradict their timestamps", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const item = {
    status: "completed",
    recovery: null,
    workflow_finished_at: null,
    finished_at: null,
    duration_seconds: null,
    lifecycle: [
      {
        stage: "requested",
        started_at: "2026-09-06T12:00:00Z",
        ended_at: "2026-09-06T12:10:00.999Z",
        duration_seconds: 600,
        status: "completed",
      },
      {
        stage: "completed",
        started_at: "2026-09-06T12:10:00.999Z",
        ended_at: "2026-09-06T12:10:00.999Z",
        duration_seconds: 0,
        status: "completed",
      },
    ],
  };

  assert.deepEqual(resolveWorkflowTiming(item), {
    finishedAt: "2026-09-06T12:10:00.999Z",
    durationSeconds: 600,
  });
  const submillisecondItem = {
    ...item,
    lifecycle: [
      {
        ...item.lifecycle[0],
        started_at: "2026-09-06T12:00:00.000900Z",
        ended_at: "2026-09-06T12:00:01.000800Z",
        duration_seconds: 0,
      },
      {
        ...item.lifecycle[1],
        started_at: "2026-09-06T12:00:01.000800Z",
        ended_at: "2026-09-06T12:00:01.000800Z",
      },
    ],
  };
  assert.deepEqual(
    resolveWorkflowTiming(submillisecondItem),
    {
      finishedAt: "2026-09-06T12:00:01.000800Z",
      durationSeconds: 0,
    },
    "duration validation must retain Core's microsecond precision",
  );
  const nanosecondItem = {
    ...item,
    lifecycle: [
      {
        ...item.lifecycle[0],
        started_at: "2026-09-06T12:00:00.0000009Z",
        ended_at: "2026-09-06T12:00:01.0000001Z",
        duration_seconds: 0,
      },
      {
        ...item.lifecycle[1],
        started_at: "2026-09-06T12:00:01.0000001Z",
        ended_at: "2026-09-06T12:00:01.0000001Z",
      },
    ],
  };
  assert.deepEqual(
    resolveWorkflowTiming(nanosecondItem),
    {
      finishedAt: "2026-09-06T12:00:01.0000001Z",
      durationSeconds: 0,
    },
    "duration validation must retain hosted nanosecond precision",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...submillisecondItem,
      lifecycle: [
        {
          ...submillisecondItem.lifecycle[0],
          duration_seconds: 1,
        },
        submillisecondItem.lifecycle[1],
      ],
    }),
    {
      finishedAt: "2026-09-06T12:00:01.000800Z",
      durationSeconds: null,
    },
    "microsecond precision must not accept the millisecond-truncated interval",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      lifecycle: [
        {
          ...item.lifecycle[0],
          duration_seconds: 1,
        },
        item.lifecycle[1],
      ],
    }),
    {
      finishedAt: "2026-09-06T12:10:00.999Z",
      durationSeconds: null,
    },
    "a supplied duration must not override the validated stage interval",
  );
});

test("resolveWorkflowTiming rejects tied latest lifecycle stages in either array order", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const requested = {
    stage: "requested",
    started_at: "2026-09-06T12:00:00Z",
    ended_at: "2026-09-06T12:10:00Z",
    duration_seconds: 600,
    status: "completed",
  };
  const running = {
    stage: "running",
    started_at: "2026-09-06T12:10:00Z",
    ended_at: "2026-09-06T12:20:00Z",
    duration_seconds: 600,
    status: "completed",
  };
  const validating = {
    stage: "validating",
    started_at: "2026-09-06T12:10:00Z",
    ended_at: "2026-09-06T12:30:00Z",
    duration_seconds: 1200,
    status: "completed",
  };
  const timingFor = (latestStages) =>
    resolveWorkflowTiming({
      status: "failed",
      recovery: null,
      workflow_finished_at: null,
      finished_at: null,
      duration_seconds: null,
      lifecycle: [requested, ...latestStages],
    });

  const runningFirst = timingFor([running, validating]);
  const validatingFirst = timingFor([validating, running]);

  assert.deepEqual(
    runningFirst,
    validatingFirst,
    "ambiguous fallback timing must not depend on lifecycle array order",
  );
  assert.deepEqual(runningFirst, { finishedAt: null, durationSeconds: null });
});

test("resolveWorkflowTiming orders exact lifecycle timestamp ties by stage", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const terminalEvent = {
    event_type: "workspace.state_changed",
    old_state: "pushing",
    new_state: "failed",
    occurred_at: "2026-09-06T12:30:00Z",
  };
  const item = {
    status: "failed",
    recovery: null,
    workflow_finished_at: null,
    finished_at: null,
    duration_seconds: null,
    lifecycle: [
      {
        stage: "requested",
        started_at: "2026-09-06T12:00:00Z",
        ended_at: "2026-09-06T12:10:00Z",
        duration_seconds: 600,
        status: "completed",
      },
      {
        stage: "running",
        started_at: "2026-09-06T12:10:00Z",
        ended_at: "2026-09-06T12:20:00Z",
        duration_seconds: 600,
        status: "completed",
      },
      {
        stage: "validating",
        started_at: "2026-09-06T12:20:00Z",
        ended_at: "2026-09-06T12:20:00Z",
        duration_seconds: 0,
        status: "completed",
      },
      {
        stage: "pushing",
        started_at: "2026-09-06T12:20:00Z",
        ended_at: "2026-09-06T12:30:00Z",
        duration_seconds: 600,
        status: "completed",
      },
    ],
    latest_workflow_terminal_state_change: terminalEvent,
    last_event: terminalEvent,
  };

  assert.deepEqual(resolveWorkflowTiming(item), {
    finishedAt: "2026-09-06T12:30:00Z",
    durationSeconds: 1800,
  });
  assert.deepEqual(
    resolveWorkflowTiming({ ...item, lifecycle: [...item.lifecycle, item.lifecycle[3]] }),
    {
      finishedAt: null,
      durationSeconds: null,
    },
    "a duplicate stage cannot resolve an exact timestamp tie",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      lifecycle: [...item.lifecycle, { ...item.lifecycle[3], stage: "future_stage" }],
    }),
    { finishedAt: null, durationSeconds: null },
    "an unknown stage cannot resolve an exact timestamp tie",
  );
});

test("resolveWorkflowTiming orders latest lifecycle stages at full recorded precision", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const item = {
    status: "completed",
    recovery: null,
    workflow_finished_at: null,
    finished_at: null,
    duration_seconds: null,
    lifecycle: [
      {
        stage: "requested",
        started_at: "2026-09-06T12:00:00.000100Z",
        ended_at: "2026-09-06T12:00:01.000100Z",
        duration_seconds: 1,
        status: "completed",
      },
      {
        stage: "pushing",
        started_at: "2026-09-06T12:00:01.000100Z",
        ended_at: "2026-09-06T12:00:01.000800Z",
        duration_seconds: 0,
        status: "completed",
      },
      {
        stage: "completed",
        started_at: "2026-09-06T12:00:01.000800Z",
        ended_at: "2026-09-06T12:00:01.000800Z",
        duration_seconds: 0,
        status: "completed",
      },
    ],
  };

  assert.deepEqual(resolveWorkflowTiming(item), {
    finishedAt: "2026-09-06T12:00:01.000800Z",
    durationSeconds: 1,
  });
});

test("resolveWorkflowTiming rejects a sub-millisecond gap before completed", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const item = {
    status: "completed",
    recovery: null,
    workflow_finished_at: null,
    finished_at: null,
    duration_seconds: null,
    lifecycle: [
      {
        stage: "requested",
        started_at: "2026-09-06T12:00:00.000000Z",
        ended_at: "2026-09-06T12:00:01.000100Z",
        duration_seconds: 1,
        status: "completed",
      },
      {
        stage: "completed",
        started_at: "2026-09-06T12:00:01.000800Z",
        ended_at: "2026-09-06T12:00:01.000800Z",
        duration_seconds: 0,
        status: "completed",
      },
    ],
  };

  assert.deepEqual(resolveWorkflowTiming(item), {
    finishedAt: null,
    durationSeconds: null,
  });
});

test("resolveWorkflowTiming rejects sub-millisecond gaps between lifecycle stages", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const item = {
    status: "completed",
    recovery: null,
    workflow_finished_at: null,
    finished_at: null,
    duration_seconds: null,
    lifecycle: [
      {
        stage: "requested",
        started_at: "2026-09-06T12:00:00.000000Z",
        ended_at: "2026-09-06T12:00:01.000100Z",
        duration_seconds: 1,
        status: "completed",
      },
      {
        stage: "running",
        started_at: "2026-09-06T12:00:01.000800Z",
        ended_at: "2026-09-06T12:00:02.000800Z",
        duration_seconds: 1,
        status: "completed",
      },
      {
        stage: "completed",
        started_at: "2026-09-06T12:00:02.000800Z",
        ended_at: "2026-09-06T12:00:02.000800Z",
        duration_seconds: 0,
        status: "completed",
      },
    ],
  };

  assert.deepEqual(resolveWorkflowTiming(item), {
    finishedAt: "2026-09-06T12:00:02.000800Z",
    durationSeconds: null,
  });
});

test("resolveWorkflowTiming truncates aggregate lifecycle duration once", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const item = {
    status: "completed",
    recovery: null,
    workflow_finished_at: null,
    finished_at: null,
    duration_seconds: null,
    lifecycle: [
      {
        stage: "requested",
        started_at: "2026-09-06T12:00:00.000000Z",
        ended_at: "2026-09-06T12:00:00.600000Z",
        duration_seconds: 0,
        status: "completed",
      },
      {
        stage: "pushing",
        started_at: "2026-09-06T12:00:00.600000Z",
        ended_at: "2026-09-06T12:00:01.200000Z",
        duration_seconds: 0,
        status: "completed",
      },
      {
        stage: "completed",
        started_at: "2026-09-06T12:00:01.200000Z",
        ended_at: "2026-09-06T12:00:01.200000Z",
        duration_seconds: 0,
        status: "completed",
      },
    ],
  };

  assert.deepEqual(resolveWorkflowTiming(item), {
    finishedAt: "2026-09-06T12:00:01.200000Z",
    durationSeconds: 1,
  });
});

test("resolveWorkflowTiming requires terminal evidence after the latest represented stage", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const item = {
    status: "failed",
    recovery: null,
    workflow_finished_at: null,
    finished_at: null,
    duration_seconds: null,
    lifecycle: [
      {
        stage: "requested",
        started_at: "2026-09-06T12:00:00Z",
        ended_at: "2026-09-06T12:01:00Z",
        duration_seconds: 60,
        status: "completed",
      },
      {
        stage: "running",
        started_at: "2026-09-06T12:01:00Z",
        ended_at: "2026-09-06T12:05:00Z",
        duration_seconds: 240,
        status: "completed",
      },
    ],
  };
  const stateChanged = (oldState, newState, occurredAt) => ({
    event_type: "workspace.state_changed",
    old_state: oldState,
    new_state: newState,
    occurred_at: occurredAt,
  });
  const terminalRuntimeReleased = {
    event_type: "workspace.terminal_runtime_released",
    old_state: null,
    new_state: null,
    occurred_at: "2026-09-06T12:06:00Z",
  };

  assert.deepEqual(resolveWorkflowTiming({ ...item, last_event: null }), {
    finishedAt: null,
    durationSeconds: null,
  });
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      status: "cancelled",
      last_event: stateChanged("blocked", "cancelled", "2026-09-06T12:10:00Z"),
    }),
    { finishedAt: null, durationSeconds: null },
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      last_event: stateChanged("recovering", "failed", "2026-09-06T12:10:00Z"),
    }),
    { finishedAt: null, durationSeconds: null },
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      last_event: stateChanged("blocked", "failed", "2026-09-06T12:05:00Z"),
    }),
    { finishedAt: null, durationSeconds: null },
    "an omitted pause must not be accepted when timestamps happen to coincide",
  );
  // Regression for PR #964 review thread PRRT_kwDOSJAM6s6hrdNM: Core's
  // dedicated retained terminal event proves the finish after an omitted pause,
  // but the collapsed lifecycle still cannot prove the complete duration.
  for (const pauseStatus of ["blocked", "recovering"]) {
    for (const terminalStatus of ["failed", "cancelled"]) {
      const terminalEvent = stateChanged(
        pauseStatus,
        terminalStatus,
        "2026-09-06T12:10:00Z",
      );
      assert.deepEqual(
        resolveWorkflowTiming({
          ...item,
          status: terminalStatus,
          latest_workflow_terminal_state_change: terminalEvent,
          last_event: terminalEvent,
        }),
        { finishedAt: "2026-09-06T12:10:00Z", durationSeconds: null },
        `${pauseStatus} -> ${terminalStatus} must use the retained terminal finish`,
      );
    }
  }
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      status: "completed",
      latest_workflow_terminal_state_change: stateChanged(
        "blocked",
        "completed",
        "2026-09-06T12:10:00Z",
      ),
    }),
    { finishedAt: null, durationSeconds: null },
    "an unsupported blocked -> completed event must not bypass recovery corroboration",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      last_event: stateChanged("running", "cancelled", "2026-09-06T12:05:00Z"),
    }),
    { finishedAt: null, durationSeconds: null },
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      last_event: stateChanged("running", "failed", "2026-09-06T12:05:00Z"),
    }),
    { finishedAt: "2026-09-06T12:05:00Z", durationSeconds: 300 },
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      lifecycle: [
        item.lifecycle[0],
        {
          ...item.lifecycle[1],
          ended_at: "2026-09-06T12:05:00.000100Z",
        },
      ],
      latest_workflow_terminal_state_change: stateChanged(
        "running",
        "failed",
        "2026-09-06T12:05:00.000900Z",
      ),
    }),
    { finishedAt: null, durationSeconds: null },
    "a same-millisecond terminal event at a different instant must not corroborate the lifecycle finish",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      status: "destroying",
      last_event: stateChanged("running", "destroying", "2026-09-06T12:05:00Z"),
    }),
    { finishedAt: null, durationSeconds: null },
    "a direct pre-terminal destroy must not infer workflow timing",
  );
  for (const terminalStatus of ["failed", "cancelled"]) {
    assert.deepEqual(
      resolveWorkflowTiming({
        ...item,
        status: "destroying",
        latest_workflow_terminal_state_change: stateChanged(
          "running",
          terminalStatus,
          "2026-09-06T12:05:00Z",
        ),
        latest_state_change: stateChanged(
          terminalStatus,
          "destroying",
          "2026-09-06T12:20:00Z",
        ),
        last_event: stateChanged(
          terminalStatus,
          "destroying",
          "2026-09-06T12:20:00Z",
        ),
      }),
      { finishedAt: "2026-09-06T12:05:00Z", durationSeconds: 300 },
      `destroying cleanup must preserve the earlier ${terminalStatus} transition`,
    );
  }
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      status: "destroying",
      lifecycle: [
        ...item.lifecycle,
        {
          stage: "completed",
          started_at: "2026-09-06T12:05:00Z",
          ended_at: "2026-09-06T12:20:00Z",
          duration_seconds: 900,
          status: "completed",
        },
      ],
      latest_workflow_terminal_state_change: stateChanged(
        "running",
        "completed",
        "2026-09-06T12:05:00Z",
      ),
      latest_state_change: stateChanged(
        "completed",
        "destroying",
        "2026-09-06T12:20:00Z",
      ),
      last_event: stateChanged(
        "completed",
        "destroying",
        "2026-09-06T12:20:00Z",
      ),
    }),
    { finishedAt: "2026-09-06T12:05:00Z", durationSeconds: 300 },
    "destroying cleanup must preserve an earlier completed transition",
  );
  for (const terminalStatus of ["failed", "cancelled"]) {
    assert.deepEqual(
      resolveWorkflowTiming({
        ...item,
        status: "destroyed",
        latest_workflow_terminal_state_change: stateChanged(
          "running",
          terminalStatus,
          "2026-09-06T12:05:00Z",
        ),
        latest_state_change: stateChanged(
          "destroying",
          "destroyed",
          "2026-09-06T12:20:00Z",
        ),
        last_event: stateChanged(
          "destroying",
          "destroyed",
          "2026-09-06T12:20:00Z",
        ),
      }),
      { finishedAt: "2026-09-06T12:05:00Z", durationSeconds: 300 },
      `destroy cleanup must not hide the earlier ${terminalStatus} transition`,
    );
  }
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      status: "failed",
      latest_workflow_terminal_state_change: stateChanged(
        "running",
        "cancelled",
        "2026-09-06T12:05:00Z",
      ),
      latest_state_change: stateChanged(
        "destroying",
        "failed",
        "2026-09-06T12:20:00Z",
      ),
      last_event: stateChanged(
        "destroying",
        "failed",
        "2026-09-06T12:20:00Z",
      ),
    }),
    { finishedAt: "2026-09-06T12:05:00Z", durationSeconds: 300 },
    "cleanup failure must not hide the earlier cancellation transition",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      latest_state_change: stateChanged(
        "running",
        "failed",
        "2026-09-06T12:05:00Z",
      ),
      last_event: terminalRuntimeReleased,
    }),
    { finishedAt: "2026-09-06T12:05:00Z", durationSeconds: 300 },
    "terminal cleanup must not hide an earlier direct terminal transition",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      status: "cancelled",
      latest_state_change: stateChanged(
        "running",
        "cancelled",
        "2026-09-06T12:05:00Z",
      ),
      last_event: terminalRuntimeReleased,
    }),
    { finishedAt: "2026-09-06T12:05:00Z", durationSeconds: 300 },
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      latest_state_change: stateChanged(
        "blocked",
        "failed",
        "2026-09-06T12:05:00Z",
      ),
      last_event: terminalRuntimeReleased,
    }),
    { finishedAt: null, durationSeconds: null },
    "a cleanup event must not bypass the direct-transition requirement",
  );
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      status: "completed",
      last_event: null,
      lifecycle: [
        ...item.lifecycle,
        {
          stage: "completed",
          started_at: "2026-09-06T12:05:00Z",
          ended_at: "2026-09-06T12:05:00Z",
          duration_seconds: 0,
          status: "completed",
        },
      ],
    }),
    { finishedAt: "2026-09-06T12:05:00Z", durationSeconds: 300 },
  );
});

test("resolveWorkflowTiming requires the initial requested stage before inferring duration", async () => {
  const { resolveWorkflowTiming } = await import("./agent-format.ts");
  const running = {
    stage: "running",
    started_at: "2026-09-06T12:01:00Z",
    ended_at: "2026-09-06T12:05:00Z",
    duration_seconds: 240,
    status: "completed",
  };
  const item = {
    status: "failed",
    recovery: null,
    workflow_finished_at: null,
    finished_at: null,
    duration_seconds: null,
    last_event: {
      event_type: "workspace.state_changed",
      old_state: "running",
      new_state: "failed",
      occurred_at: "2026-09-06T12:05:00Z",
    },
  };
  const expected = {
    finishedAt: "2026-09-06T12:05:00Z",
    durationSeconds: null,
  };

  assert.deepEqual(resolveWorkflowTiming({ ...item, lifecycle: [running] }), expected);
  assert.deepEqual(
    resolveWorkflowTiming({
      ...item,
      lifecycle: [
        {
          stage: "requested",
          started_at: null,
          ended_at: null,
          duration_seconds: null,
          status: "pending",
        },
        running,
      ],
    }),
    expected,
  );
});
