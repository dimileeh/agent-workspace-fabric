import assert from "node:assert/strict";
import test from "node:test";

import {
  OVERVIEW_LIST_MAX_PAGES,
  OVERVIEW_LIST_PAGE_SIZE,
  OVERVIEW_REFRESH_BATCH_SIZE,
  appendUniqueOverviewItems,
  collectOverviewPages,
  overviewItemMatchesQuery,
  overviewListPath,
  reconcileOverviewFirstPage,
  reconcileOverviewRetainedItems,
  retainedOverviewIdBatch,
  usableContinuationCursor,
} from "./overview-list.ts";

function page(items, { has_more = false, next_cursor = null } = {}) {
  return { items, has_more, next_cursor };
}

test("overviewListPath includes limit and omits empty cursor", () => {
  assert.equal(
    overviewListPath({}),
    `/api/awf/workspaces/overview?limit=${OVERVIEW_LIST_PAGE_SIZE}`,
  );
  assert.equal(
    overviewListPath({ status: "running", agent: "codex", repo_url: "https://example.com/r.git" }, "c1"),
    `/api/awf/workspaces/overview?limit=${OVERVIEW_LIST_PAGE_SIZE}&status=running&agent=codex&repo_url=https%3A%2F%2Fexample.com%2Fr.git&cursor=c1`,
  );
});

test("overviewItemMatchesQuery applies every overview query filter", () => {
  const item = {
    workspace_id: "ws_1",
    status: "running",
    agent: "codex",
    repo_url: "https://example.com/awf.git",
  };
  const cases = [
    [{ statusFilters: [], agentFilters: [], repoFilter: "" }, true],
    [{
      statusFilters: ["running", "completed"],
      agentFilters: ["codex", "claude_code"],
      repoFilter: " https://example.com/awf.git ",
    }, true],
    [{ statusFilters: ["completed"], agentFilters: [], repoFilter: "" }, false],
    [{ statusFilters: [], agentFilters: ["claude_code"], repoFilter: "" }, false],
    [{ statusFilters: [], agentFilters: [], repoFilter: "https://example.com/other.git" }, false],
  ];

  for (const [query, expected] of cases) {
    assert.equal(overviewItemMatchesQuery(item, query), expected);
  }
});

test("appendUniqueOverviewItems appends only unseen workspace IDs", () => {
  const current = [{ workspace_id: "ws_1", title: "one" }];
  const appended = appendUniqueOverviewItems(current, [
    { workspace_id: "ws_1", title: "duplicate" },
    { workspace_id: "ws_2", title: "two" },
    { workspace_id: "ws_2", title: "duplicate in page" },
  ]);

  assert.deepEqual(appended.map((item) => item.workspace_id), ["ws_1", "ws_2"]);
  assert.equal(appended[0], current[0]);
});

test("reconcileOverviewFirstPage keeps unchanged rows stable and retains loaded history", () => {
  const current = [
    { workspace_id: "ws_1", title: "one" },
    { workspace_id: "ws_2", title: "old two" },
    { workspace_id: "ws_3", title: "history" },
  ];
  const reconciled = reconcileOverviewFirstPage(current, [
    { workspace_id: "ws_1", title: "one" },
    { workspace_id: "ws_2", title: "new two" },
    { workspace_id: "ws_2", title: "duplicate" },
  ]);

  assert.deepEqual(reconciled.map((item) => item.workspace_id), ["ws_1", "ws_2", "ws_3"]);
  assert.equal(reconciled[0], current[0]);
  assert.notEqual(reconciled[1], current[1]);
  assert.equal(reconciled[1].title, "new two");
  assert.equal(reconciled[2], current[2]);
});

test("retainedOverviewIdBatch excludes page one and rotates one API-sized batch", () => {
  const current = Array.from({ length: OVERVIEW_REFRESH_BATCH_SIZE * 2 + 3 }, (_, index) => ({
    workspace_id: `ws_${index}`,
  }));
  const pageItems = [
    { workspace_id: "ws_0" },
    { workspace_id: "ws_1" },
    { workspace_id: "ws_1" },
  ];

  const first = retainedOverviewIdBatch(current, pageItems, 0);
  const second = retainedOverviewIdBatch(current, pageItems, first.nextOffset);
  const final = retainedOverviewIdBatch(current, pageItems, second.nextOffset);
  const wrapped = retainedOverviewIdBatch(current, pageItems, final.nextOffset);

  assert.equal(first.workspaceIds.length, OVERVIEW_REFRESH_BATCH_SIZE);
  assert.deepEqual(first.workspaceIds.slice(0, 2), ["ws_2", "ws_3"]);
  assert.equal(second.workspaceIds.length, OVERVIEW_REFRESH_BATCH_SIZE);
  assert.deepEqual(second.workspaceIds.slice(0, 2), ["ws_202", "ws_203"]);
  assert.deepEqual(final.workspaceIds, ["ws_402"]);
  assert.equal(final.nextOffset, 0);
  assert.deepEqual(wrapped.workspaceIds, first.workspaceIds);
  assert.equal(
    new Set([...first.workspaceIds, ...second.workspaceIds, ...final.workspaceIds]).size,
    current.length - 2,
  );
  assert.deepEqual(
    retainedOverviewIdBatch(current.slice(0, 5), pageItems, second.nextOffset),
    { workspaceIds: ["ws_2", "ws_3", "ws_4"], nextOffset: 0 },
  );
  assert.deepEqual(retainedOverviewIdBatch(pageItems, pageItems, 0), {
    workspaceIds: [],
    nextOffset: 0,
  });
});

