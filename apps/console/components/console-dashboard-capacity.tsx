"use client";

import {
AlertCircle,
ArrowDown,
ArrowUp,
CheckCircle2,
ChevronDown,
ChevronUp,
CircleDot,
Clock3,
FileText,
GitPullRequest,
Loader2,
Server
} from "lucide-react";
import {
useMemo,
useState
} from "react";

import {
bytes,
compactDuration,
compactId,
fallbackLifecycleStages,
formatDateTime,
relativeTime,
statusGlyph,
statusTone,
toneClass,
toneFillClass,
toneTextClass
} from "@/lib/format";
import {
formatRequiredNextAction,
mergeQueueMergedAt,
requiredNextActionTone,
summarizeReadiness,
summarizeRecovery,
summarizeStaleReasons,
summarizeValidation
} from "@/lib/merge-queue-format";
import {
formatOperationDetail,
formatOperationFailure,
formatOperationTitle,
} from "@/lib/operation-format";
import {
isReverseWorkspaceTransition
} from "@/lib/recovery-format";
import type {
  CapacityDimension,
  ConcurrencyLane,
  LocalCapacitySourceValue,
  MergeQueueItem,
  Operation,
  ResourceSaturationSummary,
  RuntimeService,
  WorkspaceAppEndpoint,
  WorkspaceEvent,
  WorkspaceLifecycleStage,
  WorkspaceReliabilitySummary,
  WorkspaceRuntime,
  WorkspaceStatus
} from "@/lib/types";
import {
Badge,
Fact,
MergeQueueStatus,
MutedLine,
Panel,
SmallExternalAnchor,
SortDirection,
Td,
Th,
emptyCapacityDimension,
formatCapacityValue,
formatGb,
formatPercent,
formatPrLinkLabel,
formatScalar,
type CapacityUnit,
} from "./console-dashboard-shared";

