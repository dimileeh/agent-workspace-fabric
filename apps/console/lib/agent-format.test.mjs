import assert from "node:assert/strict";
import test from "node:test";

import { formatAgentTitle } from "./agent-format.ts";

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
