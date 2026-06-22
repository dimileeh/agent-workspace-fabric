import assert from "node:assert/strict";
import test from "node:test";

import {
  attentionAgeSeconds,
  attentionBadgeLabel,
  attentionSince,
  isAwaitingHuman,
} from "./attention-format.ts";

test("attentionSince returns the authoritative awaiting_human_since", () => {
  assert.equal(
    attentionSince({ awaiting_human_since: "2026-06-20T09:00:00.000Z" }),
    "2026-06-20T09:00:00.000Z",
  );
});

test("attentionSince returns null when awaiting_human_since is absent — no updated_at fallback", () => {
  // The caller only invokes this for a flagged monitoring_pr row, where the
  // server guarantees `awaiting_human_since` is populated. A missing value must
  // yield a dash, never an age measured from an arbitrary DB update.
  assert.equal(attentionSince({}), null);
  assert.equal(attentionSince({ awaiting_human_since: null }), null);
});

test("attentionAgeSeconds computes elapsed seconds with an injected clock", () => {
  const now = new Date("2026-06-20T09:05:00.000Z").getTime();
  assert.equal(attentionAgeSeconds("2026-06-20T09:00:00.000Z", now), 300);
});

test("attentionAgeSeconds returns null for missing or unparseable timestamps", () => {
  assert.equal(attentionAgeSeconds(null), null);
  assert.equal(attentionAgeSeconds("not-a-date"), null);
});

test("attentionAgeSeconds never returns a negative age", () => {
  const now = new Date("2026-06-20T08:00:00.000Z").getTime();
  assert.equal(attentionAgeSeconds("2026-06-20T09:00:00.000Z", now), 0);
});

test("isAwaitingHuman is true only for a flagged monitoring_pr workspace", () => {
  assert.equal(isAwaitingHuman({ status: "monitoring_pr", attention_required: true }), true);
  // Flag set but not monitoring_pr → never surfaced (stale-flag guard).
  assert.equal(isAwaitingHuman({ status: "completed", attention_required: true }), false);
  // monitoring_pr but not flagged.
  assert.equal(isAwaitingHuman({ status: "monitoring_pr", attention_required: false }), false);
  assert.equal(isAwaitingHuman({ status: "monitoring_pr" }), false);
});

test("attentionBadgeLabel includes the age when available", () => {
  assert.equal(attentionBadgeLabel("3m"), "Awaiting human for 3m");
});

test("attentionBadgeLabel falls back to a bare label when the age is missing", () => {
  assert.equal(attentionBadgeLabel(null), "Awaiting human");
  assert.equal(attentionBadgeLabel(""), "Awaiting human");
});
