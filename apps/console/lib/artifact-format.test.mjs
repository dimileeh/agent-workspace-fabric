import assert from "node:assert/strict";
import test from "node:test";

import {
  ARTIFACT_LIST_MAX_PAGES,
  ARTIFACT_LIST_PAGE_SIZE,
  CONFORMANCE_ARTIFACT_NAME,
  PLAN_ARTIFACT_NAME,
  artifactDownloadPath,
  artifactListPath,
  collectArtifactsForPresence,
  findArtifactByRelativePath,
  formatConformanceJson,
  hasConformanceArtifact,
  hasPlanArtifact,
} from "./artifact-format.ts";

function artifact(relativePath) {
  return {
    artifact_id: `art_${relativePath}`,
    workspace_id: "ws_1",
    // The API sets ``name`` to the basename only; ``relative_path`` is the
    // downloadable path. Root artifacts have ``relative_path === name``.
    name: relativePath.split("/").pop(),
    relative_path: relativePath,
    path: `/work/artifacts/ws_1/${relativePath}`,
    kind: relativePath.endsWith(".json") ? "json" : "md",
    size_bytes: 12,
    modified_at: "2026-06-12T00:00:00.000Z",
  };
}

test("findArtifactByRelativePath returns the matching artifact or undefined", () => {
  const items = [artifact(PLAN_ARTIFACT_NAME), artifact("other.txt")];
  assert.equal(
    findArtifactByRelativePath(items, PLAN_ARTIFACT_NAME)?.relative_path,
    PLAN_ARTIFACT_NAME,
  );
  assert.equal(findArtifactByRelativePath(items, "missing.md"), undefined);
});

test("findArtifactByRelativePath ignores a nested file that only shares the basename", () => {
  // ``validation_worktree/plan.md`` has ``name === "plan.md"`` but is not the
  // root deposit the download endpoint serves at ``path=plan.md``.
  const nested = artifact("validation_worktree/plan.md");
  assert.equal(nested.name, PLAN_ARTIFACT_NAME);
  assert.equal(findArtifactByRelativePath([nested], PLAN_ARTIFACT_NAME), undefined);
  assert.equal(hasPlanArtifact([nested]), false);
});

test("hasPlanArtifact / hasConformanceArtifact detect presence", () => {
  const both = [artifact(PLAN_ARTIFACT_NAME), artifact(CONFORMANCE_ARTIFACT_NAME)];
  assert.equal(hasPlanArtifact(both), true);
  assert.equal(hasConformanceArtifact(both), true);
});

test("hasPlanArtifact / hasConformanceArtifact are independent when only one exists", () => {
  const planOnly = [artifact(PLAN_ARTIFACT_NAME)];
  assert.equal(hasPlanArtifact(planOnly), true);
  assert.equal(hasConformanceArtifact(planOnly), false);

  const conformanceOnly = [artifact(CONFORMANCE_ARTIFACT_NAME)];
  assert.equal(hasPlanArtifact(conformanceOnly), false);
  assert.equal(hasConformanceArtifact(conformanceOnly), true);
});

test("hasPlanArtifact / hasConformanceArtifact are false for an empty list", () => {
  assert.equal(hasPlanArtifact([]), false);
  assert.equal(hasConformanceArtifact([]), false);
});

test("artifactDownloadPath encodes the workspace id and artifact name", () => {
  assert.equal(
    artifactDownloadPath("ws 1", CONFORMANCE_ARTIFACT_NAME),
    "/api/awf/workspaces/ws%201/artifacts/download?path=conformance.json",
  );
});

test("formatConformanceJson pretty-prints valid JSON", () => {
  assert.equal(
    formatConformanceJson('{"status":"satisfied","gaps":[]}'),
    '{\n  "status": "satisfied",\n  "gaps": []\n}',
  );
});

test("formatConformanceJson falls back to raw text for invalid JSON", () => {
  assert.equal(formatConformanceJson("not json {"), "not json {");
});

test("artifactListPath requests the large page size and omits a null cursor", () => {
  assert.equal(
    artifactListPath("ws 1", null),
    `/api/awf/workspaces/ws%201/artifacts?limit=${ARTIFACT_LIST_PAGE_SIZE}`,
  );
});

test("artifactListPath threads a cursor when one is supplied", () => {
  assert.equal(
    artifactListPath("ws_1", "page-2"),
    `/api/awf/workspaces/ws_1/artifacts?limit=${ARTIFACT_LIST_PAGE_SIZE}&cursor=page-2`,
  );
});

