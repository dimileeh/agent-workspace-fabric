"use client";

import {
Activity,
AlertCircle,
Ban,
CheckCircle2,
FileText,
Loader2,
Radio,
RefreshCw,
X
} from "lucide-react";
import {
useEffect,
useLayoutEffect,
useState
} from "react";

import { formatAgentEffort,formatAgentLabel } from "@/lib/agent-format";
import { summarizeVisibleCoordinationWarnings } from "@/lib/coordination-format";
import {
compactId,
fallbackLlmUsage,
formatCostWithPricing,
formatDateTime,
formatUsageProvenance,
pricingAvailabilityReason,
relativeTime,
toneClass
} from "@/lib/format";
import {
requiredNextActionTone,
summarizeRecovery,
summarizeValidationProvenance
} from "@/lib/merge-queue-format";
import { providerReadinessPreflightFacts,providerReadinessPreflightTone } from "@/lib/provider-readiness-format";
import {
formatRecoveryCallout
} from "@/lib/recovery-format";
import type {
MergeQueueItem,
PricingMetadata,
ProviderReadinessPreflight,
Workspace,
WorkspaceOperatorAction,
WorkspaceOverview,
WorkspaceStatus
} from "@/lib/types";
import type { WorkspaceOperatorControl } from "@/lib/workspace-operator-controls";
import { QueueChip,freshnessTone,mergeBlockerTone,satisfiedTierTone } from "./console-dashboard-capacity";
import {
Badge,
ExternalAnchor,
Fact,
OperatorActionState,
Panel,
RetryActionState,
formatPrLinkLabel,
formatTokenCount
} from "./console-dashboard-shared";

// useLayoutEffect warns during Next.js SSR ("does nothing on the server").
// The scroll-lock effect must freeze the body before paint to avoid a
// scroll-position jump when the modal opens, so we keep layout timing on the
// client and fall back to useEffect on the server where there is no layout.
const useIsomorphicLayoutEffect =
  typeof window !== "undefined" ? useLayoutEffect : useEffect;

export function TaskDetailsModal({
  workspace,
  onClose,
}: {
  workspace: WorkspaceOverview;
  onClose: () => void;
}) {
  const labelId = `task-details-label-${workspace.workspace_id}`;
  const titleId = `task-details-title-${workspace.workspace_id}`;

  useIsomorphicLayoutEffect(() => {
    const scrollY = window.scrollY;
    const previousBodyOverflow = document.body.style.overflow;
    const previousHtmlOverflow = document.documentElement.style.overflow;
    document.body.style.overflow = "hidden";
    document.documentElement.style.overflow = "hidden";
    window.scrollTo(0, scrollY);
    return () => {
      document.body.style.overflow = previousBodyOverflow;
      document.documentElement.style.overflow = previousHtmlOverflow;
      window.scrollTo(0, scrollY);
    };
  }, []);

  return (
    <div
      className="fixed inset-0 z-50 grid overflow-hidden overscroll-contain bg-slate-950/45 p-3 sm:p-6"
      role="dialog"
      aria-modal="true"
      aria-labelledby={`${labelId} ${titleId}`}
    >
      <div className="m-auto grid max-h-[calc(100dvh-1.5rem)] w-full max-w-5xl grid-rows-[auto_minmax(0,1fr)] overflow-hidden rounded-md border border-slate-300 bg-white shadow-xl sm:max-h-[calc(100dvh-3rem)]">
        <header className="flex min-h-14 items-start justify-between gap-3 border-b border-slate-200 px-4 py-3">
          <div className="min-w-0">
            <div id={labelId} className="flex items-center gap-2 text-sm font-semibold text-slate-950">
              <FileText size={16} aria-hidden />
              Task details
            </div>
            <h2 id={titleId} className="mt-1 line-clamp-2 text-base font-semibold text-slate-950">
              {workspace.title}
            </h2>
            <div className="mono mt-1 truncate text-xs text-slate-500">{workspace.workspace_id}</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-slate-300 bg-white text-slate-700 transition hover:bg-slate-50"
            aria-label="Close task details"
          >
            <X size={16} aria-hidden />
          </button>
        </header>
        <div
          className="grid min-h-0 gap-3 overflow-y-auto overscroll-contain p-4"
          data-testid="task-details-scroll"
          tabIndex={0}
        >
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <Fact label="Agent" value={formatAgentLabel(workspace)} />
            <Fact label="Effort" value={formatAgentEffort(workspace)} />
            <Fact label="Base" value={workspace.base_branch} mono />
            <Fact label="Status" value={workspace.status} />
            <Fact label="Created" value={formatDateTime(workspace.created_at)} />
            <Fact label="Updated" value={formatDateTime(workspace.updated_at)} />
            <Fact label="Repository" value={workspace.repo_url} />
            <Fact label="Branch" value={workspace.branch_name ?? "—"} mono />
          </div>
          <CoordinationWarningBlock warnings={workspace.coordination_warnings} status={workspace.status} />
          <section className="grid gap-2 rounded-md border border-slate-200 bg-slate-50 p-3">
            <div className="text-xs font-semibold text-slate-500">Prompt sent to AWF</div>
            <TaskPromptBody prompt={workspace.task_prompt} />
          </section>
        </div>
      </div>
    </div>
  );
}

