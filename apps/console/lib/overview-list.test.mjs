import assert from "node:assert/strict";
import test from "node:test";

import {
  OVERVIEW_LIST_MAX_PAGES,
  OVERVIEW_LIST_PAGE_SIZE,
  collectOverviewPages,
  overviewListPath,
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
  assert.deepEqual(
    collected?.map((item) => item.workspace_id),
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
  assert.equal(collected?.length, 1);
});

test("collectOverviewPages returns null when a page request fails", async () => {
  const collected = await collectOverviewPages(async () => null);
  assert.equal(collected, null);
});

test("collectOverviewPages stops at the page ceiling for an endless cursor", async () => {
  let calls = 0;
  const collected = await collectOverviewPages(async () => {
    calls += 1;
    return page([{ workspace_id: `ws_${calls}` }], {
      has_more: true,
      next_cursor: `page-${calls + 1}`,
    });
  });
  assert.equal(calls, OVERVIEW_LIST_MAX_PAGES);
  assert.equal(collected?.length, OVERVIEW_LIST_MAX_PAGES);
});
