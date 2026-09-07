import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  capabilityIdentityKey,
  capabilityRouteToAwfPath,
  controlUnsupportedReason,
  isControlAvailable,
  isWidgetAvailable,
  parseConsoleCapabilities,
  resolveCapabilityWorkspaceRoute,
  capabilitiesForMutatingControls,
  resolveRetryCapabilityGate,
  resolveWorkspaceLogStreamAccess,
} from "./console-capabilities.ts";
import { fleetKpisFromDashboardSummary, parseDashboardSummary } from "./console-dashboard-summary.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const IDENTITY_MATRIX = JSON.parse(
  readFileSync(
    join(HERE, "../../../docs/console/fixtures/v1/capabilities.identity-matrix.json"),
    "utf8",
  ),
);
const ROUTE_MATRIX = JSON.parse(
  readFileSync(
    join(HERE, "../../../docs/console/fixtures/v1/capabilities.route-matrix.json"),
    "utf8",
  ),
);
const NEGATIVE_MATRIX = JSON.parse(
  readFileSync(
    join(HERE, "../../../docs/console/fixtures/v1/capabilities.negative-matrix.json"),
    "utf8",
  ),
);

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
    { id: "retry", availability: "available", semantics: "retry" },
  ],
};

const hostedCapabilities = {
  schema_version: 1,
  backend_kind: "hosted",
  generated_at: "2026-09-06T17:00:00Z",
  identity: {
    backend_id: "awf-cloud-tenant-a",
    scope: "tenant",
    tenant_id: "tenant_a",
  },
  widgets: [
    {
      id: "fleet_summary",
      availability: "available",
      route: "/v1/console/dashboard-summary",
      semantics: "fleet",
    },
    {
      id: "cloud_runtime",
      availability: "available",
      route: "/v1/console/cloud-runtime",
      semantics: "cloud",
    },
  ],
  diagnostics: [],
  controls: [{ id: "cancel", availability: "available", semantics: "cancel" }],
};

test("parseConsoleCapabilities accepts schema v1", () => {
  const parsed = parseConsoleCapabilities(localCapabilities);
  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.equal(parsed.capabilities.backend_kind, "local");
});

test("parseConsoleCapabilities rejects missing or unparseable generated_at", () => {
  const { generated_at: _drop, ...without } = localCapabilities;
  const missing = parseConsoleCapabilities(without);
  assert.equal(missing.ok, false);
  if (missing.ok) return;
  assert.equal(missing.kind, "malformed");

  for (const generated_at of ["", "not-a-date", "   ", "Invalid Date", 123]) {
    const parsed = parseConsoleCapabilities({ ...localCapabilities, generated_at });
    assert.equal(parsed.ok, false, `expected reject for generated_at=${JSON.stringify(generated_at)}`);
    if (parsed.ok) return;
    assert.equal(parsed.kind, "malformed");
  }
});

test("parseConsoleCapabilities rejects local identity missing backend_id or scope", () => {
  const missingBackend = parseConsoleCapabilities({
    ...localCapabilities,
    identity: { scope: "local" },
  });
  assert.equal(missingBackend.ok, false);
  if (missingBackend.ok) return;
  assert.equal(missingBackend.kind, "malformed");

  const emptyScope = parseConsoleCapabilities({
    ...localCapabilities,
    identity: { backend_id: "local-awf", scope: "" },
  });
  assert.equal(emptyScope.ok, false);
  if (emptyScope.ok) return;
  assert.equal(emptyScope.kind, "malformed");
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
  assert.equal(isControlAvailable(parsed.capabilities, "retry"), true);
});

test("capabilitiesForMutatingControls clears inventory while capabilityError is set", () => {
  const parsed = parseConsoleCapabilities(localCapabilities);
  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.equal(capabilitiesForMutatingControls(parsed.capabilities, null), parsed.capabilities);
  assert.equal(capabilitiesForMutatingControls(parsed.capabilities, undefined), parsed.capabilities);
  assert.equal(
    capabilitiesForMutatingControls(parsed.capabilities, "capabilities endpoint unavailable"),
    null,
  );
  assert.equal(capabilitiesForMutatingControls(null, "capabilities endpoint unavailable"), null);
});

test("resolveRetryCapabilityGate fails closed without ready capabilities", () => {
  assert.deepEqual(resolveRetryCapabilityGate({ capabilities: null, capabilitiesReady: false }), {
    enabled: false,
    reason: "waiting for console capabilities",
  });
  assert.deepEqual(resolveRetryCapabilityGate({ capabilities: null, capabilitiesReady: true }), {
    enabled: false,
    reason: "console capabilities unavailable",
  });
});

