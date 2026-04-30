import type { WorkspaceCoordinationWarning } from "@/lib/types";
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
  const advisoryCount = items.filter((warning) => warning.severity === "advisory").length;
  const labelCount = advisoryCount || items.length;
  const detail = [
    ...visible.map(formatCoordinationWarningDetail),
    ...(overflowCount ? [`+${overflowCount} more`] : []),
  ].join("; ");

  return {
    count: items.length,
    label: `${labelCount} advisory overlap${labelCount === 1 ? "" : "s"}`,
    detail,
    overflowCount,
    warnings: visible,
  };
}

function formatCoordinationWarningDetail(warning: WorkspaceCoordinationWarning): string {
  const overlap = warning.overlaps[0] ?? null;
  const workspaceId = warning.workspace_ids[0] ?? overlap?.workspace_id ?? "unknown workspace";
  const pathDetail = overlap
    ? `${compactId(workspaceId, 12)} ${overlap.existing_path} -> ${overlap.requested_path}`
    : compactId(workspaceId, 12);
  const staleReason = warning.stale_policy_context.stale_reason_code;
  return [warning.warning_code, pathDetail, staleReason].filter(Boolean).join(" / ");
}
