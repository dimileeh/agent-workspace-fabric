export type FleetFeedMarks = {
  appliedSuccess: number;
  appliedFailure: number;
  revoked: number;
};

export function emptyFleetFeedMarks(): FleetFeedMarks {
  return { appliedSuccess: 0, appliedFailure: 0, revoked: 0 };
}

/**
 * A completed fleet snapshot is stale only when the auth/tenant epoch moved.
 * Inspector gated-detail generation is intentionally not an input: a listing
 * or tail 401/403 must not drop this feed's own denial or outage.
 */
export function fleetAuthorizedEpochStillCurrent(
  capturedEpoch: number,
  currentEpoch: number,
): boolean {
  return capturedEpoch === currentEpoch;
}

/**
 * Apply a completed 401/403 unless a newer success already owns the feed.
 * A newer request that has only started is not recovery: stamp revoked through
 * the latest started generation so that in-flight success or transient failure
 * cannot restore cleared snapshots or replace the denial.
 */
export function claimFleetFeedDenial(
  generation: number,
  latestGeneration: number,
  marks: FleetFeedMarks,
): boolean {
  if (generation < marks.appliedSuccess) {
    return false;
  }
  if (generation <= marks.revoked) {
    return false;
  }
  marks.revoked = Math.max(marks.revoked, latestGeneration);
  return true;
}

/**
 * Capability withdrawal bumps the request generation and clears the snapshot
 * and error. Cover that bumped generation so an in-flight 503 or 401 cannot
 * restore the cleared error. A later request increments past this floor.
 * Refresh overlap does not call this — an older outage still applies until a
 * newer success lands.
 */
export function revokeFleetFeedThroughGeneration(
  generation: number,
  marks: FleetFeedMarks,
): void {
  marks.revoked = Math.max(marks.revoked, generation);
}

/**
 * Apply a completed network/5xx (or other non-auth) failure unless a newer
 * success, a newer outage, or an applied denial already owns the feed.
 * A newer request merely starting is not recovery.
 */
export function claimFleetFeedOutage(generation: number, marks: FleetFeedMarks): boolean {
  if (generation < marks.appliedSuccess) {
    return false;
  }
  if (generation < marks.appliedFailure) {
    return false;
  }
  if (generation <= marks.revoked) {
    return false;
  }
  marks.appliedFailure = Math.max(marks.appliedFailure, generation);
  return true;
}

/**
 * Apply a success only when this request is still the latest and no denial or
 * newer outage already owns the feed. An older success must not clear a
 * failure that landed after a newer request started.
 */
export function claimFleetFeedSuccess(
  generation: number,
  latestGeneration: number,
  marks: FleetFeedMarks,
): boolean {
  if (generation !== latestGeneration) {
    return false;
  }
  if (generation <= marks.revoked) {
    return false;
  }
  if (generation < marks.appliedSuccess) {
    return false;
  }
  if (generation < marks.appliedFailure) {
    return false;
  }
  marks.appliedSuccess = Math.max(marks.appliedSuccess, generation);
  return true;
}
