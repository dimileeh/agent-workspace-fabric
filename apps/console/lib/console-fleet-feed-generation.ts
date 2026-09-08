export type FleetFeedMarks = {
  appliedSuccess: number;
  appliedFailure: number;
  revoked: number;
};

export function emptyFleetFeedMarks(): FleetFeedMarks {
  return { appliedSuccess: 0, appliedFailure: 0, revoked: 0 };
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