export function ResourceCapacityPanel({
  saturation,
  error,
  stale = false,
  summaryStale = false,
  workspaceSummary,
  workspaceSummaryError,
}: {
  saturation: ResourceSaturationSummary | null;
  error: string | null;
  stale?: boolean;
  // Reliability-summary fields (Stuck, Reason coverage) come from a separate
  // feed and dim independently of the saturation-driven panel staleness.
  summaryStale?: boolean;
  workspaceSummary: WorkspaceReliabilitySummary | null;
  workspaceSummaryError: string | null;
}) {
  const totalReason = workspaceSummary ? workspaceSummary.actionable_reason_count + workspaceSummary.unactionable_reason_count : 0;
  const coverage = totalReason > 0 ? Math.round((workspaceSummary!.actionable_reason_count / totalReason) * 100) : 0;
  const showOldestQueued =
    saturation !== null &&
    saturation.capacity_queue.queued_workspace_count > 0 &&
    saturation.capacity_queue.oldest_wait_seconds !== null;
  const queueBlockedReasonEntries = saturation
    ? Object.entries(saturation.capacity_queue.blocked_reason_counts ?? {})
    : [];
  const pressureReasons =
    saturation === null
      ? []
      : saturation.allocated_capacity.pressure_reasons.length > 0
        ? saturation.allocated_capacity.pressure_reasons
        : saturation.capacity.pressure_reasons;
  const runtimeCapacitySource = saturation ? localCapacitySourceLabel(saturation.local_capacity.source as LocalCapacitySourceValue | null) : "";
  const runtimeCapacityLimits = saturation ? localCapacityLimitLabel(saturation) : "";
  const runtimeCapacityDetail = saturation
    ? localCapacityDetail(saturation.local_capacity.source as LocalCapacitySourceValue | null)
    : "";

  return (
    <Panel
      title="Resource / Runtime Capacity"
      icon={<Server size={16} aria-hidden />}
      stale={stale || summaryStale}
      dimBody={false}
    >
      {!saturation ? (
        <MutedLine>{error ? `Unable to load capacity: ${error}` : "Capacity snapshot loading."}</MutedLine>
      ) : (
        <div className="grid gap-3">
          {error ? (
            <div className={`rounded-md border px-3 py-2 text-xs ${toneClass("warn")}`}>
              Showing last capacity snapshot. Refresh failed: {error}
            </div>
          ) : null}
          {workspaceSummaryError ? (
            <div className={`rounded-md border px-3 py-2 text-xs ${toneClass("warn")}`}>
              Unable to load workspace reliability metrics: {workspaceSummaryError}
            </div>
          ) : null}
          {/* Reliability-summary facts dim with the summary feed only — kept out
              of the saturation region so a stale saturation snapshot can't fade
              freshly-refreshed Stuck / Reason coverage. */}
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <Fact
              label="Stuck"
              value={workspaceSummary ? `${workspaceSummary.stuck_count} workspaces` : "—"}
              stale={summaryStale}
            />
            <Fact
              label="Reason Coverage"
              value={workspaceSummary ? `${coverage}% (${totalReason} tracked)` : "—"}
              stale={summaryStale}
            />
          </div>
          {/* Saturation-driven content dims with the saturation feed. */}
          <div data-awf-stale={stale ? "true" : undefined} className="grid gap-3">
          <div className={`rounded-md border px-3 py-2 text-xs ${toneClass("info")}`}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-semibold">Scheduler capacity source</span>
              <span className="mono">{runtimeCapacitySource}</span>
            </div>
            <div className="mt-1">
              {runtimeCapacityDetail}: {runtimeCapacityLimits}.
            </div>
          </div>
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <Fact label="Active" value={`${saturation.workspace_counts.active_total} workspaces`} />
            <Fact
              label="Reserved runtime CPU"
              value={`${formatScalar(saturation.reserved_resources.steady_cpu)} steady / ${formatScalar(
                saturation.reserved_resources.peak_cpu,
              )} peak`}
            />
            <Fact
              label="Allocated runtime CPU"
              value={`${formatScalar(saturation.allocated_resources.steady_cpu)} steady / ${formatScalar(
                saturation.allocated_resources.peak_cpu,
              )} peak`}
            />
            <Fact
              label="Reserved runtime memory"
              value={`${formatGb(saturation.reserved_resources.steady_memory_gb)} steady / ${formatGb(
                saturation.reserved_resources.peak_memory_gb,
              )} peak`}
            />
            <Fact
              label="Capacity queue"
              value={`${saturation.capacity_queue.queued_workspace_count} requested`}
            />
            {showOldestQueued ? (
              <Fact
                label="Oldest queued"
                value={compactDuration(saturation.capacity_queue.oldest_wait_seconds)}
              />
            ) : null}
            <Fact label="Reserved disk" value={formatCapacityValue(saturation.reserved_resources.disk_mb, "mb")} />
            <Fact label="DinD slots" value={`${saturation.reserved_resources.dind_slots} reserved`} />
            <Fact
              label="Disk free"
              value={`${bytes(saturation.disk.free_bytes)} / ${formatPercent(saturation.disk.percent_free)}`}
            />
          </div>
          <StatusCountStrip counts={saturation.workspace_counts} />
          <div className="grid gap-2 md:grid-cols-2">
            <LaneMeter label="Provision" lane={saturation.concurrency.provision} />
            <LaneMeter label="Execution" lane={saturation.concurrency.execution} />
          </div>
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
            <ResourceDimensionMeter label="Runtime CPU peak" dimension={saturation.allocated_capacity.peak_cpu} unit="cores" />
            <ResourceDimensionMeter label="Runtime memory peak" dimension={saturation.allocated_capacity.peak_memory_gb} unit="gb" />
            <ResourceDimensionMeter label="Disk" dimension={saturation.allocated_capacity.disk_mb} unit="mb" />
            <ResourceDimensionMeter label="DinD" dimension={saturation.allocated_capacity.dind_slots} unit="slots" />
          </div>
          {queueBlockedReasonEntries.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {queueBlockedReasonEntries.map(([reason, count]) => (
                <span
                  key={reason}
                  className={`inline-flex min-h-6 items-center rounded-md border px-2 text-[11px] font-medium ${toneClass(
                    "bad",
                  )}`}
                  title="Deferred blockers count the first FIFO frontier per constraint; unsatisfiable requests count each workspace."
                >
                  {reason}: {count} frontier-counted blocker{count === 1 ? "" : "s"}
                </span>
              ))}
            </div>
          ) : pressureReasons.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {pressureReasons.map((reason) => (
                <span
                  key={reason}
                  className={`inline-flex min-h-6 items-center rounded-md border px-2 text-[11px] font-medium ${toneClass(
                    reason.endsWith("_UNKNOWN") ? "warn" : "bad",
                  )}`}
                >
                  {reason}
                </span>
              ))}
            </div>
          ) : null}
          <div className="grid gap-2 md:grid-cols-2">
            <div className={`rounded-md border px-3 py-2 text-xs ${toneClass(saturation.disk.ok ? "good" : "bad")}`}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold">Disk {saturation.disk.status}</span>
                <span className="mono">{saturation.disk.reason}</span>
              </div>
              <div className="mt-1 text-fg-muted">
                threshold {bytes(saturation.disk.threshold_bytes)} / checked {saturation.disk.checked_path}
              </div>
            </div>
            <div
              className={`rounded-md border px-3 py-2 text-xs ${toneClass(
                saturation.admission.ok
                  ? saturation.admission.status === "saturated"
                    ? "warn"
                    : "good"
                  : "bad",
              )}`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold">Admission {saturation.admission.status}</span>
                <span className="mono">{saturation.admission.reason}</span>
              </div>
              {saturation.admission.detail ? (
                <div className="mt-1 text-fg-muted">{saturation.admission.detail}</div>
              ) : null}
            </div>
          </div>
          <div className="text-[11px] text-fg-muted">
            generated {relativeTime(saturation.generated_at)}
          </div>
          </div>
        </div>
      )}
    </Panel>
  );
}

function localCapacitySourceLabel(source: LocalCapacitySourceValue | null): string {
  switch (source) {
    case "docker":
      return "Docker runtime";
    case "operator_config":
      return "operator config";
    case "mixed":
      return "mixed config/runtime";
    case null:
      return "runtime capacity";
    default:
      return "runtime capacity";
  }
}

function localCapacityLimitLabel(saturation: ResourceSaturationSummary): string {
  const cpuLimit = saturation.local_capacity.cpu_cores ?? saturation.allocated_capacity.peak_cpu.limit;
  const memoryLimit =
    saturation.local_capacity.memory_gb ?? saturation.allocated_capacity.peak_memory_gb.limit;
  const cpu =
    cpuLimit != null
      ? formatScalar(cpuLimit) + " cores"
      : "CPU unknown";
  const memory =
    memoryLimit != null
      ? formatGb(memoryLimit)
      : "memory unknown";
  return `${cpu} / ${memory}`;
}

