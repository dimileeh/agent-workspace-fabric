import assert from "node:assert/strict";
import test from "node:test";

import {
  controlUnsupportedReason,
  isControlAvailable,
  isWidgetAvailable,
  parseConsoleCapabilities,
} from "./console-capabilities.ts";
import {
  fleetKpisFromDashboardSummary,
  parseDashboardSummary,
} from "./console-dashboard-summary.ts";

const localCapabilities = {
  schema_version: 1,
  backend_kind: "local",
  generated_at: "2026-09-06T17:00:00Z",
  widgets: [
    {
      id: "fleet_summary",
      availability: "available",
      route: "/v1/console/dashboard-summary",
      semantics: "fleet",
    },
    {
      id: "resource_capacity",
      availability: "available",
      route: "/v1/metrics/resources/saturation",
      semantics: "capacity",
    },
    {
      id: "cloud_runtime",
      availability: "unsupported",
      reason_code: "backend_kind_local",
      message: "hosted only",
      semantics: "cloud",
    },
  ],
  diagnostics: [],
  controls: [
    { id: "cancel", availability: "available", semantics: "cancel" },
    {
      id: "remonitor",
      availability: "unsupported",
      reason_code: "policy_disabled",
      message: "remonitor disabled",
      semantics: "remonitor",
    },
  ],
};

test("parseConsoleCapabilities accepts schema v1", () => {
  const parsed = parseConsoleCapabilities(localCapabilities);
  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.equal(parsed.capabilities.backend_kind, "local");
});

test("parseConsoleCapabilities fails closed on unknown version", () => {
  const parsed = parseConsoleCapabilities({ ...localCapabilities, schema_version: 99 });
  assert.equal(parsed.ok, false);
  if (parsed.ok) return;
  assert.equal(parsed.kind, "unknown_version");
});

test("parseConsoleCapabilities distinguishes auth denial", () => {
  const parsed = parseConsoleCapabilities(null, { status: 401 });
  assert.equal(parsed.ok, false);
  if (parsed.ok) return;
  assert.equal(parsed.kind, "auth_denied");
});

test("widget and control gating helpers", () => {
  const parsed = parseConsoleCapabilities(localCapabilities);
  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.equal(isWidgetAvailable(parsed.capabilities, "resource_capacity"), true);
  assert.equal(isWidgetAvailable(parsed.capabilities, "cloud_runtime"), false);
  assert.equal(isControlAvailable(parsed.capabilities, "cancel"), true);
  assert.equal(isControlAvailable(parsed.capabilities, "remonitor"), false);
  assert.equal(controlUnsupportedReason(parsed.capabilities, "remonitor"), "remonitor disabled");
});

test("parseConsoleCapabilities rejects available widget without route", () => {
  const parsed = parseConsoleCapabilities({
    ...localCapabilities,
    widgets: [{ id: "fleet_summary", availability: "available", semantics: "fleet" }],
  });
  assert.equal(parsed.ok, false);
  if (parsed.ok) return;
  assert.equal(parsed.kind, "malformed");
});

test("parseConsoleCapabilities rejects control missing id or availability", () => {
  const parsed = parseConsoleCapabilities({
    ...localCapabilities,
    controls: [{ availability: "available", semantics: "cancel" }],
  });
  assert.equal(parsed.ok, false);
  if (parsed.ok) return;
  assert.equal(parsed.kind, "malformed");
});

test("fleet KPIs come from dashboard summary and preserve null as dash", () => {
  const kpis = fleetKpisFromDashboardSummary({
    summary: {
      schema_version: 1,
      scope: "local",
      generated_at: "2026-09-06T17:00:00Z",
      as_of: "2026-09-06T17:00:00Z",
      last_success_at: "2026-09-06T17:00:00Z",
      window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
      coverage: { status: "partial", notes: ["queued_count_unavailable"] },
      counts: {
        active: 5,
        executing: 4,
        monitoring_pr: 1,
        awaiting_operator: 1,
        awaiting_human: 0,
        retrying: 0,
        queued: null,
        completed_last_window: 3,
        cancelled_last_window: null,
        failed_last_window: 2,
      },
      overlap: {
        awaiting_human_subset_of_monitoring_pr: true,
        awaiting_operator_in_active_not_executing: true,
        retrying_in_active_not_executing: true,
      },
    },
    summaryStale: false,
    saturation: null,
    saturationStale: false,
    showCapacity: false,
  });
  const byId = Object.fromEntries(kpis.map((kpi) => [kpi.id, kpi]));
  assert.equal(byId.running.value, 4);
  assert.equal(byId.queued.value, "—");
  assert.equal(byId.cancelled.value, "—");
  assert.equal(byId.active.value, 5);
  assert.equal(kpis.some((kpi) => kpi.id === "capacity"), false);
});

test("parseDashboardSummary rejects incomplete counts or missing window", () => {
  assert.equal(
    parseDashboardSummary({
      schema_version: 1,
      scope: "local",
      generated_at: "2026-09-06T17:00:00Z",
      as_of: "2026-09-06T17:00:00Z",
      last_success_at: "2026-09-06T17:00:00Z",
      window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
      coverage: { status: "complete", notes: [] },
      counts: {},
      overlap: {
        awaiting_human_subset_of_monitoring_pr: true,
        awaiting_operator_in_active_not_executing: true,
        retrying_in_active_not_executing: true,
      },
    }),
    null,
  );
  assert.equal(
    parseDashboardSummary({
      schema_version: 1,
      scope: "local",
      generated_at: "2026-09-06T17:00:00Z",
      as_of: "2026-09-06T17:00:00Z",
      last_success_at: "2026-09-06T17:00:00Z",
      coverage: { status: "complete", notes: [] },
      counts: {
        active: 1,
        executing: 1,
        monitoring_pr: 0,
        awaiting_operator: 0,
        awaiting_human: 0,
        retrying: 0,
        queued: 0,
        completed_last_window: 0,
        cancelled_last_window: 0,
        failed_last_window: 0,
      },
      overlap: {
        awaiting_human_subset_of_monitoring_pr: true,
        awaiting_operator_in_active_not_executing: true,
        retrying_in_active_not_executing: true,
      },
    }),
    null,
  );
});

test("fleet KPIs mark stale when showing last-successful summary after outage", () => {
  const kpis = fleetKpisFromDashboardSummary({
    summary: {
      schema_version: 1,
      scope: "local",
      generated_at: "2026-09-06T17:00:00Z",
      as_of: "2026-09-06T17:00:00Z",
      last_success_at: "2026-09-06T17:00:00Z",
      window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
      coverage: { status: "complete", notes: [] },
      counts: {
        active: 9,
        executing: 4,
        monitoring_pr: 1,
        awaiting_operator: 0,
        awaiting_human: 0,
        retrying: 0,
        queued: 0,
        completed_last_window: 0,
        cancelled_last_window: 0,
        failed_last_window: 0,
      },
      overlap: {
        awaiting_human_subset_of_monitoring_pr: true,
        awaiting_operator_in_active_not_executing: true,
        retrying_in_active_not_executing: true,
      },
    },
    summaryStale: true,
    saturation: null,
    saturationStale: false,
    showCapacity: false,
  });
  const byId = Object.fromEntries(kpis.map((kpi) => [kpi.id, kpi]));
  assert.equal(byId.active.value, 9);
  assert.equal(byId.active.stale, true);
  assert.equal(byId.running.stale, true);
});