test("resolveRetryCapabilityGate disables unsupported or omitted retry", () => {
  const unsupported = parseConsoleCapabilities({
    ...localCapabilities,
    controls: [
      {
        id: "retry",
        availability: "unsupported",
        reason_code: "policy_disabled",
        message: "Retry is not available on this backend.",
        semantics: "retry",
      },
    ],
  });
  assert.equal(unsupported.ok, true);
  if (!unsupported.ok) return;
  assert.deepEqual(
    resolveRetryCapabilityGate({
      capabilities: unsupported.capabilities,
      capabilitiesReady: true,
    }),
    { enabled: false, reason: "Retry is not available on this backend." },
  );

  const omitted = parseConsoleCapabilities({
    ...localCapabilities,
    controls: [{ id: "cancel", availability: "available", semantics: "cancel" }],
  });
  assert.equal(omitted.ok, true);
  if (!omitted.ok) return;
  assert.deepEqual(
    resolveRetryCapabilityGate({
      capabilities: omitted.capabilities,
      capabilitiesReady: true,
    }),
    { enabled: false, reason: "unsupported by backend" },
  );
});

test("resolveRetryCapabilityGate enables advertised retry", () => {
  const parsed = parseConsoleCapabilities(localCapabilities);
  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.deepEqual(
    resolveRetryCapabilityGate({
      capabilities: parsed.capabilities,
      capabilitiesReady: true,
    }),
    { enabled: true, reason: null },
  );
});

test("resolveWorkspaceLogStreamAccess fails closed without capabilities", () => {
  assert.deepEqual(resolveWorkspaceLogStreamAccess(null), {
    allowLogs: false,
    allowStream: false,
    allowStreamLogs: false,
  });
  assert.deepEqual(resolveWorkspaceLogStreamAccess(undefined), {
    allowLogs: false,
    allowStream: false,
    allowStreamLogs: false,
  });
});

test("resolveWorkspaceLogStreamAccess mirrors negotiated workspace_logs and workspace_stream", () => {
  const available = parseConsoleCapabilities({
    ...localCapabilities,
    diagnostics: [
      {
        id: "workspace_logs",
        availability: "available",
        route: "/v1/workspaces/{workspace_id}/logs",
        semantics: "Optional workspace log listing.",
      },
      {
        id: "workspace_stream",
        availability: "available",
        route: "/v1/workspaces/{workspace_id}/stream",
        semantics: "Optional workspace live stream.",
      },
    ],
  });
  assert.equal(available.ok, true);
  if (!available.ok) return;
  assert.deepEqual(resolveWorkspaceLogStreamAccess(available.capabilities), {
    allowLogs: true,
    allowStream: true,
    allowStreamLogs: true,
  });

  const unsupported = parseConsoleCapabilities({
    ...localCapabilities,
    diagnostics: [
      {
        id: "workspace_logs",
        availability: "unsupported",
        reason_code: "not_implemented",
        message: "logs unavailable",
        semantics: "Optional workspace log listing.",
      },
      {
        id: "workspace_stream",
        availability: "unsupported",
        reason_code: "not_implemented",
        message: "stream unavailable",
        semantics: "Optional workspace live stream.",
      },
    ],
  });
  assert.equal(unsupported.ok, true);
  if (!unsupported.ok) return;
  assert.deepEqual(resolveWorkspaceLogStreamAccess(unsupported.capabilities), {
    allowLogs: false,
    allowStream: false,
    allowStreamLogs: false,
  });

  const omitted = parseConsoleCapabilities({
    ...localCapabilities,
    diagnostics: [],
  });
  assert.equal(omitted.ok, true);
  if (!omitted.ok) return;
  assert.deepEqual(resolveWorkspaceLogStreamAccess(omitted.capabilities), {
    allowLogs: false,
    allowStream: false,
    allowStreamLogs: false,
  });
});

test("resolveWorkspaceLogStreamAccess keeps stream without silently consuming log frames", () => {
  const streamOnly = parseConsoleCapabilities({
    ...localCapabilities,
    diagnostics: [
      {
        id: "workspace_logs",
        availability: "unsupported",
        reason_code: "not_implemented",
        message: "logs unavailable",
        semantics: "Optional workspace log listing.",
      },
      {
        id: "workspace_stream",
        availability: "available",
        route: "/v1/workspaces/{workspace_id}/stream",
        semantics: "Optional workspace live stream.",
      },
    ],
  });
  assert.equal(streamOnly.ok, true);
  if (!streamOnly.ok) return;
  assert.deepEqual(resolveWorkspaceLogStreamAccess(streamOnly.capabilities), {
    allowLogs: false,
    allowStream: true,
    allowStreamLogs: false,
  });
});

