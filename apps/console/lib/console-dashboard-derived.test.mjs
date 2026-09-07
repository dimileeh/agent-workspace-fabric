import assert from "node:assert/strict";
import test from "node:test";

import {
  capabilityFeedWithdrawalCleared,
  filterAndSortOverview,
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
  const filtered = filterAndSortOverview(overview, {
    searchText: "awf-key-137",
    statusFilters: [],
    agentFilters: [],
    modelFilters: [],
    sortKey: "updated_at",
    sortDirection: "desc",
  });
  assert.deepEqual(
    filtered.map((item) => item.workspace_id),
    ["w-match"],
  );
});