test("collectArtifactsForPresence follows next_cursor until both artifacts appear", async () => {
  const pages = [
    {
      items: [artifact("00-early.txt"), artifact("01-early.txt")],
      next_cursor: "page-2",
      has_more: true,
    },
    {
      items: [artifact(PLAN_ARTIFACT_NAME), artifact(CONFORMANCE_ARTIFACT_NAME)],
      next_cursor: "page-3",
      has_more: true,
    },
  ];
  const cursors = [];
  const collected = await collectArtifactsForPresence(async (cursor) => {
    cursors.push(cursor);
    return pages[cursors.length - 1];
  });
  // Stops after page 2 (both found) without fetching the advertised page 3.
  assert.deepEqual(cursors, [null, "page-2"]);
  assert.equal(hasPlanArtifact(collected), true);
  assert.equal(hasConformanceArtifact(collected), true);
  assert.equal(collected.length, 4);
});

test("collectArtifactsForPresence stops at the last page when artifacts are absent", async () => {
  let calls = 0;
  const collected = await collectArtifactsForPresence(async () => {
    calls += 1;
    return { items: [artifact("only.txt")], next_cursor: null, has_more: false };
  });
  assert.equal(calls, 1);
  assert.deepEqual(
    collected.map((item) => item.name),
    ["only.txt"],
  );
});

test("collectArtifactsForPresence returns null when a page request fails", async () => {
  const collected = await collectArtifactsForPresence(async () => null);
  assert.equal(collected, null);
});

test("collectArtifactsForPresence rejects a prefix when the page ceiling is still incomplete", async () => {
  let calls = 0;
  const collected = await collectArtifactsForPresence(async () => {
    calls += 1;
    return { items: [artifact(`f-${calls}.txt`)], next_cursor: `c-${calls}`, has_more: true };
  });
  assert.equal(calls, ARTIFACT_LIST_MAX_PAGES);
  // Distinct cursors are not a cycle, but the provider never ended the list.
  // Returning the prefix would hide plan.md / conformance.json past the cap.
  assert.equal(collected, null);
});

test("collectArtifactsForPresence rejects a repeated cursor before the page ceiling", async () => {
  let calls = 0;
  const collected = await collectArtifactsForPresence(async () => {
    calls += 1;
    return { items: [{ relative_path: "a.txt" }], has_more: true, next_cursor: "same" };
  });
  assert.equal(collected, null);
  // The first "same" cursor is a real next page. The repeat is a cycle and
  // must not be followed out to the page ceiling.
  assert.equal(calls, 2);
});

test("collectArtifactsForPresence rejects a cursor that returns to an earlier page", async () => {
  const cursors = ["a", "b"];
  let calls = 0;
  const collected = await collectArtifactsForPresence(async () => {
    const nextCursor = cursors[calls % cursors.length];
    calls += 1;
    return { items: [{ relative_path: "a.txt" }], has_more: true, next_cursor: nextCursor };
  });
  assert.equal(collected, null);
  // null → a → b → a. The third page points back at "a", which was already requested.
  assert.equal(calls, 3);
});

test("collectArtifactsForPresence returns both targets found on the final allowed page", async () => {
  let calls = 0;
  const collected = await collectArtifactsForPresence(async () => {
    calls += 1;
    const last = calls === ARTIFACT_LIST_MAX_PAGES;
    return {
      items: last
        ? [artifact(PLAN_ARTIFACT_NAME), artifact(CONFORMANCE_ARTIFACT_NAME)]
        : [artifact(`f-${calls}.txt`)],
      next_cursor: `c-${calls}`,
      has_more: true,
    };
  });
  assert.equal(calls, ARTIFACT_LIST_MAX_PAGES);
  assert.equal(hasPlanArtifact(collected), true);
  assert.equal(hasConformanceArtifact(collected), true);
});

test("collectArtifactsForPresence keeps a list the provider exhausts on the final allowed page", async () => {
  let calls = 0;
  const collected = await collectArtifactsForPresence(async () => {
    calls += 1;
    const last = calls === ARTIFACT_LIST_MAX_PAGES;
    return {
      items: [artifact(`f-${calls}.txt`)],
      next_cursor: last ? null : `c-${calls}`,
      has_more: !last,
    };
  });
  assert.equal(calls, ARTIFACT_LIST_MAX_PAGES);
  assert.equal(collected.length, ARTIFACT_LIST_MAX_PAGES);
  assert.equal(hasPlanArtifact(collected), false);
  assert.equal(hasConformanceArtifact(collected), false);
});