function localCapacityDetail(source: LocalCapacitySourceValue | null): string {
  switch (source) {
    case "docker":
      return "Limits from docker info (native Docker Engine: host resources; Docker Desktop: VM allocation, host may be larger)";
    case "operator_config":
      return "Limits are the AWF_LOCAL_CAPACITY_* operator overrides used by the scheduler";
    case "mixed":
      return "Limits combine AWF_LOCAL_CAPACITY_* overrides with Docker runtime detection";
    case null:
    default:
      return "Limits from runtime capacity AWF detects (native Docker Engine: host resources; Docker Desktop: VM allocation)";
  }
}

export function MergeQueuePanel({
  items,
  hasMore,
  status,
  error,
  stale = false,
}: {
  items: MergeQueueItem[];
  hasMore: boolean;
  status: MergeQueueStatus;
  error: string | null;
  stale?: boolean;
}) {
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());
  const hasSnapshot = items.length > 0;
  const isLoading = status === "loading";
  const isError = status === "error";
  const summary =
    isLoading && !hasSnapshot
      ? "loading"
      : isError && !hasSnapshot
        ? "error"
        : `${items.length}${hasMore ? "+" : ""} candidate${items.length === 1 ? "" : "s"}`;

  return (
    <Panel
      title="Merge Queue"
      icon={<GitPullRequest size={16} aria-hidden />}
      stale={stale}
      fill
      className="2xl:h-full"
      action={
        <span className="rounded-[var(--radius-control)] border border-line bg-surface-2 px-2.5 py-1 text-[11px] text-fg-muted">
          {summary}
        </span>
      }
    >
      {isLoading && !hasSnapshot ? (
        <MutedLine>Merge queue snapshot loading.</MutedLine>
      ) : isError && !hasSnapshot ? (
        <MutedLine>Unable to load merge queue: {error}</MutedLine>
      ) : !hasSnapshot ? (
        <MutedLine>No PR-backed merge candidates are queued.</MutedLine>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-2">
          {error ? (
            <div className={`rounded-md border px-3 py-2 text-xs ${toneClass("warn")}`}>
              Showing last merge queue snapshot. Refresh failed: {error}
            </div>
          ) : null}
          {hasMore ? (
            <div className={`rounded-md border px-3 py-2 text-xs ${toneClass("neutral")}`}>
              Showing first {items.length} merge candidates. More are queued.
            </div>
          ) : null}
          <div className="grid max-h-[460px] min-h-0 content-start gap-2 overflow-auto pr-1 2xl:max-h-none 2xl:flex-1">
            {items.map((item, index) => {
              const rowKey = item.candidate_id ?? item.workspace_id;
              const expanded = expandedRows.has(rowKey);
              return (
                <MergeQueueRow
                  key={rowKey}
                  item={item}
                  position={index + 1}
                  expanded={expanded}
                  onToggle={() =>
                    setExpandedRows((current) => {
                      const next = new Set(current);
                      if (next.has(rowKey)) {
                        next.delete(rowKey);
                      } else {
                        next.add(rowKey);
                      }
                      return next;
                    })
                  }
                />
              );
            })}
          </div>
        </div>
      )}
    </Panel>
  );
}

