"use client";

import { Cpu, HardDrive, Wallet } from "lucide-react";
import { useMemo, type ReactNode } from "react";

import { Fact, Panel } from "@/components/console-dashboard-shared";
import { bytes, formatDateTime } from "@/lib/format";
import type {
  TelemetryViewWindow,
  WorkspaceTelemetrySeriesPoint,
  WorkspaceTelemetryView,
} from "@/lib/console-workspace-telemetry";
import { TELEMETRY_VIEWS } from "@/lib/console-workspace-telemetry";

export type ConsoleWorkspaceTelemetryProps = {
  viewModel: WorkspaceTelemetryView;
  selectedView: TelemetryViewWindow;
  onViewChange: (view: TelemetryViewWindow) => void;
  mode?: "live" | "historical";
  lastGoodAt?: string | null;
  requestError?: string | null;
  /**
   * When false, render nothing. Parents should omit this component when
   * capabilities lack telemetry/allocation/cost — not show an unsupported widget.
   */
  available?: boolean;
  workspaceId?: string;
  modelLabel?: string;
};

function formatCores(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  const text = Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/\.?0+$/, "");
  return `${text} cores`;
}

function formatWorkloadUsd(value: number): string {
  if (value >= 1) {
    return `$${value.toFixed(2)}`;
  }
  if (value >= 0.01) {
    return `$${value.toFixed(4)}`;
  }
  return `$${value.toPrecision(4)}`;
}

function formatBytesOrUnknown(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return bytes(value);
}

function costLabel(view: WorkspaceTelemetryView): string {
  const { displayState, estimatedUsd } = view.estimate;
  if (displayState === "unallocated") {
    return "Unallocated";
  }
  if (displayState === "unpriced") {
    return "Unpriced";
  }
  if (estimatedUsd === null) {
    return "Unpriced";
  }
  const amount = formatWorkloadUsd(estimatedUsd);
  if (displayState === "partial") {
    return `${amount} (partial)`;
  }
  return amount;
}

function ResourceMeter({
  label,
  icon,
  usedLabel,
  requestLabel,
  limitLabel,
  fillPct,
  partial,
}: {
  label: string;
  icon: ReactNode;
  usedLabel: string;
  requestLabel: string;
  limitLabel: string;
  fillPct: number | null;
  partial: boolean;
}) {
  const width = fillPct == null ? 0 : Math.min(100, Math.max(0, fillPct));
  return (
    <div
      className="min-w-0 rounded-[var(--radius-control)] border border-line bg-surface-2 px-3 py-2 text-xs"
      data-testid={`telemetry-meter-${label.toLowerCase()}`}
    >
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-1.5 font-semibold text-fg">
          {icon}
          <span className="truncate">{label}</span>
          {partial ? (
            <span className="shrink-0 text-[10px] font-medium text-attention-text">partial</span>
          ) : null}
        </span>
        <span className="tnum shrink-0 text-fg-strong">{usedLabel}</span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-line">
        <div
          className="h-full bg-info"
          style={{ width: `${width}%` }}
          data-testid={`telemetry-meter-${label.toLowerCase()}-fill`}
        />
      </div>
      <div className="mt-2 flex min-w-0 flex-wrap gap-x-3 gap-y-1 text-fg-muted">
        <span className="tnum truncate">req {requestLabel}</span>
        <span className="tnum truncate">lim {limitLabel}</span>
      </div>
    </div>
  );
}

