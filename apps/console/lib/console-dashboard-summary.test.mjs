import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  fleetKpisFromDashboardSummary,
  formatDashboardCoverageNotice,
  parseDashboardSummary,
} from "./console-dashboard-summary.ts";

const fixture = JSON.parse(
  readFileSync(
    new URL("../../../docs/console/fixtures/v1/dashboard-summary.local.json", import.meta.url),
    "utf8",
  ),
);

function validSummary(overrides = {}) {
  return structuredClone({ ...fixture, ...overrides });
}

const countKeys = Object.keys(fixture.counts);

function zeroConfirmedCounts() {
  return Object.fromEntries(countKeys.map((key) => [key, 0]));
}

function summaryWithCountEvidence({
  total = 29,
  known = 24,
  unknown = 5,
  confirmed = {},
} = {}) {
  return validSummary({
    coverage: { status: "partial", notes: ["workflow_status_incomplete"] },
    counts: Object.fromEntries(countKeys.map((key) => [key, null])),
    count_evidence: {
      total_workspaces: total,
      status_known_workspaces: known,
      status_unknown_workspaces: unknown,
      confirmed_counts: { ...zeroConfirmedCounts(), ...confirmed },
    },
  });
}

for (const level of [null, "window", "coverage", "counts", "overlap"]) {
  test(`parseDashboardSummary rejects unknown ${level ?? "envelope"} properties`, () => {
    const payload = validSummary();
    const target = level === null ? payload : payload[level];
    target.unpublished_field = "not part of schema v1";
    assert.equal(parseDashboardSummary(payload, "local"), null);
  });
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

test("parseDashboardSummary accepts a first partial snapshot with null last_success_at", () => {
  const firstPartial = JSON.parse(
    readFileSync(
      new URL(
        "../../../docs/console/fixtures/v1/dashboard-summary.no-prior-success.json",
        import.meta.url,
      ),
      "utf8",
    ),
  );
  const parsed = parseDashboardSummary(firstPartial, "hosted");
  assert.ok(parsed);
  assert.equal(parsed.scope, "tenant");
  assert.equal(parsed.last_success_at, null);
  assert.equal(parsed.coverage.status, "partial");
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

test("formatDashboardCoverageNotice surfaces partial and unknown notes", () => {
  const partial = JSON.parse(
    readFileSync(
      new URL("../../../docs/console/fixtures/v1/dashboard-summary.partial.json", import.meta.url),
      "utf8",
    ),
  );
  assert.equal(
    formatDashboardCoverageNotice(partial.coverage),
    "partial coverage — queued count unavailable",
  );
  const noPrior = JSON.parse(
    readFileSync(
      new URL(
        "../../../docs/console/fixtures/v1/dashboard-summary.no-prior-success.json",
        import.meta.url,
      ),
      "utf8",
    ),
  );
  assert.equal(
    formatDashboardCoverageNotice(noPrior.coverage),
    "partial coverage — queued count unavailable; no prior successful snapshot",
  );
  assert.equal(
    formatDashboardCoverageNotice({ status: "unknown", notes: ["provider_lag"] }),
    "coverage unknown — provider lag",
  );
  assert.equal(
    formatDashboardCoverageNotice({ status: "unknown", notes: [] }),
    "coverage unknown — some counts are incomplete",
  );
  assert.equal(
    formatDashboardCoverageNotice({ status: "partial", notes: ["", "  "] }),
    "partial coverage — some counts are incomplete",
  );
  assert.equal(formatDashboardCoverageNotice({ status: "complete", notes: [] }), null);
  assert.equal(formatDashboardCoverageNotice(null), null);
  assert.equal(formatDashboardCoverageNotice(undefined), null);
});

test("parseDashboardSummary accepts absent, null, and valid count evidence", () => {
  const legacy = parseDashboardSummary(validSummary());
  assert.ok(legacy);
  assert.equal("count_evidence" in legacy, false);

  const explicitNull = parseDashboardSummary(validSummary({ count_evidence: null }));
  assert.ok(explicitNull);
  assert.equal(explicitNull.count_evidence, null);

  const allZero = parseDashboardSummary(summaryWithCountEvidence());
  assert.ok(allZero);
  assert.equal(allZero.count_evidence.total_workspaces, 29);
  assert.equal(allZero.count_evidence.status_known_workspaces, 24);
  assert.equal(allZero.count_evidence.status_unknown_workspaces, 5);
  assert.equal(allZero.count_evidence.confirmed_counts.active, 0);

  const oneRunning = parseDashboardSummary(
    summaryWithCountEvidence({
      total: 30,
      known: 25,
      unknown: 5,
      confirmed: { active: 1, executing: 1 },
    }),
  );
  assert.ok(oneRunning);
  assert.equal(oneRunning.counts.active, null);
  assert.equal(oneRunning.count_evidence.confirmed_counts.active, 1);
  assert.equal(oneRunning.count_evidence.confirmed_counts.executing, 1);
});

test("parseDashboardSummary rejects malformed count evidence objects", () => {
  for (const level of ["count_evidence", "confirmed_counts"]) {
    const payload = summaryWithCountEvidence();
    const target = level === "count_evidence"
      ? payload.count_evidence
      : payload.count_evidence.confirmed_counts;
    target.unpublished_field = 0;
    assert.equal(parseDashboardSummary(payload), null, `unknown key at ${level}`);
  }

  for (const [level, key] of [
    ["count_evidence", "total_workspaces"],
    ["count_evidence", "status_known_workspaces"],
    ["count_evidence", "status_unknown_workspaces"],
    ["count_evidence", "confirmed_counts"],
    ...countKeys.map((key) => ["confirmed_counts", key]),
  ]) {
    const payload = summaryWithCountEvidence();
    const target = level === "count_evidence"
      ? payload.count_evidence
      : payload.count_evidence.confirmed_counts;
    delete target[key];
    assert.equal(parseDashboardSummary(payload), null, `missing ${level}.${key}`);
  }
});

test("parseDashboardSummary rejects non-strict count evidence integers", () => {
  for (const path of [
    ["total_workspaces"],
    ["status_known_workspaces"],
    ["status_unknown_workspaces"],
    ["confirmed_counts", "active"],
  ]) {
    for (const invalid of ["1", true, -1, 1.5]) {
      const payload = summaryWithCountEvidence();
      const target = path.length === 1
        ? payload.count_evidence
        : payload.count_evidence.confirmed_counts;
      target[path.at(-1)] = invalid;
      assert.equal(parseDashboardSummary(payload), null, `${path.join(".")}=${invalid}`);
    }
  }
});

test("parseDashboardSummary rejects count evidence contradictions", () => {
  assert.equal(
    parseDashboardSummary(summaryWithCountEvidence({ total: 30, known: 24, unknown: 5 })),
    null,
  );
  assert.equal(
    parseDashboardSummary(summaryWithCountEvidence({ confirmed: { active: 25 } })),
    null,
  );
  assert.equal(
    parseDashboardSummary(summaryWithCountEvidence({
      confirmed: {
        active: 20,
        completed_last_window: 10,
        cancelled_last_window: 10,
        failed_last_window: 10,
      },
    })),
    null,
    "disjoint active and terminal statuses cannot exceed the known population",
  );
  const completeWithUnknownStatus = summaryWithCountEvidence({ total: 1, known: 0, unknown: 1 });
  completeWithUnknownStatus.coverage = { status: "complete", notes: [] };
  completeWithUnknownStatus.counts = zeroConfirmedCounts();
  assert.equal(parseDashboardSummary(completeWithUnknownStatus), null);
  for (const confirmed of [
    { active: 1, executing: 2 },
    { active: 1, monitoring_pr: 2, awaiting_human: 0 },
    { active: 1, queued: 2 },
    { active: 2, monitoring_pr: 1, awaiting_human: 2 },
    { active: 2, executing: 2, awaiting_operator: 1 },
    { active: 2, executing: 2, retrying: 1 },
    { active: 3, executing: 1, monitoring_pr: 1, awaiting_operator: 1, retrying: 1 },
    { active: 1, executing: 1, queued: 1 },
  ]) {
    assert.equal(
      parseDashboardSummary(summaryWithCountEvidence({ confirmed })),
      null,
      `relationship contradiction ${JSON.stringify(confirmed)}`,
    );
  }
});

test("parseDashboardSummary rejects every exact and confirmed count mismatch", () => {
  for (const key of countKeys) {
    const payload = summaryWithCountEvidence();
    payload.counts[key] = 1;
    assert.equal(parseDashboardSummary(payload), null, `mismatch at ${key}`);
  }
});

test("parseDashboardSummary rejects exact counts while any workflow status is unknown", () => {
  for (const key of countKeys) {
    const payload = summaryWithCountEvidence();
    payload.counts[key] = payload.count_evidence.confirmed_counts[key];
    assert.equal(parseDashboardSummary(payload), null, `unproven exact count at ${key}`);
  }
});

test("parseDashboardSummary rejects a positive exact lower bound with one unknown status", () => {
  const payload = summaryWithCountEvidence({
    total: 30,
    known: 29,
    unknown: 1,
    confirmed: { active: 8, executing: 8 },
  });
  payload.counts.active = 8;
  assert.equal(parseDashboardSummary(payload), null);
});

test("confirmed KPI lower bounds stay qualified while exact values win", () => {
  // Production-shaped 29/24/5 evidence: confirmed nonzero + evidenced zero → N confirmed.
  const lowerBounds = parseDashboardSummary(
    summaryWithCountEvidence({ confirmed: { active: 1, executing: 1 } }),
  );
  assert.ok(lowerBounds);
  assert.equal(lowerBounds.count_evidence.total_workspaces, 29);
  assert.equal(lowerBounds.count_evidence.status_known_workspaces, 24);
  assert.equal(lowerBounds.count_evidence.status_unknown_workspaces, 5);
  const lowerBoundKpis = fleetKpisFromDashboardSummary({
    summary: lowerBounds,
    summaryStale: true,
    saturation: null,
    saturationStale: false,
    showCapacity: false,
    includeSummary: true,
  });
  const active = lowerBoundKpis.find((item) => item.id === "active");
  const monitoring = lowerBoundKpis.find((item) => item.id === "monitoring_pr");
  const completed = lowerBoundKpis.find((item) => item.id === "completed");
  assert.deepEqual(
    { value: active.value, suffix: active.suffix, stale: active.stale },
    { value: 1, suffix: " confirmed", stale: true },
  );
  assert.deepEqual(
    { value: monitoring.value, suffix: monitoring.suffix },
    { value: 0, suffix: " confirmed" },
  );
  assert.match(active.hint, /exact metric count is incomplete/);
  assert.match(completed.hint, /last 24h/);
  assert.match(completed.hint, /exact metric count is incomplete/);
  assert.equal(lowerBounds.counts.active, null);

  const exactPayload = summaryWithCountEvidence({
    total: 1,
    known: 1,
    unknown: 0,
    confirmed: { active: 1, executing: 1 },
  });
  exactPayload.counts.active = 1;
  exactPayload.counts.monitoring_pr = 0;
  const exactSummary = parseDashboardSummary(exactPayload);
  assert.ok(exactSummary);
  const exactKpis = fleetKpisFromDashboardSummary({
    summary: exactSummary,
    summaryStale: false,
    saturation: null,
    saturationStale: false,
    showCapacity: false,
    includeSummary: true,
  });
  assert.deepEqual(
    {
      value: exactKpis.find((item) => item.id === "active").value,
      suffix: exactKpis.find((item) => item.id === "active").suffix,
    },
    { value: 1, suffix: undefined },
  );
  assert.deepEqual(
    {
      value: exactKpis.find((item) => item.id === "monitoring_pr").value,
      suffix: exactKpis.find((item) => item.id === "monitoring_pr").suffix,
    },
    { value: 0, suffix: undefined },
  );
});

test("confirmed KPI hints describe metric gaps when every workflow status is known", () => {
  const summary = parseDashboardSummary(
    summaryWithCountEvidence({ total: 1, known: 1, unknown: 0, confirmed: { active: 1 } }),
  );
  assert.ok(summary);
  summary.coverage.notes = ["terminal_timestamp_unavailable", "attention_evidence_unavailable"];

  const kpis = fleetKpisFromDashboardSummary({
    summary,
    summaryStale: false,
    saturation: null,
    saturationStale: false,
    showCapacity: false,
    includeSummary: true,
  });
  for (const id of ["active", "completed"]) {
    const hint = kpis.find((item) => item.id === id).hint;
    assert.match(hint, /exact metric count is incomplete/);
    assert.doesNotMatch(hint, /project total is incomplete/);
  }
});

test("missing count evidence keeps null KPIs as honest dashes", () => {
  const summary = parseDashboardSummary(
    validSummary({
      coverage: { status: "partial", notes: ["queued_count_unavailable"] },
      counts: { ...fixture.counts, queued: null },
    }),
  );
  assert.ok(summary);
  const queued = fleetKpisFromDashboardSummary({
    summary,
    summaryStale: false,
    saturation: null,
    saturationStale: false,
    showCapacity: false,
    includeSummary: true,
  }).find((item) => item.id === "queued");
  assert.deepEqual(
    { value: queued.value, suffix: queued.suffix, hint: queued.hint },
    { value: "—", suffix: undefined, hint: undefined },
  );
});

test("coverage notice quantifies statuses without hiding other evidence gaps", () => {
  const parsed = parseDashboardSummary(
    summaryWithCountEvidence({ total: 1, known: 1, unknown: 0 }),
  );
  assert.ok(parsed);
  parsed.coverage.notes = ["terminal_timestamp_unavailable", "attention_evidence_unavailable"];
  assert.equal(
    formatDashboardCoverageNotice(parsed.coverage, parsed.count_evidence),
    "partial coverage — 1 of 1 workflow statuses known; 0 unknown; terminal timestamp unavailable; attention evidence unavailable",
  );
  assert.equal(
    formatDashboardCoverageNotice(
      { status: "unknown", notes: ["provider_lag"] },
      {
        total_workspaces: 29,
        status_known_workspaces: 24,
        status_unknown_workspaces: 5,
        confirmed_counts: zeroConfirmedCounts(),
      },
    ),
    "coverage unknown — 24 of 29 workflow statuses known; 5 unknown; provider lag",
  );
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

test("parseDashboardSummary allows null last_success_at until a successful snapshot exists", () => {
  for (const status of ["partial", "unknown"]) {
    const parsed = parseDashboardSummary(
      validSummary({
        last_success_at: null,
        coverage: { status, notes: ["no_prior_successful_snapshot"] },
        counts: { ...fixture.counts, queued: status === "partial" ? null : fixture.counts.queued },
      }),
    );
    assert.ok(parsed);
    assert.equal(parsed.last_success_at, null);
  }
  // Field stays required: omit, non-timestamp, and complete+null fail closed.
  const omitted = validSummary();
  delete omitted.last_success_at;
  assert.equal(parseDashboardSummary(omitted), null);
  assert.equal(parseDashboardSummary(validSummary({ last_success_at: null })), null);
  assert.equal(parseDashboardSummary(validSummary({ last_success_at: 0 })), null);
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

test("parseDashboardSummary skips subset checks when related counts are null", () => {
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
        coverage: { status: "partial", notes: [] },
        counts: { ...fixture.counts, monitoring_pr: 1, awaiting_human: null },
      }),
    ),
  );
});

test("parseDashboardSummary rejects false overlap invariant flags", () => {
  assert.equal(
    parseDashboardSummary(
      validSummary({
        counts: {
          ...fixture.counts,
          monitoring_pr: 1,
          awaiting_human: 2,
        },
        overlap: { ...fixture.overlap, awaiting_human_subset_of_monitoring_pr: false },
      }),
    ),
    null,
  );
  assert.equal(
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
    null,
  );
  assert.equal(
    parseDashboardSummary(
      validSummary({
        overlap: { ...fixture.overlap, retrying_in_active_not_executing: false },
      }),
    ),
    null,
  );
});