export function TaskPromptBody({ prompt }: { prompt: string }) {
  const lines = prompt.trim() ? prompt.trim().split(/\r?\n/) : ["No prompt stored for this workspace."];

  return (
    <div className="grid gap-1 text-sm leading-6 text-slate-800">
      {lines.map((line, index) => {
        const trimmed = line.trim();
        const key = `${index}:${line}`;
        if (!trimmed) {
          return <div key={key} className="h-2" />;
        }
        if (trimmed.startsWith("### ")) {
          return (
            <h4 key={key} className="mt-3 text-sm font-semibold text-slate-950">
              {trimmed.slice(4)}
            </h4>
          );
        }
        if (trimmed.startsWith("## ")) {
          return (
            <h3 key={key} className="mt-4 text-base font-semibold text-slate-950">
              {trimmed.slice(3)}
            </h3>
          );
        }
        if (trimmed.startsWith("# ")) {
          return (
            <h3 key={key} className="text-base font-semibold text-slate-950">
              {trimmed.slice(2)}
            </h3>
          );
        }
        if (trimmed.startsWith("- ")) {
          return (
            <div key={key} className="grid grid-cols-[16px_minmax(0,1fr)] gap-2">
              <span className="text-slate-400">•</span>
              <span className="min-w-0 whitespace-pre-wrap break-words">{trimmed.slice(2)}</span>
            </div>
          );
        }
        if (/^\d+\.\s/.test(trimmed)) {
          const [prefix, ...rest] = trimmed.split(/\s+/);
          return (
            <div key={key} className="grid grid-cols-[28px_minmax(0,1fr)] gap-2">
              <span className="mono text-xs text-slate-400">{prefix}</span>
              <span className="min-w-0 whitespace-pre-wrap break-words">{rest.join(" ")}</span>
            </div>
          );
        }
        if (trimmed.startsWith("```")) {
          return (
            <div key={key} className="mono rounded-md bg-slate-900 px-2 py-1 text-xs text-slate-100">
              {trimmed}
            </div>
          );
        }
        return (
          <p key={key} className="whitespace-pre-wrap break-words">
            {line}
          </p>
        );
      })}
    </div>
  );
}

