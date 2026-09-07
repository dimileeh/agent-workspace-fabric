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

test("parseDashboardSummary accepts the hosted fixture", () => {
  const hosted = JSON.parse(
    readFileSync(
      new URL("../../../docs/console/fixtures/v1/dashboard-summary.hosted.json", import.meta.url),
      "utf8",
    ),
  );
  // executing + monitoring_pr + queued + retrying (+ awaiting_operator=0) must stay within active.
  const parsed = parseDashboardSummary(hosted, "hosted");
  assert.ok(parsed);
  assert.equal(parsed.scope, "tenant");
  assert.equal(parsed.counts.active, 15);
  assert.equal(
    parsed.counts.executing +
      parsed.counts.monitoring_pr +
      parsed.counts.queued +
      parsed.counts.retrying,
    15,
  );
});

test("parseDashboardSummary rejects pre-fix hosted active underflow", () => {
  // Bugbot: active=12 with executing+monitoring_pr+queued+retrying=15 must fail closed.
  const hosted = JSON.parse(
    readFileSync(
      new URL("../../../docs/console/fixtures/v1/dashboard-summary.hosted.json", import.meta.url),
      "utf8",
    ),
  );
  assert.equal(
    parseDashboardSummary({ ...hosted, counts: { ...hosted.counts, active: 12 } }, "hosted"),
    null,
  );
});

test("parseDashboardSummary rejects scope that conflicts with backend_kind", () => {
  // Hosted negotiation must not accept node-local counters as tenant fleet totals.
  assert.equal(parseDashboardSummary(validSummary({ scope: "local" }), "hosted"), null);
  assert.equal(parseDashboardSummary(validSummary({ scope: "tenant" }), "local"), null);
  assert.ok(parseDashboardSummary(validSummary({ scope: "local" }), "local"));
  assert.ok(parseDashboardSummary(validSummary({ scope: "tenant" }), "hosted"));
  // Without negotiated kind, keep enum-only acceptance (callers that have caps must pass kind).
  assert.ok(parseDashboardSummary(validSummary({ scope: "tenant" })));
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
  assert.ok(
    parseDashboardSummary(
      validSummary({
        generated_at: "2024-02-29T12:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2024-02-28T12:00:00Z" },
      }),
    ),
  );
});

test("parseDashboardSummary accepts lowercase RFC 3339 t/z separators", () => {
  // RFC 3339 §5.6: T and Z are case-insensitive; keep window.start aligned.
  assert.ok(
    parseDashboardSummary(
      validSummary({
        generated_at: "2026-09-06t17:00:00Z",
        as_of: "2026-09-06t17:00:00Z",
        last_success_at: "2026-09-06t17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05t17:00:00Z" },
      }),
    ),
  );
  assert.ok(
    parseDashboardSummary(
      validSummary({
        generated_at: "2026-09-06T17:00:00z",
        as_of: "2026-09-06T17:00:00z",
        last_success_at: "2026-09-06T17:00:00z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00z" },
      }),
    ),
  );
  assert.ok(
    parseDashboardSummary(
      validSummary({
        generated_at: "2026-09-06t17:00:00z",
        as_of: "2026-09-06t17:00:00z",
        last_success_at: "2026-09-06t17:00:00z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05t17:00:00z" },
      }),
    ),
  );
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
    validSummary({
      window: { anchor: "generated_at", since_hours: 1, start: "2026-09-06T16:00:00Z" },
    }),
  );
  assert.ok(ok);
  assert.equal(ok.window.since_hours, 1);
});

