import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const dashboardSource = readFileSync(new URL("../components/console-dashboard.tsx", import.meta.url), "utf8");

test("task details modal locks body scroll in a layout effect", () => {
  const modalSource = extractFunctionSource("TaskDetailsModal");
  const scrollLockEffect = modalSource.match(
    /use(?:Layout)?Effect\(\(\) => \{\s*const scrollY = window\.scrollY;[\s\S]*?document\.body\.style\.overflow = "hidden";[\s\S]*?\}, \[\]\);/,
  );

  assert.ok(scrollLockEffect, "Expected TaskDetailsModal to lock and restore body scroll");
  assert.ok(
    scrollLockEffect[0].startsWith("useLayoutEffect("),
    "Expected the modal scroll lock to use useLayoutEffect so it runs before paint",
  );
});

function extractFunctionSource(functionName) {
  const marker = `function ${functionName}(`;
  const start = dashboardSource.indexOf(marker);
  assert.notEqual(start, -1, `Expected ${functionName} to exist`);

  const nextFunction = dashboardSource.indexOf("\nfunction ", start + marker.length);
  const end = nextFunction === -1 ? dashboardSource.length : nextFunction;
  return dashboardSource.slice(start, end);
}
