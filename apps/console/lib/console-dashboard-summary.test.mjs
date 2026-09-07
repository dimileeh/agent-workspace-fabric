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

test("parseDashboardSummary rejects impossible calendar timestamps", () => {
  // Date.parse normalizes 2026-02-29 → March 1; keep fail-closed like Python datetime.
  for (const generated_at of [
    "2026-02-29T12:00:00Z",
    "2026-04-31T12:00:00Z",
    "2026-13-01T12:00:00Z",
    "2026-01-01T25:00:00Z",
    "2026-09-07T08:43:57+99:00",
  ]) {
    assert.equal(
      parseDashboardSummary(validSummary({ generated_at })),
      null,
      `expected reject for generated_at=${JSON.stringify(generated_at)}`,
    );
  }
  assert.ok(parseDashboardSummary(validSummary({ generated_at: "2024-02-29T12:00:00Z" })));
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

test("parseDashboardSummary rejects contradictory count subset relationships", () => {
  // Domain: executing ⊆ active.
  assert.equal(
    parseDashboardSummary(validSummary({ counts: { ...fixture.counts, active: 1, executing: 2 } })),
    null,
  );
  // Domain: monitoring_pr ⊆ active.
  assert.equal(
    parseDashboardSummary(
      validSummary({ counts: { ...fixture.counts, active: 1, monitoring_pr: 2, awaiting_human: 0 } }),
    ),
    null,
  );
  // Declared overlap: awaiting_human ⊆ monitoring_pr.
  assert.equal(
    parseDashboardSummary(
      validSummary({
        counts: { ...fixture.counts, monitoring_pr: 1, awaiting_human: 2 },
        overlap: { ...fixture.overlap, awaiting_human_subset_of_monitoring_pr: true },
      }),
    ),
    null,
  );
  // Declared overlap: awaiting_operator ∈ active ∉ executing.
  assert.equal(
    parseDashboardSummary(
      validSummary({
        counts: { ...fixture.counts, active: 2, executing: 2, awaiting_operator: 1 },
        overlap: { ...fixture.overlap, awaiting_operator_in_active_not_executing: true },
      }),
    ),
    null,
  );
  // Declared overlap: retrying ∈ active ∉ executing.
  assert.equal(
    parseDashboardSummary(
      validSummary({
        counts: { ...fixture.counts, active: 2, executing: 2, retrying: 1 },
        overlap: { ...fixture.overlap, retrying_in_active_not_executing: true },
      }),
    ),
    null,
  );
});

test("parseDashboardSummary skips subset checks when related counts are null or flags false", () => {
  assert.ok(
    parseDashboardSummary(validSummary({ counts: { ...fixture.counts, active: null, executing: 5 } })),
  );
  assert.ok(
    parseDashboardSummary(
      validSummary({
        counts: { ...fixture.counts, monitoring_pr: 1, awaiting_human: 2 },
        overlap: { ...fixture.overlap, awaiting_human_subset_of_monitoring_pr: false },
      }),
    ),
  );
  assert.ok(
    parseDashboardSummary(
      validSummary({
        counts: { ...fixture.counts, active: 2, executing: 2, awaiting_operator: 1 },
        overlap: { ...fixture.overlap, awaiting_operator_in_active_not_executing: false },
      }),
    ),
  );
  assert.ok(
    parseDashboardSummary(
      validSummary({ counts: { ...fixture.counts, monitoring_pr: 1, awaiting_human: null } }),
    ),
  );
});