test("parseDashboardSummary rejects window.start that disagrees with generated_at - since_hours", () => {
  // Contract: window.start = generated_at - since_hours. A valid but unrelated start
  // must fail closed so KPI "last Nh" hints cannot mislabel a different interval.
  assert.equal(
    parseDashboardSummary(
      validSummary({
        window: {
          anchor: "generated_at",
          since_hours: 24,
          start: "2026-09-01T17:00:00Z",
        },
      }),
    ),
    null,
  );
  assert.equal(
    parseDashboardSummary(
      validSummary({
        window: {
          anchor: "generated_at",
          since_hours: 24,
          start: "2026-09-05T16:00:00Z",
        },
      }),
    ),
    null,
  );
  // Same absolute instant via a non-Z offset still matches.
  assert.ok(
    parseDashboardSummary(
      validSummary({
        window: {
          anchor: "generated_at",
          since_hours: 24,
          start: "2026-09-05T10:00:00-07:00",
        },
      }),
    ),
  );
  // Fractional seconds: start must track generated_at - since_hours exactly.
  assert.ok(
    parseDashboardSummary(
      validSummary({
        generated_at: "2026-09-06T17:00:00.250Z",
        as_of: "2026-09-06T17:00:00.250Z",
        last_success_at: "2026-09-06T17:00:00.250Z",
        window: {
          anchor: "generated_at",
          since_hours: 24,
          start: "2026-09-05T17:00:00.250Z",
        },
      }),
    ),
  );
  assert.equal(
    parseDashboardSummary(
      validSummary({
        generated_at: "2026-09-06T17:00:00.250Z",
        as_of: "2026-09-06T17:00:00.250Z",
        last_success_at: "2026-09-06T17:00:00.250Z",
        window: {
          anchor: "generated_at",
          since_hours: 24,
          start: "2026-09-05T17:00:00.000Z",
        },
      }),
    ),
    null,
  );
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
  // Combined disjoint active subsets: pairwise checks pass but four ones cannot
  // fit in active=3 (executing + monitoring_pr + awaiting_operator + retrying).
  assert.equal(
    parseDashboardSummary(
      validSummary({
        counts: {
          ...fixture.counts,
          active: 3,
          executing: 1,
          monitoring_pr: 1,
          awaiting_operator: 1,
          awaiting_human: 0,
          retrying: 1,
        },
        overlap: {
          ...fixture.overlap,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      }),
    ),
    null,
  );
  // executing + monitoring_pr alone must also stay within active.
  assert.equal(
    parseDashboardSummary(
      validSummary({
        counts: {
          ...fixture.counts,
          active: 2,
          executing: 2,
          monitoring_pr: 1,
          awaiting_human: 0,
          awaiting_operator: 0,
          retrying: 0,
        },
      }),
    ),
    null,
  );
  // queued (requested) ⊆ active.
  assert.equal(
    parseDashboardSummary(
      validSummary({
        counts: {
          ...fixture.counts,
          active: 1,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 2,
        },
      }),
    ),
    null,
  );
  // queued is disjoint from executing; active=1 cannot hold both ones.
  assert.equal(
    parseDashboardSummary(
      validSummary({
        counts: {
          ...fixture.counts,
          active: 1,
          executing: 1,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 1,
        },
      }),
    ),
    null,
  );
});

test("parseDashboardSummary rejects complete coverage when any count is null", () => {
  // Contract: null counters require coverage.status partial|unknown, not complete.
  assert.equal(
    parseDashboardSummary(validSummary({ counts: { ...fixture.counts, queued: null } })),
    null,
  );
  assert.equal(
    parseDashboardSummary(
      validSummary({
        coverage: { status: "complete", notes: [] },
        counts: { ...fixture.counts, active: null },
      }),
    ),
    null,
  );
  assert.ok(
    parseDashboardSummary(
      validSummary({
        coverage: { status: "partial", notes: ["queued_count_unavailable"] },
        counts: { ...fixture.counts, queued: null },
      }),
    ),
  );
  assert.ok(
    parseDashboardSummary(
      validSummary({
        coverage: { status: "unknown", notes: [] },
        counts: { ...fixture.counts, active: null },
      }),
    ),
  );
});

test("parseDashboardSummary skips subset checks when related counts are null or flags false", () => {
  assert.ok(
    parseDashboardSummary(
      validSummary({
        coverage: { status: "partial", notes: [] },
        counts: { ...fixture.counts, active: null, executing: 5 },
      }),
    ),
  );
  assert.ok(
    parseDashboardSummary(
      validSummary({
        counts: {
          ...fixture.counts,
          // Keep combined disjoint sum within active while skipping the human⊆PR check.
          monitoring_pr: 1,
          awaiting_human: 2,
        },
        overlap: { ...fixture.overlap, awaiting_human_subset_of_monitoring_pr: false },
      }),
    ),
  );
  assert.ok(
    parseDashboardSummary(
      validSummary({
        counts: {
          ...fixture.counts,
          active: 2,
          executing: 2,
          monitoring_pr: 0,
          awaiting_operator: 1,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
        },
        overlap: { ...fixture.overlap, awaiting_operator_in_active_not_executing: false },
      }),
    ),
  );
  assert.ok(
    parseDashboardSummary(
      validSummary({
        coverage: { status: "partial", notes: [] },
        counts: { ...fixture.counts, monitoring_pr: 1, awaiting_human: null },
      }),
    ),
  );
});
