import type { WorkspaceCoordinationWarning, WorkspaceStatus } from "@/lib/types";
import { compactId } from "./format.ts";

export interface CoordinationWarningSummary {
  count: number;
  label: string;
  detail: string;
  overflowCount: number;
  warnings: WorkspaceCoordinationWarning[];
}

export function summarizeCoordinationWarnings(
  warnings: WorkspaceCoordinationWarning[] | null | undefined,
  options: { maxWarnings?: number } = {},
): CoordinationWarningSummary {
  const items = (warnings ?? []).filter((warning) => warning.warning_code);
  if (items.length === 0) {
    return {
      count: 0,
      label: "none",
      detail: "no coordination warnings",
      overflowCount: 0,
      warnings: [],
    };
  }

  const maxWarnings = Math.max(1, options.maxWarnings ?? 2);
  const visible = items.slice(0, maxWarnings);
  const overflowCount = Math.max(0, items.length - visible.length);
  const label = formatCoordinationWarningLabel(items);
  const detail = [
    ...visible.map(formatCoordinationWarningDetail),
    ...(overflowCount ? [`+${overflowCount} more`] : []),
  ].join("; ");

  return {
    count: items.length,
    label,
    detail,
    overflowCount,
    warnings: visible,
  };
}

export function summarizeVisibleCoordinationWarnings(
  warnings: WorkspaceCoordinationWarning[] | null | undefined,
  status: WorkspaceStatus,
  options: { maxWarnings?: number } = {},
): CoordinationWarningSummary {
  if (status === "completed") {
    return summarizeCoordinationWarnings([], options);
  }
  return summarizeCoordinationWarnings(warnings, options);
}

function formatCoordinationWarningLabel(warnings: WorkspaceCoordinationWarning[]): string {
  const labelCount = warnings.length;
  const plural = labelCount === 1 ? "" : "s";
  const severity = sharedCoordinationWarningSeverity(warnings);
  if (severity === "advisory") {
    return `${labelCount} advisory overlap${plural}`;
  }
  if (severity) {
    return `${labelCount} ${severity} coordination warning${plural}`;
  }
  return `${labelCount} coordination warning${plural}`;
}

function sharedCoordinationWarningSeverity(warnings: WorkspaceCoordinationWarning[]): string | null {
  const firstSeverity = coordinationWarningSeverity(warnings[0]);
  if (!firstSeverity) {
    return null;
  }
  return warnings.every((warning) => coordinationWarningSeverity(warning) === firstSeverity)
    ? firstSeverity
    : null;
}

function coordinationWarningSeverity(warning: WorkspaceCoordinationWarning | undefined): string {
  return warning && typeof warning.severity === "string" ? warning.severity.trim().toLowerCase() : "";
}

function formatCoordinationWarningDetail(warning: WorkspaceCoordinationWarning): string {
  const overlaps = Array.isArray(warning.overlaps) ? warning.overlaps : [];
  const workspaceIds = Array.isArray(warning.workspace_ids) ? warning.workspace_ids : [];
  const overlap = overlaps[0] ?? null;
  const workspaceId = workspaceIds[0] ?? overlap?.workspace_id ?? "unknown workspace";
  const pathDetail = overlap?.existing_path && overlap.requested_path
    ? `${compactId(workspaceId, 12)} ${overlap.existing_path} -> ${overlap.requested_path}`
    : compactId(workspaceId, 12);
  const staleReason = warning.stale_policy_context?.stale_reason_code;
  return [warning.warning_code, pathDetail, staleReason].filter(Boolean).join(" / ");
}