export function MergeQueueRow({
  item,
  position,
  expanded,
  onToggle,
}: {
  item: MergeQueueItem;
  position: number;
  expanded: boolean;
  onToggle: () => void;
}) {
  const rowDetailsId = `merge-candidate-${item.candidate_id ?? item.workspace_id}`;
  const actionLabel = formatRequiredNextAction(item.required_next_action, item.merge_blocker_reason);
  const readiness = summarizeReadiness(item);
  const stale = summarizeStaleReasons(item);
  const validation = summarizeValidation(item);
  const recovery = summarizeRecovery(item);
  const mergedAt = mergeQueueMergedAt(item);
  return (
    <article className="grid gap-2 border-b border-line pb-2 text-xs last:border-b-0 last:pb-0">
      <div className="flex min-w-0 items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <span className="mono shrink-0 rounded-md border border-line bg-surface-2 px-1.5 py-0.5 text-[10px] text-fg-muted">
              #{position}
            </span>
            <h3 className="truncate text-sm font-semibold text-fg-strong">{item.title}</h3>
          </div>
          <div className="mono mt-1 truncate text-[11px] text-fg-muted">{item.workspace_id}</div>
          <div className="mt-1 flex min-w-0 flex-wrap gap-x-2 gap-y-1 text-[11px] text-fg-muted">
            <span className="truncate">created {formatDateTime(item.created_at)}</span>
            <span className="truncate">merged {formatDateTime(mergedAt)}</span>
            <span className="truncate">
              base <span className="mono text-fg">{item.base_branch}</span>
            </span>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">
          <Badge value={item.status} />
          <SmallExternalAnchor href={item.pr_url} label={formatPrLinkLabel(item.pr_url)} />
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={expanded}
            aria-controls={rowDetailsId}
            className="inline-flex h-7 items-center gap-1 rounded-md border border-line bg-surface px-2 text-[11px] font-medium text-fg hover:bg-surface-2"
          >
            {expanded ? <ChevronUp size={13} aria-hidden /> : <ChevronDown size={13} aria-hidden />}
            {expanded ? "Hide" : "Details"}
          </button>
        </div>
      </div>
      {expanded ? (
        <div id={rowDetailsId} className="grid gap-2">
          <div className="grid gap-1 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-5">
            <QueueChip
              label="Action"
              value={recovery.recommendedActionLabel}
              tone={requiredNextActionTone(item.required_next_action, item.merge_blocker_reason)}
            />
            <QueueChip
              label="Blocker"
              value={recovery.blockerLabel}
              detail={recovery.blockerDetail}
              tone={mergeBlockerTone(item.merge_blocker_reason)}
            />
            <QueueChip label="Required" value={recovery.requiredTierLabel} detail={item.task_class ?? "task class unknown"} />
            <QueueChip
              label="Satisfied"
              value={recovery.latestSatisfiedTierLabel}
              detail={recovery.latestSatisfiedTierDetail}
              tone={satisfiedTierTone(recovery.latestSatisfiedTierLabel)}
            />
            <QueueChip
              label="Freshness"
              value={recovery.freshnessLabel}
              detail={recovery.targetRangeLabel}
              tone={freshnessTone(recovery.freshnessLabel)}
            />
            <QueueChip
              label="Readiness"
              value={`${readiness.canonicalLabel} / ${readiness.label}`}
              detail={readiness.detail}
              tone={readinessTone(readiness.label)}
            />
            <QueueChip
              label="Candidate"
              value={`${recovery.candidateLabel} / ${recovery.attemptLabel}`}
              detail={item.candidate_status ?? "unknown"}
              mono
            />
            <QueueChip
              label="Base"
              value={recovery.baseShaLabel}
              detail={item.latest_validation?.target_branch ?? item.base_branch}
              mono
            />
            <QueueChip
              label="Targets"
              value={recovery.targetRangeLabel}
              detail={`${recovery.validatedTargetShaLabel} / ${recovery.currentTargetShaLabel}`}
              mono
            />
            <QueueChip
              label="Stale"
              value={recovery.staleReasonLabel}
              detail={recovery.staleReasonDetail}
              tone={
                recovery.staleReasonBlockingCount > 0
                  ? "warn"
                  : recovery.staleReasonAdvisoryCount > 0
                    ? "info"
                    : "neutral"
              }
              mono={recovery.staleReasonCount > 0}
            />
            <QueueChip
              label="Queue"
              value={recovery.queueBlockerLabel}
              detail={recovery.queueBlockerDetail}
              tone={recovery.queueBlockerCount > 0 ? "warn" : "neutral"}
            />
            <QueueChip
              label="Policy"
              value={recovery.policyFindingLabel}
              detail={recovery.policyFindingDetail}
              tone={recovery.policyFindingCount > 0 ? "bad" : "neutral"}
            />
            <QueueChip label="Validation" value={validation.label} detail={validation.detail} tone={validationTone(item)} />
            <QueueChip label="Coverage" value={validation.coverageLabel} detail={validation.headLabel} mono />
          </div>
          <div className="grid gap-1 sm:grid-cols-3">
            <QueueDatum label="Candidate" value={item.candidate_id ? compactId(item.candidate_id, 10) : "legacy"} mono />
            <QueueDatum label="Attempt" value={item.attempt_id ? compactId(item.attempt_id, 10) : "none"} mono />
            <QueueDatum
              label="Canonical"
              value={readiness.canonicalLabel}
              tone={item.canonical ? statusTone("completed") : statusTone("failed")}
            />
            <QueueDatum label="Candidate status" value={item.candidate_status ?? "unknown"} mono />
            <QueueDatum
              label="Action"
              value={actionLabel}
              tone={requiredNextActionTone(item.required_next_action, item.merge_blocker_reason)}
            />
            <QueueDatum label="Readiness" value={readiness.detail} mono tone={readinessTone(readiness.label)} />
            <QueueDatum label="Mode" value={item.auto_merge ? "auto-merge" : "manual"} />
            <QueueDatum label="Created" value={formatDateTime(item.created_at)} />
            <QueueDatum label="Merged" value={formatDateTime(mergedAt)} />
            <QueueDatum label="Last event" value={lastEventReason(item.last_event)} mono />
            <QueueDatum label="Blocker" value={item.merge_blocker_reason} mono tone={mergeBlockerTone(item.merge_blocker_reason)} />
            <QueueDatum label="Required tier" value={recovery.requiredTierLabel} />
            <QueueDatum label="Latest satisfied" value={recovery.latestSatisfiedTierLabel} tone={satisfiedTierTone(recovery.latestSatisfiedTierLabel)} />
            <QueueDatum label="Validation" value={validation.label} tone={validationTone(item)} />
            <QueueDatum label="Freshness" value={recovery.freshnessLabel} tone={freshnessTone(recovery.freshnessLabel)} />
            <QueueDatum label="Base SHA" value={recovery.baseShaLabel} mono />
            <QueueDatum label="Workspace head" value={recovery.workspaceHeadShaLabel} mono />
            <QueueDatum label="Validated target" value={recovery.validatedTargetShaLabel} mono />
            <QueueDatum label="Current target" value={recovery.currentTargetShaLabel} mono />
            <QueueDatum label="Validation heads" value={recovery.targetRangeLabel} mono />
            <QueueDatum label="Reason" value={recovery.validationReasonLabel} mono />
            <QueueDatum label="Command hash" value={recovery.commandHashLabel} mono />
            <QueueDatum label="Profile" value={recovery.profileLabel} />
            <QueueDatum label="Env identity" value={recovery.environmentLabel} mono />
            <QueueDatum label="Coverage" value={validation.coverageLabel} mono />
          </div>
          <div className="grid gap-1 text-[11px] text-fg-muted sm:grid-cols-2">
            <span className="truncate">
              base <span className="mono text-fg">{item.base_branch}</span>
            </span>
            <span className="truncate">
              branch <span className="mono text-fg">{item.branch_name ?? "none"}</span>
            </span>
            <span className="truncate">updated {formatDateTime(item.updated_at)}</span>
          </div>
          <MergeQueueBlockerDetails blockers={item.queue_blockers ?? []} />
          <MergeQueueStaleReasonDetails reasons={stale.activeReasons} />
          <MergeQueuePolicyFindingDetails findings={item.policy_findings ?? []} />
        </div>
      ) : null}
    </article>
  );
}

