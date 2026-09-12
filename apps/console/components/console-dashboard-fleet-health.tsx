import { formatDashboardCoverageNotice } from "@/lib/console-dashboard-summary";
import type { StatusTone } from "@/lib/format";
import type { ConsoleDashboardCountEvidence } from "@/lib/types";

import { KpiStat } from "./console-dashboard-shared";

export type FleetKpi = {
  id: string;
  label: string;
  value: string | number;
  tone?: StatusTone;
  suffix?: string;
  hint?: string;
  stale?: boolean;
};

// Status layer (ISA-101 three-layer model): a single-glance fleet HUD answering
// "is the fleet ok?" — the 5-7 KPIs an operator scans first, most critical first.
// KPIs dim per source (saturation vs reliability summary) so only the actually
// stale values fade, while the warning above stays at full opacity.
export function FleetHealthStrip({
  kpis,
  error,
  lastSuccessAt,
  coverageStatus,
  coverageNotes,
  countEvidence,
}: {
  kpis: FleetKpi[];
  error?: string | null;
  lastSuccessAt?: string | null;
  coverageStatus?: "complete" | "partial" | "unknown" | null;
  coverageNotes?: readonly string[] | null;
  countEvidence?: ConsoleDashboardCountEvidence | null;
}) {
  const anyStale = kpis.some((kpi) => kpi.stale);
  // HTTP 200 can still be incomplete. Do not treat partial/unknown as a request
  // error — that banner is cleared on success — but do not let non-null counts
  // look fully current either.
  const coverageNotice = formatDashboardCoverageNotice(
    coverageStatus ? { status: coverageStatus, notes: coverageNotes ?? [] } : null,
    countEvidence,
  );
  return (
    <div className="border-b border-line bg-canvas px-4 py-3" aria-label="Fleet health">
      {error ? (
        <div
          className="mb-2 inline-flex max-w-full flex-wrap items-center gap-1 rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-2 py-0.5 text-[11px] font-medium text-danger-text"
          role="alert"
          data-testid="dashboard-summary-error"
        >
          <span aria-hidden>⚠</span>
          <span>{error}</span>
          {lastSuccessAt ? (
            <span className="text-danger-text/80">· last success {lastSuccessAt}</span>
          ) : null}
        </div>
      ) : null}
      {coverageNotice ? (
        <div
          className="mb-2 inline-flex max-w-full flex-wrap items-center gap-1 rounded-[var(--radius-control)] border border-attention-border bg-attention-soft px-2 py-0.5 text-[11px] font-medium text-attention-text"
          role="status"
          data-testid="dashboard-summary-coverage"
        >
          <span aria-hidden>⚠</span>
          <span>{coverageNotice}</span>
          {!error && lastSuccessAt ? (
            <span className="text-attention-text/80">· last complete {lastSuccessAt}</span>
          ) : null}
        </div>
      ) : null}
      {anyStale ? (
        <div className="mb-2 inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-attention-border bg-attention-soft px-2 py-0.5 text-[11px] font-medium text-attention-text">
          <span aria-hidden>⚠</span>
          some values show the last snapshot — live data may be stale
        </div>
      ) : null}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-9">
        {kpis.map((kpi) => (
          <KpiStat
            key={kpi.id}
            label={kpi.label}
            value={kpi.value}
            tone={kpi.tone}
            suffix={kpi.suffix}
            hint={kpi.hint}
            stale={kpi.stale}
          />
        ))}
      </div>
    </div>
  );
}
