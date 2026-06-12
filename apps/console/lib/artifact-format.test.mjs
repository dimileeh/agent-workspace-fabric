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

test("collectArtifactsForPresence stops at the page ceiling for an endless cursor", async () => {
  let calls = 0;
  const collected = await collectArtifactsForPresence(async () => {
    calls += 1;
    return { items: [artifact(`f-${calls}.txt`)], next_cursor: `c-${calls}`, has_more: true };
  });
  assert.equal(calls, ARTIFACT_LIST_MAX_PAGES);
  assert.equal(collected.length, ARTIFACT_LIST_MAX_PAGES);
});
