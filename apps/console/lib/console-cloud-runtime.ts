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

/**
 * OpenAPI `format: date-time` / RFC 3339 profile: full date-time with `T`/`t`
 * and a timezone (`Z`/`z` or ±HH:mm). Rejects Date.parse-permissive forms like
 * `09/07/2026` or date-only `2026-09-07`.
 * Capturing groups let us reject impossible calendar values that Date.parse
 * would normalize (e.g. 2026-02-29 → March 1).
 */
const RFC3339_DATE_TIME =
  /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-](\d{2}):(\d{2}))$/;

/** True only for finite RFC 3339 date-time strings (rejects "", "not-a-date", slash dates, etc.). */
function isFiniteTimestampString(value: string): boolean {
  const match = RFC3339_DATE_TIME.exec(value);
  if (!match) {
    return false;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  // Date.UTC normalizes overflow; require a round-trip to the same components so
  // impossible dates/times (2026-02-29, 25:00:00, month 13) are rejected.
  const dt = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  if (
    dt.getUTCFullYear() !== year ||
    dt.getUTCMonth() !== month - 1 ||
    dt.getUTCDate() !== day ||
    dt.getUTCHours() !== hour ||
    dt.getUTCMinutes() !== minute ||
    dt.getUTCSeconds() !== second
  ) {
    return false;
  }
  const tzDesignator = match[8];
  if (tzDesignator !== "Z" && tzDesignator !== "z") {
    const tzHour = Number(match[9]);
    const tzMinute = Number(match[10]);
    if (tzHour > 23 || tzMinute > 59) {
      return false;
    }
  }
  return Number.isFinite(Date.parse(value));
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
