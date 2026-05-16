import assert from "node:assert/strict";
import test from "node:test";

import {
  summarizeCoordinationWarnings,
  summarizeVisibleCoordinationWarnings,
} from "./coordination-format.ts";

test("summarizeCoordinationWarnings handles empty warning lists", () => {
  assert.deepEqual(summarizeCoordinationWarnings([]), {
    count: 0,
    label: "none",
    detail: "no coordination warnings",
    overflowCount: 0,
    warnings: [],
  });
});

test("summarizeCoordinationWarnings describes an advisory overlap", () => {
  const summary = summarizeCoordinationWarnings([
    {
      warning_code: "OWNED_PATH_OVERLAP_RISK",
      message: "Owned paths overlap active workspaces.",
      severity: "advisory",
      blocks_launch: false,
      workspace_ids: ["ws_existing"],
      overlaps: [
        {
          workspace_id: "ws_existing",
          existing_path: "src/awf/service/**",
          requested_path: "src/awf/service/workspaces.py",
          match_reason_code: "OWNED_PATH_WILDCARD_MATCH",
          explanation: "Wildcard owned-path prefixes overlap.",
        },
      ],
      stale_policy_context: {
        trigger_type: "path_overlap",
        stale_reason_code: "STALE_OVERLAP",
      },
      overlap_count: 1,
      overlaps_truncated: false,
    },
  ]);

  assert.equal(summary.count, 1);
  assert.equal(summary.label, "1 advisory overlap");
  assert.equal(
    summary.detail,
    "OWNED_PATH_OVERLAP_RISK / ws_existing src/awf/service/** -> src/awf/service/workspaces.py / STALE_OVERLAP",
  );
});

test("summarizeCoordinationWarnings reports truncation", () => {
  const summary = summarizeCoordinationWarnings(
    [
      {
        warning_code: "OWNED_PATH_OVERLAP_RISK",
        message: "Owned paths overlap active workspaces.",
        severity: "advisory",
        blocks_launch: false,
        workspace_ids: ["ws_one"],
        overlaps: [
          {
            workspace_id: "ws_one",
            existing_path: "a/**",
            requested_path: "a/file.ts",
          },
        ],
        stale_policy_context: {},
        overlap_count: 1,
        overlaps_truncated: false,
      },
      {
        warning_code: "OWNED_PATH_OVERLAP_RISK",
        message: "Owned paths overlap active workspaces.",
        severity: "advisory",
        blocks_launch: false,
        workspace_ids: ["ws_two"],
        overlaps: [],
        stale_policy_context: {},
        overlap_count: 0,
        overlaps_truncated: true,
      },
    ],
    { maxWarnings: 1 },
  );

  assert.equal(summary.count, 2);
  assert.equal(summary.label, "2 advisory overlaps");
  assert.equal(summary.overflowCount, 1);
  assert.match(summary.detail, /\+1 more/);
});

test("summarizeCoordinationWarnings uses a generic label for mixed warning severities", () => {
  const summary = summarizeCoordinationWarnings([
    {
      warning_code: "OWNED_PATH_OVERLAP_RISK",
      message: "Owned paths overlap active workspaces.",
      severity: "advisory",
      blocks_launch: false,
      workspace_ids: ["ws_one"],
      overlaps: [
        {
          workspace_id: "ws_one",
          existing_path: "a/**",
          requested_path: "a/file.ts",
        },
      ],
      stale_policy_context: {},
      overlap_count: 1,
      overlaps_truncated: false,
    },
    {
      warning_code: "EXCLUSIVE_LOCK_CONFLICT",
      message: "An exclusive coordination lock conflicts with this workspace.",
      severity: "blocking",
      blocks_launch: true,
      workspace_ids: ["ws_two"],
      overlaps: [],
      stale_policy_context: {},
      overlap_count: 0,
      overlaps_truncated: false,
    },
  ]);

  assert.equal(summary.count, 2);
  assert.equal(summary.label, "2 coordination warnings");
});

test("summarizeVisibleCoordinationWarnings hides advisory overlaps for completed workspaces", () => {
  const warnings = [
    {
      warning_code: "OWNED_PATH_OVERLAP_RISK",
      message: "Owned paths overlap active workspaces.",
      severity: "advisory",
      blocks_launch: false,
      workspace_ids: ["ws_existing"],
      overlaps: [
        {
          workspace_id: "ws_existing",
          existing_path: "src/awf/service/**",
          requested_path: "src/awf/service/workspaces.py",
        },
      ],
      stale_policy_context: {
        stale_reason_code: "STALE_OVERLAP",
      },
      overlap_count: 1,
      overlaps_truncated: false,
    },
  ];

  assert.equal(summarizeVisibleCoordinationWarnings(warnings, "running").count, 1);
  assert.deepEqual(summarizeVisibleCoordinationWarnings(warnings, "completed"), {
    count: 0,
    label: "none",
    detail: "no coordination warnings",
    overflowCount: 0,
    warnings: [],
  });
});
