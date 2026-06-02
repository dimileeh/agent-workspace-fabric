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