export function QueueDatum({
  label,
  value,
  mono = false,
  tone,
}: {
  label: string;
  value: string;
  mono?: boolean;
  tone?: ReturnType<typeof statusTone>;
}) {
  return (
    <div className={`min-w-0 rounded-[var(--radius-control)] border px-2 py-1.5 ${tone ? toneClass(tone) : "border-line bg-surface-2"}`}>
      <div className="label-caps">{label}</div>
      <div className={`${mono ? "mono" : "tnum"} truncate text-[11px] ${tone && tone !== "neutral" ? "" : "text-fg"}`}>
        {value}
      </div>
    </div>
  );
}

export function QueueChip({
  label,
  value,
  detail,
  mono = false,
  tone = "neutral",
}: {
  label: string;
  value: string;
  detail?: string;
  mono?: boolean;
  tone?: ReturnType<typeof statusTone>;
}) {
  return (
    <div
      title={detail ? `${label}: ${value} (${detail})` : `${label}: ${value}`}
      className={`min-w-0 rounded-[var(--radius-control)] border px-2 py-1 ${toneClass(tone)}`}
    >
      <div className="label-caps">{label}</div>
      <div className={`${mono ? "mono" : "tnum"} truncate text-[11px] font-medium ${tone && tone !== "neutral" ? "" : "text-fg"}`}>
        {value}
      </div>
      {detail ? <div className="truncate text-[10px] text-fg-faint">{detail}</div> : null}
    </div>
  );
}

export function MergeQueueBlockerDetails({ blockers }: { blockers: MergeQueueItem["queue_blockers"] }) {
  if (blockers.length === 0) {
    return null;
  }
  return (
    <div className={`grid gap-1 rounded-md border px-2 py-1.5 ${toneClass("warn")}`}>
      <div className="text-[10px] font-medium">Queue blockers</div>
      {blockers.map((blocker) => (
        <div key={blocker.candidate_id} className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[11px]">
          <span className="truncate font-medium">{blocker.title}</span>
          <span className="mono">{blocker.pr_number ? `#${blocker.pr_number}` : compactId(blocker.candidate_id, 10)}</span>
          <span className="mono">{blocker.blocker_state}</span>
          <span className="mono">{blocker.status}</span>
          <span className="mono truncate">{blocker.reason_code}</span>
          <SmallExternalAnchor
            href={blocker.pr_url}
            label={formatPrLinkLabel(blocker.pr_url, blocker.pr_number)}
          />
        </div>
      ))}
    </div>
  );
}

export function MergeQueueStaleReasonDetails({ reasons }: { reasons: MergeQueueItem["stale_reasons"] }) {
  if (reasons.length === 0) {
    return null;
  }
  return (
    <div className={`grid gap-1 rounded-md border px-2 py-1.5 ${toneClass("warn")}`}>
      <div className="text-[10px] font-medium">Active stale reasons</div>
      {reasons.map((reason) => (
        <div key={reason.id} className="grid min-w-0 gap-0.5 text-[11px]">
          <div className="flex min-w-0 flex-wrap gap-x-2 gap-y-1">
            <span className="mono font-medium">{reason.reason_code}</span>
            <span className="mono">{reason.severity}</span>
            <span className="mono">{reason.trigger_type}</span>
            <span className="mono truncate">{reason.trigger_ref ?? "no trigger ref"}</span>
            <span>detected {formatDateTime(reason.detected_at)}</span>
          </div>
          <div className="truncate">{reason.explanation}</div>
        </div>
      ))}
    </div>
  );
}

export function MergeQueuePolicyFindingDetails({ findings }: { findings: MergeQueueItem["policy_findings"] }) {
  if (findings.length === 0) {
    return null;
  }
  return (
    <div className={`grid gap-1 rounded-md border px-2 py-1.5 ${toneClass("bad")}`}>
      <div className="text-[10px] font-medium">Policy findings</div>
      {findings.map((finding) => (
        <div key={finding.id} className="grid min-w-0 gap-0.5 text-[11px]">
          <div className="flex min-w-0 flex-wrap gap-x-2 gap-y-1">
            <span className="mono font-medium">{finding.reason_code}</span>
            <span className="mono">{finding.severity}</span>
            <span className="mono truncate">{finding.subject_path ?? "no subject path"}</span>
            <span>detected {formatDateTime(finding.detected_at)}</span>
          </div>
          <div className="truncate">{finding.explanation}</div>
        </div>
      ))}
    </div>
  );
}

export function lastEventReason(event: WorkspaceEvent | null): string {
  if (!event) {
    return "none";
  }
  return event.reason_code ?? event.event_type;
}

