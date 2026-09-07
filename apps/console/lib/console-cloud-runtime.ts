import type { CloudRuntimeSummary } from "./types.ts";

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

function isNonNegativeNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isNullableNonNegativeNumber(value: unknown): value is number | null {
  return value === null || isNonNegativeNumber(value);
}

function isNullableNonNegativeInteger(value: unknown): value is number | null {
  return value === null || isNonNegativeInteger(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

/** True only when `value` parses to a finite epoch ms (rejects "", "not-a-date", etc.). */
function isFiniteTimestampString(value: string): boolean {
  return value.length > 0 && Number.isFinite(Date.parse(value));
}

/**
 * Fail closed on hosted cloud-runtime payloads that lack the nested objects
 * CloudRuntimePanel reads. Callers keep the last good snapshot and surface an error.
 */
export function parseCloudRuntimeSummary(payload: unknown): CloudRuntimeSummary | null {
  if (!isPlainObject(payload)) {
    return null;
  }
  if (payload.schema_version !== 1) {
    return null;
  }
  if (typeof payload.generated_at !== "string" || !isFiniteTimestampString(payload.generated_at)) {
    return null;
  }
  if (
    !isPlainObject(payload.queue) ||
    !isPlainObject(payload.provisioning) ||
    !isPlainObject(payload.admission)
  ) {
    return null;
  }

  const queue = payload.queue;
  if (
    !isNullableNonNegativeInteger(queue.queued_count) ||
    !isNullableNonNegativeNumber(queue.oldest_wait_seconds) ||
    (queue.oldest_workspace_id !== undefined && !isNullableString(queue.oldest_workspace_id))
  ) {
    return null;
  }

  const provisioning = payload.provisioning;
  if (
    !isNullableNonNegativeInteger(provisioning.in_progress) ||
    !isNullableNonNegativeInteger(provisioning.pending)
  ) {
    return null;
  }

  const admission = payload.admission;
  if (
    typeof admission.ok !== "boolean" ||
    typeof admission.status !== "string" ||
    typeof admission.reason !== "string" ||
    (admission.detail !== undefined && !isNullableString(admission.detail))
  ) {
    return null;
  }
  if (admission.quota !== undefined && admission.quota !== null) {
    if (!isPlainObject(admission.quota)) {
      return null;
    }
    if (
      !isNullableNonNegativeInteger(admission.quota.limit) ||
      !isNullableNonNegativeInteger(admission.quota.in_use) ||
      !isNullableNonNegativeInteger(admission.quota.available)
    ) {
      return null;
    }
  }

  return payload as unknown as CloudRuntimeSummary;
}
