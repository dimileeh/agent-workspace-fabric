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
  // Cover EVERY recorded violation, not just the first: the blocked resume only
  // suppresses violations covered by active grants, and the preserved-commit guard
  // requires all recorded block_violations to be granted, so a single-path command
  // would fail to unblock a multi-violation pause.
  const paths = violationPaths(violations);
  const grants = paths
    .map((path) => `--grant '${shellSingleQuote(globEscape(path))}'`)
    .join(" ");
  const reverts = paths.map((path) => `revert ${shellSingleQuote(path)}`).join("; ");
  return {
    // The guide service rejects a grant that covers a recorded block violation
    // unless `--approve-policy-downgrade` is set (controls_guide.py): the grants
    // target the recorded violation paths, so the flag is mandatory for the command
    // to actually unblock — include it so the copied command works.
    grantCommand: `awf workspace guide ${workspaceId} ${grants} --approve-policy-downgrade --reason '${REASON_PLACEHOLDER}'`,
    revertCommand: `awf workspace guide ${workspaceId} --directive '${reverts}; ${ALTERNATIVE_PLACEHOLDER}'`,
  };
}

// The violating path is interpolated inside single-quoted shell arguments above.
// Git filenames may legally contain a single quote (e.g. `it's/config.yml`), which
// would break the quoting and let extra shell tokens slip in when an operator copies
// the command. Escape it the POSIX way: close the quote, emit a literal quote, reopen
// (`'` -> `'\''`). The `<path>` placeholder has no quote, so this is a no-op for it.
function shellSingleQuote(value: string): string {
  return value.replace(/'/g, "'\\''");
}

// `--grant` is applied as an fnmatch path glob by the guide gate (controls_guide.py),
// so a literal violation path containing glob metacharacters (`*`, `?`, `[`) would
// match the wrong files. Escape each the fnmatch way (wrap in a character class) so
// the grant targets exactly the recorded file. Mirrors Python's glob.escape. Applied
// to grants only — the revert directive is free text, not a glob.
function globEscape(value: string): string {
  return value.replace(/([*?[])/g, "[$1]");
}

// All distinct, non-empty violation paths in recorded order, or the placeholder when
// none are available so the command stays copy-able.
function violationPaths(
  violations: readonly Pick<WorkspaceBlockViolation, "path">[] | null | undefined,
): string[] {
  const seen = new Set<string>();
  const paths: string[] = [];
  for (const violation of violations ?? []) {
    const candidate = violation.path?.trim();
    if (candidate && !seen.has(candidate)) {
      seen.add(candidate);
      paths.push(candidate);
    }
  }
  return paths.length > 0 ? paths : [PATH_PLACEHOLDER];
}