export function WorkspaceSummary({
  overview,
  workspace,
  mergeQueueItem,
  retryState,
  operatorControls,
  operatorActionState,
  onRetry,
  onOperatorAction,
}: {
  overview: WorkspaceOverview;
  workspace: Workspace | null;
  mergeQueueItem: MergeQueueItem | null;
  retryState: RetryActionState;
  operatorControls: WorkspaceOperatorControl[];
  operatorActionState: OperatorActionState;
  onRetry: () => void;
  onOperatorAction: (action: WorkspaceOperatorAction, requestedTier?: number) => void;
}) {
  const canRetry = overview.status === "failed" || overview.status === "cancelled";
  const recovery = workspace?.recovery ?? overview.recovery ?? null;
  const coordinationWarnings =
    workspace?.coordination_warnings ?? overview.coordination_warnings ?? [];

  return (
    <Panel
      title="Workspace"
      icon={<Activity size={16} aria-hidden />}
      action={
        <div className="flex items-center gap-2">
          {canRetry ? (
            <button
              type="button"
              onClick={onRetry}
              disabled={retryState.status === "submitting"}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 text-xs text-slate-800 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <RefreshCw
                size={13}
                className={retryState.status === "submitting" ? "animate-spin" : ""}
                aria-hidden
              />
              Retry
            </button>
          ) : null}
          {overview.pr_url ? (
            <ExternalAnchor href={overview.pr_url} label={formatPrLinkLabel(overview.pr_url, overview.pr_number)} />
          ) : null}
        </div>
      }
    >
      <div className="grid min-w-0 gap-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-lg font-semibold">{overview.title}</h2>
            <p className="mt-1 truncate text-sm text-[var(--muted)]">{overview.repo_url}</p>
          </div>
          <div className="flex flex-col items-end gap-1">
            <div className="flex items-center gap-1">
              <Badge value={overview.status} />
              {overview.status === "running" && overview.subphase ? (
                <span className="inline-flex h-6 items-center rounded-md border border-slate-200 bg-slate-100 px-2 text-[11px] font-medium text-slate-800">
                  ({overview.subphase})
                </span>
              ) : null}
            </div>
            {overview.is_stale_running ? (
              <span className="inline-flex h-6 items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 text-[11px] font-medium text-amber-900">
                <AlertCircle size={12} aria-hidden />
                <span>Stale execution (check logs)</span>
              </span>
            ) : null}
          </div>
        </div>
        <div className="grid min-w-0 gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <Fact label="Workspace" value={overview.workspace_id} mono />
          <Fact label="Agent" value={formatAgentLabel(overview)} />
          <Fact label="Effort" value={formatAgentEffort(overview)} />
          <Fact label="Branch" value={workspace?.branch_name ?? overview.branch_name ?? "—"} mono />
          <Fact label="Base" value={overview.base_branch} mono />
          <Fact label="Phase" value={overview.current_phase} />
          <Fact label="Operation" value={overview.active_operation ?? "none"} />
          <Fact label="Updated" value={formatDateTime(overview.updated_at)} />
        </div>
        <WorkspaceRecoveryBlock item={mergeQueueItem} workspace={workspace} overview={overview} />
        <OperatorControlsBlock
          // Reset the block's local confirm state when the operator switches
          // workspaces so a pending destructive cancel confirmation can never
          // carry over onto a different (still cancel-enabled) workspace.
          key={overview.workspace_id}
          controls={operatorControls}
          state={operatorActionState}
          workspaceId={overview.workspace_id}
          onAction={onOperatorAction}
        />
        <UsageSummaryBlock
          usage={workspace?.llm_usage ?? overview.llm_usage}
          pricing={workspace?.pricing ?? overview.pricing ?? null}
        />
        <ProviderReadinessPreflightBlock
          preflight={workspace?.provider_readiness_preflight ?? overview.provider_readiness_preflight ?? null}
        />
        {recovery && overview.status !== "completed" ? (
          <RecoveryCallout recovery={recovery} status={overview.status} />
        ) : null}
        <CoordinationWarningBlock warnings={coordinationWarnings} status={overview.status} />
        {overview.failure_reason || overview.failure_message ? (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
            <div className="font-semibold">{overview.failure_reason ?? "failure"}</div>
            <div className="mt-1 text-red-800">{overview.failure_message ?? "No details."}</div>
          </div>
        ) : null}
        {retryState.status === "success" ? (
          <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
            Retry queued as{" "}
            <span className="mono font-semibold">{retryState.newWorkspaceId}</span>
            <span className="text-emerald-800"> / operation </span>
            <span className="mono">{compactId(retryState.operationId, 10)}</span>
          </div>
        ) : null}
        {retryState.status === "error" ? (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
            {retryState.message}
          </div>
        ) : null}
      </div>
    </Panel>
  );
}

