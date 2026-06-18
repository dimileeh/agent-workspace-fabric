import assert from "node:assert/strict";
import test from "node:test";

import {
  blockedAgeSeconds,
  blockedSince,
  formatBlockedResolutionCommands,
} from "./blocked-format.ts";

test("blockedSince prefers last_event.occurred_at when the workspace entered blocked", () => {
  assert.equal(
    blockedSince({
      updated_at: "2026-06-18T10:00:00.000Z",
      last_event: { new_state: "blocked", occurred_at: "2026-06-18T09:30:00.000Z" },
    }),
    "2026-06-18T09:30:00.000Z",
  );
});

test("blockedSince falls back to updated_at when the last event is not the block transition", () => {
  assert.equal(
    blockedSince({
      updated_at: "2026-06-18T10:00:00.000Z",
      last_event: { new_state: "monitoring_pr", occurred_at: "2026-06-18T09:30:00.000Z" },
    }),
    "2026-06-18T10:00:00.000Z",
  );
});

test("blockedSince falls back to updated_at when the block event has no timestamp", () => {
  assert.equal(
    blockedSince({
      updated_at: "2026-06-18T10:00:00.000Z",
      last_event: { new_state: "blocked", occurred_at: null },
    }),
    "2026-06-18T10:00:00.000Z",
  );
});

test("blockedSince falls back to updated_at when there is no last event", () => {
  assert.equal(
    blockedSince({ updated_at: "2026-06-18T10:00:00.000Z", last_event: null }),
    "2026-06-18T10:00:00.000Z",
  );
});

test("blockedSince returns null when neither a block event nor updated_at is present", () => {
  assert.equal(blockedSince({ updated_at: null, last_event: null }), null);
});

test("blockedAgeSeconds returns elapsed seconds against an injected clock", () => {
  const now = Date.parse("2026-06-18T10:05:00.000Z");
  assert.equal(blockedAgeSeconds("2026-06-18T10:00:00.000Z", now), 300);
});

test("blockedAgeSeconds clamps a future timestamp to zero", () => {
  const now = Date.parse("2026-06-18T10:00:00.000Z");
  assert.equal(blockedAgeSeconds("2026-06-18T10:05:00.000Z", now), 0);
});

test("blockedAgeSeconds returns null for a missing or unparseable timestamp", () => {
  assert.equal(blockedAgeSeconds(null, Date.parse("2026-06-18T10:00:00.000Z")), null);
  assert.equal(blockedAgeSeconds("not-a-date", Date.parse("2026-06-18T10:00:00.000Z")), null);
});

test("formatBlockedResolutionCommands builds grant and revert commands from the first violating path", () => {
  const { grantCommand, revertCommand } = formatBlockedResolutionCommands("ws_abc123", [
    {
      path: ".github/workflows/ci.yml",
      protected_pattern: ".github/**",
      section: "ci",
      line: null,
      reason: "protected quality gate",
    },
  ]);
  assert.equal(
    grantCommand,
    "awf workspace guide ws_abc123 --grant '.github/workflows/ci.yml' --reason '<why>'",
  );
  assert.equal(
    revertCommand,
    "awf workspace guide ws_abc123 --directive 'revert .github/workflows/ci.yml; <alternative>'",
  );
});

test("formatBlockedResolutionCommands uses a placeholder when there are no violations", () => {
  const { grantCommand, revertCommand } = formatBlockedResolutionCommands("ws_abc123", []);
  assert.equal(grantCommand, "awf workspace guide ws_abc123 --grant '<path>' --reason '<why>'");
  assert.equal(revertCommand, "awf workspace guide ws_abc123 --directive 'revert <path>; <alternative>'");
});

test("formatBlockedResolutionCommands treats an empty/whitespace path as missing", () => {
  const { grantCommand, revertCommand } = formatBlockedResolutionCommands("ws_abc123", [
    { path: "   " },
  ]);
  assert.equal(grantCommand, "awf workspace guide ws_abc123 --grant '<path>' --reason '<why>'");
  assert.equal(revertCommand, "awf workspace guide ws_abc123 --directive 'revert <path>; <alternative>'");
});

test("formatBlockedResolutionCommands tolerates a null violations list", () => {
  const { grantCommand } = formatBlockedResolutionCommands("ws_abc123", null);
  assert.equal(grantCommand, "awf workspace guide ws_abc123 --grant '<path>' --reason '<why>'");
});
