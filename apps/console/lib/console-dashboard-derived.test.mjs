import assert from "node:assert/strict";
import test from "node:test";

import {
  allGatedDetailFeedsDropped,
  capabilityFeedWithdrawalCleared,
  DROP_ALL_GATED_DETAIL_FEEDS,
  gatedDetailDropFromWithdrawal,
  gatedDetailDropsSince,
  inspectorDetailFeedWithdrawn,
  noteGatedDetailDrop,
  filterAndSortOverview,
  overviewSearchText,
  orderFullscreenWorkspaceIds,
  planCapabilityFeedWithdrawal,
  resolveDashboardPanelVisibility,
} from "./console-dashboard-derived.ts";

const localCaps = {
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
  ],
  diagnostics: [
    {
      id: "merge_queue",
      availability: "available",
      route: "/v1/merge-queue",
      semantics: "merge",
    },
    {
      id: "workspace_logs",
      availability: "available",
      route: "/v1/workspaces/{workspace_id}/logs",
      semantics: "logs",
    },
    {
      id: "workspace_stream",
      availability: "available",
      route: "/v1/workspaces/{workspace_id}/stream",
      semantics: "stream",
    },
  ],
  controls: [],
};

test("planCapabilityFeedWithdrawal clears withdrawn fleet_summary", () => {
  const next = {
    ...localCaps,
    widgets: [
      {
        id: "fleet_summary",
        availability: "unsupported",
        reason_code: "policy_disabled",
        message: "withdrawn",
        semantics: "fleet",
      },
      {
        id: "resource_capacity",
        availability: "available",
        route: "/v1/metrics/resources/saturation",
        semantics: "capacity",
      },
    ],
  };
  const plan = planCapabilityFeedWithdrawal(localCaps, next);
  assert.equal(plan.clearDashboardSummary, true);
  assert.equal(plan.clearResourceCapacity, false);
  assert.equal(capabilityFeedWithdrawalCleared(plan), true);
  assert.equal(inspectorDetailFeedWithdrawn(plan), false);
  assert.equal(allGatedDetailFeedsDropped(gatedDetailDropFromWithdrawal(plan)), false);
});

test("planCapabilityFeedWithdrawal drops only withdrawn inspector diagnostics", () => {
  const previous = {
    ...localCaps,
    diagnostics: [
      ...localCaps.diagnostics,
      {
        id: "workspace_runtime",
        availability: "available",
        route: "/v1/workspaces/{workspace_id}/runtime",
        semantics: "runtime",
      },
      {
        id: "workspace_events",
        availability: "available",
        route: "/v1/workspaces/{workspace_id}/events",
        semantics: "events",
      },
    ],
  };
  const next = {
    ...previous,
    diagnostics: previous.diagnostics.map((item) =>
      item.id === "workspace_runtime"
        ? {
            ...item,
            availability: "unsupported",
            reason_code: "not_implemented",
            message: "runtime withdrawn",
          }
        : item,
    ),
  };
  const plan = planCapabilityFeedWithdrawal(previous, next);
  const dropped = gatedDetailDropFromWithdrawal(plan);
  assert.equal(inspectorDetailFeedWithdrawn(plan), true);
  assert.equal(dropped.runtime, true);
  assert.equal(dropped.events, false);
  assert.equal(dropped.operations, false);
  assert.equal(dropped.logs, false);
  assert.equal(allGatedDetailFeedsDropped(dropped), false);
});

test("gated-detail drop log unions bumps since the captured generation", () => {
  const stamps = { current: [] };
  const generation = { current: 0 };
  const capturedBeforeAnyDrop = generation.current;

  noteGatedDetailDrop(stamps, generation, {
    runtime: true,
    events: false,
    operations: false,
    logs: false,
  });
  const capturedAfterRuntimeDrop = generation.current;
  noteGatedDetailDrop(stamps, generation, DROP_ALL_GATED_DETAIL_FEEDS);
  const capturedAfterDropAll = generation.current;
  noteGatedDetailDrop(stamps, generation, {
    runtime: false,
    events: true,
    operations: false,
    logs: false,
  });

  const spannedBoth = gatedDetailDropsSince(stamps.current, capturedBeforeAnyDrop);
  assert.equal(allGatedDetailFeedsDropped(spannedBoth), true);

  const startedAfterDropAll = gatedDetailDropsSince(stamps.current, capturedAfterDropAll);
  assert.equal(startedAfterDropAll.runtime, false);
  assert.equal(startedAfterDropAll.events, true);
  assert.equal(startedAfterDropAll.operations, false);
  assert.equal(startedAfterDropAll.logs, false);

  const startedAfterRuntime = gatedDetailDropsSince(stamps.current, capturedAfterRuntimeDrop);
  assert.equal(allGatedDetailFeedsDropped(startedAfterRuntime), true);
  assert.equal(generation.current, 3);
  // A generation mismatch with no surviving stamp must not fail open.
  assert.equal(allGatedDetailFeedsDropped(gatedDetailDropsSince([], capturedBeforeAnyDrop)), true);
});

