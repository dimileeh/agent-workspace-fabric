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

test("blockedSince prefers the authoritative blocked_at over the last event and updated_at", () => {
  // The overview now carries blocked_at; a trailing non-blocked last_event must
  // not skew the derived age (regression for the list "Blocked for N" age bug).
  assert.equal(
    blockedSince({
      updated_at: "2026-06-18T11:00:00.000Z",
      blocked_at: "2026-06-18T09:00:00.000Z",
      last_event: { new_state: "monitoring_pr", occurred_at: "2026-06-18T10:30:00.000Z" },
    }),
    "2026-06-18T09:00:00.000Z",
  );
});

test("blockedSince falls back to the heuristic when blocked_at is absent or null", () => {
  assert.equal(
    blockedSince({
      updated_at: "2026-06-18T10:00:00.000Z",
      blocked_at: null,
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
    "awf workspace guide ws_abc123 --grant '.github/workflows/ci.yml' --approve-policy-downgrade --reason '<why>'",
  );
  assert.equal(
    revertCommand,
    "awf workspace guide ws_abc123 --directive 'revert .github/workflows/ci.yml; <alternative>'",
  );
});

test("formatBlockedResolutionCommands uses a placeholder when there are no violations", () => {
  const { grantCommand, revertCommand } = formatBlockedResolutionCommands("ws_abc123", []);
  assert.equal(grantCommand, "awf workspace guide ws_abc123 --grant '<path>' --approve-policy-downgrade --reason '<why>'");
  assert.equal(revertCommand, "awf workspace guide ws_abc123 --directive 'revert <path>; <alternative>'");
});

test("formatBlockedResolutionCommands treats an empty/whitespace path as missing", () => {
  const { grantCommand, revertCommand } = formatBlockedResolutionCommands("ws_abc123", [
    { path: "   " },
  ]);
  assert.equal(grantCommand, "awf workspace guide ws_abc123 --grant '<path>' --approve-policy-downgrade --reason '<why>'");
  assert.equal(revertCommand, "awf workspace guide ws_abc123 --directive 'revert <path>; <alternative>'");
});

test("formatBlockedResolutionCommands tolerates a null violations list", () => {
  const { grantCommand } = formatBlockedResolutionCommands("ws_abc123", null);
  assert.equal(grantCommand, "awf workspace guide ws_abc123 --grant '<path>' --approve-policy-downgrade --reason '<why>'");
});

test("formatBlockedResolutionCommands escapes a single quote in the path so the shell command stays valid", () => {
  // Git filenames may legally contain a single quote; without escaping it would
  // break the single-quoted shell argument and could inject extra tokens.
  const { grantCommand, revertCommand } = formatBlockedResolutionCommands("ws_abc123", [
    { path: "it's/config.yml" },
  ]);
  assert.equal(
    grantCommand,
    "awf workspace guide ws_abc123 --grant 'it'\\''s/config.yml' --approve-policy-downgrade --reason '<why>'",
  );
  assert.equal(
    revertCommand,
    "awf workspace guide ws_abc123 --directive 'revert it'\\''s/config.yml; <alternative>'",
  );
});

test("formatBlockedResolutionCommands grants and reverts every recorded violation path", () => {
  // The resume path requires ALL recorded violations to be granted, so a
  // multi-violation pause must emit one --grant per path (and revert each).
  const { grantCommand, revertCommand } = formatBlockedResolutionCommands("ws_abc123", [
    { path: "pyproject.toml" },
    { path: ".github/workflows/ci.yml" },
  ]);
  assert.equal(
    grantCommand,
    "awf workspace guide ws_abc123 --grant 'pyproject.toml' --grant '.github/workflows/ci.yml' --approve-policy-downgrade --reason '<why>'",
  );
  assert.equal(
    revertCommand,
    "awf workspace guide ws_abc123 --directive 'revert pyproject.toml; revert .github/workflows/ci.yml; <alternative>'",
  );
});

test("formatBlockedResolutionCommands de-duplicates repeated violation paths", () => {
  const { grantCommand } = formatBlockedResolutionCommands("ws_abc123", [
    { path: "pyproject.toml" },
    { path: "   " },
    { path: "pyproject.toml" },
  ]);
  assert.equal(
    grantCommand,
    "awf workspace guide ws_abc123 --grant 'pyproject.toml' --approve-policy-downgrade --reason '<why>'",
  );
});

test("formatBlockedResolutionCommands escapes glob metacharacters in the grant path only", () => {
  // `--grant` is applied as an fnmatch glob, so a literal path with `*?[` must be
  // escaped to target the exact file; the revert directive is free text and stays literal.
  const { grantCommand, revertCommand } = formatBlockedResolutionCommands("ws_abc123", [
    { path: "config[1].yml" },
  ]);
  assert.equal(
    grantCommand,
    "awf workspace guide ws_abc123 --grant 'config[[]1].yml' --approve-policy-downgrade --reason '<why>'",
  );
  assert.equal(
    revertCommand,
    "awf workspace guide ws_abc123 --directive 'revert config[1].yml; <alternative>'",
  );
});