test("resolveCapabilityWorkspaceRoute substitutes workspace id", () => {
  assert.equal(
    resolveCapabilityWorkspaceRoute("/v1/workspaces/{workspace_id}/runtime", "ws_abc"),
    "/v1/workspaces/ws_abc/runtime",
  );
});

const CONSOLE_URL_ENV_KEYS = [
  "NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH",
  "NEXT_PUBLIC_AWF_CONSOLE_API_BASE",
  "NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE",
  "NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS",
];

function snapshotConsoleUrlEnv() {
  return Object.fromEntries(CONSOLE_URL_ENV_KEYS.map((key) => [key, process.env[key]]));
}

function restoreConsoleUrlEnv(previous) {
  for (const key of CONSOLE_URL_ENV_KEYS) {
    if (previous[key] === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = previous[key];
    }
  }
}

test("capabilityRouteToAwfPath uses local /api/awf base by default", () => {
  const previous = snapshotConsoleUrlEnv();
  for (const key of CONSOLE_URL_ENV_KEYS) {
    delete process.env[key];
  }
  try {
    assert.equal(
      capabilityRouteToAwfPath("/v1/console/dashboard-summary"),
      "/api/awf/console/dashboard-summary",
    );
    assert.equal(
      capabilityRouteToAwfPath("/v1/console/cloud-runtime"),
      "/api/awf/console/cloud-runtime",
    );
    assert.equal(capabilityRouteToAwfPath("/other"), "/other");
  } finally {
    restoreConsoleUrlEnv(previous);
  }
});