export function WorkspaceRecoveryBlock({
  item,
  workspace,
  overview,
}: {
  item: MergeQueueItem | null;
  workspace: Workspace | null;
  overview: WorkspaceOverview;
}) {
  if (!item) {
    const validation = summarizeValidationProvenance(workspace?.validation_provenance);
    return (
      <div className="grid gap-2 rounded-md border border-slate-200 bg-white p-2 text-xs">
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="font-semibold text-slate-900">Validation freshness</div>
            <div className="mono mt-0.5 truncate text-[11px] text-slate-500">
              {workspace?.branch_name ?? overview.branch_name ?? "no branch"} / {overview.base_branch}
            </div>
          </div>
          <Badge value={overview.status} />
        </div>
        <div className="grid gap-1 sm:grid-cols-2 xl:grid-cols-3">
          <QueueChip label="Required" value={validation.requiredTierLabel} />
          <QueueChip
            label="Satisfied"
            value={validation.latestSatisfiedTierLabel}
            detail={validation.latestSatisfiedTierDetail}
            tone={satisfiedTierTone(validation.latestSatisfiedTierLabel)}
          />
          <QueueChip
            label="Freshness"
            value={validation.freshnessLabel}
            detail={validation.targetRangeLabel}
            tone={freshnessTone(validation.freshnessLabel)}
          />
          <QueueChip label="Reason" value={validation.validationReasonLabel} mono />
          <QueueChip label="Command" value={validation.commandHashLabel} mono />
          <QueueChip label="Profile" value={validation.profileLabel} />
          <QueueChip label="Env" value={validation.environmentLabel} mono />
          <QueueChip label="Base" value={validation.baseShaLabel} mono />
          <QueueChip label="Workspace head" value={validation.workspaceHeadShaLabel} mono />
          <QueueChip
            label="Targets"
            value={validation.targetRangeLabel}
            detail={`${validation.validatedTargetShaLabel} / ${validation.currentTargetShaLabel}`}
            mono
          />
        </div>
      </div>
    );
  }

  const recovery = summarizeRecovery(item);
  return (
    <div className="grid gap-2 rounded-md border border-slate-200 bg-white p-2 text-xs">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="font-semibold text-slate-900">Validation freshness</div>
          <div className="mono mt-0.5 truncate text-[11px] text-slate-500">
            {item.branch_name ?? "no branch"} / {item.base_branch}
          </div>
        </div>
        <Badge value={item.status} />
      </div>
      <div className="grid gap-1 sm:grid-cols-2 xl:grid-cols-3">
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
          label="Base"
          value={recovery.baseShaLabel}
          detail={item.latest_validation?.target_branch ?? item.base_branch}
          mono
        />
        <QueueChip label="Workspace head" value={recovery.workspaceHeadShaLabel} mono />
        <QueueChip label="Reason" value={recovery.validationReasonLabel} mono />
        <QueueChip label="Command" value={recovery.commandHashLabel} mono />
        <QueueChip label="Profile" value={recovery.profileLabel} />
        <QueueChip label="Env" value={recovery.environmentLabel} mono />
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
      </div>
    </div>
  );
}

export function OperatorControlsBlock({
  controls,
  state,
  workspaceId,
  onAction,
}: {
  controls: WorkspaceOperatorControl[];
  state: OperatorActionState;
  workspaceId: string;
  onAction: (action: WorkspaceOperatorAction, requestedTier?: number) => void;
}) {
  const [confirming, setConfirming] = useState<WorkspaceOperatorAction | null>(null);
  const visibleControls = controls.filter((control) => control.visible);

  const submittingAction = state.status === "submitting" ? state.action : null;
  const busy = submittingAction !== null;
  const cancelControl = visibleControls.find((control) => control.action === "cancel");
  const cancelConfirmEnabled = cancelControl !== undefined && cancelControl.enabled && !busy;

  // Drop a pending cancel confirmation as soon as it no longer applies — the
  // cancel became disabled or is no longer available, so Confirm cancel can never
  // act on a stale or guarded selection.
  useEffect(() => {
    if (confirming === "cancel" && !cancelConfirmEnabled) {
      setConfirming(null);
    }
  }, [confirming, cancelConfirmEnabled]);

  if (visibleControls.length === 0) {
    return null;
  }

  return (
    <div className="grid gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold text-slate-900">Operator controls</span>
        {busy ? (
          <span className="mono text-[11px] text-slate-500">{submittingAction} active</span>
        ) : null}
      </div>
      <div className="flex flex-wrap gap-2">
        {visibleControls.map((control) => {
          const submitting = submittingAction === control.action;
          // Freeze every control while a destructive confirmation is pending so
          // an operator cannot trigger another action (or re-arm cancel) until
          // they confirm or dismiss the open dialog.
          const disabled = busy || !control.enabled || confirming !== null;
          const reason = busy && !submitting ? "operation active" : control.reason;
          const destructive = control.action === "cancel";
          const tooltip = reason ? `${control.label}: ${reason}` : null;
          const buttonClassName = destructive
            ? "inline-flex h-8 items-center gap-1.5 rounded-md border border-red-300 bg-white px-2.5 text-[11px] font-medium text-red-700 transition hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
            : "inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-[11px] font-medium text-slate-800 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50";
          return (
            <div
              key={control.action}
              tabIndex={disabled && reason ? 0 : undefined}
              aria-describedby={disabled && reason ? `operator-control-tip-${workspaceId}-${control.action}` : undefined}
              className="group relative flex min-w-0 items-center gap-1.5"
            >
              <button
                type="button"
                onClick={() =>
                  destructive ? setConfirming(control.action) : onAction(control.action, control.requestedTier)
                }
                disabled={disabled}
                className={buttonClassName}
              >
                <OperatorControlIcon action={control.action} spinning={submitting} />
                {control.label}
              </button>
              {reason ? (
                <span
                  id={`operator-control-tip-${workspaceId}-${control.action}`}
                  role="tooltip"
                  className="pointer-events-none absolute left-0 top-[calc(100%+6px)] z-20 sr-only max-w-56 rounded-md border border-slate-700 bg-slate-950 px-2 py-1 text-[11px] font-medium text-white shadow-lg group-focus-within:not-sr-only group-hover:not-sr-only"
                >
                  {tooltip}
                </span>
              ) : null}
            </div>
          );
        })}
      </div>
      {confirming === "cancel" && cancelConfirmEnabled ? (
        <div
          data-testid="operator-cancel-confirm"
          className="grid gap-2 rounded-md border border-red-200 bg-red-50 px-2 py-1.5 text-red-900"
        >
          <span className="min-w-0 break-words">
            Cancel workspace <span className="mono">{workspaceId}</span>? This stops its stack and ends the run.
          </span>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => {
                if (!cancelConfirmEnabled) {
                  return;
                }
                onAction("cancel");
                setConfirming(null);
              }}
              disabled={!cancelConfirmEnabled}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-red-300 bg-red-600 px-2.5 text-[11px] font-medium text-white transition hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Ban size={13} aria-hidden />
              Confirm cancel
            </button>
            <button
              type="button"
              onClick={() => setConfirming(null)}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-[11px] font-medium text-slate-800 transition hover:bg-slate-50"
            >
              Dismiss
            </button>
          </div>
        </div>
      ) : null}
      {state.status === "success" ? (
        <div className="rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1.5 text-emerald-900">
          <span>{state.message}</span>
          <span className="mono ml-2 text-emerald-800">{compactId(state.operationId, 10)}</span>
        </div>
      ) : null}
      {state.status === "success" && state.warnings.length > 0 ? (
        <div className="grid gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 py-1.5 text-amber-900">
          {state.warnings.map((warning) => (
            <div key={`${warning.warning_code}:${warning.message}`} className="flex min-w-0 items-start gap-1.5">
              <AlertCircle className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
              <span className="min-w-0 break-words">{warning.message || warning.warning_code}</span>
            </div>
          ))}
        </div>
      ) : null}
      {state.status === "error" ? (
        <div className="rounded-md border border-red-200 bg-red-50 px-2 py-1.5 text-red-900">
          {state.message}
        </div>
      ) : null}
    </div>
  );
}

