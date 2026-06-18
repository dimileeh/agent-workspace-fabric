import type { WorkspaceBlockViolation } from "@/lib/types";

// Pure helpers for the `blocked` (protected-file pause) surface. Mirrors the
// per-concern `lib/*-format.ts` convention so the age + resolution-command logic
// is node:test-covered without a browser. No backend behaviour lives here — these
// only read the fields WS-1/WS-2 already expose.

// Operator-fillable placeholders woven into the ready-to-run guide commands. The
// path is auto-filled from the recorded violation; the justification and the
// revert alternative are the operator's to supply.
const PATH_PLACEHOLDER = "<path>";
const REASON_PLACEHOLDER = "<why>";
const ALTERNATIVE_PLACEHOLDER = "<alternative>";

export interface BlockedResolutionCommands {
  /** Approve-and-keep: record a scoped grant for the violating path. */
  grantCommand: string;
  /** Revert the protected change and steer the agent to an alternative. */
  revertCommand: string;
}

// Best-effort "blocked since" timestamp for the overview/list surface, which does
// NOT carry the precise `block_state.blocked_at` (only the full GET does). Prefer
// the recorded `blocked` state-transition event; otherwise fall back to
// `updated_at`. The inspector uses the authoritative `block_state.blocked_at`.
export function blockedSince(
  overview: {
    updated_at: string | null;
    last_event: { new_state: string | null; occurred_at: string | null } | null;
  },
): string | null {
  const event = overview.last_event;
  if (event && event.new_state === "blocked" && event.occurred_at) {
    return event.occurred_at;
  }
  return overview.updated_at ?? null;
}

// Elapsed seconds since a blocked workspace paused, for the "Blocked for N"
// indicators. Returns null when the source timestamp is missing/unparseable so
// `compactDuration` renders a dash rather than a misleading age. `now` is
// injectable so the conversion stays deterministically unit-testable.
export function blockedAgeSeconds(since: string | null, now: number = Date.now()): number | null {
  if (!since) {
    return null;
  }
  const started = new Date(since).getTime();
  if (Number.isNaN(started)) {
    return null;
  }
  return Math.max(0, (now - started) / 1000);
}

// Build the two ready-to-run `awf workspace guide` commands an operator pastes to
// resolve a pre-PR protected-file pause. The violating path is taken from the
// first recorded violation (operators address them one at a time); a placeholder
// is substituted when no path is available so the command stays copy-able.
export function formatBlockedResolutionCommands(
  workspaceId: string,
  violations: readonly Pick<WorkspaceBlockViolation, "path">[] | null | undefined,
): BlockedResolutionCommands {
  const path = firstViolationPath(violations);
  return {
    grantCommand: `awf workspace guide ${workspaceId} --grant '${path}' --reason '${REASON_PLACEHOLDER}'`,
    revertCommand: `awf workspace guide ${workspaceId} --directive 'revert ${path}; ${ALTERNATIVE_PLACEHOLDER}'`,
  };
}

function firstViolationPath(
  violations: readonly Pick<WorkspaceBlockViolation, "path">[] | null | undefined,
): string {
  for (const violation of violations ?? []) {
    const candidate = violation.path?.trim();
    if (candidate) {
      return candidate;
    }
  }
  return PATH_PLACEHOLDER;
}