test("capabilityRouteToAwfPath routes through hosted API base and context keys", () => {
  const previous = snapshotConsoleUrlEnv();
  process.env.NEXT_PUBLIC_AWF_CONSOLE_API_BASE = "/api/core-console";
  process.env.NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS = "org_id,project_id";
  try {
    assert.equal(
      capabilityRouteToAwfPath("/v1/console/dashboard-summary"),
      "/api/core-console/console/dashboard-summary",
    );
    assert.equal(
      capabilityRouteToAwfPath(
        "/v1/console/cloud-runtime",
        "?org_id=org_1&project_id=proj_1",
      ),
      "/api/core-console/console/cloud-runtime?org_id=org_1&project_id=proj_1",
    );
  } finally {
    restoreConsoleUrlEnv(previous);
  }
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

test("parseConsoleCapabilities rejects fleet_summary with non-inventory relative route", () => {
  const parsed = parseConsoleCapabilities({
    ...localCapabilities,
    widgets: [
      {
        id: "fleet_summary",
        availability: "available",
        route: "/v1/workspaces",
        semantics: "fleet",
      },
    ],
  });
  assert.equal(parsed.ok, false);
  if (parsed.ok) return;
  assert.equal(parsed.kind, "malformed");
  assert.match(parsed.message, /fleet_summary.*\/v1\/console\/dashboard-summary/);
});

test("parseConsoleCapabilities rejects unknown widget id even with relative route", () => {
  const parsed = parseConsoleCapabilities({
    ...localCapabilities,
    widgets: [
      {
        id: "evil_widget",
        availability: "available",
        route: "/v1/workspaces",
        semantics: "evil",
      },
    ],
  });
  assert.equal(parsed.ok, false);
  if (parsed.ok) return;
  assert.equal(parsed.kind, "malformed");
  assert.match(parsed.message, /Unknown console widget id=evil_widget/);
});

test("parseConsoleCapabilities rejects duplicate widget ids", () => {
  const parsed = parseConsoleCapabilities({
    ...localCapabilities,
    widgets: [
      {
        id: "fleet_summary",
        availability: "available",
        route: "/v1/console/dashboard-summary",
        semantics: "fleet",
      },
      {
        id: "fleet_summary",
        availability: "available",
        route: "/v1/console/dashboard-summary",
        semantics: "fleet-dup",
      },
    ],
  });
  assert.equal(parsed.ok, false);
  if (parsed.ok) return;
  assert.equal(parsed.kind, "malformed");
  assert.match(parsed.message, /Duplicate console widget id=fleet_summary/);
});

test("parseConsoleCapabilities rejects diagnostic with wrong workspace template", () => {
  const parsed = parseConsoleCapabilities({
    ...localCapabilities,
    diagnostics: [
      {
        id: "workspace_logs",
        availability: "available",
        route: "/v1/workspaces/{workspace_id}/events",
        semantics: "logs",
      },
    ],
  });
  assert.equal(parsed.ok, false);
  if (parsed.ok) return;
  assert.equal(parsed.kind, "malformed");
  assert.match(parsed.message, /workspace_logs.*\/v1\/workspaces\/\{workspace_id\}\/logs/);
});

test("parseConsoleCapabilities rejects available widget without inventory route", () => {
  const parsed = parseConsoleCapabilities({
    ...localCapabilities,
    widgets: [
      {
        id: "telemetry",
        availability: "available",
        route: "/v1/console/telemetry",
        semantics: "telemetry",
      },
    ],
  });
  assert.equal(parsed.ok, false);
  if (parsed.ok) return;
  assert.equal(parsed.kind, "malformed");
  assert.match(parsed.message, /telemetry.*no inventory route/);
});

test("isWidgetAvailable rejects inventory-mismatched routes", () => {
  assert.equal(
    isWidgetAvailable(
      {
        ...localCapabilities,
        widgets: [
          {
            id: "fleet_summary",
            availability: "available",
            route: "/v1/workspaces",
            semantics: "fleet",
          },
        ],
      },
      "fleet_summary",
    ),
    false,
  );
});

test("hosted capabilities require identity with a non-empty tenant_id", () => {
  const ok = parseConsoleCapabilities(hostedCapabilities);
  assert.equal(ok.ok, true);
  if (!ok.ok) return;
  assert.equal(ok.identityKey, "hosted|awf-cloud-tenant-a|tenant|tenant_a");

  const { identity: _omitted, ...withoutIdentity } = hostedCapabilities;
  const omitted = parseConsoleCapabilities(withoutIdentity);
  assert.equal(omitted.ok, false);
  if (omitted.ok) return;
  assert.equal(omitted.kind, "malformed");

  const emptyTenant = parseConsoleCapabilities({
    ...hostedCapabilities,
    identity: { backend_id: "awf-cloud", scope: "tenant", tenant_id: "" },
  });
  assert.equal(emptyTenant.ok, false);
  if (emptyTenant.ok) return;
  assert.equal(emptyTenant.kind, "malformed");

  const nullTenant = parseConsoleCapabilities({
    ...hostedCapabilities,
    identity: { backend_id: "awf-cloud", scope: "tenant", tenant_id: null },
  });
  assert.equal(nullTenant.ok, false);
  if (nullTenant.ok) return;
  assert.equal(nullTenant.kind, "malformed");

  const whitespaceTenant = parseConsoleCapabilities({
    ...hostedCapabilities,
    identity: { backend_id: "awf-cloud", scope: "tenant", tenant_id: "   " },
  });
  assert.equal(whitespaceTenant.ok, false);
  if (whitespaceTenant.ok) return;
  assert.equal(whitespaceTenant.kind, "malformed");
});

test("shared identity matrix matches parseConsoleCapabilities", () => {
  for (const caseRow of IDENTITY_MATRIX.cases) {
    const parsed = parseConsoleCapabilities(caseRow.payload);
    const expectOk = caseRow.expect === "accept";
    assert.equal(
      parsed.ok,
      expectOk,
      `${caseRow.name}: expected ${caseRow.expect}, got ok=${parsed.ok}`,
    );
  }
});

test("shared route inventory matrix matches parseConsoleCapabilities", () => {
  for (const caseRow of ROUTE_MATRIX.cases) {
    const parsed = parseConsoleCapabilities(caseRow.payload);
    const expectOk = caseRow.expect === "accept";
    assert.equal(
      parsed.ok,
      expectOk,
      `${caseRow.name}: expected ${caseRow.expect}, got ok=${parsed.ok}` +
        (parsed.ok ? "" : ` (${parsed.message})`),
    );
  }
});

test("shared negative matrix matches parseConsoleCapabilities", () => {
  assert.deepEqual(NEGATIVE_MATRIX.unsupported_reason_codes, [
    "backend_kind_local",
    "backend_kind_hosted",
    "not_implemented",
    "policy_disabled",
  ]);
  for (const caseRow of NEGATIVE_MATRIX.cases) {
    const parsed = parseConsoleCapabilities(caseRow.payload);
    const expectOk = caseRow.expect === "accept";
    assert.equal(
      parsed.ok,
      expectOk,
      `${caseRow.name}: expected ${caseRow.expect}, got ok=${parsed.ok}` +
        (parsed.ok ? "" : ` (${parsed.message})`),
    );
  }
});

test("parseConsoleCapabilities rejects incomplete unsupported capability reasons", () => {
  const missingReason = parseConsoleCapabilities({
    ...localCapabilities,
    widgets: [
      {
        id: "telemetry",
        availability: "unsupported",
        message: "missing reason",
        semantics: "telemetry",
      },
    ],
  });
  assert.equal(missingReason.ok, false);
  if (missingReason.ok) return;
  assert.equal(missingReason.kind, "malformed");
  assert.match(missingReason.message, /reason_code/);

  const missingMessage = parseConsoleCapabilities({
    ...localCapabilities,
    widgets: [
      {
        id: "telemetry",
        availability: "unsupported",
        reason_code: "not_implemented",
        semantics: "telemetry",
      },
    ],
  });
  assert.equal(missingMessage.ok, false);
  if (missingMessage.ok) return;
  assert.equal(missingMessage.kind, "malformed");
  assert.match(missingMessage.message, /message/);

  const arbitrary = parseConsoleCapabilities({
    ...localCapabilities,
    controls: [
      {
        id: "retry",
        availability: "unsupported",
        reason_code: "made_up",
        message: "nope",
        semantics: "retry",
      },
    ],
  });
  assert.equal(arbitrary.ok, false);
  if (arbitrary.ok) return;
  assert.equal(arbitrary.kind, "malformed");
  assert.match(arbitrary.message, /outside the v1 inventory/);
});

test("capabilityIdentityKey treats whitespace hosted tenant as missing discriminator", () => {
  const blankish = capabilityIdentityKey({
    ...hostedCapabilities,
    identity: { backend_id: "awf-cloud", scope: "tenant", tenant_id: " \t " },
  });
  assert.match(blankish, /^hosted\|missing-tenant-discriminator\|/);
});

test("hosted tenant identity keys differ so feed epochs can advance", () => {
  const tenantA = parseConsoleCapabilities(hostedCapabilities);
  const tenantB = parseConsoleCapabilities({
    ...hostedCapabilities,
    identity: {
      backend_id: "awf-cloud-tenant-b",
      scope: "tenant",
      tenant_id: "tenant_b",
    },
  });
  assert.equal(tenantA.ok, true);
  assert.equal(tenantB.ok, true);
  if (!tenantA.ok || !tenantB.ok) return;
  assert.notEqual(tenantA.identityKey, tenantB.identityKey);
  assert.equal(
    capabilityIdentityKey(tenantA.capabilities),
    "hosted|awf-cloud-tenant-a|tenant|tenant_a",
  );
});

test("capabilityIdentityKey never collapses incomplete hosted identity to hosted|||", () => {
  const omitted = capabilityIdentityKey({
    ...hostedCapabilities,
    identity: undefined,
  });
  assert.notEqual(omitted, "hosted|||");
  assert.match(omitted, /^hosted\|missing-tenant-discriminator\|/);

  const emptyTenant = capabilityIdentityKey({
    ...hostedCapabilities,
    identity: { backend_id: "awf-cloud", scope: "tenant", tenant_id: "" },
  });
  assert.notEqual(emptyTenant, "hosted|||");
  assert.match(emptyTenant, /^hosted\|missing-tenant-discriminator\|/);
});

test("local capabilities still accept omitted identity", () => {
  const parsed = parseConsoleCapabilities({ ...localCapabilities, identity: undefined });
  assert.equal(parsed.ok, true);
  if (!parsed.ok) return;
  assert.equal(parsed.identityKey, "local|||");
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

test("parseDashboardSummary rejects unparseable timestamps", () => {
  const base = {
    schema_version: 1,
    scope: "local",
    generated_at: "2026-09-06T17:00:00Z",
    as_of: "2026-09-06T17:00:00Z",
    last_success_at: "2026-09-06T17:00:00Z",
    window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
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
  };
  assert.ok(parseDashboardSummary(base));
  for (const key of ["generated_at", "as_of", "last_success_at"]) {
    assert.equal(
      parseDashboardSummary({ ...base, [key]: "not-a-date" }),
      null,
      `expected reject for ${key}=not-a-date`,
    );
  }
  assert.equal(
    parseDashboardSummary({
      ...base,
      window: { ...base.window, start: "not-a-date" },
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
