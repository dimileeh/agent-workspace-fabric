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
import {
  buildSparklineGeometry,
  formatCores,
  TELEMETRY_VIEWS,
} from "@/lib/console-workspace-telemetry";

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
  const geom = useMemo(() => {
    if (points.length === 0) {
      return null;
    }
    const sorted = [...points].sort(
      (a, b) => Date.parse(a.sampleTime) - Date.parse(b.sampleTime),
    );
    return buildSparklineGeometry(sorted);
  }, [points]);

  if (!geom) {
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
      data-sparkline-qualification={geom.qualification ?? "ok"}
    >
      {geom.qualification ? (
        <div className="mb-0.5 flex items-center justify-end">
          <span
            className="text-[10px] font-medium text-attention-text"
            data-testid={`telemetry-series-${label}-qualify`}
          >
            {geom.qualification}
          </span>
        </div>
      ) : null}
      <svg
        width="100%"
        height={geom.h}
        viewBox={`0 0 ${geom.w} ${geom.h}`}
        preserveAspectRatio="none"
        aria-hidden
        className="block"
      >
        {geom.paths.map((path, index) => {
          const nonOk = path.quality !== "ok";
          return (
            <path
              key={`${path.quality}-${index}`}
              d={path.d}
              fill="none"
              stroke={nonOk ? "var(--attention)" : "var(--info)"}
              strokeWidth="1.5"
              strokeDasharray={nonOk ? "3 2" : undefined}
              data-sparkline-path={path.quality}
              data-testid={
                index === 0
                  ? `telemetry-series-${label}-path-${path.quality}`
                  : `telemetry-series-${label}-path-${path.quality}-${index}`
              }
            />
          );
        })}
        {geom.markers.map((marker, index) => {
          const nonOk = marker.quality !== "ok";
          const markerTestId =
            marker.quality === "ok"
              ? `telemetry-series-${label}-marker`
              : `telemetry-series-${label}-marker-${marker.quality}`;
          return (
            <circle
              key={`${marker.quality}-${index}`}
              cx={marker.x}
              cy={marker.y}
              r={2.5}
              fill={nonOk ? "var(--attention)" : "var(--info)"}
              stroke={nonOk ? "var(--attention-text)" : undefined}
              strokeWidth={nonOk ? 1 : undefined}
              data-sparkline-marker={marker.quality}
              data-testid={
                index === 0 || marker.quality === "ok"
                  ? markerTestId
                  : `${markerTestId}-${index}`
              }
            />
          );
        })}
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
  // Envelope-level partial is not implied by nested meter/cost badges; surface it
  // when the producer marks state or quality partial (data_quality_notes stay hidden).
  const envelopePartial =
    viewModel.state === "partial" || viewModel.quality === "partial";

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
        data-awf-sample-time-mixed={viewModel.sampleTimeMixed ? "true" : "false"}
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
          {envelopePartial ? (
            <span
              className="inline-flex items-center rounded-[var(--radius-control)] border border-attention-border bg-attention-soft px-1.5 py-0.5 font-medium text-attention-text"
              data-testid="telemetry-partial-indicator"
              title="Producer reported incomplete telemetry for this window"
            >
              partial
            </span>
          ) : null}
          <span
            className="tnum"
            data-testid="telemetry-sample-time"
            data-awf-sample-time-mixed={
              viewModel.sampleTimeMixed ? "true" : "false"
            }
            title={
              viewModel.sampleTimeMixed
                ? "CPU and memory readings use different sample times"
                : undefined
            }
          >
            {viewModel.sampleTimeMixed ? (
              <>
                Sample (mixed) CPU{" "}
                {formatDateTime(viewModel.cpu.sampleTime)} · Mem{" "}
                {formatDateTime(viewModel.memory.sampleTime)}
              </>
            ) : (
              <>Sample {formatDateTime(viewModel.sampleTime)}</>
            )}
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
              partial={
                viewModel.cpu.usedPartial || Boolean(admitted?.partial)
              }
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
              partial={
                viewModel.memory.usedPartial || Boolean(admitted?.partial)
              }
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
