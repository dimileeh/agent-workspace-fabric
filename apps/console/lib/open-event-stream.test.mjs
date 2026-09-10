import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const fixtureSource = readFileSync(
  new URL("../tests/fixtures/open-event-stream.ts", import.meta.url),
  "utf8",
);

test("open EventStream fixture permits CORS without reflecting origins or credentials", () => {
  assert.match(fixtureSource, /"access-control-allow-origin": "\*"/);
  assert.doesNotMatch(fixtureSource, /request\.headers\.origin/);
  assert.doesNotMatch(fixtureSource, /access-control-allow-credentials/);
});
