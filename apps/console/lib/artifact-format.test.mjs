import assert from "node:assert/strict";
import test from "node:test";

import {
  CONFORMANCE_ARTIFACT_NAME,
  PLAN_ARTIFACT_NAME,
  artifactDownloadPath,
  findArtifactByName,
  formatConformanceJson,
  hasConformanceArtifact,
  hasPlanArtifact,
} from "./artifact-format.ts";

function artifact(name) {
  return {
    artifact_id: `art_${name}`,
    workspace_id: "ws_1",
    name,
    relative_path: name,
    path: `/work/artifacts/ws_1/${name}`,
    kind: name.endsWith(".json") ? "json" : "md",
    size_bytes: 12,
    modified_at: "2026-06-12T00:00:00.000Z",
  };
}

test("findArtifactByName returns the matching artifact or undefined", () => {
  const items = [artifact(PLAN_ARTIFACT_NAME), artifact("other.txt")];
  assert.equal(findArtifactByName(items, PLAN_ARTIFACT_NAME)?.name, PLAN_ARTIFACT_NAME);
  assert.equal(findArtifactByName(items, "missing.md"), undefined);
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
