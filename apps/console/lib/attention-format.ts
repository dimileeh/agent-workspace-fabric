// Pure helpers for the awaiting-human attention surface (#657). Mirrors the
// per-concern `lib/*-format.ts` convention (see `blocked-format.ts`) so the age +
// badge-label logic is node:test-covered without a browser. No backend behaviour
// lives here — these only read the `awaiting_human_*` / `attention_required` fields
// the API already exposes (gated server-side on `status === "monitoring_pr"`).

// "Awaiting human since" timestamp for the overview/list + KPI surfaces. The
// overview carries the authoritative `awaiting_human_since` (the same column the
// detail response reads). The caller only reads this for a flagged monitoring_pr
// row, where the server (`workspace_attention_fields`) guarantees
// `awaiting_human_since` is populated whenever `attention_required` is true — so
// there is deliberately no `updated_at` fallback. A missing value yields null (a
// dash), matching `attentionAgeSeconds`, rather than a misleading "Awaiting human
// for N" age measured from the last arbitrary DB update.
export function attentionSince(
  overview: {
    awaiting_human_since?: string | null;
  },
): string | null {
  return overview.awaiting_human_since ?? null;
}

// Elapsed seconds since a workspace started awaiting human attention, for the
// "Awaiting human for N" indicators. Returns null when the source timestamp is
// missing/unparseable so `compactDuration` renders a dash rather than a misleading
// age. `now` is injectable so the conversion stays deterministically unit-testable.
export function attentionAgeSeconds(
  since: string | null,
  now: number = Date.now(),
): number | null {
  if (!since) {
    return null;
  }
  const started = new Date(since).getTime();
  if (Number.isNaN(started)) {
    return null;
  }
  return Math.max(0, (now - started) / 1000);
}

// Whether a workspace row should surface the awaiting-human attention badge. The
// server gates `attention_required` on `status === "monitoring_pr"`, but re-assert
// the status here so a stale flag can never paint a non-monitoring row.
export function isAwaitingHuman(
  overview: { status?: string | null; attention_required?: boolean | null },
): boolean {
  return overview.status === "monitoring_pr" && Boolean(overview.attention_required);
}

// Short badge label for the monitoring_pr row marker, e.g. "Awaiting human for 3m".
// Falls back to a bare "Awaiting human" when the age is unavailable.
export function attentionBadgeLabel(ageLabel: string | null): string {
  if (!ageLabel) {
    return "Awaiting human";
  }
  return `Awaiting human for ${ageLabel}`;
}