test("gated-detail drop log fails closed when an earlier bump was truncated", () => {
  const stamps = { current: [] };
  const generation = { current: 0 };
  for (let index = 0; index < 40; index += 1) {
    noteGatedDetailDrop(stamps, generation, {
      runtime: index === 0,
      events: false,
      operations: false,
      logs: false,
    });
  }
  const dropped = gatedDetailDropsSince(stamps.current, 0);
  assert.equal(allGatedDetailFeedsDropped(dropped), true);
  assert.equal(stamps.current.length <= 32, true);
  assert.equal(stamps.current[0].generation > 1, true);
});

test("resolveDashboardPanelVisibility gates fullscreen stream on logs", () => {
  const visible = resolveDashboardPanelVisibility(localCaps);
  assert.equal(visible.showCapacitySection, true);
  assert.equal(visible.showMergeQueue, true);
  assert.equal(visible.allowFullscreenLogs, true);
  assert.equal(visible.allowFullscreenStreamLogs, true);

  const noLogs = {
    ...localCaps,
    diagnostics: localCaps.diagnostics.map((item) =>
      item.id === "workspace_logs"
        ? {
            ...item,
            availability: "unsupported",
            reason_code: "policy_disabled",
            message: "no logs",
            route: undefined,
          }
        : item,
    ),
  };
  const gated = resolveDashboardPanelVisibility(noLogs);
  assert.equal(gated.allowFullscreenLogs, false);
  assert.equal(gated.allowFullscreenStreamLogs, false);
});

test("filterAndSortOverview and orderFullscreenWorkspaceIds preserve selection order", () => {
  const overview = [
    {
      workspace_id: "w2",
      task_id: "t2",
      title: "Beta",
      repo_url: "https://example.com/b",
      base_branch: "main",
      agent: "cursor",
      agent_model: "auto",
      agent_effort: null,
      status: "running",
      created_at: "2026-09-06T17:00:00Z",
      updated_at: "2026-09-06T18:00:00Z",
      recovery: null,
    },
    {
      workspace_id: "w1",
      task_id: "t1",
      title: "Alpha",
      repo_url: "https://example.com/a",
      base_branch: "main",
      agent: "cursor",
      agent_model: "auto",
      agent_effort: null,
      status: "ready",
      created_at: "2026-09-06T16:00:00Z",
      updated_at: "2026-09-06T17:30:00Z",
      recovery: null,
    },
  ];
  const filtered = filterAndSortOverview(overview, {
    searchText: "alpha",
    statusFilters: [],
    agentFilters: [],
    modelFilters: [],
    sortKey: "updated_at",
    sortDirection: "desc",
  });
  assert.equal(filtered.length, 1);
  assert.equal(filtered[0].workspace_id, "w1");
  assert.deepEqual(orderFullscreenWorkspaceIds(["w1", "w2"], filtered), ["w1", "w2"]);
});

test("filterAndSortOverview matches task_key shown on workspace cards", () => {
  const overview = [
    {
      workspace_id: "w-other",
      task_id: "t-other",
      title: "Unrelated title",
      task_key: "TASK-other",
      repo_url: "https://example.com/other",
      base_branch: "main",
      agent: "cursor",
      agent_model: "auto",
      agent_effort: null,
      status: "running",
      created_at: "2026-09-06T17:00:00Z",
      updated_at: "2026-09-06T18:00:00Z",
      recovery: null,
    },
    {
      workspace_id: "w-match",
      task_id: "t-match",
      title: "Visible card",
      task_key: "AWF-KEY-137",
      repo_url: "https://example.com/match",
      base_branch: "main",
      agent: "cursor",
      agent_model: "auto",
      agent_effort: null,
      status: "ready",
      created_at: "2026-09-06T16:00:00Z",
      updated_at: "2026-09-06T17:30:00Z",
      recovery: null,
    },
    {
      workspace_id: "w-missing",
      task_id: "t-missing",
      title: "No key",
      task_key: null,
      repo_url: "https://example.com/missing",
      base_branch: "main",
      agent: "cursor",
      agent_model: null,
      agent_effort: null,
      status: "failed",
      created_at: "2026-09-06T15:00:00Z",
      updated_at: "2026-09-06T17:00:00Z",
      recovery: null,
    },
  ];
  const byFullKey = filterAndSortOverview(overview, {
    searchText: "  AWF-KEY-137  ",
    statusFilters: [],
    agentFilters: [],
    modelFilters: [],
    sortKey: "updated_at",
    sortDirection: "desc",
  });
  assert.deepEqual(
    byFullKey.map((item) => item.workspace_id),
    ["w-match"],
  );
  // The visible key must match even when no legacy field contains the query.
  const byKeyFragment = filterAndSortOverview(overview, {
    searchText: "key-137",
    statusFilters: [],
    agentFilters: [],
    modelFilters: [],
    sortKey: "updated_at",
    sortDirection: "desc",
  });
  assert.deepEqual(
    byKeyFragment.map((item) => item.workspace_id),
    ["w-match"],
  );
  assert.equal(overviewSearchText(overview[1]).includes("awf-key-137"), true);
  assert.equal(overviewSearchText(overview[2]).includes("awf-key-137"), false);
});
