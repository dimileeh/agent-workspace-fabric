"use client";

import { Cpu, HardDrive, Wallet } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { Fact, Panel } from "@/components/console-dashboard-shared";
import { bytes, formatDateTime } from "@/lib/format";
import type { VMEstimate, VMTelemetryView } from "@/lib/console-vm-telemetry";
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
  viewModel: WorkspaceTelemetryView | VMTelemetryView;
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
  showTelemetry?: boolean;
  showAllocation?: boolean;
  showCost?: boolean;
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
  if (displayState === "not_recorded") {
    return "Not recorded";
  }
  if (displayState === "unallocated") {
    return "Unallocated";
  }
  if (displayState === "unpriced") {
    return "Unpriced";
  }
  if (displayState === "partial") {
    // Null amount is valid for partial with zero priced seconds; still qualify
    // incomplete coverage rather than collapsing to the unpriced label.
    return estimatedUsd === null ? "Partial" : `${formatWorkloadUsd(estimatedUsd)} (partial)`;
  }
  if (estimatedUsd === null) {
    return "Unpriced";
  }
  return formatWorkloadUsd(estimatedUsd);
}

function ResourceMeter({
  label,
  icon,
  usedLabel,
  requestLabel,
  limitLabel,
  fillPct,
  partial,
  showAllocation,
}: {
  label: string;
  icon: ReactNode;
  usedLabel: string;
  requestLabel: string;
  limitLabel: string;
  fillPct: number | null;
  partial: boolean;
  showAllocation: boolean;
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
      {showAllocation ? <div className="mt-2 flex min-w-0 flex-wrap gap-x-3 gap-y-1 text-fg-muted">
        <span className="tnum truncate">req {requestLabel}</span>
        <span className="tnum truncate">lim {limitLabel}</span>
      </div> : null}
    </div>
  );
}

