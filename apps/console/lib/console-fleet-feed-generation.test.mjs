import assert from "node:assert/strict";
import test from "node:test";

import {
  claimFleetFeedDenial,
  claimFleetFeedOutage,
  claimFleetFeedSuccess,
  emptyFleetFeedMarks,
  revokeFleetFeedThroughGeneration,
} from "./console-fleet-feed-generation.ts";

test("superseded fleet-feed 401/403 applies while a newer request has only started", () => {
  const marks = emptyFleetFeedMarks();
  assert.equal(claimFleetFeedDenial(1, 2, marks), true);
  assert.equal(marks.revoked, 2);
  assert.equal(claimFleetFeedSuccess(2, 2, marks), false);
  assert.equal(claimFleetFeedOutage(2, marks), false);
});

test("superseded fleet-feed 401/403 is suppressed after a newer success applied", () => {
  const marks = emptyFleetFeedMarks();
  assert.equal(claimFleetFeedSuccess(2, 2, marks), true);
  assert.equal(claimFleetFeedDenial(1, 2, marks), false);
  assert.equal(marks.revoked, 0);
});

test("superseded fleet-feed outage applies while a newer request hangs", () => {
  const marks = emptyFleetFeedMarks();
  assert.equal(claimFleetFeedOutage(1, marks), true);
  assert.equal(marks.appliedFailure, 1);
  assert.equal(claimFleetFeedSuccess(1, 2, marks), false);
});

test("superseded fleet-feed outage is suppressed after a newer success applied", () => {
  const marks = emptyFleetFeedMarks();
  assert.equal(claimFleetFeedSuccess(2, 2, marks), true);
  assert.equal(claimFleetFeedOutage(1, marks), false);
  assert.equal(marks.appliedFailure, 0);
});

test("a request that starts after an applied denial may recover", () => {
  const marks = emptyFleetFeedMarks();
  assert.equal(claimFleetFeedDenial(1, 1, marks), true);
  assert.equal(claimFleetFeedSuccess(2, 2, marks), true);
  assert.equal(marks.appliedSuccess, 2);
});

test("an older outage does not replace a newer applied outage", () => {
  const marks = emptyFleetFeedMarks();
  assert.equal(claimFleetFeedOutage(2, marks), true);
  assert.equal(claimFleetFeedOutage(1, marks), false);
  assert.equal(marks.appliedFailure, 2);
});

test("capability withdrawal covers in-flight 503s without discarding overlap", () => {
  // Withdrawal bumps request generation and clears the error. An in-flight 503
  // at the previous generation must not restore that error. A newer request
  // merely starting (Refresh overlap) does not revoke, so the older 503 still
  // applies until a newer success lands.
  const withdrawn = emptyFleetFeedMarks();
  revokeFleetFeedThroughGeneration(2, withdrawn);
  assert.equal(claimFleetFeedOutage(1, withdrawn), false);
  assert.equal(claimFleetFeedDenial(1, 2, withdrawn), false);
  assert.equal(withdrawn.appliedFailure, 0);
  assert.equal(claimFleetFeedOutage(3, withdrawn), true);
  assert.equal(claimFleetFeedSuccess(4, 4, withdrawn), true);

  const overlap = emptyFleetFeedMarks();
  assert.equal(claimFleetFeedOutage(1, overlap), true);
  assert.equal(overlap.appliedFailure, 1);
});
