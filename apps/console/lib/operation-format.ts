import type { Operation } from "@/lib/types";

const monitorTitles: Record<string, string> = {
  grace_wait: "Monitor: initial review grace",
  check_wait: "Monitor: waiting for checks",
  merge_queue_wait: "Monitor: waiting in merge queue",
  reviewer_settle_wait: "Monitor: waiting for reviewer settle",
  merge_ready: "Monitor: ready to merge",
  merge: "Monitor: merging",
  completed: "Monitor: completed",
};

const actionTitles: Record<string, string> = {
  validate_only: "Validate-only recovery",
  rebase_only: "Rebase recovery",
  comment_repair: "Comment repair",
};

const operationTitles: Record<string, string> = {
  comment_repair: "Comment repair",
  ci_repair: "CI repair",
  sync_base: "Base refresh",
  human_wait: "Manual wait",
  rebase: "Rebase",
  remonitor: "Remonitor",
  refresh: "Refresh",
  validate: "Validate",
};

export function formatOperationTitle(operation: Operation): string {
  const action = operation.action ?? stringValue(operation.payload, "action");
  const requestedAction = stringValue(operation.payload, "requested_action");
  const source = stringValue(operation.payload, "source");
  if (operation.type === "monitor_state" && action && monitorTitles[action]) {
    return monitorTitles[action];
  }
  if (action && actionTitles[action]) {
    return actionTitles[action];
  }
  if (operation.type === "validate" && requestedAction === "validate" && source === "operator_api") {
    return "Revalidate";
  }
  if (operationTitles[operation.type]) {
    return operationTitles[operation.type];
  }
  return operation.type;
}

export function formatOperationDetail(operation: Operation): string {
  const payload = recordValue(operation.payload);
  const result = recordValue(operation.result);
  const parts: string[] = [];
  const prNumber = operation.pr_number ?? numberValue(payload, "pr_number");
  const reasonCode = operation.reason_code ?? stringValue(payload, "reason_code");
  const reason = operation.reason ?? stringValue(payload, "reason");
  const waitSeconds = numberValue(payload, "wait_seconds") ?? numberValue(result, "slept_seconds");
  const staleReason = stringValue(payload, "stale_reason");
  const headSha = operation.source_head_sha ?? stringValue(payload, "source_head_sha");
  const baseSha = operation.source_base_sha ?? stringValue(payload, "source_base_sha");

  if (prNumber !== null) {
    parts.push(`PR #${prNumber}`);
  }
  if (reasonCode) {
    parts.push(reasonCode);
  } else if (reason) {
    parts.push(reason);
  }
  if (waitSeconds !== null) {
    parts.push(`waits ${formatSeconds(waitSeconds)}`);
  }
  if (staleReason) {
    parts.push(`stale ${staleReason}`);
  }
  if (headSha) {
    parts.push(`head ${shortSha(headSha)}`);
  }
  if (baseSha) {
    parts.push(`base ${shortSha(baseSha)}`);
  }

  return parts.length > 0 ? parts.join(" / ") : "No details recorded.";
}

export function formatOperationFailure(operation: Operation): string | null {
  const code = operation.failure_code ?? operation.error_code;
  const message = operation.failure_message ?? operation.error_message;
  if (code && message) {
    return `${code}: ${message}`;
  }
  return message ?? code ?? null;
}

function recordValue(value: Record<string, unknown> | null): Record<string, unknown> {
  return value ?? {};
}

function stringValue(value: Record<string, unknown> | null, key: string): string | null {
  const record = recordValue(value);
  return typeof record[key] === "string" ? record[key] : null;
}

function numberValue(value: Record<string, unknown> | null, key: string): number | null {
  const record = recordValue(value);
  return typeof record[key] === "number" && Number.isFinite(record[key]) ? record[key] : null;
}

function formatSeconds(value: number): string {
  return `${Math.round(value)}s`;
}

function shortSha(value: string): string {
  return value.length > 10 ? value.slice(0, 10) : value;
}