export function mergeBlockerTone(reason: MergeQueueItem["merge_blocker_reason"]): ReturnType<typeof statusTone> {
  switch (reason) {
    case "ready_to_merge_or_waiting_for_github":
    case "completed":
      return "good";
    case "manual_merge_required":
    case "waiting_for_monitor":
    case "waiting_for_older_candidate":
    case "stale":
      return "warn";
    case "failed_or_cancelled":
    case "not_canonical":
    case "policy_blocked":
      return "bad";
    case "workspace_not_terminal":
      return "info";
    default:
      return "info";
  }
}

export function readinessTone(label: string): ReturnType<typeof statusTone> {
  switch (label) {
    case "ready":
    case "completed":
      return "good";
    case "manual":
    case "waiting":
    case "stale":
    case "blocked":
      return "warn";
    case "failed/cancelled":
    case "not canonical":
      return "bad";
    case "legacy":
      return "neutral";
    default:
      return "info";
  }
}

export function validationTone(item: MergeQueueItem): ReturnType<typeof statusTone> {
  const validation = item.latest_validation;
  if (!validation) {
    return "neutral";
  }
  if (validation.status === "failed" || validation.coverage_status === "failed") {
    return "bad";
  }
  if (validation.fresh_for_target === false) {
    return "warn";
  }
  if (validation.status === "succeeded") {
    return "good";
  }
  if (validation.status === "running") {
    return "info";
  }
  return "neutral";
}

export function freshnessTone(label: string): ReturnType<typeof statusTone> {
  if (label === "fresh") {
    return "good";
  }
  if (label.includes("stale")) {
    return "warn";
  }
  return "neutral";
}

export function satisfiedTierTone(label: string): ReturnType<typeof statusTone> {
  if (label.startsWith("T")) {
    return "good";
  }
  if (label.startsWith("unknown")) {
    return "warn";
  }
  return "neutral";
}

export function StatusCountStrip({ counts }: { counts: ResourceSaturationSummary["workspace_counts"] }) {
  const items: [string, string, number][] = [
    ["requested", "requested", counts.requested],
    ["provisioning", "provisioning", counts.provisioning],
    ["ready", "ready", counts.ready],
    ["running", "running", counts.running],
    ["validating", "validating", counts.validating],
    ["pushing", "pushing", counts.pushing],
    ["pr", "monitoring_pr", counts.monitoring_pr],
    ["destroying", "destroying", counts.destroying],
  ];

  return (
    <div className="grid grid-cols-2 gap-1 text-xs sm:grid-cols-4 xl:grid-cols-8">
      {items.map(([label, status, value]) => {
        const tone = statusTone(status);
        return (
          <div key={label} className="min-w-0 rounded-[var(--radius-control)] border border-line bg-surface-2 px-2 py-1.5">
            <div className="label-caps flex items-center gap-1 truncate">
              <span aria-hidden className={toneTextClass(tone)}>
                {statusGlyph(status)}
              </span>
              {label}
            </div>
            <div className={`mono text-sm ${value > 0 ? toneTextClass(tone) : "text-fg"}`}>{value}</div>
          </div>
        );
      })}
    </div>
  );
}

export function LaneMeter({ label, lane }: { label: string; lane: ConcurrencyLane }) {
  const hasFiniteLimit = lane.limit > 0;
  const fill = hasFiniteLimit ? Math.min(100, Math.max(0, (lane.in_use / lane.limit) * 100)) : 0;
  const saturated = hasFiniteLimit && lane.in_use >= lane.limit;
  const limitLabel = hasFiniteLimit ? `${lane.in_use}/${lane.limit}` : `${lane.in_use} active`;

  return (
    <div className="rounded-[var(--radius-control)] border border-line bg-surface-2 px-3 py-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold text-fg">{label}</span>
        <span className={`tnum ${saturated ? "font-semibold text-attention-text" : "text-fg-muted"}`}>
          {hasFiniteLimit ? `${limitLabel} in use` : `${limitLabel} / no limit`}
        </span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-line">
        <div className={saturated ? "h-full bg-attention" : "h-full bg-info"} style={{ width: `${fill}%` }} />
      </div>
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-fg-muted">
        <span>{lane.queued} queued</span>
        <span>{lane.available} available</span>
      </div>
    </div>
  );
}