function SeriesSparkline({
  points,
  label,
}: {
  points: Pick<WorkspaceTelemetrySeriesPoint, "sampleTime" | "value" | "quality">[];
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

function TelemetryWindowSelector({ displayedView, selectedView, onViewChange }: {
  displayedView: TelemetryViewWindow; selectedView: TelemetryViewWindow; onViewChange: (view: TelemetryViewWindow) => void;
}) {
  return <div className="inline-flex rounded-[var(--radius-control)] border border-line p-0.5"
    role="tablist" aria-label="Telemetry window" data-testid="telemetry-view-selector"
    data-awf-displayed-view={displayedView} data-awf-requested-view={selectedView}>
    {TELEMETRY_VIEWS.map(view => {
      const selected = displayedView === view, requested = selectedView === view && !selected;
      return <button key={view} type="button" role="tab" aria-selected={selected}
        data-testid={`telemetry-view-${view}`} data-awf-view-pending={requested ? "true" : undefined}
        title={requested ? `Requested ${view}; showing ${displayedView}` : undefined}
        className={`rounded-[var(--radius-control)] px-2 py-0.5 text-[11px] font-medium transition ${selected ? "bg-surface-2 text-fg" : "text-fg-muted hover:text-fg"}`}
        onClick={() => onViewChange(view)}>{view}</button>;
    })}
  </div>;
}

function vmBytes(value: number | null): string {
  if (value === null) return "Unavailable";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return `${Number(value.toPrecision(4))} ${units[index]}`;
}

function VMEstimateFact({ label, estimate }: { label: string; estimate: VMEstimate | null }) {
  const amount = estimate?.estimated_usd;
  return <div className="min-w-0 border-t border-line py-2 text-xs">
    <div className="flex flex-wrap justify-between gap-2">
      <span className="font-semibold text-fg">{label}</span>
      <span className="tnum text-fg-strong">{amount == null ? "Unpriced" : `${formatWorkloadUsd(Number(amount))} (observed)`}</span>
    </div>
    {estimate && <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-fg-muted">
      <span>{estimate.estimate_state}</span>
      <span className="tnum">Priced {estimate.priced_interval_seconds}s / unpriced {estimate.unpriced_interval_seconds}s</span>
    </div>}
    {estimate?.rate_table_version && <details className="mt-1 min-w-0 text-[11px] text-fg-muted">
      <summary className="cursor-pointer break-words">Rate table: {estimate.rate_table_version}</summary>
      {estimate.evidence.rates?.map((rate, index) => <div key={index} className="mt-1 break-all">
        <div>{rate.sku}: {rate.per_hour} USD/{rate.unit === "instance_hour" ? "instance-hour" : "GiB-hour"}</div>
        <div>Applicable from {formatDateTime(rate.effective_at)}</div>
        <a className="underline" href={rate.source_url} target="_blank" rel="noreferrer">Published rate</a>
        <div className="mono">SHA256 {rate.source_sha256}</div>
      </div>)}
    </details>}
  </div>;
}

function VMWorkspaceTelemetry({ viewModel, selectedView, onViewChange, mode, requestError, lastGoodAt,
  showTelemetry, showAllocation, showCost }: {
  viewModel: VMTelemetryView; selectedView: TelemetryViewWindow; onViewChange: (view: TelemetryViewWindow) => void;
  mode: "live" | "historical"; requestError: string | null; lastGoodAt: string | null;
  showTelemetry: boolean; showAllocation: boolean; showCost: boolean;
}) {
  const { data, cpu, memory } = viewModel;
  const [selectedBoot, setSelectedBoot] = useState(data.node?.boot_generation ?? 0);
  const boots = [...new Set([...(data.node ? [data.node.boot_generation] : []),
    ...cpu.series.map(s => s.bootGeneration), ...memory.series.map(s => s.bootGeneration)])].sort((a, b) => a - b);
  const boot = boots.includes(selectedBoot) ? selectedBoot : data.node?.boot_generation;
  const stale = (mode === "live" && viewModel.isStale) || viewModel.hasFutureTimestamp
    || (showTelemetry && (data.state === "stale" || cpu.quality === "stale" || memory.quality === "stale"))
    || (showAllocation && data.reservation?.quality === "stale");
  const reservation = data.reservation?.values;
  const cost = data.node_cost;
  const historical = data.historical_nodes.length ? data.historical_nodes : data.previous_node ? [data.previous_node] : [];
  if (!showTelemetry && !showAllocation && !showCost) return null;
  return <Panel title="VM resources" icon={<Cpu size={16} aria-hidden />} stale={Boolean(stale)} className="min-w-0"
    action={<div className="flex flex-wrap items-center gap-2">
      <span className="text-[10px] font-medium text-attention-text" data-testid="telemetry-partial-indicator">⚠ partial</span>
      {showTelemetry && <TelemetryWindowSelector displayedView={data.view} selectedView={selectedView} onViewChange={onViewChange} />}
    </div>}>
    <div className="flex min-w-0 flex-col gap-3" data-testid="console-workspace-telemetry" data-awf-resource-kind="vm_node">
      {requestError && <div className="break-words text-xs text-danger-text" data-testid="telemetry-request-error">{requestError}</div>}
      {lastGoodAt && <div className="text-[11px] text-fg-muted" data-testid="telemetry-last-good">Last good {formatDateTime(lastGoodAt)}</div>}
      {(showTelemetry || showCost) && data.node && <div className="min-w-0 text-[11px] text-fg-muted">
        <div>{mode === "historical" ? "Historical" : "Observed"} shared node</div>
        <div className="mono break-all">{data.node.node_id} / physical {data.node.provider_id}</div>
        <div className="tnum">Recovery {data.node.recovery_generation} / Boot {data.node.boot_generation}</div>
        {historical.length > 0 && <details className="mt-1">
          <summary className="cursor-pointer">Physical history ({historical.length})</summary>
          {historical.map(node => <div key={node.provider_id} className="mono mt-1 break-all">
            Physical {node.provider_id} / recovery {node.recovery_generation} / recorded boot {node.boot_generation}
          </div>)}
        </details>}
      </div>}
      {showTelemetry && data.node_visibility === "not_authorized" && <div className="text-xs text-fg-muted">Shared-node metrics unavailable for this scope</div>}
      {showTelemetry && data.node && <>
        <div className="text-[11px] text-fg-muted">Current boot / shared-node measurements</div>
        <div className="grid min-w-0 gap-2 sm:grid-cols-2">
          <div className="min-w-0">
            <ResourceMeter label="CPU" icon={<Cpu size={13} aria-hidden />} usedLabel={cpu.value === null ? "Unavailable" : `${Number((cpu.value * 100).toPrecision(4))}%`}
              requestLabel="" limitLabel="" showAllocation={false} fillPct={cpu.value === null ? null : cpu.value * 100} partial={cpu.quality !== "ok"} />
            <div className="mt-1 text-[11px] text-fg-muted">CPU utilization / {formatDateTime(cpu.sampleTime)}</div>
          </div>
          <div className="min-w-0">
            <ResourceMeter label="Memory" icon={<HardDrive size={13} aria-hidden />} usedLabel={vmBytes(memory.value)}
              requestLabel="" limitLabel="" showAllocation={false} fillPct={null} partial={memory.quality !== "ok"} />
            <div className="mt-1 text-[11px] text-fg-muted">{memory.value === null ? "Memory unavailable" : `Used memory / ${formatDateTime(memory.sampleTime)}`}</div>
          </div>
        </div>
        <div className="min-w-0" data-vm-boot={boot}>
          <label className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-fg-muted">Measured history
            <select aria-label="Telemetry boot" value={boot} onChange={event => setSelectedBoot(Number(event.target.value))}
              className="max-w-full rounded-[var(--radius-control)] border border-line bg-surface px-2 py-1 text-fg">
              {boots.map(value => <option key={value} value={value}>Boot {value}</option>)}
            </select>
          </label>
          <div className="grid min-w-0 gap-2 sm:grid-cols-2">
            <div className="min-w-0"><div className="label-caps mb-1">CPU utilization</div><SeriesSparkline points={cpu.series.find(s => s.bootGeneration === boot)?.points ?? []} label="cpu" /></div>
            <div className="min-w-0"><div className="label-caps mb-1">Memory bytes</div><SeriesSparkline points={memory.series.find(s => s.bootGeneration === boot)?.points ?? []} label="memory" /></div>
          </div>
        </div>
      </>}
      {showAllocation && <div className="min-w-0">
        <div className="label-caps mb-2">Own reservation</div>
        {reservation ? <>
          <div className="grid min-w-0 gap-2 sm:grid-cols-2">
            <Fact label="Steady CPU" value={`${reservation.steady_cpu} cores`} />
            <Fact label="Peak CPU" value={`${reservation.peak_cpu} cores`} />
            <Fact label="Steady memory" value={`${reservation.steady_memory_gb} GB`} />
            <Fact label="Peak memory" value={`${reservation.peak_memory_gb} GB`} />
            <Fact label="Reserved disk" value={reservation.disk_mb === null ? "Unknown" : `${reservation.disk_mb} MB`} />
            <Fact label="DinD slots" value={String(reservation.dind_slots)} />
          </div>
          <div className="mt-1 break-words text-[11px] text-fg-muted">{reservation.phase} / {data.reservation?.quality} / {formatDateTime(data.reservation?.observed_at)}</div>
        </> : <div className="text-xs text-fg-muted">{data.reservation?.state === "none" ? "No active reservation" : "Reservation not recorded"}</div>}
      </div>}
      {showCost && <div className="min-w-0 text-xs">
        <div className="flex flex-wrap items-center justify-between gap-2 font-semibold"><span>Workspace resource cost</span><span>Unallocated</span></div>
        {cost && <>
          <div className="mt-3 flex items-center gap-1.5 font-semibold"><Wallet size={13} aria-hidden />Shared node / partial resource estimate</div>
          <div className="mt-1 text-[11px] text-fg-muted">Observed intervals only; not a workspace bill. LLM, credits, taxes and shared infrastructure excluded.</div>
          <div className="my-2 grid min-w-0 gap-2 sm:grid-cols-2">
            <Fact label="Powered (observed)" value={cost.observed_powered_seconds === null ? "Unknown" : `${cost.observed_powered_seconds}s`} />
            <Fact label="Idle powered (observed)" value={cost.observed_idle_seconds === null ? "Unknown" : `${cost.observed_idle_seconds}s`} />
            <Fact label="Storage (observed)" value={cost.observed_storage_gib_seconds === null ? "Unknown" : `${cost.observed_storage_gib_seconds} GiB-seconds`} />
            <Fact label="Idle storage (observed)" value={cost.observed_idle_storage_gib_seconds === null ? "Unknown" : `${cost.observed_idle_storage_gib_seconds} GiB-seconds`} />
          </div>
          <VMEstimateFact label="Compute" estimate={cost.compute} />
          <VMEstimateFact label="Idle compute (included in compute)" estimate={cost.idle_compute} />
          <VMEstimateFact label="Persistent storage" estimate={cost.persistent_storage} />
          <VMEstimateFact label="Idle storage (included in storage)" estimate={cost.idle_storage} />
        </>}
      </div>}
    </div>
  </Panel>;
}

/**
 * Compact workspace resource + allocation estimate view.
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
  showTelemetry = true,
  showAllocation = true,
  showCost = true,
  workspaceId,
  modelLabel,
}: ConsoleWorkspaceTelemetryProps) {
  if (!available) {
    return null;
  }

  if ("kind" in viewModel) return <VMWorkspaceTelemetry viewModel={viewModel}
    selectedView={selectedView} onViewChange={onViewChange} mode={mode} requestError={requestError}
    lastGoodAt={lastGoodAt} showTelemetry={showTelemetry} showAllocation={showAllocation} showCost={showCost} />;

  const admitted = showAllocation ? viewModel.admitted : null;
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

  // Historical mode suppresses wall-clock aging only; producer stale
  // (envelope state/quality or current sample quality) must still dim/flag
  // the panel, as must future producer time relative to the projection clock.
  // Age-only isStale must not surface in historical mode.
  // Meter usedStale is telemetry-section evidence — when usage chrome is gated
  // off, ignore it so allocation/cost-only facts do not inherit hidden sample
  // stale (live already gates via section-aware viewModel.isStale).
  const producerStale =
    viewModel.state === "stale" ||
    viewModel.quality === "stale" ||
    (showTelemetry &&
      (viewModel.cpu.usedStale || viewModel.memory.usedStale));
  const stale =
    mode === "live"
      ? viewModel.isStale
      : producerStale || viewModel.hasFutureTimestamp;
  // Allocation/cost facts stay visible when telemetry is unsupported; do not
  // hide freshness chrome for aged admitted.observed_at (or other isStale
  // signals) just because usage/history chrome is gated off.
  const showStaleChrome =
    stale && (showTelemetry || showAllocation || showCost);
  const modeLabel = mode === "historical" ? "Historical" : "Live";
  // Envelope-level partial is not implied by nested meter/cost badges; surface it
  // in the panel header (glyph + label) when the producer marks state or quality
  // partial — data_quality_notes stay hidden. Keep this chip independent of the
  // telemetry tab selector so allocation/cost-only panels still qualify values.
  const envelopePartial =
    viewModel.state === "partial" || viewModel.quality === "partial";
  // Highlight the window whose meters/history are on screen. selectedView may
  // diverge while a request is in flight or after a failed window change retains
  // the prior payload — never imply the requested tab owns the displayed data.
  const displayedView = viewModel.view;

  return (
    <Panel
      title="Workspace resources"
      icon={<Cpu size={16} aria-hidden />}
      stale={showStaleChrome}
      staleLabel="stale"
      className="min-w-0"
      action={
        envelopePartial || showTelemetry ? (
          <div className="flex min-w-0 items-center gap-2">
            {envelopePartial ? (
              <span
                className="inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-attention-border bg-attention-soft px-1.5 py-0.5 text-[10px] font-medium text-attention-text"
                data-testid="telemetry-partial-indicator"
                title="Incomplete telemetry or allocation evidence"
              >
                <span aria-hidden>⚠</span>
                partial
              </span>
            ) : null}
            {showTelemetry ? (
              <TelemetryWindowSelector displayedView={displayedView} selectedView={selectedView} onViewChange={onViewChange} />
            ) : null}
          </div>
        ) : null
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

        {showTelemetry || lastGoodAt ? (
          <div className="flex min-w-0 flex-wrap items-center gap-2 text-[11px] text-fg-muted">
            {showTelemetry ? (
              <>
                <span
                  className="inline-flex items-center rounded-[var(--radius-control)] border border-line bg-surface px-1.5 py-0.5 font-medium text-fg"
                  data-testid="telemetry-mode-label"
                >
                  {modeLabel}
                </span>
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
              </>
            ) : null}
            {lastGoodAt ? (
              <span className="tnum" data-testid="telemetry-last-good">
                Last good {formatDateTime(lastGoodAt)}
              </span>
            ) : null}
          </div>
        ) : null}

        {showAllocation && admitted === null && viewModel.state !== "unallocated" ? (
          <div data-testid="telemetry-admission-missing" className="text-xs text-fg-muted">
            Admission not recorded
          </div>
        ) : null}

        {showAllocation && viewModel.state === "unallocated" ? (
          <div
            data-testid="telemetry-unallocated"
            className="rounded-[var(--radius-control)] border border-line bg-surface-2 px-3 py-2 text-xs text-fg-muted"
          >
            Unallocated — shared Core monitor runtime is not free capacity.
          </div>
        ) : (showTelemetry || showAllocation) && viewModel.state !== "unallocated" ? (
          <div className="grid min-w-0 gap-2 sm:grid-cols-2">
            <ResourceMeter
              showAllocation={showAllocation}
              label="CPU"
              icon={<Cpu size={13} aria-hidden />}
              usedLabel={formatCores(showTelemetry ? viewModel.cpu.usedCores : null)}
              requestLabel={cpuRequestLabel}
              limitLabel={cpuLimitLabel}
              fillPct={fillAgainstLimit(
                showTelemetry ? viewModel.cpu.usedCores : null,
                admitted?.cpuLimitCores ?? null,
              )}
              partial={
                (showTelemetry && viewModel.cpu.usedPartial) || Boolean(admitted?.partial)
              }
            />
            <ResourceMeter
              showAllocation={showAllocation}
              label="Memory"
              icon={<HardDrive size={13} aria-hidden />}
              usedLabel={
                !showTelemetry || viewModel.memory.usedBytes == null
                  ? "—"
                  : formatBytesOrUnknown(viewModel.memory.usedBytes)
              }
              requestLabel={memRequestLabel}
              limitLabel={memLimitLabel}
              fillPct={fillAgainstLimit(
                showTelemetry ? viewModel.memory.usedBytes : null,
                admitted?.memoryLimitBytes ?? null,
              )}
              partial={
                (showTelemetry && viewModel.memory.usedPartial) || Boolean(admitted?.partial)
              }
            />
          </div>
        ) : null}

        {showTelemetry ? <div className="grid min-w-0 gap-2 sm:grid-cols-2">
          <div className="min-w-0">
            <div className="label-caps mb-1">CPU samples</div>
            <SeriesSparkline points={viewModel.cpu.series} label="cpu" />
          </div>
          <div className="min-w-0">
            <div className="label-caps mb-1">Memory samples</div>
            <SeriesSparkline points={viewModel.memory.series} label="memory" />
          </div>
        </div> : null}

        {showCost ? <div
          data-testid="telemetry-workload-cost"
          className="rounded-[var(--radius-control)] border border-line bg-surface-2 px-3 py-2 text-xs"
        >
          <div className="flex min-w-0 items-center justify-between gap-2">
            <span className="flex items-center gap-1.5 font-semibold text-fg">
              <Wallet size={13} aria-hidden />
              Allocation estimate
            </span>
            <span
              className="tnum shrink-0 text-sm font-medium text-fg-strong"
              data-testid="telemetry-workload-cost-value"
            >
              {costLabel(viewModel)}
            </span>
          </div>
          <div data-testid="telemetry-estimate-scope" className="mt-1 text-[11px] text-fg-muted">
            {viewModel.estimate.scope === "resource_attempt"
              ? "Retained resource / placement-attempt estimate; independent of chart window."
              : "Allocation estimate for the displayed chart window."}
          </div>
          <div className="mt-1 text-[11px] text-fg-muted">{viewModel.exclusionNote}</div>
        </div> : null}

        <div className="grid min-w-0 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {showCost ? <>
          <Fact
            label="Rate table"
            value={viewModel.estimate.rateTableVersion ?? "—"}
            mono
            stale={showStaleChrome}
          />
          <Fact
            label="Rate source"
            value={viewModel.estimate.rateSource ?? "—"}
            mono
            stale={showStaleChrome}
          />
          <Fact
            label="Priced / unpriced"
            value={
              viewModel.estimate.pricedIntervalSeconds === null ||
              viewModel.estimate.unpricedIntervalSeconds === null
                ? "Not recorded"
                : `${viewModel.estimate.pricedIntervalSeconds}s / ${viewModel.estimate.unpricedIntervalSeconds}s`
            }
            mono
            stale={showStaleChrome}
          />
          </> : null}
          {admitted ? (
            <>
              <Fact
                label="Compute class"
                value={admitted.computeClass ?? "—"}
                stale={showStaleChrome}
              />
              <Fact label="Region" value={admitted.region ?? "—"} stale={showStaleChrome} />
              <Fact
                label="Billable"
                value={admitted.billable == null ? "—" : admitted.billable ? "yes" : "no"}
                stale={showStaleChrome}
              />
            </>
          ) : null}
        </div>
      </div>
    </Panel>
  );
}