test("collectArtifactsForPresence resolves null when items is missing", async () => {
  const missingItems = await collectArtifactsForPresence(async () => ({
    next_cursor: null,
    has_more: false,
  }));
  assert.equal(missingItems, null);

  // Reproduced hosted envelope. Core must reject it, not unwrap it.
  const envelope = await collectArtifactsForPresence(async () => ({
    request_id: "req_1",
    tenant: { id: "ten_1" },
    artifacts: { workspace_id: "ws_1", artifact_ids: ["native_pr_evidence"] },
  }));
  assert.equal(envelope, null);
});

test("collectArtifactsForPresence resolves null when items is not an array", async () => {
  const fromString = await collectArtifactsForPresence(async () => ({
    items: "plan.md",
    next_cursor: null,
    has_more: false,
  }));
  assert.equal(fromString, null);

  const fromObject = await collectArtifactsForPresence(async () => ({
    items: { artifact_ids: ["native_pr_evidence"] },
    next_cursor: null,
    has_more: false,
  }));
  assert.equal(fromObject, null);
});

test("collectArtifactsForPresence resolves null when an item identity is invalid", async () => {
  const pages = [
    [{ name: "plan.md" }],
    [null],
    [{ relative_path: "" }],
    [{ relative_path: 1 }],
    [{ relative_path: PLAN_ARTIFACT_NAME }, { name: "plan.md" }],
  ];
  for (const items of pages) {
    const collected = await collectArtifactsForPresence(async () => ({
      items,
      next_cursor: null,
      has_more: false,
    }));
    assert.equal(collected, null);
  }
});

test("collectArtifactsForPresence resolves null for malformed pagination", async () => {
  const pages = [
    { items: [], next_cursor: null },
    { items: [], next_cursor: null, has_more: "false" },
    { items: [], next_cursor: 1, has_more: false },
    { items: [], next_cursor: "", has_more: false },
    // has_more advertises another page, but a null cursor cannot be followed.
    { items: [], next_cursor: null, has_more: true },
  ];
  for (const page of pages) {
    const collected = await collectArtifactsForPresence(async () => page);
    assert.equal(collected, null);
  }
});

test("collectArtifactsForPresence resolves null when fetchPage throws", async () => {
  const collected = await collectArtifactsForPresence(async () => {
    throw new Error("network down");
  });
  assert.equal(collected, null);
});

test("collectArtifactsForPresence accepts a minimal relative_path entry", async () => {
  const collected = await collectArtifactsForPresence(async () => ({
    items: [{ relative_path: PLAN_ARTIFACT_NAME }],
    next_cursor: null,
    has_more: false,
  }));
  assert.ok(collected);
  assert.equal(collected.length, 1);
  assert.equal(collected[0].relative_path, PLAN_ARTIFACT_NAME);
  assert.equal(hasPlanArtifact(collected), true);
  assert.equal("path" in collected[0], false);
  assert.equal("size_bytes" in collected[0], false);
  assert.equal("modified_at" in collected[0], false);
});

test("collectArtifactsForPresence keeps full local artifact metadata", async () => {
  const plan = artifact(PLAN_ARTIFACT_NAME);
  const collected = await collectArtifactsForPresence(async () => ({
    items: [plan],
    next_cursor: null,
    has_more: false,
  }));
  assert.deepEqual(collected, [plan]);
  assert.equal(hasPlanArtifact(collected), true);
});

test("collectArtifactsForPresence returns an empty array for a valid empty page", async () => {
  const collected = await collectArtifactsForPresence(async () => ({
    items: [],
    next_cursor: null,
    has_more: false,
  }));
  assert.deepEqual(collected, []);
});

test("collectArtifactsForPresence rejects a prefix when has_more has no usable cursor", async () => {
  let calls = 0;
  const collected = await collectArtifactsForPresence(async () => {
    calls += 1;
    return { items: [artifact("only.txt")], next_cursor: null, has_more: true };
  });
  assert.equal(calls, 1);
  // The advertised later page is unreachable. Returning the prefix would hide
  // plan.md / conformance.json that sort after only.txt.
  assert.equal(collected, null);
});