test("reconcileOverviewRetainedItems refreshes history in place and removes missing rows", () => {
  const current = [
    { workspace_id: "ws_1", title: "old first" },
    { workspace_id: "ws_2", title: "unchanged history" },
    { workspace_id: "ws_3", title: "stale history", status: "running" },
    { workspace_id: "ws_4", title: "removed history" },
  ];
  const reconciled = reconcileOverviewRetainedItems(
    current,
    [{ workspace_id: "ws_1", title: "new first" }],
    [
      { workspace_id: "ws_2", title: "unchanged history" },
      { workspace_id: "ws_3", title: "fresh history", status: "completed" },
    ],
    ["ws_4"],
  );

  assert.deepEqual(reconciled.map((item) => item.workspace_id), ["ws_1", "ws_2", "ws_3"]);
  assert.equal(reconciled[0].title, "new first");
  assert.equal(reconciled[1], current[1]);
  assert.notEqual(reconciled[2], current[2]);
  assert.equal(reconciled[2].status, "completed");
});

test("collectOverviewPages follows next_cursor across pages", async () => {
  const calls = [];
  const collected = await collectOverviewPages(async (cursor) => {
    calls.push(cursor);
    if (cursor === null) {
      return page([{ workspace_id: "ws_1" }], { has_more: true, next_cursor: "page-2" });
    }
    if (cursor === "page-2") {
      return page([{ workspace_id: "ws_2" }], { has_more: false, next_cursor: null });
    }
    throw new Error(`unexpected cursor ${cursor}`);
  });
  assert.deepEqual(calls, [null, "page-2"]);
  assert.equal(collected?.truncated, false);
  assert.equal(collected?.truncationReason, null);
  assert.deepEqual(
    collected?.items.map((item) => item.workspace_id),
    ["ws_1", "ws_2"],
  );
});

test("collectOverviewPages stops when has_more is false even if next_cursor is set", async () => {
  let calls = 0;
  const collected = await collectOverviewPages(async () => {
    calls += 1;
    return page([{ workspace_id: "ws_only" }], { has_more: false, next_cursor: "ignored" });
  });
  assert.equal(calls, 1);
  assert.equal(collected?.truncated, false);
  assert.equal(collected?.items.length, 1);
});

test("collectOverviewPages returns null when a page request fails", async () => {
  const collected = await collectOverviewPages(async () => null);
  assert.equal(collected, null);
});

test("usableContinuationCursor rejects omitted, null, and blank cursors", () => {
  assert.equal(usableContinuationCursor(null), false);
  assert.equal(usableContinuationCursor(undefined), false);
  assert.equal(usableContinuationCursor(""), false);
  assert.equal(usableContinuationCursor("   "), false);
  assert.equal(usableContinuationCursor("page-2"), true);
});

test("collectOverviewPages marks truncated when has_more is true but next_cursor is absent", async () => {
  const envelopes = [
    page([{ workspace_id: "ws_partial" }], { has_more: true, next_cursor: null }),
    page([{ workspace_id: "ws_partial" }], { has_more: true, next_cursor: "" }),
    page([{ workspace_id: "ws_partial" }], { has_more: true, next_cursor: "  \t" }),
    { items: [{ workspace_id: "ws_partial" }], has_more: true },
  ];
  for (const envelope of envelopes) {
    let calls = 0;
    const collected = await collectOverviewPages(async () => {
      calls += 1;
      return envelope;
    });
    assert.equal(calls, 1);
    assert.equal(collected?.truncated, true);
    assert.equal(collected?.truncationReason, "missing_cursor");
    assert.deepEqual(
      collected?.items.map((item) => item.workspace_id),
      ["ws_partial"],
    );
  }
});

test("collectOverviewPages marks truncated when the page ceiling stops with has_more", async () => {
  let calls = 0;
  const collected = await collectOverviewPages(async () => {
    calls += 1;
    return page([{ workspace_id: `ws_${calls}` }], {
      has_more: true,
      next_cursor: `page-${calls + 1}`,
    });
  });
  assert.equal(calls, OVERVIEW_LIST_MAX_PAGES);
  assert.equal(collected?.truncated, true);
  assert.equal(collected?.truncationReason, "page_ceiling");
  assert.equal(collected?.items.length, OVERVIEW_LIST_MAX_PAGES);
});