export function OperatorControlIcon({
  action,
  spinning,
}: {
  action: WorkspaceOperatorAction;
  spinning: boolean;
}) {
  if (spinning) {
    return <Loader2 size={13} className="animate-spin" aria-hidden />;
  }
  if (action === "remonitor") {
    return <Radio size={13} aria-hidden />;
  }
  if (action === "refresh") {
    return <RefreshCw size={13} aria-hidden />;
  }
  if (action === "cancel") {
    return <Ban size={13} aria-hidden />;
  }
  return <CheckCircle2 size={13} aria-hidden />;
}

export function UsageSummaryBlock({
  usage,
  pricing,
}: {
  usage: Workspace["llm_usage"] | null | undefined;
  pricing: PricingMetadata | null | undefined;
}) {
  const safeUsage = fallbackLlmUsage(usage);
  const pricingReason = pricingAvailabilityReason(pricing);
  const showCost = safeUsage.cost_estimate !== null;

  if (
    safeUsage.status === "unavailable" ||
    (
      safeUsage.input_tokens == null &&
      safeUsage.cached_input_tokens == null &&
      safeUsage.output_tokens == null &&
      safeUsage.reasoning_output_tokens == null &&
      safeUsage.total_tokens == null &&
      safeUsage.cost_estimate == null
    )
  ) {
    return (
      <div data-testid="llm-usage" className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
        <div className="flex items-center justify-between gap-2">
          <span className="font-semibold text-slate-900">LLM usage</span>
          <Badge value="unavailable" />
        </div>
        <div className="mt-2 truncate text-[11px] text-slate-500">
          {formatUsageProvenance(safeUsage.source, safeUsage.reason)}
        </div>
      </div>
    );
  }
  return (
    <div data-testid="llm-usage" className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold text-slate-900">LLM usage</span>
        <div className="flex items-center gap-1.5">
          {pricing ? (
            <span
              className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${pricing.is_current ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-700"}`}
              title={`${pricing.provider} / ${pricing.model} — ${pricing.unit}`}
            >
              {pricing.provider} / {pricing.model}
            </span>
          ) : null}
          <Badge value={safeUsage.status} />
        </div>
      </div>
      <div className="mt-2 grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
        {safeUsage.input_tokens != null && <UsageMetric label="Input" value={formatTokenCount(safeUsage.input_tokens)} />}
        {safeUsage.cached_input_tokens != null && <UsageMetric label="Cached" value={formatTokenCount(safeUsage.cached_input_tokens)} />}
        {safeUsage.output_tokens != null && <UsageMetric label="Output" value={formatTokenCount(safeUsage.output_tokens)} />}
        {safeUsage.reasoning_output_tokens != null && <UsageMetric label="Reasoning" value={formatTokenCount(safeUsage.reasoning_output_tokens)} />}
        {safeUsage.total_tokens != null && <UsageMetric label="Total" value={formatTokenCount(safeUsage.total_tokens)} />}
        <UsageMetric
          label="Cost"
          value={
            showCost
              ? formatCostWithPricing(safeUsage.cost_estimate, safeUsage.currency, pricing, safeUsage.source)
              : "—"
          }
        />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-500">
        {formatUsageProvenance(safeUsage.source, safeUsage.reason)}
        {pricingReason && !showCost ? (
          <>
            <span className="text-slate-300">|</span>
            <span className="text-amber-600">{pricingReason}</span>
          </>
        ) : null}
      </div>
    </div>
  );
}

export function ProviderReadinessPreflightBlock({
  preflight,
}: {
  preflight: ProviderReadinessPreflight | null;
}) {
  if (!preflight) {
    return null;
  }
  const tone = providerReadinessPreflightTone(preflight);
  const facts = providerReadinessPreflightFacts(preflight, {
    formatCheckedAt: relativeTime,
  });
  return (
    <div className={`rounded-md border px-3 py-2 text-xs ${toneClass(tone)}`}>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <span className="font-semibold text-slate-900">Provider preflight</span>
        <span className="mono truncate text-[11px]">{preflight.reason_code}</span>
      </div>
      <div className="mt-2 grid gap-1 sm:grid-cols-2 xl:grid-cols-4">
        {facts.map((fact) => (
          <QueueChip
            key={fact.label}
            label={fact.label}
            value={fact.value}
            detail={fact.detail}
            mono={fact.mono}
            tone={fact.tone}
          />
        ))}
      </div>
      {preflight.override_reason ? (
        <div className="mt-2 truncate text-[11px] text-slate-600">
          override: {preflight.override_reason}
        </div>
      ) : null}
    </div>
  );
}

export function RecoveryCallout({
  recovery,
  status,
}: {
  recovery: NonNullable<Workspace["recovery"]>;
  status: WorkspaceStatus;
}) {
  const callout = formatRecoveryCallout(recovery, status);
  return (
    <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2 font-semibold">
          <RefreshCw size={14} aria-hidden />
          <span className="truncate">{callout.title}</span>
        </div>
        <span className="mono rounded-md border border-amber-300 bg-white/70 px-2 py-0.5 text-[11px] text-amber-900">
          {callout.reason}
        </span>
      </div>
      <div className="mt-2 grid gap-2 text-xs sm:grid-cols-3">
        <Fact label="Action" value={callout.action} />
        <Fact label="Current" value={callout.current} />
        <Fact label="Started" value={formatDateTime(recovery.started_at)} />
      </div>
      <div className="mt-2 text-xs text-amber-900">{callout.body}</div>
    </div>
  );
}

export function CoordinationWarningBlock({
  warnings,
  status,
}: {
  warnings: Workspace["coordination_warnings"] | WorkspaceOverview["coordination_warnings"];
  status: WorkspaceStatus;
}) {
  const summary = summarizeVisibleCoordinationWarnings(warnings, status);
  if (summary.count === 0) {
    return null;
  }
  return (
    <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2 font-semibold">
          <AlertCircle size={14} aria-hidden />
          <span className="truncate">Coordination</span>
        </div>
        <span className="rounded-md border border-amber-300 bg-white/70 px-2 py-0.5 text-[11px] text-amber-900">
          {summary.label}
        </span>
      </div>
      <div className="mt-2 text-xs text-amber-900">{summary.detail}</div>
    </div>
  );
}

export function UsageMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] font-medium text-slate-500">{label}</div>
      <div className="mono truncate text-sm text-slate-950">{value}</div>
    </div>
  );
}
