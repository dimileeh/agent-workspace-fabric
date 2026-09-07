import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { parseDashboardSummary } from "./console-dashboard-summary.ts";

const fixture = JSON.parse(
  readFileSync(
    new URL("../../../docs/console/fixtures/v1/dashboard-summary.local.json", import.meta.url),
    "utf8",
  ),
);

function validSummary(overrides = {}) {
  return structuredClone({ ...fixture, ...overrides });
}

test("parseDashboardSummary accepts the local fixture", () => {
  const parsed = parseDashboardSummary(fixture);
  assert.ok(parsed);
  assert.deepEqual(parsed.coverage.notes, []);
  assert.equal(parsed.window.since_hours, 24);
});

test("parseDashboardSummary normalizes omitted coverage.notes to []", () => {
  const payload = validSummary();
  delete payload.coverage.notes;
  const parsed = parseDashboardSummary(payload);
  assert.ok(parsed);
  assert.deepEqual(parsed.coverage.notes, []);
});

test("parseDashboardSummary rejects malformed coverage.notes", () => {
  assert.equal(parseDashboardSummary(validSummary({ coverage: { status: "complete", notes: null } })), null);
  assert.equal(parseDashboardSummary(validSummary({ coverage: { status: "complete", notes: "x" } })), null);
  assert.equal(
    parseDashboardSummary(validSummary({ coverage: { status: "complete", notes: [1, "ok"] } })),
    null,
  );
  assert.equal(
    parseDashboardSummary(validSummary({ coverage: { status: "complete", notes: [{ text: "x" }] } })),
    null,
  );
});

test("parseDashboardSummary accepts string coverage.notes arrays", () => {
  const parsed = parseDashboardSummary(
    validSummary({ coverage: { status: "partial", notes: ["queued_count_unavailable"] } }),
  );
  assert.ok(parsed);
  assert.deepEqual(parsed.coverage.notes, ["queued_count_unavailable"]);
});

test("parseDashboardSummary requires a positive integer since_hours", () => {
  assert.equal(
    parseDashboardSummary(
      validSummary({ window: { anchor: "generated_at", since_hours: -1, start: fixture.window.start } }),
    ),
    null,
  );
  assert.equal(
    parseDashboardSummary(
      validSummary({ window: { anchor: "generated_at", since_hours: 0, start: fixture.window.start } }),
    ),
    null,
  );
  assert.equal(
    parseDashboardSummary(
      validSummary({ window: { anchor: "generated_at", since_hours: 1.5, start: fixture.window.start } }),
    ),
    null,
  );
  assert.equal(
    parseDashboardSummary(
      validSummary({ window: { anchor: "generated_at", since_hours: "24", start: fixture.window.start } }),
    ),
    null,
  );
  const ok = parseDashboardSummary(
    validSummary({ window: { anchor: "generated_at", since_hours: 1, start: fixture.window.start } }),
  );
  assert.ok(ok);
  assert.equal(ok.window.since_hours, 1);
});