export function ResourceDimensionMeter({
  label,
  dimension,
  unit,
}: {
  label: string;
  dimension?: CapacityDimension | null;
  unit: CapacityUnit;
}) {
  const safeDimension = dimension ?? emptyCapacityDimension;
  const limit = safeDimension.limit;
  const hasLimit = limit !== null && limit > 0;
  const fill = hasLimit ? Math.min(100, Math.max(0, (safeDimension.reserved / limit) * 100)) : 0;
  const tone = safeDimension.reason_code ? (safeDimension.reason_code.endsWith("_UNKNOWN") ? "warn" : "bad") : "good";

  return (
    <div className={`rounded-[var(--radius-control)] border px-3 py-2 text-xs ${toneClass(tone)}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold text-fg">{label}</span>
        <span className="mono text-fg-strong">
          {formatCapacityValue(safeDimension.reserved, unit)}
          {hasLimit ? ` / ${formatCapacityValue(limit, unit)}` : " / unknown"}
        </span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-line">
        <div className={`h-full ${toneFillClass(tone)}`} style={{ width: `${fill}%` }} />
      </div>
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-fg-strong">
        <span>{formatCapacityValue(safeDimension.available, unit)} available</span>
        <span>{formatCapacityValue(safeDimension.available_after_next_default, unit)} after next</span>
      </div>
      {safeDimension.reason_code ? <div className="mono mt-1 truncate text-[11px]">{safeDimension.reason_code}</div> : null}
    </div>
  );
}

export function LifecycleRail({
  status,
  lifecycle,
  terminalSourceStage,
}: {
  status: WorkspaceStatus;
  lifecycle: WorkspaceLifecycleStage[];
  terminalSourceStage: string | null;
}) {
  const terminal = status === "failed" || status === "cancelled";
  const stages: WorkspaceLifecycleStage[] =
    lifecycle.length > 0 ? lifecycle : fallbackLifecycleStages(status, terminalSourceStage);
  return (
    <Panel title="Lifecycle" icon={<GitPullRequest size={16} aria-hidden />}>
      <div className="grid gap-2 md:grid-cols-4 xl:grid-cols-8">
        {stages.map((stage) => {
          const isActive = stage.status === "active";
          const isPr = stage.stage === "monitoring_pr";
          const cellTone =
            stage.status === "completed"
              ? "good"
              : isActive
                ? "info"
                : stage.status === "terminal_skipped"
                  ? "warn"
                  : "neutral";
          return (
            <div
              key={stage.stage}
              className={`min-h-16 rounded-[var(--radius-control)] border p-2 ${
                stage.status === "pending" ? "border-line bg-surface" : toneClass(cellTone)
              }`}
            >
              <div className="flex items-center gap-1.5">
                {stage.status === "completed" ? (
                  <CheckCircle2 size={14} className="text-healthy" aria-hidden />
                ) : isActive ? (
                  isPr ? (
                    <GitPullRequest size={14} className="text-info" aria-hidden />
                  ) : (
                    <CircleDot size={14} className="text-info" aria-hidden />
                  )
                ) : stage.status === "terminal_skipped" ? (
                  <AlertCircle size={14} className="text-attention" aria-hidden />
                ) : (
                  <Clock3 size={14} className="text-fg-faint" aria-hidden />
                )}
                <span className="truncate text-xs font-medium">{stage.stage}</span>
              </div>
              <div className="mt-2 grid gap-1 text-[11px] text-fg-muted">
                <div className="truncate">start {formatDateTime(stage.started_at)}</div>
                <div className="truncate">
                  end {stage.status === "active" ? "active" : formatDateTime(stage.ended_at)}
                </div>
                <div className="mono">{compactDuration(stage.duration_seconds)}</div>
              </div>
            </div>
          );
        })}
      </div>
      {terminal ? (
        <div className="mt-3 inline-flex items-center gap-2 rounded-[var(--radius-control)] border border-attention-border bg-attention-soft px-3 py-2 text-xs text-attention-text">
          <AlertCircle size={14} aria-hidden />
          Terminal state: {status}
        </div>
      ) : null}
    </Panel>
  );
}

export function terminalLifecycleSourceStage(
  status: WorkspaceStatus,
  events: WorkspaceEvent[],
  lastEvent: WorkspaceEvent | null,
  currentPhase: string,
): string | null {
  if (status !== "failed" && status !== "cancelled") {
    return null;
  }
  const terminalEvent =
    events.find((event) => event.event_type === "workspace.state_changed" && event.new_state === status) ??
    (lastEvent?.event_type === "workspace.state_changed" && lastEvent.new_state === status
      ? lastEvent
      : null);
  return terminalEvent?.old_state ?? currentPhase;
}

export function RuntimePanel({ runtime }: { runtime: WorkspaceRuntime | null }) {
  const services = Array.isArray(runtime?.services) ? runtime.services : [];
  const appEndpoints = Array.isArray(runtime?.app_endpoints) ? runtime.app_endpoints : [];

  return (
    <Panel title="Runtime" icon={<Server size={16} aria-hidden />}>
      {!runtime ? (
        <MutedLine>Runtime snapshot unavailable.</MutedLine>
      ) : (
        <div className="grid gap-3">
          <div className="grid gap-2 sm:grid-cols-3">
            <Fact label="Compose project" value={runtime.compose_project_name ?? "—"} mono />
            <Fact label="Stack" value={runtime.stack_state} />
            <Fact label="Services" value={String(services.length)} />
          </div>
          {runtime.reason ? (
            <div className={`rounded-md border px-3 py-2 text-xs ${toneClass("warn")}`}>
              {runtime.reason}
            </div>
          ) : null}
          {appEndpoints.length > 0 ? (
            <EndpointTable endpoints={appEndpoints} />
          ) : null}
          <div className="overflow-auto rounded-md border border-line">
            <table className="w-full min-w-full table-fixed text-left text-xs md:min-w-[720px]">
              <thead className="bg-surface-2 text-fg-muted">
                <tr>
                  <Th>Name</Th>
                  <Th>State</Th>
                  <Th>Health</Th>
                  <Th>Image</Th>
                  <Th>Ports</Th>
                  <Th>Started</Th>
                </tr>
              </thead>
              <tbody>
                {services.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center text-fg-muted">
                      No active containers reported.
                    </td>
                  </tr>
                ) : (
                  services.map((service) => <RuntimeRow key={service.name} item={service} />)
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Panel>
  );
}

export function EndpointTable({ endpoints }: { endpoints: WorkspaceAppEndpoint[] }) {
  return (
    <div className="overflow-auto rounded-md border border-line">
      <table className="w-full min-w-full table-fixed text-left text-xs md:min-w-[680px]">
        <thead className="bg-surface-2 text-fg-muted">
          <tr>
            <Th>Name</Th>
            <Th>Service</Th>
            <Th>Visibility</Th>
            <Th>Internal URL</Th>
            <Th>Health</Th>
          </tr>
        </thead>
        <tbody>
          {endpoints.map((endpoint) => (
            <tr key={endpoint.name} className="border-t border-line">
              <Td>
                <div className="font-medium">{endpoint.name}</div>
              </Td>
              <Td>
                <span className="mono">{endpoint.service}</span>
                <span className="text-fg-muted">:{endpoint.port}</span>
              </Td>
              <Td>
                <Badge value={endpoint.visibility} />
              </Td>
              <Td className="mono max-w-[280px] truncate">{endpoint.internal_url}</Td>
              <Td>
                {endpoint.health ? (
                  <span className="mono">
                    {endpoint.health.method} {endpoint.health.path}{" "}
                    {endpoint.health.expected_status}
                  </span>
                ) : (
                  "—"
                )}
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RuntimeRow({ item }: { item: RuntimeService }) {
  return (
    <tr className="border-t border-line">
      <Td>
        <div className="font-medium">{item.name}</div>
        <div className="mono text-[11px] text-fg-muted">{compactId(item.container_id, 10)}</div>
      </Td>
      <Td>
        <Badge value={item.state} />
        <div className="mt-1 text-fg-muted">{item.status ?? "—"}</div>
      </Td>
      <Td>{item.health ? <Badge value={item.health} /> : "—"}</Td>
      <Td className="mono max-w-[220px] truncate">{item.image ?? "—"}</Td>
      <Td>{item.ports.length > 0 ? item.ports.join(", ") : "—"}</Td>
      <Td>{formatDateTime(item.started_at)}</Td>
    </tr>
  );
}

export function OperationsPanel({ operations }: { operations: Operation[] }) {
  return (
    <Panel title="Operations" icon={<Loader2 size={16} aria-hidden />}>
      {operations.length === 0 ? (
        <MutedLine>No operations recorded.</MutedLine>
      ) : (
        <div className="grid gap-2">
          {operations.slice(0, 8).map((operation) => {
            const operationFailure = formatOperationFailure(operation);

            return (
              <div
                key={operation.id}
                className="grid gap-1 rounded-md border border-line bg-surface px-3 py-2 text-xs sm:grid-cols-[1fr_auto]"
              >
                <div>
                  <span className="font-medium">{formatOperationTitle(operation)}</span>
                  <span className="mono ml-2 text-fg-muted">{compactId(operation.id, 10)}</span>
                </div>
                <div className="flex items-center gap-2">
                  <Badge value={operation.status} />
                  <span className="text-fg-muted">{formatDateTime(operation.created_at)}</span>
                </div>
                <div className="text-fg-muted sm:col-span-2">
                  {formatOperationDetail(operation)}
                </div>
                {operationFailure ? (
                  <div className="text-danger-text sm:col-span-2">
                    {operationFailure}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

export function EventsPanel({ events }: { events: WorkspaceEvent[] }) {
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const sortedEvents = useMemo(() => {
    return [...events].sort((left, right) => {
      const leftTime = Date.parse(left.occurred_at) || 0;
      const rightTime = Date.parse(right.occurred_at) || 0;
      const timeDelta = sortDirection === "desc" ? rightTime - leftTime : leftTime - rightTime;

      if (timeDelta !== 0) {
        return timeDelta;
      }

      return sortDirection === "desc" ? right.id.localeCompare(left.id) : left.id.localeCompare(right.id);
    });
  }, [events, sortDirection]);

  return (
    <Panel
      title="Timeline"
      icon={<FileText size={16} aria-hidden />}
      action={
        <button
          type="button"
          onClick={() => setSortDirection((current) => (current === "desc" ? "asc" : "desc"))}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-line bg-surface px-2.5 text-xs text-fg transition hover:bg-surface-2"
          title={sortDirection === "desc" ? "Descending" : "Ascending"}
        >
          {sortDirection === "desc" ? <ArrowDown size={13} aria-hidden /> : <ArrowUp size={13} aria-hidden />}
          {sortDirection}
        </button>
      }
    >
      {events.length === 0 ? (
        <MutedLine>No events recorded.</MutedLine>
      ) : (
        <div className="max-h-[360px] overflow-auto">
          {sortedEvents.map((event) => {
            const reverse = isReverseWorkspaceTransition(event);
            return (
              <div
                key={event.id}
                className={`grid gap-1 border-b py-2 text-xs ${
                  reverse
                    ? "border-attention-border bg-attention-soft px-2"
                    : "border-line"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{event.event_type}</span>
                  <span className="text-fg-muted">{formatDateTime(event.occurred_at)}</span>
                </div>
                <div className="flex flex-wrap gap-2 text-fg-muted">
                  <span>{event.old_state ?? "—"} → {event.new_state ?? "—"}</span>
                  {event.reason_code ? <span className="mono">{event.reason_code}</span> : null}
                  {reverse ? (
                    <span className="rounded-md border border-attention-border bg-surface px-1.5 py-0.5 text-[10px] font-medium text-attention-text">
                      step-back
                    </span>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