function SeriesSparkline({
  points,
  label,
}: {
  points: WorkspaceTelemetrySeriesPoint[];
  label: string;
}) {
  const path = useMemo(() => {
    if (points.length === 0) {
      return null;
    }
    const sorted = [...points].sort(
      (a, b) => Date.parse(a.sampleTime) - Date.parse(b.sampleTime),
    );
    const values = sorted.map((p) => p.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const w = 120;
    const h = 28;
    const coords = sorted.map((p, i) => {
      const x = sorted.length === 1 ? w / 2 : (i / (sorted.length - 1)) * w;
      const y = h - ((p.value - min) / span) * (h - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    return { d: `M ${coords.join(" L ")}`, w, h };
  }, [points]);

  if (!path) {
    return (
      <div
        className="flex h-10 items-center rounded-[var(--radius-control)] border border-dashed border-line px-2 text-[11px] text-fg-muted"
        data-testid={`telemetry-series-${label}`}
      >
        No history
      </div>
    );
  }

  return (
    <div
      className="rounded-[var(--radius-control)] border border-line bg-surface px-2 py-1"
      data-testid={`telemetry-series-${label}`}
    >
      <svg
        width="100%"
        height={path.h}
        viewBox={`0 0 ${path.w} ${path.h}`}
        preserveAspectRatio="none"
        aria-hidden
        className="block"
      >
        <path d={path.d} fill="none" stroke="var(--info)" strokeWidth="1.5" />
      </svg>
    </div>
  );
}

function fillAgainstLimit(used: number | null, limit: number | null): number | null {
  if (used === null || limit === null || !(limit > 0) || !Number.isFinite(used)) {
    return null;
  }
  return (used / limit) * 100;
}

/**
 * Compact workspace resource + estimated workload cost view.
 * Props-driven only — does not fetch or poll.
 */
export function ConsoleWorkspaceTelemetry({
  viewModel,
  selectedView,
  onViewChange,
  mode = "live",
  lastGoodAt = null,
  requestError = null,
  available = true,
  workspaceId,
  modelLabel,
}: ConsoleWorkspaceTelemetryProps) {
  if (!available) {
    return null;
  }

  const admitted = viewModel.admitted;
  const cpuLimitLabel =
    admitted?.cpuLimitCores === null || admitted?.cpuLimitCores === undefined
      ? "unknown"
      : formatCores(admitted.cpuLimitCores);
  const memLimitLabel =
    admitted?.memoryLimitBytes === null || admitted?.memoryLimitBytes === undefined
      ? "unknown"
      : formatBytesOrUnknown(admitted.memoryLimitBytes);
  const cpuRequestLabel =
    admitted == null ? "—" : formatCores(admitted.cpuRequestCores);
  const memRequestLabel =
    admitted == null ? "—" : formatBytesOrUnknown(admitted.memoryRequestBytes);

  const stale = mode === "live" && viewModel.isStale;
  const modeLabel = mode === "historical" ? "Historical" : "Live";

  return (
    <Panel
      title="Workspace resources"
      icon={<Cpu size={16} aria-hidden />}
      stale={stale}
      staleLabel="stale"
      className="min-w-0"
      action={
        <div
          className="inline-flex rounded-[var(--radius-control)] border border-line p-0.5"
          role="tablist"
          aria-label="Telemetry window"
          data-testid="telemetry-view-selector"
        >
          {TELEMETRY_VIEWS.map((view) => {
            const selected = selectedView === view;
            return (
              <button
                key={view}
                type="button"
                role="tab"
                aria-selected={selected}
                data-testid={`telemetry-view-${view}`}
                className={`rounded-[var(--radius-control)] px-2 py-0.5 text-[11px] font-medium transition ${
                  selected
                    ? "bg-surface-2 text-fg"
                    : "text-fg-muted hover:text-fg"
                }`}
                onClick={() => onViewChange(view)}
              >
                {view}
              </button>
            );
          })}
        </div>
      }
    >
      <div
        data-testid="console-workspace-telemetry"
        data-awf-telemetry-state={viewModel.state}
        data-awf-telemetry-mode={mode}
        className="flex min-w-0 flex-col gap-3"
      >
        {(workspaceId || modelLabel) && (
          <div className="flex min-w-0 flex-wrap gap-x-3 gap-y-1 text-[11px] text-fg-muted">
            {workspaceId ? (
              <span className="mono min-w-0 max-w-full truncate" title={workspaceId}>
                {workspaceId}
              </span>
            ) : null}
            {modelLabel ? (
              <span className="mono min-w-0 max-w-full truncate" title={modelLabel}>
                {modelLabel}
              </span>
            ) : null}
          </div>
        )}

        {requestError ? (
          <div
            data-testid="telemetry-request-error"
            className="rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-2 py-1.5 text-[11px] text-danger-text"
          >
            {requestError}
          </div>
        ) : null}

        <div className="flex min-w-0 flex-wrap items-center gap-2 text-[11px] text-fg-muted">
          <span
            className="inline-flex items-center rounded-[var(--radius-control)] border border-line bg-surface px-1.5 py-0.5 font-medium text-fg"
            data-testid="telemetry-mode-label"
          >
            {modeLabel}
          </span>
          <span className="tnum" data-testid="telemetry-sample-time">
            Sample {formatDateTime(viewModel.sampleTime)}
          </span>
          {lastGoodAt ? (
            <span className="tnum" data-testid="telemetry-last-good">
              Last good {formatDateTime(lastGoodAt)}
            </span>
          ) : null}
        </div>

        {viewModel.state === "unallocated" ? (
          <div
            data-testid="telemetry-unallocated"
            className="rounded-[var(--radius-control)] border border-line bg-surface-2 px-3 py-2 text-xs text-fg-muted"
          >
            Unallocated — shared Core monitor runtime is not free capacity.
          </div>
        ) : (
          <div className="grid min-w-0 gap-2 sm:grid-cols-2">
            <ResourceMeter
              label="CPU"
              icon={<Cpu size={13} aria-hidden />}
              usedLabel={formatCores(viewModel.cpu.usedCores)}
              requestLabel={cpuRequestLabel}
              limitLabel={cpuLimitLabel}
              fillPct={fillAgainstLimit(
                viewModel.cpu.usedCores,
                admitted?.cpuLimitCores ?? null,
              )}
              partial={viewModel.cpu.usedPartial}
            />
            <ResourceMeter
              label="Memory"
              icon={<HardDrive size={13} aria-hidden />}
              usedLabel={
                viewModel.memory.usedBytes == null
                  ? "—"
                  : formatBytesOrUnknown(viewModel.memory.usedBytes)
              }
              requestLabel={memRequestLabel}
              limitLabel={memLimitLabel}
              fillPct={fillAgainstLimit(
                viewModel.memory.usedBytes,
                admitted?.memoryLimitBytes ?? null,
              )}
              partial={viewModel.memory.usedPartial}
            />
          </div>
        )}

        <div className="grid min-w-0 gap-2 sm:grid-cols-2">
          <div className="min-w-0">
            <div className="label-caps mb-1">CPU samples</div>
            <SeriesSparkline points={viewModel.cpu.series} label="cpu" />
          </div>
          <div className="min-w-0">
            <div className="label-caps mb-1">Memory samples</div>
            <SeriesSparkline points={viewModel.memory.series} label="memory" />
          </div>
        </div>

        <div
          data-testid="telemetry-workload-cost"
          className="rounded-[var(--radius-control)] border border-line bg-surface-2 px-3 py-2 text-xs"
        >
          <div className="flex min-w-0 items-center justify-between gap-2">
            <span className="flex items-center gap-1.5 font-semibold text-fg">
              <Wallet size={13} aria-hidden />
              Estimated workload cost
            </span>
            <span
              className="tnum shrink-0 text-sm font-medium text-fg-strong"
              data-testid="telemetry-workload-cost-value"
            >
              {costLabel(viewModel)}
            </span>
          </div>
          <div className="mt-1 text-[11px] text-fg-muted">{viewModel.exclusionNote}</div>
        </div>

        <div className="grid min-w-0 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          <Fact
            label="Rate table"
            value={viewModel.estimate.rateTableVersion ?? "—"}
            mono
            stale={stale}
          />
          <Fact
            label="Rate source"
            value={viewModel.estimate.rateSource ?? "—"}
            mono
            stale={stale}
          />
          <Fact
            label="Priced / unpriced"
            value={`${viewModel.estimate.pricedIntervalSeconds}s / ${viewModel.estimate.unpricedIntervalSeconds}s`}
            mono
            stale={stale}
          />
          {admitted ? (
            <>
              <Fact
                label="Compute class"
                value={admitted.computeClass ?? "—"}
                stale={stale}
              />
              <Fact label="Region" value={admitted.region ?? "—"} stale={stale} />
              <Fact
                label="Billable"
                value={admitted.billable == null ? "—" : admitted.billable ? "yes" : "no"}
                stale={stale}
              />
            </>
          ) : null}
        </div>
      </div>
    </Panel>
  );
}
